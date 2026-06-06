"""Loads graph.json into a networkx DiGraph."""

import json
from pathlib import Path

import networkx as nx


class SlurpLoadError(Exception):
    """Raised when a graph file cannot be loaded or is invalid."""


def detect_format(data: dict) -> str:
    """Returns 'graphify' or 'generic'.

    Graphify format is detected by:
    - presence of a 'links' key (real graphify: NetworkX JSON serialization), or
    - nodes with any of: type, description, importance, file_path, file_type, source_file
    Generic format has only id and label on nodes.
    """
    # Real graphify output uses 'links' (NetworkX node-link format)
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


def load_graph(path: Path) -> nx.DiGraph:
    """Loads graph.json and returns a DiGraph.

    Args:
        path: Path to the graph JSON file.

    Returns:
        A directed graph with node and edge attributes populated.

    Raises:
        SlurpLoadError: If the file does not exist, is not valid JSON,
            or contains no nodes.
    """
    if not path.exists():
        raise SlurpLoadError(f"Graph file not found: {path}")

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SlurpLoadError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise SlurpLoadError(f"Expected a JSON object at the top level, got {type(data).__name__}: {path}")

    nodes = data.get("nodes")
    if not nodes:
        raise SlurpLoadError(f"Graph has no nodes: {path}")

    for i, node in enumerate(nodes):
        if "id" not in node:
            raise SlurpLoadError(f"Node at index {i} is missing required field 'id': {path}")
        if "label" not in node:
            raise SlurpLoadError(f"Node '{node['id']}' is missing required field 'label': {path}")

    return _build_graph(data)
