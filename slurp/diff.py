"""Compare two graph versions and compute change impact."""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx


@dataclass
class GraphDiff:
    """Structural difference between two graph versions."""

    added_nodes: list[str] = field(default_factory=list)
    removed_nodes: list[str] = field(default_factory=list)
    modified_nodes: list[str] = field(default_factory=list)
    added_edges: list[tuple[str, str]] = field(default_factory=list)
    removed_edges: list[tuple[str, str]] = field(default_factory=list)
    impact_score: float = 0.0

    @property
    def has_changes(self) -> bool:
        return bool(
            self.added_nodes or self.removed_nodes or self.modified_nodes
            or self.added_edges or self.removed_edges
        )


def _centrality(G: nx.DiGraph) -> dict[str, float]:
    """In+out degree centrality normalized to [0.0, 1.0]."""
    n = G.number_of_nodes()
    if n <= 1:
        return {nid: 0.0 for nid in G.nodes}
    denom = 2 * (n - 1)
    return {
        nid: (G.in_degree(nid) + G.out_degree(nid)) / denom
        for nid in G.nodes
    }


def diff_graphs(old: nx.DiGraph, new: nx.DiGraph) -> GraphDiff:
    """Computes the structural difference between two graph versions.

    A node is considered modified when its label or type attribute changed.
    Other attribute changes (description, file_path, etc.) are not tracked.

    Args:
        old: Previous graph version.
        new: Current/updated graph version.

    Returns:
        GraphDiff with sorted lists of changes and an impact_score in [0.0, 1.0]
        based on the mean centrality of all affected nodes (including edge endpoints).
    """
    old_nodes = set(old.nodes)
    new_nodes = set(new.nodes)

    added_nodes = sorted(new_nodes - old_nodes)
    removed_nodes = sorted(old_nodes - new_nodes)
    modified_nodes = sorted(
        nid
        for nid in old_nodes & new_nodes
        if (
            old.nodes[nid].get("label") != new.nodes[nid].get("label")
            or old.nodes[nid].get("type") != new.nodes[nid].get("type")
        )
    )

    old_edges = set(old.edges)
    new_edges = set(new.edges)
    added_edges = sorted(new_edges - old_edges)
    removed_edges = sorted(old_edges - new_edges)

    # Impact: mean centrality of all directly affected nodes.
    new_cent = _centrality(new)
    old_cent = _centrality(old)

    affected: set[str] = set(added_nodes) | set(removed_nodes) | set(modified_nodes)
    for u, v in added_edges + removed_edges:
        affected.add(u)
        affected.add(v)

    if affected:
        scores = [new_cent.get(nid, old_cent.get(nid, 0.0)) for nid in affected]
        impact = min(1.0, sum(scores) / len(scores))
    else:
        impact = 0.0

    return GraphDiff(
        added_nodes=added_nodes,
        removed_nodes=removed_nodes,
        modified_nodes=modified_nodes,
        added_edges=added_edges,
        removed_edges=removed_edges,
        impact_score=round(impact, 4),
    )


def affected_subgraph(
    G: nx.DiGraph,
    diff: GraphDiff,
    hops: int = 2,
) -> nx.DiGraph:
    """Returns the subgraph of nodes affected by the diff plus their N-hop neighborhood.

    Removed nodes are not in G and are therefore absent from the result — use
    diff.removed_nodes directly if you need to display them.

    Args:
        G:    The new (current) graph.
        diff: Diff produced by diff_graphs.
        hops: Number of hops to expand from seed nodes.

    Returns:
        A copy of the induced subgraph over the affected neighborhood.
    """
    seeds: set[str] = set()
    seeds.update(diff.added_nodes)
    seeds.update(diff.modified_nodes)
    for u, v in diff.added_edges + diff.removed_edges:
        seeds.add(u)
        seeds.add(v)

    # Keep only nodes that actually exist in G (removed nodes won't).
    seeds &= set(G.nodes)

    if not seeds:
        return G.subgraph(set()).copy()

    visited = set(seeds)
    frontier = set(seeds)

    for _ in range(hops):
        next_f: set[str] = set()
        for nid in frontier:
            next_f.update(G.predecessors(nid))
            next_f.update(G.successors(nid))
        next_f -= visited
        if not next_f:
            break
        visited.update(next_f)
        frontier = next_f

    return G.subgraph(visited).copy()


# DECISION: one severity band drives both the headline and the score label, so
# the two lines of the summary can never disagree. Thresholds are inclusive
# (>=) to match the label logic these bands were unified with.
_HEADLINES = {
    "high": "⚠️ High impact change",
    "medium": "⚡ Medium impact change",
    "low": "✅ Low impact change",
}
_SCORE_LABELS = {"high": "🔴 high", "medium": "🟡 medium", "low": "🟢 low"}


