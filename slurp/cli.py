"""CLI entry point for Slurp."""

from pathlib import Path

import click

from slurp import __version__
from slurp.audit import log_query, read_audit, top_nodes_from_audit
from slurp.budget import select_subgraph
from slurp.formatter import format_subgraph
from slurp.loader import SlurpLoadError, load_graph
from slurp.scorer import score_nodes

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
              help="Show node scores in output.")
@click.option("--no-audit", "no_audit", is_flag=True, default=False,
              help="Skip writing to audit log.")
@click.option("--neighbor-decay", "neighbor_decay", default=0.7,
              show_default=True, help="Score decay factor for neighbor nodes.")
def run(
    query: str,
    graph: str | None,
    budget: int,
    fmt: str,
    model: str,
    explain: bool,
    no_audit: bool,
    neighbor_decay: float,
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

    scores = score_nodes(G, query)
    subG, stats = select_subgraph(
        G, scores, budget=budget, model=model, neighbor_decay=neighbor_decay
    )

    output_scores = scores if explain else None
    result = format_subgraph(subG, stats, format=fmt, scores=output_scores, query=query)
    click.echo(result)

    if not no_audit:
        top = sorted(subG.nodes, key=lambda n: scores.get(n, 0.0), reverse=True)
        log_query(
            query=query,
            graph_path=graph_path,
            stats=stats,
            top_nodes=top,
        )


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

    click.echo(f"Total queries: {len(entries)}")
    click.echo("")

    top = top_nodes_from_audit(audit_path, n=top_n)
    if top:
        click.echo(f"Top {top_n} nodes:")
        for node, count in top:
            click.echo(f"  {node}: {count}")
