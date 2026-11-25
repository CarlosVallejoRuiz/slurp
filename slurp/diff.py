"""Compare two graph versions and compute change impact."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import networkx as nx

from slurp import _graphutils
from slurp._graphutils import _call_graph_gap, _extensions_with_calls, _split_callers


@dataclass
class GraphDiff:
    """Structural difference between two graph versions."""

    added_nodes: list[str] = field(default_factory=list)
    removed_nodes: list[str] = field(default_factory=list)
    modified_nodes: list[str] = field(default_factory=list)
    added_edges: list[tuple[str, str]] = field(default_factory=list)
    removed_edges: list[tuple[str, str]] = field(default_factory=list)
    impact_score: float = 0.0
    # Production callers the change reaches — what impact_score is computed
    # from, kept so the figure can be checked.
    dependents: list[str] = field(default_factory=list)
    # Changed nodes in a language with no call graph: their callers are unknown.
    unknown_nodes: list[str] = field(default_factory=list)
    risk_reason: str = ""
    # Nodes still in the new graph that were direct neighbours of a removed
    # node in the old one, where it still exists: its callers and callees.
    removed_neighbors: list[str] = field(default_factory=list)

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


# DECISION: impact_score = 1 - exp(-D / k), where D is the number of distinct
# production callers the change reaches. It used to be the mean degree
# centrality of the touched nodes, which on a real graph never exceeds ~0.03:
# deleting createAdminClient, with 97 callers, scored 0.0012 — "low".
#
# The scale and both bands are derived from _RISK_HIGH_MIN / _RISK_MEDIUM_MIN,
# read when called, so `slurp diff`, `slurp impact` and `slurp explain` rate a
# dependent count identically. k puts the high band at score 0.5 halfway between
# HIGH_MIN - 1 and HIGH_MIN dependents; each threshold sits halfway between two
# integer counts, so no real D lands on a boundary. With the constants at 6 and
# 2: k ~= 7.9; D >= 6 is high (score >= 0.5), D 2-5 medium (>= ~0.17), 0-1 low.
def _impact_scale() -> float:
    return (_graphutils._RISK_HIGH_MIN - 0.5) / math.log(2)


def _impact_from_dependents(dependents: float) -> float:
    return 1.0 - math.exp(-dependents / _impact_scale())


def _thresholds() -> tuple[float, float]:
    """(high, medium) score thresholds, each halfway between two dependent counts."""
    return (_impact_from_dependents(_graphutils._RISK_HIGH_MIN - 0.5),
            _impact_from_dependents(_graphutils._RISK_MEDIUM_MIN - 0.5))


def diff_graphs(old: nx.DiGraph, new: nx.DiGraph) -> GraphDiff:
    """Computes the structural difference between two graph versions.

    A node is considered modified when its label or type attribute changed.
    Other attribute changes (description, file_path, etc.) are not tracked.

    Args:
        old: Previous graph version.
        new: Current/updated graph version.

    Returns:
        GraphDiff with sorted lists of changes and an impact_score in [0.0, 1.0]
        computed from the production callers the change reaches (see
        _impact_from_dependents), plus those callers and any changed nodes
        whose language has no call graph.
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

    # What could break: callers of deleted nodes (they now call nothing), and
    # callers of nodes whose own behaviour changed — relabelled, retyped, or
    # calling something different for a reason other than a deleted callee.
    removed = set(removed_nodes)
    changed = set(modified_nodes) | {
        u for u, v in added_edges + removed_edges
        if u in old and u in new and v not in removed
    }
    dependents: set[str] = set()
    for node in removed:
        dependents.update(_split_callers(old, node)[0])
    for node in changed:
        dependents.update(_split_callers(new, node)[0])
    dependents -= removed

    # A changed node with no call graph has callers nobody can count.
    old_calls, new_calls = _extensions_with_calls(old), _extensions_with_calls(new)
    gaps = {n: _call_graph_gap(old, n, old_calls) for n in removed}
    gaps.update({n: _call_graph_gap(new, n, new_calls) for n in changed})
    unknown = sorted(n for n, gap in gaps.items() if gap)

    # DECISION: a deleted node exists only in the old graph, so its risk
    # neighbourhood is read there. Until v1.1.1 the report looked for it in the
    # new graph, found nothing, and deleting a function with 97 callers listed
    # none of them as at risk. Neighbours deleted as well are not at risk.
    removed_neighbors: set[str] = set()
    for node in removed:
        removed_neighbors.update(old.predecessors(node))
        removed_neighbors.update(old.successors(node))
    removed_neighbors &= new_nodes

    return GraphDiff(
        added_nodes=added_nodes,
        removed_nodes=removed_nodes,
        modified_nodes=modified_nodes,
        added_edges=added_edges,
        removed_edges=removed_edges,
        impact_score=round(_impact_from_dependents(len(dependents)), 4),
        dependents=sorted(dependents),
        unknown_nodes=unknown,
        risk_reason=gaps[unknown[0]] if unknown else "",
        removed_neighbors=sorted(removed_neighbors),
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
    "unknown": "❔ Impact unknown",
}
_SCORE_LABELS = {"high": "🔴 high", "medium": "🟡 medium", "low": "🟢 low",
                 "unknown": "⚪ unknown"}


