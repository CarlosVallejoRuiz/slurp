"""Serializes a subgraph to structured text for LLM prompt injection."""

import json as _json

import networkx as nx


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


def _box(title: str, subtitle: str) -> str:
    """Renders a Unicode border box with a title line and a subtitle line.

    Example:
        ╭─ Slurp — (budget: 100 tokens) ─╮
        │ Selected 3/7 nodes · 58/100 ... │
        ╰──────────────────────────────────╯
    """
    # total_w must accommodate both content lines with their border decoration.
    #   title line:    "╭─ " + title + " " + "─" * fill + "╮"  = len(title) + 5 + fill
    #   subtitle line: "│ " + subtitle + spaces + " │"          = len(subtitle) + 4 + spaces
    total_w = max(len(title) + 5, len(subtitle) + 4)
    title_fill = total_w - len(title) - 5
    sub_spaces = total_w - len(subtitle) - 4

    top = f"╭─ {title} {'─' * title_fill}╮"
    mid = f"│ {subtitle}{' ' * sub_spaces} │"
    bot = "╰" + "─" * (total_w - 2) + "╯"
    return "\n".join([top, mid, bot])


def _sorted_nodes(G: nx.DiGraph, scores: dict[str, float] | None) -> list[str]:
    """Returns node IDs sorted by score descending, then alphabetically for ties."""
    return sorted(
        G.nodes,
        key=lambda n: (-(scores.get(n, 0.0) if scores else 0.0), str(n)),
    )


def _build_data(G: nx.DiGraph, stats: dict, scores: dict[str, float] | None) -> dict:
    """Builds the canonical dict shared by JSON and YAML formatters."""
    nodes = []
    for nid in _sorted_nodes(G, scores):
        attrs = G.nodes[nid]
        node: dict = {"id": nid}
        for field in ("label", "type", "description", "importance", "file_path"):
            if (val := attrs.get(field)) is not None:
                node[field] = val
        if scores and nid in scores:
            node["score"] = round(scores[nid], 4)
        nodes.append(node)

    edges = []
    for u, v in G.edges:
        edge_attrs = G.edges[u, v]
        edge: dict = {"source": u, "target": v}
        if rel := edge_attrs.get("relation"):
            edge["relation"] = rel
        if (w := edge_attrs.get("weight")) is not None:
            edge["weight"] = w
        edges.append(edge)

    return {"stats": stats, "nodes": nodes, "edges": edges}


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

    # Relevant nodes -----------------------------------------------------
    lines.append("## Relevant Nodes")
    lines.append("")

    for nid in _sorted_nodes(G, scores):
        attrs = G.nodes[nid]
        label = attrs.get("label", nid)
        ntype = attrs.get("type", "")
        desc = attrs.get("description", "")
        fpath = attrs.get("file_path", "")

        heading = f"### {label}"
        if ntype:
            heading += f" ({ntype})"
        if scores and nid in scores:
            heading += f" · score: {scores[nid]:.2f}"
        lines.append(heading)

        if desc:
            lines.append(desc)
        if fpath:
            lines.append(f"→ File: {fpath}")
        lines.append("")

    # Key relationships --------------------------------------------------
    if G.number_of_edges() > 0:
        lines.append("## Key Relationships")
        for u, v in G.edges:
            rel = G.edges[u, v].get("relation", "→")
            lines.append(f"- {u} → {rel} → {v}")
        lines.append("")

    # Tip ----------------------------------------------------------------
    excluded = stats["nodes_total"] - stats["nodes_selected"]
    if excluded > 0:
        lines.append("---")
        lines.append(
            f"💡 {excluded} additional connected nodes available"
            " — increase --budget to include them"
        )

    return "\n".join(lines)


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
        first = True
        for k, v in node.items():
            prefix = "  - " if first else "    "
            lines.append(f"{prefix}{k}: {_yaml_scalar(v)}")
            first = False

    lines.append("edges:")
    if data["edges"]:
        for edge in data["edges"]:
            first = True
            for k, v in edge.items():
                prefix = "  - " if first else "    "
                lines.append(f"{prefix}{k}: {_yaml_scalar(v)}")
                first = False
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
