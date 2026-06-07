"""CLI entry point for Slurp."""

import tempfile
import webbrowser
from io import StringIO
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from slurp import __version__
from slurp.audit import log_query, read_audit, top_nodes_from_audit
from slurp.budget import select_subgraph
from slurp.formatter import format_subgraph
from slurp.ignore import apply_ignore, load_ignore
from slurp.loader import SlurpLoadError, load_graph
from slurp.scorer import score_nodes, score_nodes_detailed

_GRAPH_SEARCH_PATHS = [
    Path("graph.json"),
    Path("graphify-out/graph.json"),
    Path(".graphify/graph.json"),
]


def _find_graph() -> Path | None:
    for candidate in _GRAPH_SEARCH_PATHS:
        if candidate.exists():
            return candidate
    return None


def _echo_rich(renderable) -> None:
    """Render a rich renderable and echo via click.echo (always CliRunner-safe)."""
    buf = StringIO()
    Console(file=buf, no_color=True, width=100).print(renderable)
    click.echo(buf.getvalue(), nl=False)


class _SlurpGroup(click.Group):
    """Routes unknown first args to the hidden 'run' subcommand."""

    def resolve_command(
        self, ctx: click.Context, args: list
    ) -> tuple:
        try:
            cmd_name, cmd, remaining = super().resolve_command(ctx, args)
        except click.UsageError:
            cmd_name, cmd, remaining = None, None, list(args)
        if cmd is None and args:
            run_cmd = self.get_command(ctx, "run")
            if run_cmd is not None:
                return "run", run_cmd, list(args)
        return cmd_name, cmd, remaining


@click.group(cls=_SlurpGroup, invoke_without_command=True)
@click.version_option(__version__, prog_name="slurp")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Slurp — token-budget-aware graph navigation for AI coding agents.

    Select an optimal subgraph for your LLM within a token budget.

    Run a query:  slurp QUERY [--graph PATH] [--budget N] [--format FMT]
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command("run", hidden=True)
@click.argument("query")
@click.option("--graph", "-g", type=click.Path(), default=None,
              help="Path to graph.json (auto-discovered if omitted).")
@click.option("--budget", "-b", default=4000, show_default=True,
              help="Token budget for subgraph selection.")
@click.option("--format", "-f", "fmt",
              type=click.Choice(["markdown", "json", "yaml"]),
              default="markdown", show_default=True, help="Output format.")
@click.option("--model", "-m", default="cl100k_base", show_default=True,
              help="Tiktoken encoding model for token counting.")
@click.option("--explain", is_flag=True, default=False,
              help="Print a score breakdown table after the formatted output.")
@click.option("--no-audit", "no_audit", is_flag=True, default=False,
              help="Skip writing to audit log.")
@click.option("--neighbor-decay", "neighbor_decay", default=0.7,
              show_default=True, help="Score decay factor for neighbor nodes.")
@click.option("--min-score", "min_score", default=0.15, show_default=True,
              help="Minimum relevance score threshold; nodes below this are excluded.")
@click.option("--viz", is_flag=True, default=False,
              help="Open an interactive graph visualisation in the browser.")
@click.option("--ignore-file", "ignore_file", default=".slurpignore", show_default=True,
              help="Path to .slurpignore file for excluding nodes.")
@click.option("--backend", default="tfidf", show_default=True,
              type=click.Choice(["tfidf", "openai", "anthropic"]),
              help="Scoring backend. 'openai'/'anthropic' use real embeddings.")
