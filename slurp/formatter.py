"""Serializes a subgraph to structured text for LLM prompt injection."""

import json as _json

import networkx as nx

from slurp._graphutils import _box


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _yaml_scalar(v: object) -> str:
    """Renders a primitive value as a YAML scalar string."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    needs_quote = (
        not s
        or s[0] in "-0123456789 "
        or any(c in s for c in ":{},[]#&*?|>'\"%@`\n\r")
    )
    # JSON string literals are valid YAML scalars.
    return _json.dumps(s) if needs_quote else s


_LANG_MAP: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
}


def _file_lang(file_path: str) -> str:
    """Return a fenced-code-block language identifier from a file path's extension."""
    if not file_path:
        return ""
    dot = file_path.rfind(".")
    if dot < 0:
        return ""
    return _LANG_MAP.get(file_path[dot:].lower(), "")


def _sorted_nodes(G: nx.DiGraph, scores: dict[str, float] | None) -> list[str]:
    """Returns node IDs sorted by score descending, then alphabetically for ties."""
    return sorted(
        G.nodes,
        key=lambda n: (-(scores.get(n, 0.0) if scores else 0.0), str(n)),
    )


def _file_of(attrs: dict) -> str:
    """The node's file, whichever key the source graph used.

    graphify writes `source_file`, generic graphs `file_path`. Every renderer —
    and through them the budget — reads it here, so the two can never disagree.
    """
    return attrs.get("source_file") or attrs.get("file_path") or ""


def _node_record(G: nx.DiGraph, nid: str, scores: dict[str, float] | None) -> dict:
    """One node as the JSON and YAML formats write it."""
    attrs = G.nodes[nid]
    node: dict = {"id": nid}
    for field in ("label", "type", "description", "importance"):
        if (val := attrs.get(field)) is not None:
            node[field] = val
    if fpath := _file_of(attrs):
        node["file_path"] = fpath
    if scores and nid in scores:
        node["score"] = round(scores[nid], 4)
    return node


def _edge_record(G: nx.DiGraph, u: str, v: str) -> dict:
    """One edge as the JSON and YAML formats write it."""
    edge_attrs = G.edges[u, v]
    edge: dict = {"source": u, "target": v}
    if rel := edge_attrs.get("relation"):
        edge["relation"] = rel
    if (w := edge_attrs.get("weight")) is not None:
        edge["weight"] = w
    return edge


def _build_data(G: nx.DiGraph, stats: dict, scores: dict[str, float] | None) -> dict:
    """Builds the canonical dict shared by JSON and YAML formatters."""
    return {
        "stats": stats,
        "nodes": [_node_record(G, nid, scores) for nid in _sorted_nodes(G, scores)],
        "edges": [_edge_record(G, u, v) for u, v in G.edges],
    }


def _md_node_lines(G: nx.DiGraph, nid: str, scores: dict[str, float] | None) -> list[str]:
    """One node's block in the markdown format, trailing blank line included."""
    attrs = G.nodes[nid]
    label = attrs.get("label", nid)
    ntype = attrs.get("type", "")
    desc = attrs.get("description", "")
    fpath = _file_of(attrs)

    heading = f"### {label}"
    if ntype:
        heading += f" ({ntype})"
    if scores and nid in scores:
        heading += f" · score: {scores[nid]:.2f}"
    lines = [heading]
    if desc:
        lines.append(desc)
    if fpath:
        lines.append(f"→ File: {fpath}")
    code_block = attrs.get("code_block", "")
    if code_block:
        lines.append(f"```{_file_lang(fpath)}")
        lines.append(code_block)
        lines.append("```")
    lines.append("")
    return lines


def _md_edge_line(G: nx.DiGraph, u: str, v: str) -> str:
    rel = G.edges[u, v].get("relation", "→")
    return f"- {u} → {rel} → {v}"


def _yaml_entry(record: dict) -> list[str]:
    """One list entry (node or edge) in the YAML format."""
    return [f"{'  - ' if i == 0 else '    '}{k}: {_yaml_scalar(v)}"
            for i, (k, v) in enumerate(record.items())]


# A score with the most digits any real score is written with, so that an
# item's cost measured once with it is never below its cost with a real score.
_PLACEHOLDER_SCORE = 0.1234


def item_text(G: nx.DiGraph, item: tuple, format: str = "markdown") -> str:
    """The text one node or edge adds to the output in *format*.

    Used by the budget selector to cost items with exactly the renderers this
    module uses, so what is counted is what is written. A node is costed with a
    placeholder score of the widest width, which keeps the cost independent of
    the query and therefore cacheable.

    Args:
        G: The graph holding the item.
        item: ("n", node_id) or ("e", source, target).
        format: 'markdown', 'json' or 'yaml'.

    Returns:
        The item's text, including its line break or list separator.
    """
    if item[0] == "n":
        nid = item[1]
        scores = {nid: _PLACEHOLDER_SCORE}
        if format == "markdown":
            return "\n".join(_md_node_lines(G, nid, scores)) + "\n"
        record = _node_record(G, nid, scores)
    else:
        if format == "markdown":
            return _md_edge_line(G, item[1], item[2]) + "\n"
        record = _edge_record(G, item[1], item[2])
    if format == "json":
        body = _json.dumps(record, indent=2)
        return "\n".join("    " + line for line in body.splitlines()) + ",\n"
    return "\n".join(_yaml_entry(record)) + "\n"


# ---------------------------------------------------------------------------
# Per-format renderers
# ---------------------------------------------------------------------------


