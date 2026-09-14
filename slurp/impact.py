"""Blast radius of editing one file, predicted from the current graph alone.

`slurp diff` answers this after the fact, by comparing two snapshots. This
answers it before: given a file about to be edited, which nodes in it carry
dependants, and how far those reach. No second snapshot, no commit needed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import networkx as nx

from slurp.explainer import _display, _split_callers

# Thresholds mirror explainer._risk_level so a file and its nodes never
# disagree about how dangerous the same code is.
_RISK_HIGH_MIN = 6
_RISK_MEDIUM_MIN = 2
_TOP_FILES = 10
_SAFE_LISTED = 6


@dataclass
class ImpactResult:
    """What editing one file could reach."""

    file_path: str
    nodes_in_file: list[str] = field(default_factory=list)
    direct_dependents: list[str] = field(default_factory=list)
    transitive_dependents: list[str] = field(default_factory=list)
    highest_risk_node: str = ""
    risk_level: str = "LOW"
    affected_files: list[str] = field(default_factory=list)
    impact_score: float = 0.0
    # Nodes in the file nothing depends on, which is the other half of the
    # question: not only what is dangerous, but what is free to touch.
    safe_nodes: list[str] = field(default_factory=list)
    dependents_per_node: dict[str, int] = field(default_factory=dict)


def _source_of(G: nx.DiGraph, node: str) -> str:
    attrs = G.nodes[node]
    return str(attrs.get("source_file") or attrs.get("file_path") or "")


def _normalise(path: str) -> str:
    """Compare paths without tripping over separators or a leading `./`."""
    return str(path).replace("\\", "/").removeprefix("./").strip("/")


def nodes_in_file(G: nx.DiGraph, file_path: str) -> list[str]:
    """Every node defined in *file_path*.

    Matched on a normalised suffix, so `admin-actions.ts` finds the file that
    `lib/supabase/admin-actions.ts` holds without the caller having to know
    how the graph spelled its paths.
    """
    wanted = _normalise(file_path)
    if not wanted:
        return []
    exact = [n for n in G.nodes if _normalise(_source_of(G, n)) == wanted]
    if exact:
        return sorted(exact)
    return sorted(
        n for n in G.nodes
        if _normalise(_source_of(G, n)).endswith(f"/{wanted}")
    )


def _is_file_node(G: nx.DiGraph, node: str) -> bool:
    """Whether *node* represents the file itself rather than a definition in it."""
    attrs = G.nodes[node]
    if attrs.get("type") in ("module", "file"):
        return True
    label = str(attrs.get("label", "")).strip()
    source = _normalise(_source_of(G, node))
    return bool(label) and bool(source) and source.rsplit("/", 1)[-1] == label


def _dependants(G: nx.DiGraph, node: str) -> list[str]:
    """Nodes that depend on *node* — callers, never its containing module."""
    production, tests = _split_callers(G, node)
    return production + tests


def analyze_impact(G: nx.DiGraph, file_path: str, hops: int = 2) -> ImpactResult:
    """Predict what editing *file_path* could break.

    Args:
        G: The project graph.
        file_path: Path of the file about to be edited, relative to the project.
        hops: How far to follow dependants outward.

    Returns:
        An ImpactResult. A file with no nodes in the graph yields an empty
        result rather than an error: asking about an unindexed file is a
        reasonable question with a boring answer.
    """
    result = ImpactResult(file_path=file_path)
    owned = nodes_in_file(G, file_path)
    if not owned:
        return result

    result.nodes_in_file = owned
    owned_set = set(owned)

    direct: set[str] = set()
    per_node: dict[str, int] = {}
    untouched: list[str] = []
    for node in owned:
        callers = _dependants(G, node)
        outside = [c for c in callers if c not in owned_set]
        per_node[node] = len(outside)
        direct.update(outside)
        # "Safe" means nothing depends on it at all. A node with three callers
        # in this same file is not safe to edit — it is merely contained.
        if not callers:
            untouched.append(node)

    result.direct_dependents = sorted(direct)
    result.dependents_per_node = per_node
    result.safe_nodes = sorted(untouched)

    # Transitive reach: follow dependants of dependants, against the edges,
    # since "who would notice this change" runs the opposite way to "calls".
    reached: set[str] = set(direct)
    frontier = set(direct)
    for _ in range(max(0, hops - 1)):
        nxt: set[str] = set()
        for node in frontier:
            nxt.update(c for c in _dependants(G, node)
                       if c not in reached and c not in owned_set)
        if not nxt:
            break
        reached |= nxt
        frontier = nxt
    result.transitive_dependents = sorted(reached - direct)

    # The file's own node is in the file by definition and usually carries the
    # import count, which would crown it every time. It is not a thing anyone
    # edits, so a real definition wins whenever one exists. Type alone will not
    # find it — graphify writes no `type` at all — but the file node is the one
    # labelled after the file.
    definitions = {n: c for n, c in per_node.items() if not _is_file_node(G, n)}
    ranked = definitions or per_node
    if ranked:
        result.highest_risk_node = max(ranked, key=lambda n: (ranked[n], -len(n)))

    total = len(direct)
    result.risk_level = (
        "HIGH" if total >= _RISK_HIGH_MIN
        else "MEDIUM" if total >= _RISK_MEDIUM_MIN
        else "LOW"
    )

    files = {
        _source_of(G, n) for n in direct | reached
        if _source_of(G, n) and _normalise(_source_of(G, n)) != _normalise(file_path)
    }
    result.affected_files = sorted(files)

    # Share of the project that would notice, direct and transitive alike.
    total_nodes = G.number_of_nodes() or 1
    result.impact_score = round(len(direct | reached) / total_nodes, 4)
    return result


def affected_subgraph(G: nx.DiGraph, result: ImpactResult) -> nx.DiGraph:
    """The file's nodes plus everything that depends on them."""
    keep = set(result.nodes_in_file) | set(result.direct_dependents) | set(
        result.transitive_dependents)
    return G.subgraph(keep).copy()


