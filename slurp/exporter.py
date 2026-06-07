"""Exports a subgraph context block in AI-specific prompt formats."""

from __future__ import annotations

import networkx as nx

from slurp.formatter import format_subgraph


def _strip_box(body: str) -> str:
    """Remove the Unicode box header, keeping everything from '## Relevant Nodes' on."""
    idx = body.find("## Relevant Nodes")
    return body[idx:] if idx != -1 else body


def _meta_line(stats: dict, query: str) -> str:
    return (
        f'query: "{query}" | '
        f'nodes: {stats["nodes_selected"]}/{stats["nodes_total"]} | '
        f'tokens: {stats["tokens_used"]:,}/{stats["tokens_budget"]:,} | '
        f'coverage: {stats["coverage_pct"]}%'
    )


def export_context(
    subG: nx.DiGraph,
    stats: dict,
    query: str,
    format: str = "claude",
    scores: dict[str, float] | None = None,
) -> str:
    """Build a ready-to-use context block in the target AI format.

    Args:
        subG:   Subgraph produced by budget.select_subgraph.
        stats:  Stats dict from budget.select_subgraph.
        query:  Original query string.
        format: Target format — 'claude', 'chatgpt', or 'claudemd'.
        scores: Optional {node_id: score} for sorting and display.

    Returns:
        Formatted context string ready to paste into a system prompt or CLAUDE.md.

    Raises:
        ValueError: If format is not 'claude', 'chatgpt', or 'claudemd'.
    """
    body = format_subgraph(subG, stats, format="markdown", scores=scores, query=query)
    meta = _meta_line(stats, query)

    if format == "claude":
        attrs = (
            f'source="slurp-graph" '
            f'query="{query}" '
            f'nodes="{stats["nodes_selected"]}/{stats["nodes_total"]}" '
            f'tokens="{stats["tokens_used"]}/{stats["tokens_budget"]}" '
            f'coverage="{stats["coverage_pct"]}%"'
        )
        return f"<context {attrs}>\n\n{body}\n\n</context>"

    if format == "chatgpt":
        clean = _strip_box(body)
        return (
            f"[CODEBASE CONTEXT — slurp-graph]\n"
            f"{meta}\n\n"
            f"{clean}\n\n"
            f"[END CODEBASE CONTEXT]"
        )

    if format == "claudemd":
        clean = _strip_box(body)
        return (
            f"## Codebase Context\n\n"
            f"<!-- slurp-graph · {meta} -->\n\n"
            f"{clean}"
        )

    raise ValueError(
        f"Unknown export format {format!r}. Expected 'claude', 'chatgpt', or 'claudemd'."
    )