def _format_markdown(
    G: nx.DiGraph,
    stats: dict,
    scores: dict[str, float] | None,
    query: str,
) -> str:
    lines: list[str] = []

    # Header box ---------------------------------------------------------
    budget_str = f'(budget: {stats["tokens_budget"]:,} tokens)'
    if query:
        title = f'Slurp — Subgraph for: "{query}" {budget_str}'
    else:
        title = f"Slurp — {budget_str}"

    subtitle = (
        f'Selected {stats["nodes_selected"]}/{stats["nodes_total"]} nodes'
        f' · {stats["tokens_used"]:,}/{stats["tokens_budget"]:,} tokens used'
        f' ({stats["coverage_pct"]}%)'
    )
    lines.append(_box(title, subtitle))
    lines.append("")

    # The budget could not hold a single node: say so instead of an empty list.
    if warning := stats.get("warning"):
        lines.append(f"⚠️ {warning}")
        return "\n".join(lines)

    # Relevant nodes -----------------------------------------------------
    lines.append("## Relevant Nodes")
    lines.append("")

    for nid in _sorted_nodes(G, scores):
        lines.extend(_md_node_lines(G, nid, scores))

    # Key relationships --------------------------------------------------
    if G.number_of_edges() > 0:
        lines.append("## Key Relationships")
        for u, v in G.edges:
            lines.append(_md_edge_line(G, u, v))
        lines.append("")

    # Tip ----------------------------------------------------------------
    if tips := _left_out_tips(stats):
        lines.append("---")
        lines.extend(tips)

    return "\n".join(lines)


def _nodes(n: int) -> str:
    return "node" if n == 1 else "nodes"


def _left_out_tips(stats: dict) -> list[str]:
    """Why nodes of the graph are missing from this subgraph, one line per cause.

    DECISION: a node that did not fit comes in with a bigger budget; a node
    below min_score never does, so the two are never added up. Neither is
    called "connected": nothing here knows whether they touch the selection.
    Stats built by hand, without the breakdown build_context adds, get a bare
    count that claims no cause.
    """
    excluded = stats["nodes_total"] - stats["nodes_selected"]
    if excluded <= 0:
        return []
    if "nodes_over_budget" not in stats:
        return [f"💡 {excluded} more {_nodes(excluded)} in the graph "
                f"{'is' if excluded == 1 else 'are'} not in this subgraph"]
    tips = []
    threshold = f"{stats.get('min_score', 0):g}"
    if over := stats["nodes_over_budget"]:
        tips.append(f"💡 {over} more {_nodes(over)} scored {threshold} or more but did not "
                    "fit the budget — increase --budget to include them")
    if below := stats["nodes_below_min_score"]:
        tips.append(f"💡 {below} {_nodes(below)} scored below the minimum score {threshold} "
                    "— a bigger budget will not include them (see --min-score)")
    if kinds := _left_out_kinds_tip(stats):
        tips.append(kinds)
    return tips


def _left_out_kinds_tip(stats: dict) -> str:
    """The tests and imports that did not compete, and the flags that let them in."""
    tests = stats.get("nodes_excluded_tests", 0)
    imports = stats.get("nodes_excluded_imports", 0)
    if not tests and not imports:
        return ""
    parts, flags = [], []
    if tests:
        parts.append(f"{tests} test {_nodes(tests)}")
        flags.append("--include-tests")
    if imports:
        parts.append(f"{imports} import {_nodes(imports)}")
        flags.append("--include-imports")
    one = tests + imports == 1
    return (f"💡 {' and '.join(parts)} {'is' if one else 'are'} left out by default — "
            f"{' / '.join(flags)} to let {'it' if one else 'them'} compete")


def _format_json(
    G: nx.DiGraph,
    stats: dict,
    scores: dict[str, float] | None,
) -> str:
    return _json.dumps(_build_data(G, stats, scores), indent=2)


def _format_yaml(
    G: nx.DiGraph,
    stats: dict,
    scores: dict[str, float] | None,
) -> str:
    data = _build_data(G, stats, scores)
    lines: list[str] = []

    lines.append("stats:")
    for k, v in data["stats"].items():
        lines.append(f"  {k}: {_yaml_scalar(v)}")

    lines.append("nodes:")
    for node in data["nodes"]:
        lines.extend(_yaml_entry(node))

    lines.append("edges:")
    if data["edges"]:
        for edge in data["edges"]:
            lines.extend(_yaml_entry(edge))
    else:
        lines.append("  []")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def format_subgraph(
    G: nx.DiGraph,
    stats: dict,
    format: str = "markdown",
    scores: dict[str, float] | None = None,
    query: str = "",
) -> str:
    """Serializes a subgraph to a structured string for LLM prompt injection.

    Args:
        G: Subgraph produced by budget.select_subgraph.
        stats: Stats dict from budget.select_subgraph.
        format: Output format — 'markdown', 'json', or 'yaml'.
        scores: Optional {node_id: score} from scorer.score_nodes.
            Nodes are sorted by score descending and scores are displayed.
        query: Original query string, shown in the markdown header.

    Returns:
        Formatted string ready to inject into an LLM prompt.

    Raises:
        ValueError: If format is not 'markdown', 'json', or 'yaml'.
    """
    if format == "markdown":
        return _format_markdown(G, stats, scores, query)
    if format == "json":
        return _format_json(G, stats, scores)
    if format == "yaml":
        return _format_yaml(G, stats, scores)
    raise ValueError(
        f"Unknown format {format!r}. Expected 'markdown', 'json', or 'yaml'."
    )