def _file_counts(G: nx.DiGraph, dependents: list[str]) -> list[tuple[str, int]]:
    counts = Counter(_source_of(G, n) for n in dependents if _source_of(G, n))
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _recommendation(result: ImpactResult) -> str:
    total = len(result.direct_dependents)
    if result.risk_level == "HIGH":
        return (f"Add integration tests before editing. {total} nodes depend on "
                f"functions in this file.")
    if result.risk_level == "MEDIUM":
        return (f"Check the {total} dependent{'s' if total != 1 else ''} before "
                "changing any signature.")
    if not result.nodes_in_file:
        return "Nothing in the graph comes from this file — nothing to break."
    return "Safe to edit — nothing outside this file depends on it."


def _truncate(path: str, width: int = 34) -> str:
    return path if len(path) <= width else "…" + path[-(width - 1):]


def format_impact(result: ImpactResult, G: nx.DiGraph, width: int = 69) -> str:
    """Render an ImpactResult as the report shown in the terminal."""
    from slurp.formatter import _box

    if not result.nodes_in_file:
        return (f"No nodes found for {result.file_path}.\n"
                "Check the path, or re-index if the file is new.")

    count = len(result.nodes_in_file)
    subtitle = (
        f"{count} definition{'s' if count != 1 else ''} · "
        f"RISK: {result.risk_level} · affects {len(result.affected_files)} "
        f"file{'s' if len(result.affected_files) != 1 else ''}"
    )
    lines = [_box(f"Impact Analysis — {result.file_path}", subtitle), ""]

    if result.highest_risk_node and result.dependents_per_node.get(
            result.highest_risk_node):
        top = result.highest_risk_node
        dependants = result.dependents_per_node[top]
        share = round(dependants / (G.number_of_nodes() or 1) * 100, 1)
        lines.append("HIGHEST RISK NODE")
        lines.append(f"  {_display(G, top)} — {dependants} direct "
                     f"dependent{'s' if dependants != 1 else ''} "
                     f"({share}% of codebase)")
        lines.append("")

    counts = _file_counts(G, result.direct_dependents)
    if counts:
        lines.append(f"AFFECTED FILES (top {min(_TOP_FILES, len(counts))})")
        pad = max(len(_truncate(f)) for f, _ in counts[:_TOP_FILES])
        for path, n in counts[:_TOP_FILES]:
            lines.append(f"  {_truncate(path).ljust(pad)}  {n} "
                         f"dependent{'s' if n != 1 else ''}")
        if len(counts) > _TOP_FILES:
            lines.append(f"  … and {len(counts) - _TOP_FILES} more files")
        lines.append("")

    if result.safe_nodes:
        shown = [_display(G, n) for n in result.safe_nodes[:_SAFE_LISTED]]
        extra = len(result.safe_nodes) - len(shown)
        lines.append("SAFE TO EDIT")
        lines.append("  " + ", ".join(shown)
                     + (f", +{extra} more" if extra > 0 else "")
                     + "  — 0 dependents")
        lines.append("")

    lines.append("RECOMMENDATION")
    lines.append(f"  {_recommendation(result)}")
    lines.append("")
    lines.append("─" * width)
    lines.append(
        f"Direct: {len(result.direct_dependents)} · "
        f"Transitive: {len(result.transitive_dependents)} · "
        f"Impact score: {result.impact_score}"
    )
    return "\n".join(lines)


def impact_to_dict(result: ImpactResult) -> dict:
    """JSON-serialisable form, for CI pipelines that gate on the risk level."""
    return {
        "file_path": result.file_path,
        "risk_level": result.risk_level,
        "impact_score": result.impact_score,
        "nodes_in_file": result.nodes_in_file,
        "direct_dependents": result.direct_dependents,
        "transitive_dependents": result.transitive_dependents,
        "highest_risk_node": result.highest_risk_node,
        "affected_files": result.affected_files,
        "safe_nodes": result.safe_nodes,
        "dependents_per_node": result.dependents_per_node,
    }