def run(
    query: str,
    graph: str | None,
    budget: int,
    fmt: str,
    model: str,
    explain: bool,
    no_audit: bool,
    neighbor_decay: float,
    min_score: float,
    viz: bool,
    ignore_file: str,
    backend: str,
) -> None:
    """Run a query against the graph."""
    graph_path = Path(graph) if graph else _find_graph()
    if graph_path is None:
        raise click.ClickException(
            "No graph.json found. Pass --graph or run from a directory with graph.json."
        )

    try:
        G = load_graph(graph_path)
    except SlurpLoadError as exc:
        raise click.ClickException(str(exc)) from exc

    G = apply_ignore(G, load_ignore(Path(ignore_file)))

    if explain:
        scores, structural, semantic = score_nodes_detailed(G, query, backend=backend)
    else:
        scores = score_nodes(G, query, backend=backend)
        structural = semantic = {}

    subG, stats = select_subgraph(
        G, scores, budget=budget, model=model,
        neighbor_decay=neighbor_decay, min_score=min_score,
    )

    result = format_subgraph(subG, stats, format=fmt, query=query)
    click.echo(result)

    if not no_audit:
        top = sorted(subG.nodes, key=lambda n: scores.get(n, 0.0), reverse=True)
        log_query(
            query=query,
            graph_path=graph_path,
            stats=stats,
            top_nodes=top,
        )

    if explain:
        _echo_rich(_build_explain_table(subG, scores, structural, semantic))

    if viz:
        from slurp.viz import build_html
        html = build_html(subG, stats, scores, query)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        ) as f:
            f.write(html)
            viz_path = Path(f.name)
        webbrowser.open(viz_path.as_uri())


def _build_explain_table(
    subG,
    scores: dict[str, float],
    structural: dict[str, float],
    semantic: dict[str, float],
) -> Table:
    """Build a rich Table with per-node score breakdowns for --explain."""
    table = Table(title="Score Breakdown", show_header=True, header_style="bold")
    table.add_column("Rank", style="dim", width=5)
    table.add_column("Label")
    table.add_column("Final", justify="right")
    table.add_column("Structural", justify="right")
    table.add_column("Semantic", justify="right")
    table.add_column("Type", style="dim")
    table.add_column("File", style="dim")

    for rank, nid in enumerate(
        sorted(subG.nodes, key=lambda n: scores.get(n, 0.0), reverse=True), 1
    ):
        attrs = subG.nodes[nid]
        table.add_row(
            str(rank),
            attrs.get("label", nid),
            f"{scores.get(nid, 0.0):.4f}",
            f"{structural.get(nid, 0.0):.4f}",
            f"{semantic.get(nid, 0.0):.4f}",
            attrs.get("type") or attrs.get("file_type") or "",
            attrs.get("file_path") or attrs.get("source_file") or "",
        )

    return table


@cli.command()
@click.option("--graph", "-g", type=click.Path(), default=None,
              help="Path to graph.json (auto-discovered if omitted).")
def stats(graph: str | None) -> None:
    """Show statistics for a graph file."""
    graph_path = Path(graph) if graph else _find_graph()
    if graph_path is None:
        raise click.ClickException(
            "No graph.json found. Pass --graph or run from a directory with graph.json."
        )

    try:
        G = load_graph(graph_path)
    except SlurpLoadError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Graph: {graph_path}")
    click.echo(f"Nodes: {G.number_of_nodes()}")
    click.echo(f"Edges: {G.number_of_edges()}")


@cli.command("diff")
@click.argument("old_graph", type=click.Path(exists=True))
@click.argument("new_graph", type=click.Path(exists=True))
@click.option("--hops", default=2, show_default=True,
              help="Depth of impact neighborhood expansion.")
@click.option("--viz", is_flag=True, default=False,
              help="Open interactive visualiser of the affected area.")
@click.option("--budget", "-b", default=None, type=int,
              help="Token budget; selects most relevant nodes from affected area.")