def impact_severity(impact_score: float) -> str:
    """Severity band for an impact score.

    Args:
        impact_score: GraphDiff.impact_score, in [0, 1].

    Returns:
        One of 'high' (>= 0.5), 'medium' (>= 0.2), or 'low'.
    """
    if impact_score >= 0.5:
        return "high"
    if impact_score >= 0.2:
        return "medium"
    return "low"


def impact_headline(impact_score: float) -> str:
    """One-line verdict on how risky a change is.

    Args:
        impact_score: GraphDiff.impact_score, in [0, 1].

    Returns:
        Headline string with a severity marker.
    """
    return _HEADLINES[impact_severity(impact_score)]


def review_candidates(
    G_new: nx.DiGraph,
    diff: GraphDiff,
    hops: int = 2,
    limit: int = 5,
) -> list[tuple[str, float]]:
    """Rank the affected neighborhood by how central its nodes are.

    DECISION: nodes come from the affected subgraph, but centrality is measured
    on the full graph — a node's blast radius is how connected it is in the whole
    codebase, not within the slice we happen to be showing.

    Args:
        G_new: Current graph.
        diff:  Diff produced by diff_graphs.
        hops:  Neighborhood expansion depth for the affected subgraph.
        limit: Maximum number of nodes to return.

    Returns:
        List of (node_id, centrality) ordered by centrality descending, then by
        node id for deterministic output.
    """
    affected = affected_subgraph(G_new, diff, hops=hops)
    if affected.number_of_nodes() == 0:
        return []

    cent = _centrality(G_new)
    ranked = sorted(
        ((nid, cent.get(nid, 0.0)) for nid in affected.nodes),
        key=lambda item: (-item[1], item[0]),
    )
    return ranked[:limit]


def format_diff(diff: GraphDiff, G_new: nx.DiGraph, hops: int = 2) -> str:
    """Formats a GraphDiff as a Markdown impact report.

    Sections: executive summary, added/removed/modified nodes, affected edges,
    at-risk nodes (direct neighbors of changed nodes with their centrality), and
    a review shortlist ranked by centrality.

    Args:
        diff:  Diff produced by diff_graphs.
        G_new: Current graph (used for labels, types, and centrality).
        hops:  Neighborhood depth used for the "What to review" shortlist.

    Returns:
        Markdown string.
    """
    lines: list[str] = []
    lines.append("# Slurp Diff — Impact Analysis")
    lines.append("")

    # --- Summary ---
    lines.append("## Summary")
    impact_label = _SCORE_LABELS[impact_severity(diff.impact_score)]
    lines.append(f"- **{impact_headline(diff.impact_score)}**")
    lines.append(f"- **Impact score:** {diff.impact_score:.4f} ({impact_label})")
    lines.append(
        f"- {len(diff.added_nodes)} nodes added · "
        f"{len(diff.removed_nodes)} removed · "
        f"{len(diff.modified_nodes)} modified"
    )
    lines.append(
        f"- {len(diff.added_edges)} edges added · "
        f"{len(diff.removed_edges)} removed"
    )

    # At-risk: direct neighbors of added/modified nodes in G_new.
    affected_ids = set(diff.added_nodes) | set(diff.modified_nodes)
    at_risk: set[str] = set()
    for nid in affected_ids:
        if nid in G_new:
            at_risk.update(G_new.predecessors(nid))
            at_risk.update(G_new.successors(nid))
    at_risk -= affected_ids

    lines.append(f"- {len(at_risk)} nodes in risk neighborhood")
    lines.append("")

    if not diff.has_changes:
        lines.append("No changes detected between the two graphs.")
        return "\n".join(lines)

    # --- Added nodes ---
    if diff.added_nodes:
        lines.append(f"## Added Nodes ({len(diff.added_nodes)})")
        lines.append("")
        for nid in diff.added_nodes:
            attrs = G_new.nodes[nid] if nid in G_new else {}
            label = attrs.get("label", nid)
            ntype = attrs.get("type", "")
            heading = f"### {label}"
            if ntype:
                heading += f" ({ntype})"
            lines.append(heading)
            if desc := attrs.get("description"):
                lines.append(desc)
            fpath = attrs.get("file_path") or attrs.get("source_file") or ""
            if fpath:
                lines.append(f"→ File: {fpath}")
            lines.append("")

    # --- Removed nodes ---
    if diff.removed_nodes:
        lines.append(f"## Removed Nodes ({len(diff.removed_nodes)})")
        lines.append("")
        for nid in diff.removed_nodes:
            lines.append(f"### {nid}")
            lines.append("")

    # --- Modified nodes ---
    if diff.modified_nodes:
        lines.append(f"## Modified Nodes ({len(diff.modified_nodes)})")
        lines.append("")
        for nid in diff.modified_nodes:
            attrs = G_new.nodes[nid] if nid in G_new else {}
            label = attrs.get("label", nid)
            ntype = attrs.get("type", "")
            heading = f"### {label}"
            if ntype:
                heading += f" ({ntype})"
            lines.append(heading)
            lines.append("")

    # --- Affected edges ---
    if diff.added_edges or diff.removed_edges:
        lines.append("## Affected Edges")
        lines.append("")
        if diff.added_edges:
            lines.append(f"**Added ({len(diff.added_edges)}):**")
            for u, v in diff.added_edges:
                rel = (G_new.edges[u, v].get("relation", "→") if (u, v) in G_new.edges else "→")
                lines.append(f"- {u} → {rel} → {v}")
            lines.append("")
        if diff.removed_edges:
            lines.append(f"**Removed ({len(diff.removed_edges)}):**")
            for u, v in diff.removed_edges:
                lines.append(f"- {u} → {v}")
            lines.append("")

    # --- Nodes at risk ---
    if at_risk:
        cent = _centrality(G_new)
        lines.append("## Nodes at Risk")
        lines.append("Direct neighbors of changed nodes:")
        lines.append("")
        for nid in sorted(at_risk, key=lambda n: -cent.get(n, 0.0)):
            attrs = G_new.nodes[nid] if nid in G_new else {}
            label = attrs.get("label", nid)
            ntype = attrs.get("type", "")
            c = cent.get(nid, 0.0)
            entry = f"- {label}"
            if ntype:
                entry += f" ({ntype})"
            entry += f" · centrality: {c:.4f}"
            lines.append(entry)
        lines.append("")

    # --- What to review ---
    if candidates := review_candidates(G_new, diff, hops=hops):
        lines.append("## What to review")
        lines.append("Most connected nodes in the blast radius — review these first:")
        lines.append("")
        for rank, (nid, c) in enumerate(candidates, 1):
            attrs = G_new.nodes[nid] if nid in G_new else {}
            label = attrs.get("label", nid)
            ntype = attrs.get("type", "")
            entry = f"{rank}. {label}"
            if ntype:
                entry += f" ({ntype})"
            entry += f" · centrality: {c:.4f}"
            fpath = attrs.get("file_path") or attrs.get("source_file") or ""
            if fpath:
                entry += f" · {fpath}"
            lines.append(entry)
        lines.append("")

    return "\n".join(lines)