def impact_severity(impact_score: float) -> str:
    """Severity band for an impact score, on the explain/impact dependent bands.

    Args:
        impact_score: GraphDiff.impact_score, in [0, 1].

    Returns:
        'high', 'medium' or 'low' — thresholds derived from the risk constants
        (0.5 and ~0.17 with the defaults; see _thresholds).
    """
    high, medium = _thresholds()
    if impact_score >= high:
        return "high"
    if impact_score >= medium:
        return "medium"
    return "low"


def diff_severity(diff: GraphDiff) -> str:
    """The band to report for a whole diff.

    Like impact_severity, except a "low" that rests on code with no call graph
    becomes "unknown": zero counted callers there says nothing.
    """
    band = impact_severity(diff.impact_score)
    return "unknown" if band == "low" and diff.unknown_nodes else band


def diff_headline(diff: GraphDiff) -> str:
    """One-line verdict for a diff, unknown risk included."""
    return _HEADLINES[diff_severity(diff)]


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
    impact_label = _SCORE_LABELS[diff_severity(diff)]
    lines.append(f"- **{diff_headline(diff)}**")
    lines.append(f"- **Impact score:** {diff.impact_score:.4f} ({impact_label}) — "
                 f"{len(diff.dependents)} production "
                 f"{'caller' if len(diff.dependents) == 1 else 'callers'} reached")
    if diff.unknown_nodes:
        lines.append(f"- **Risk unknown for {len(diff.unknown_nodes)} changed "
                     f"{'node' if len(diff.unknown_nodes) == 1 else 'nodes'}:** "
                     f"{diff.risk_reason}")
    lines.append(
        f"- {len(diff.added_nodes)} nodes added · "
        f"{len(diff.removed_nodes)} removed · "
        f"{len(diff.modified_nodes)} modified"
    )
    lines.append(
        f"- {len(diff.added_edges)} edges added · "
        f"{len(diff.removed_edges)} removed"
    )

    # At-risk: direct neighbors of added/modified nodes in G_new, and of
    # removed nodes in the old graph (diff.removed_neighbors).
    affected_ids = set(diff.added_nodes) | set(diff.modified_nodes)
    at_risk: set[str] = set(diff.removed_neighbors)
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

    # Removed nodes aren't in G_new — add them from G_old, wired to the
    # neighbours they had there, so the viz shows who depended on them. Only
    # edges touching a removed node come back: the rest is the new graph.
    removed_set = set(diff.removed_nodes)
    for nid in diff.removed_nodes:
        old_attrs = dict(G_old.nodes[nid]) if nid in G_old else {}
        viz_G.add_node(nid, **old_attrs)
    for u, v in diff.removed_edges:
        if (u in removed_set or v in removed_set) and u in viz_G and v in viz_G:
            viz_G.add_edge(u, v, **(G_old.edges[u, v] if G_old.has_edge(u, v) else {}))

    # Stamp diff status as node type for colour-coding.
    added_set = set(diff.added_nodes)
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
    # No token figures: a diff view has no budget and sends no text, so the viz
    # shows neither a token count nor a savings badge (as for `slurp impact`).
    # Until v1.1.0 both were made up from a fixed cost per node.
    stats = {
        "nodes_selected": n_shown,
        "nodes_total": max(1, n_total),
        "tokens_used": 0,
        "tokens_budget": 0,
        "coverage_pct": round(n_shown / max(1, n_total) * 100, 1),
    }

    return viz_G, scores, stats