def diff_cmd(
    old_graph: str,
    new_graph: str,
    hops: int,
    viz: bool,
    budget: int | None,
) -> None:
    """Compare two graph.json files and show the impact of changes."""
    from slurp.diff import (
        affected_subgraph,
        build_diff_viz_graph,
        diff_graphs,
        format_diff,
    )

    try:
        G_old = load_graph(Path(old_graph))
        G_new = load_graph(Path(new_graph))
    except SlurpLoadError as exc:
        raise click.ClickException(str(exc)) from exc

    diff = diff_graphs(G_old, G_new)
    click.echo(format_diff(diff, G_new))

    if budget is not None:
        sub = affected_subgraph(G_new, diff, hops)
        if sub.number_of_nodes() > 0:
            query_terms = [
                G_new.nodes[n].get("label", n)
                for n in (diff.added_nodes + diff.modified_nodes)[:5]
                if n in G_new
            ]
            query = " ".join(query_terms) or "diff"
            scores = score_nodes(sub, query)
            subG, stats = select_subgraph(sub, scores, budget=budget)
            click.echo("\n## Token-Budgeted Affected Subgraph\n")
            click.echo(format_subgraph(subG, stats, query=query))

    if viz:
        viz_G, viz_scores, viz_stats = build_diff_viz_graph(G_old, G_new, diff, hops)
        from slurp.viz import build_html
        query = (
            f"diff +{len(diff.added_nodes)} "
            f"-{len(diff.removed_nodes)} "
            f"~{len(diff.modified_nodes)}"
        )
        html = build_html(viz_G, viz_stats, viz_scores, query)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        ) as f:
            f.write(html)
            viz_path = Path(f.name)
        webbrowser.open(viz_path.as_uri())


@cli.command("export")
@click.argument("query")
@click.option("--graph", "-g", type=click.Path(), default=None,
              help="Path to graph.json (auto-discovered if omitted).")
@click.option("--budget", "-b", default=4000, show_default=True,
              help="Token budget for subgraph selection.")
@click.option("--format", "-f", "fmt",
              type=click.Choice(["claude", "chatgpt", "claudemd"]),
              default="claude", show_default=True,
              help="Target AI format.")
@click.option("--output", "-o", type=click.Path(), default=None,
              help="Write output to file instead of stdout.")
@click.option("--model", "-m", default="cl100k_base", show_default=True,
              help="Tiktoken encoding model for token counting.")
@click.option("--min-score", "min_score", default=0.15, show_default=True,
              help="Minimum relevance score threshold.")
@click.option("--neighbor-decay", "neighbor_decay", default=0.7, show_default=True,
              help="Score decay factor for neighbor nodes.")
@click.option("--ignore-file", "ignore_file", default=".slurpignore", show_default=True,
              help="Path to .slurpignore file for excluding nodes.")
@click.option("--backend", default="tfidf", show_default=True,
              type=click.Choice(["tfidf", "openai", "anthropic"]),
              help="Scoring backend. 'openai'/'anthropic' use real embeddings.")
def export_cmd(
    query: str,
    graph: str | None,
    budget: int,
    fmt: str,
    output: str | None,
    model: str,
    min_score: float,
    neighbor_decay: float,
    ignore_file: str,
    backend: str,
) -> None:
    """Export a context block ready to paste into a Claude/ChatGPT/CLAUDE.md system prompt."""
    from slurp.exporter import export_context

    graph_path = Path(graph) if graph else _find_graph()
    if graph_path is None:
        raise click.ClickException(
            "No graph.json found. Pass --graph or run from a directory with graph.json."
        )

    try:
        G = load_graph(graph_path)
    except SlurpLoadError as exc:
        raise click.ClickException(str(exc)) from exc

    G = apply_ignore(G, load_ignore(Path(ignore_file)))
    scores = score_nodes(G, query, backend=backend)
    subG, stats = select_subgraph(
        G, scores, budget=budget, model=model,
        neighbor_decay=neighbor_decay, min_score=min_score,
    )

    result = export_context(subG, stats, query, format=fmt, scores=scores)

    if output:
        out_path = Path(output)
        out_path.write_text(result, encoding="utf-8")
        click.echo(f"Exported to {out_path} ({len(result):,} chars)")
    else:
        click.echo(result)


@cli.command("benchmark")
@click.option("--graph", "-g", type=click.Path(), default=None,
              help="Path to graph.json (auto-discovered if omitted).")
@click.option("--queries", "-q", multiple=True, required=True,
              help="Query strings to benchmark (repeatable).")
@click.option("--budget", "budgets", multiple=True, type=int,
              default=(2000, 4000, 8000), show_default=True,
              help="Token budgets to test (repeatable).")
@click.option("--model", "-m", default="cl100k_base", show_default=True,
              help="Tiktoken encoding model for token counting.")
