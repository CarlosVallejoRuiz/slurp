"""Loads graphs from multiple formats into a networkx DiGraph.

Supported formats:
  - JSON (graphify / generic) — .json
  - GraphML                   — .graphml
  - Neo4j CSV export          — .csv (nodes) + optional .csv (relationships)
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import networkx as nx


class SlurpLoadError(Exception):
    """Raised when a graph file cannot be loaded or is invalid."""


# ---------------------------------------------------------------------------
# JSON — graphify / generic
# ---------------------------------------------------------------------------

def detect_format(data: dict) -> str:
    """Returns 'graphify' or 'generic'.

    Graphify format is detected by:
    - presence of a 'links' key (real graphify: NetworkX JSON serialization), or
    - nodes with any of: type, description, importance, file_path, file_type, source_file
    Generic format has only id and label on nodes.
    """
    if "links" in data:
        return "graphify"

    nodes = data.get("nodes", [])
    if not nodes:
        return "generic"

    graphify_keys = {"type", "description", "importance", "file_path", "file_type", "source_file"}
    for node in nodes:
        if graphify_keys & set(node.keys()):
            return "graphify"
    return "generic"


def _build_graph(data: dict) -> nx.DiGraph:
    """Constructs a DiGraph from raw parsed JSON data.

    Supports both 'links' (real graphify / NetworkX JSON) and 'edges' key for edges.
    """
    G = nx.DiGraph()

    for node in data["nodes"]:
        node_id = node["id"]
        attrs = {k: v for k, v in node.items() if k != "id"}
        G.add_node(node_id, **attrs)

    # Real graphify uses 'links'; our test/generic format uses 'edges'
    edges = data["links"] if "links" in data else data.get("edges", [])
    for edge in edges:
        source = edge["source"]
        target = edge["target"]
        attrs = {k: v for k, v in edge.items() if k not in ("source", "target")}
        G.add_edge(source, target, **attrs)

    return G


def _load_json(path: Path) -> nx.DiGraph:
    """Load a JSON graph file (graphify or generic format)."""
    if not path.exists():
        raise SlurpLoadError(f"Graph file not found: {path}")

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SlurpLoadError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise SlurpLoadError(
            f"Expected a JSON object at the top level, got {type(data).__name__}: {path}"
        )

    nodes = data.get("nodes")
    if not nodes:
        raise SlurpLoadError(f"Graph has no nodes: {path}")

    for i, node in enumerate(nodes):
        if "id" not in node:
            raise SlurpLoadError(
                f"Node at index {i} is missing required field 'id': {path}"
            )
        if "label" not in node:
            raise SlurpLoadError(
                f"Node '{node['id']}' is missing required field 'label': {path}"
            )

    return _build_graph(data)


# ---------------------------------------------------------------------------
# GraphML
# ---------------------------------------------------------------------------

def _normalize_graphml_attrs(attrs: dict, node_id: str) -> dict:
    """Normalize GraphML node attributes to the slurp schema.

    Mapping:
    - label   ← existing 'label', else 'name', else node_id
    - type    ← existing 'type', else 'kind'
    - description ← existing 'description', else 'desc'
    - All other attributes are preserved as-is.
    """
    result = dict(attrs)

    # Canonical 'label' field
    if "label" not in result:
        if "name" in result and result["name"]:
            result["label"] = result.pop("name")
        else:
            result["label"] = node_id

    # Canonical 'type' field
    if "type" not in result and "kind" in result:
        result["type"] = result.pop("kind")

    # Canonical 'description' field
    if "description" not in result and "desc" in result:
        result["description"] = result.pop("desc")

    return result


def _load_graphml(path: Path) -> nx.DiGraph:
    """Load a GraphML file and return a normalized DiGraph.

    Uses nx.read_graphml for parsing, then normalises node attributes so that
    slurp's scorer and formatter can work with any GraphML source.
    """
    if not path.exists():
        raise SlurpLoadError(f"Graph file not found: {path}")

    try:
        raw_G = nx.read_graphml(str(path))
    except Exception as exc:
        raise SlurpLoadError(f"Cannot read GraphML from {path}: {exc}") from exc

    G = nx.DiGraph()

    for node_id, attrs in raw_G.nodes(data=True):
        normalized = _normalize_graphml_attrs(dict(attrs), str(node_id))
        G.add_node(str(node_id), **normalized)

    for u, v, attrs in raw_G.edges(data=True):
        edge_attrs = dict(attrs)
        # In GraphML, edge labels often appear as 'label'; map to 'relation'
        if "relation" not in edge_attrs and "label" in edge_attrs:
            edge_attrs["relation"] = edge_attrs.pop("label")
        G.add_edge(str(u), str(v), **edge_attrs)

    if G.number_of_nodes() == 0:
        raise SlurpLoadError(f"Graph has no nodes: {path}")

    return G


# ---------------------------------------------------------------------------
# Neo4j CSV
# ---------------------------------------------------------------------------

# Column name aliases — each tuple is tried in order; first match wins.
_NEO4J_NODE_ID_COLS = ("nodeId", ":ID", "id")
_NEO4J_NODE_LABELS_COLS = ("labels", ":LABEL")
_NEO4J_NODE_NAME_COLS = ("name", "label")
_NEO4J_NODE_DESC_COLS = ("description", "desc")
_NEO4J_NODE_TYPE_COLS = ("type", "kind")
_NEO4J_START_COLS = ("startNodeId", ":START_ID", "source")
_NEO4J_END_COLS = ("endNodeId", ":END_ID", "target")
_NEO4J_REL_TYPE_COLS = ("type", ":TYPE", "relation")

# Columns consumed during normalization — anything else becomes an extra attr.
_KNOWN_NODE_COLS = frozenset(
    _NEO4J_NODE_ID_COLS + _NEO4J_NODE_LABELS_COLS + _NEO4J_NODE_NAME_COLS
    + _NEO4J_NODE_DESC_COLS + _NEO4J_NODE_TYPE_COLS
)
_KNOWN_REL_COLS = frozenset(
    _NEO4J_START_COLS + _NEO4J_END_COLS + _NEO4J_REL_TYPE_COLS
)


def _pick(row: dict, candidates: tuple[str, ...]) -> str | None:
    """Return the value of the first matching candidate column, or None."""
    for col in candidates:
        if col in row and row[col]:
            return row[col]
    return None


def load_graph_neo4j(
    nodes_path: Path,
    relationships_path: Path | None = None,
) -> nx.DiGraph:
    """Load a Neo4j CSV export into a normalized DiGraph.

    Supports both the Neo4j native export format (:ID, :LABEL, :START_ID, …)
    and the simplified slurp-generated format (nodeId, labels, startNodeId, …).

    Args:
        nodes_path: Path to the nodes CSV file.
        relationships_path: Optional path to the relationships CSV file.
            If None, the graph has no edges.

    Returns:
        Normalized DiGraph with slurp-compatible node attributes.

    Raises:
        SlurpLoadError: If nodes_path does not exist or has no ID column.
    """
    if not nodes_path.exists():
        raise SlurpLoadError(f"Nodes CSV file not found: {nodes_path}")

    G = nx.DiGraph()

    try:
        with nodes_path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise SlurpLoadError(f"Nodes CSV is empty: {nodes_path}")

            for row in reader:
                node_id = _pick(row, _NEO4J_NODE_ID_COLS)
                if node_id is None:
                    raise SlurpLoadError(
                        f"Nodes CSV has no ID column "
                        f"(expected one of {_NEO4J_NODE_ID_COLS}): {nodes_path}"
                    )

                # Display label: prefer 'name' column, fall back to 'label', then node_id
                label = _pick(row, _NEO4J_NODE_NAME_COLS) or node_id

                # Node type: 'type'/'kind' column, else the labels/category column
                node_type = (
                    _pick(row, _NEO4J_NODE_TYPE_COLS)
                    or _pick(row, _NEO4J_NODE_LABELS_COLS)
                    or ""
                )
                description = _pick(row, _NEO4J_NODE_DESC_COLS) or ""

                # Extra attributes (anything not consumed by the known columns)
                extras = {
                    k: v for k, v in row.items()
                    if k not in _KNOWN_NODE_COLS and v
                }

                attrs: dict = {"label": label}
                if node_type:
                    attrs["type"] = node_type
                if description:
                    attrs["description"] = description
                attrs.update(extras)

                G.add_node(node_id, **attrs)

    except (OSError, csv.Error) as exc:
        raise SlurpLoadError(f"Error reading nodes CSV {nodes_path}: {exc}") from exc

    if G.number_of_nodes() == 0:
        raise SlurpLoadError(f"Nodes CSV has no data rows: {nodes_path}")

    if relationships_path is not None:
        if not relationships_path.exists():
            raise SlurpLoadError(
                f"Relationships CSV file not found: {relationships_path}"
            )
        try:
            with relationships_path.open(encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    src = _pick(row, _NEO4J_START_COLS)
                    tgt = _pick(row, _NEO4J_END_COLS)
                    if not src or not tgt:
                        continue  # skip malformed rows silently
                    rel_type = _pick(row, _NEO4J_REL_TYPE_COLS) or ""
                    extras = {
                        k: v for k, v in row.items()
                        if k not in _KNOWN_REL_COLS and v
                    }
                    edge_attrs: dict = {}
                    if rel_type:
                        edge_attrs["relation"] = rel_type
                    edge_attrs.update(extras)
                    # Only add edges between nodes that exist
                    if G.has_node(src) and G.has_node(tgt):
                        G.add_edge(src, tgt, **edge_attrs)
        except (OSError, csv.Error) as exc:
            raise SlurpLoadError(
                f"Error reading relationships CSV {relationships_path}: {exc}"
            ) from exc

    return G


def _find_relationships_csv(nodes_path: Path) -> Path | None:
    """Try to locate the companion relationships CSV for a nodes CSV file."""
    stem = nodes_path.stem
    parent = nodes_path.parent

    candidates = [
        parent / "relationships.csv",
        parent / f"{stem.removesuffix('_nodes')}_relationships.csv",
        parent / f"{stem}_relationships.csv",
    ]
    for candidate in candidates:
        if candidate.exists() and candidate != nodes_path:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Export / convert
# ---------------------------------------------------------------------------

def _sanitize_graphml_attrs(attrs: dict) -> dict:
    """Keep only GraphML-compatible values (str, int, float, bool). Drop None and complex types."""
    return {
        k: v for k, v in attrs.items()
        if isinstance(v, (str, int, float, bool))
    }


def convert_graph(
    G: nx.DiGraph,
    output_path: Path,
    format: str = "graphml",
) -> list[Path]:
    """Export a slurp DiGraph to another format.

    Args:
        G: Graph to export.
        output_path: Target file path. For 'neo4j' format, two files are written:
            ``{stem}_nodes.csv`` and ``{stem}_relationships.csv`` in the same
            directory as output_path.
        format: 'graphml' or 'neo4j'.

    Returns:
        List of Path objects for every file written.

    Raises:
        ValueError: If format is unknown.
    """
    if format == "graphml":
        G_clean = nx.DiGraph()
        for nid in G.nodes:
            G_clean.add_node(nid, **_sanitize_graphml_attrs(dict(G.nodes[nid])))
        for u, v in G.edges:
            G_clean.add_edge(u, v, **_sanitize_graphml_attrs(dict(G.edges[u, v])))
        nx.write_graphml(G_clean, str(output_path))
        return [output_path]

    if format == "neo4j":
        stem = output_path.stem
        parent = output_path.parent
        nodes_out = parent / f"{stem}_nodes.csv"
        rels_out = parent / f"{stem}_relationships.csv"

        with nodes_out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["nodeId", "labels", "name", "description", "type", "file_path"]
            )
            writer.writeheader()
            for nid in G.nodes:
                attrs = G.nodes[nid]
                writer.writerow({
                    "nodeId": nid,
                    "labels": attrs.get("type") or attrs.get("file_type") or "",
                    "name": attrs.get("label", str(nid)),
                    "description": attrs.get("description") or "",
                    "type": attrs.get("type") or attrs.get("file_type") or "",
                    "file_path": attrs.get("file_path") or attrs.get("source_file") or "",
                })

        with rels_out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["startNodeId", "endNodeId", "type"])
            writer.writeheader()
            for u, v in G.edges:
                writer.writerow({
                    "startNodeId": u,
                    "endNodeId": v,
                    "type": G.edges[u, v].get("relation", ""),
                })

        return [nodes_out, rels_out]

    raise ValueError(
        f"Unknown export format {format!r}. Expected 'graphml' or 'neo4j'."
    )


# ---------------------------------------------------------------------------
# Public entry point — auto-detects format by file extension
# ---------------------------------------------------------------------------

def load_graph(path: Path) -> nx.DiGraph:
    """Load a graph file and return a DiGraph.

    Format is detected by file extension:
    - ``.json``    → graphify or generic JSON format
    - ``.graphml`` → GraphML (standard NetworkX / yEd / Gephi format)
    - ``.csv``     → Neo4j nodes CSV; automatically searches for a sibling
                     relationships CSV in the same directory

    Args:
        path: Path to the graph file.

    Returns:
        A directed graph with node and edge attributes normalised to slurp schema.

    Raises:
        SlurpLoadError: If the file cannot be read or contains no nodes.
    """
    suffix = path.suffix.lower()

    if suffix == ".graphml":
        return _load_graphml(path)

    if suffix == ".csv":
        rels_path = _find_relationships_csv(path)
        return load_graph_neo4j(path, rels_path)

    # Default: JSON (graphify / generic)
    return _load_json(path)
