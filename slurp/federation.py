"""Merge several project graphs into one queryable graph.

A microservice split across repos is one system, but `slurp` sees one graph at a
time — a query about "auth flow" stops at the edge of whichever repo you pointed
it at. Federation stitches the graphs together under project-prefixed node ids,
so a single query can range across all of them without ids colliding.
"""

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# Separates the project label from the node's original id: "auth::login".
PROJECT_SEPARATOR = "::"

# Node attribute recording which graph a node came from.
PROJECT_ATTR = "_project"


@dataclass
class FederationStats:
    """What a merge produced.

    Attributes:
        graphs_merged: Number of input graphs.
        total_nodes: Nodes in the merged graph.
        nodes_per_graph: Node count keyed by project label.
        total_edges: Edges in the merged graph.
        collision_count: Original node ids that appeared in more than one graph.
            These are the ids that would have silently overwritten each other
            without prefixing.
    """

    graphs_merged: int = 0
    total_nodes: int = 0
    nodes_per_graph: dict[str, int] = field(default_factory=dict)
    total_edges: int = 0
    collision_count: int = 0


def _edges_of(graph: dict) -> list[dict]:
    """Return a graph dict's edge list under either supported key."""
    if "links" in graph:
        return graph.get("links") or []
    return graph.get("edges") or []


def derive_labels(paths: list[Path]) -> list[str]:
    """Name each graph from its path, disambiguating repeats.

    The obvious label is the file stem, but federated projects overwhelmingly
    hold their graph at ``<project>/graphify-out/graph.json`` — every stem would
    be "graph". Colliding stems fall back to the parent directory name, then to
    the grandparent, and finally to a numeric suffix.

    Args:
        paths: Graph file paths, in the order they were given.

    Returns:
        One label per path, same order, all distinct.
    """
    def _candidates(path: Path) -> list[str]:
        parts = [path.stem, *[p.name for p in path.parents if p.name]]
        # "graphify-out" names the tool, not the project — skip it as a label.
        return [p for p in parts if p and p not in {"graphify-out", ".", ".."}]

    stem_counts = Counter(p.stem for p in paths)
    labels: list[str] = []
    used: set[str] = set()

    for index, path in enumerate(paths):
        options = _candidates(path)
        # A stem shared by several inputs identifies none of them — drop it and
        # walk up the path for something that actually names the project.
        if stem_counts[path.stem] > 1:
            options = [o for o in options if o != path.stem]

        chosen = ""
        for option in options:
            if option not in used:
                chosen = option
                break
        if not chosen:
            chosen = f"graph{index + 1}"
            while chosen in used:
                index += 1
                chosen = f"graph{index + 1}"
        used.add(chosen)
        labels.append(chosen)

    return labels


def _resolve_labels(graphs: list[dict], labels: list[str] | None) -> list[str]:
    """Validate caller-supplied labels, or invent positional ones.

    merge_graphs receives parsed dicts rather than paths, so it cannot see a
    file stem; callers that have paths should pass derive_labels(paths).
    """
    if labels is None:
        return [f"graph{i + 1}" for i in range(len(graphs))]
    if len(labels) != len(graphs):
        raise ValueError(
            f"Got {len(labels)} labels for {len(graphs)} graphs — "
            "pass one label per graph or none at all."
        )
    if len(set(labels)) != len(labels):
        dupes = sorted({lbl for lbl in labels if labels.count(lbl) > 1})
        raise ValueError(
            f"Duplicate graph labels: {', '.join(dupes)}. "
            "Labels must be unique — they namespace the node ids."
        )
    return list(labels)


def merge_graphs(graphs: list[dict], labels: list[str] | None = None) -> dict:
    """Merge several graph.json dicts into one.

    Node ids are namespaced as ``<label>::<original id>`` so that two projects
    can both define ``main`` without one erasing the other, and every node gains
    a ``_project`` attribute naming its source graph. Edge endpoints are
    rewritten to match.

    A single graph passes through untouched — no prefixes, no ``_project`` — so
    that federating one graph is indistinguishable from not federating at all.

    Args:
        graphs: Parsed graph.json dicts, each with 'nodes' and 'links'/'edges'.
        labels: One project label per graph. Defaults to graph1, graph2, …;
            callers holding file paths should pass derive_labels(paths).

    Returns:
        A standard graph.json dict with 'nodes' and 'links', loadable by
        loader.load_graph like any other.

    Raises:
        ValueError: If labels is the wrong length or contains duplicates.
    """
    merged, _ = merge_graphs_with_stats(graphs, labels)
    return merged