def build_diff_viz_graph(
    G_old: nx.DiGraph,
    G_new: nx.DiGraph,
    diff: GraphDiff,
    hops: int = 2,
) -> tuple[nx.DiGraph, dict[str, float], dict]:
    """Build a graph + scores/stats for visualising a diff with viz.build_html.

    Affected nodes get score 1.0; neighborhood context nodes get 0.4.
    Each node's 'type' is set to 'added', 'removed', 'modified', or 'unchanged'
    to trigger the diff colour palette in the visualiser.

    Args:
        G_old: Previous graph (provides attributes for removed nodes).
        G_new: Current graph.
        diff:  Diff from diff_graphs.
        hops:  Neighborhood expansion depth.

    Returns:
        Tuple of (viz_graph, scores, stats) ready to pass to viz.build_html.
    """
    affected = affected_subgraph(G_new, diff, hops)
    viz_G: nx.DiGraph = affected.copy()

    # Removed nodes aren't in G_new — add them from G_old as isolated nodes.
    for nid in diff.removed_nodes:
        old_attrs = dict(G_old.nodes[nid]) if nid in G_old else {}
        viz_G.add_node(nid, **old_attrs)

    # Stamp diff status as node type for colour-coding.
    added_set = set(diff.added_nodes)
    removed_set = set(diff.removed_nodes)
    modified_set = set(diff.modified_nodes)

    for nid in viz_G.nodes:
        if nid in added_set:
            viz_G.nodes[nid]["type"] = "added"
        elif nid in removed_set:
            viz_G.nodes[nid]["type"] = "removed"
        elif nid in modified_set:
            viz_G.nodes[nid]["type"] = "modified"
        else:
            viz_G.nodes[nid]["type"] = "unchanged"

    direct = added_set | removed_set | modified_set
    scores: dict[str, float] = {
        nid: 1.0 if nid in direct else 0.4
        for nid in viz_G.nodes
    }

    n_total = G_new.number_of_nodes()
    n_shown = viz_G.number_of_nodes()
    stats = {
        "nodes_selected": n_shown,
        "nodes_total": max(1, n_total),
        "tokens_used": n_shown * 50,
        "tokens_budget": max(1, n_total) * 50,
        "coverage_pct": round(n_shown / max(1, n_total) * 100, 1),
    }

    return viz_G, scores, stats