@click.option("--output", "-o", type=click.Path(), default=None,
              help="Save raw results to JSON file.")
@click.option("--min-score", "min_score", default=0.0, show_default=True,
              help="Minimum score filter (0.0 = include all nodes).")
@click.option("--backend", default="tfidf", show_default=True,
              type=click.Choice(["tfidf", "openai", "anthropic"]),
              help="Scoring backend. 'openai'/'anthropic' use real embeddings.")
def benchmark_cmd(
    graph: str | None,
    queries: tuple[str, ...],
    budgets: tuple[int, ...],
    model: str,
    output: str | None,
    min_score: float,
    backend: str,
) -> None:
    """Measure token savings of slurp vs injecting the full graph."""
    from slurp.benchmark import format_benchmark, run_benchmark

    graph_path = Path(graph) if graph else _find_graph()
    if graph_path is None:
        raise click.ClickException(
            "No graph.json found. Pass --graph or run from a directory with graph.json."
        )

    try:
        G = load_graph(graph_path)
    except SlurpLoadError as exc:
        raise click.ClickException(str(exc)) from exc

    result = run_benchmark(
        G,
        queries=list(queries),
        budgets=list(budgets),
        model=model,
        min_score=min_score,
        backend=backend,
    )

    click.echo(format_benchmark(result))

    if output:
        import json as _json
        out_path = Path(output)
        out_path.write_text(_json.dumps(result.to_dict(), indent=2), encoding="utf-8")
        click.echo(f"Results saved to {out_path}")


@cli.command()
@click.option("--graph", "-g", type=click.Path(), default=None,
              help="Path to graph.json (auto-discovered if omitted).")
def serve(graph: str | None) -> None:
    """Start an MCP stdio server exposing the slurp_query tool."""
    graph_path = Path(graph) if graph else _find_graph()
    if graph_path is None:
        raise click.ClickException(
            "No graph.json found. Pass --graph or run from a directory with graph.json."
        )
    import slurp.mcp as _mcp
    _mcp.serve(graph_path)


@cli.command()
@click.option("--top-nodes", "top_n", default=10, show_default=True,
              help="Show N most frequently selected nodes.")
@click.option("--audit-dir", default=".slurp", show_default=True,
              help="Directory containing audit.jsonl.")
def audit(top_n: int, audit_dir: str) -> None:
    """Show query history from the audit log."""
    audit_path = Path(audit_dir)
    entries = read_audit(audit_path)

    if not entries:
        click.echo("No audit entries found.")
        return

    # --- Queries table (last 20) ---
    recent = entries[-20:]
    queries_table = Table(
        title=f"Query History (last {len(recent)} of {len(entries)})",
        show_header=True,
    )
    queries_table.add_column("#", style="dim", width=4)
    queries_table.add_column("Timestamp")
    queries_table.add_column("Query")
    queries_table.add_column("Nodes", justify="right")
    queries_table.add_column("Tokens", justify="right")
    queries_table.add_column("Savings", justify="right")

    for i, entry in enumerate(recent, 1):
        baseline = entry.get("nodes_total", 0) * 50
        used = entry.get("tokens_used", 0)
        savings_pct = round(max(0, baseline - used) / baseline * 100) if baseline > 0 else 0
        queries_table.add_row(
            str(i),
            entry.get("timestamp", ""),
            entry.get("query", ""),
            str(entry.get("nodes_selected", "")),
            str(used),
            f"{savings_pct}%",
        )

    _echo_rich(queries_table)

    # --- Top nodes ranking ---
    top = top_nodes_from_audit(audit_path, n=top_n)
    if top:
        nodes_table = Table(title=f"Top {top_n} Most Selected Nodes", show_header=True)
        nodes_table.add_column("Rank", style="dim", width=5)
        nodes_table.add_column("Node")
        nodes_table.add_column("Times Selected", justify="right")
        for rank, (node, count) in enumerate(top, 1):
            nodes_table.add_row(str(rank), node, str(count))
        _echo_rich(nodes_table)