def merge_graphs_with_stats(
    graphs: list[dict],
    labels: list[str] | None = None,
) -> tuple[dict, FederationStats]:
    """Merge graphs and report what happened.

    Same contract as :func:`merge_graphs`, but also returns a FederationStats
    describing the merge — used by the CLI to print the federation header.

    Args:
        graphs: Parsed graph.json dicts.
        labels: One project label per graph, or None.

    Returns:
        Tuple of (merged graph dict, FederationStats).

    Raises:
        ValueError: If labels is the wrong length or contains duplicates.
    """
    if not graphs:
        return {"nodes": [], "links": [], "built_at_commit": None}, FederationStats()

    resolved = _resolve_labels(graphs, labels)

    if len(graphs) == 1:
        nodes = list(graphs[0].get("nodes") or [])
        edges = list(_edges_of(graphs[0]))
        stats = FederationStats(
            graphs_merged=1,
            total_nodes=len(nodes),
            nodes_per_graph={resolved[0]: len(nodes)},
            total_edges=len(edges),
            collision_count=0,
        )
        return {**graphs[0], "nodes": nodes, "links": edges}, stats

    # Count original ids across graphs to report genuine collisions. Ids repeated
    # *within* one graph are that graph's own duplicates, not a federation issue.
    id_owners: Counter[str] = Counter()
    for graph in graphs:
        for node_id in {n["id"] for n in (graph.get("nodes") or []) if "id" in n}:
            id_owners[node_id] += 1
    collision_count = sum(1 for count in id_owners.values() if count > 1)

    merged_nodes: list[dict] = []
    merged_edges: list[dict] = []
    nodes_per_graph: dict[str, int] = {}
    seen_ids: set[str] = set()

    for graph, label in zip(graphs, resolved, strict=True):
        prefix = f"{label}{PROJECT_SEPARATOR}"
        count = 0
        local_ids: set[str] = set()

        for node in graph.get("nodes") or []:
            if "id" not in node:
                continue
            new_id = f"{prefix}{node['id']}"
            local_ids.add(node["id"])
            if new_id in seen_ids:
                continue
            seen_ids.add(new_id)
            merged_nodes.append({**node, "id": new_id, PROJECT_ATTR: label})
            count += 1

        for edge in _edges_of(graph):
            source, target = edge.get("source"), edge.get("target")
            if source is None or target is None:
                continue
            merged_edges.append({
                **edge,
                "source": f"{prefix}{source}",
                "target": f"{prefix}{target}",
            })

        nodes_per_graph[label] = count

    # Drop edges whose endpoints did not survive, matching indexer behaviour.
    merged_edges = [
        e for e in merged_edges
        if e["source"] in seen_ids and e["target"] in seen_ids
    ]

    stats = FederationStats(
        graphs_merged=len(graphs),
        total_nodes=len(merged_nodes),
        nodes_per_graph=nodes_per_graph,
        total_edges=len(merged_edges),
        collision_count=collision_count,
    )
    return {"nodes": merged_nodes, "links": merged_edges,
            "built_at_commit": None}, stats


def load_graphs(paths: list[Path]) -> list[dict]:
    """Read graph.json files from disk as raw dicts.

    Args:
        paths: Graph file paths.

    Returns:
        One parsed dict per path, in order.

    Raises:
        SlurpLoadError: If any file is missing or unreadable as JSON.
    """
    import json

    from slurp.loader import SlurpLoadError

    graphs: list[dict] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SlurpLoadError(f"Graph file not found: {path}") from None
        except (OSError, ValueError) as exc:
            raise SlurpLoadError(f"Could not read graph {path}: {exc}") from None
        if not isinstance(data, dict) or not data.get("nodes"):
            raise SlurpLoadError(f"Graph has no nodes: {path}")
        graphs.append(data)
    return graphs


def load_and_merge(paths: list[Path]) -> tuple[dict, FederationStats]:
    """Read graph files from disk and merge them, labelling by path.

    Args:
        paths: Graph file paths. Labels are derived from the paths.

    Returns:
        Tuple of (merged graph dict, FederationStats).

    Raises:
        SlurpLoadError: If any file is missing or unreadable as JSON.
    """
    return merge_graphs_with_stats(load_graphs(paths), derive_labels(paths))


def format_federation_header(stats: FederationStats) -> str:
    """One-line summary of a federated graph, for the query header."""
    return (
        f"Federated: {stats.graphs_merged} graphs · "
        f"{stats.total_nodes:,} nodes total"
    )
