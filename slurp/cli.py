"""CLI entry point for Slurp."""

import tempfile
import webbrowser
from io import StringIO
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from slurp import __version__
from slurp.audit import log_query, read_audit, top_nodes_from_audit
# Imported at module level because the --price-model choices are built at decoration time.
from slurp.benchmark import MODEL_PRICES_PER_MTok
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

_SLURP_ASCII = r""" _____ _
/ ____| |
| (___ | |_   _ _ __ _ __
 \___ \| | | | | '__| '_ \
 ____) | | |_| | |  | |_) |
|_____/|_|\__,_|_|  | .__/
                     | |
                     |_|    🍜"""


def _version_callback(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    """Print the ASCII banner and exit."""
    if not value or ctx.resilient_parsing:
        return
    import sys
    from rich.text import Text

    is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    buf = StringIO()
    con = Console(file=buf, no_color=not is_tty, width=80, highlight=False)
    con.print(Text(_SLURP_ASCII, style="bold orange1"))
    con.print(f"\nslurp — token-budget graph navigation — v{__version__}")
    click.echo(buf.getvalue(), nl=False)
    ctx.exit()


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


def _deliver_viz(html: str, viz: bool, viz_output: str | None) -> None:
    """Save and/or open a rendered visualisation.

    DECISION: when both --viz and --viz-output are given, the browser opens the
    saved file rather than a temp copy, so what the user sees is the artifact
    they can share.

    Args:
        html: Rendered HTML document.
        viz: Whether to open the result in a browser.
        viz_output: Path to write the HTML to, or None for a temp file. Parent
            directories are created if missing.
    """
    if viz_output:
        out_path = Path(viz_output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")
        click.echo(f"✓ Visualization saved to {out_path}")
        target = out_path
    else:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        ) as f:
            f.write(html)
            target = Path(f.name)

    if viz:
        webbrowser.open(target.resolve().as_uri())


_LANGUAGE_BY_EXT: dict[str, str] = {
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".go": "Go",
    ".java": "Java",
    ".rs": "Rust",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".scala": "Scala",
    ".swift": "Swift",
}


def _detect_language(root: Path) -> tuple[str | None, dict[str, int]]:
    """Detect the dominant source language of a project.

    Counts indexable source files per language, reusing the indexer's own
    discovery rules so vendored trees (node_modules, .venv, ...) never skew
    the result.

    Args:
        root: Project root to scan.

    Returns:
        Tuple of (dominant language or None if no source files, counts by
        language). Ties break alphabetically for deterministic output.
    """
    from slurp.indexer import iter_source_files

    counts: dict[str, int] = {}
    for path in iter_source_files(root):
        lang = _LANGUAGE_BY_EXT.get(path.suffix.lower())
        if lang is not None:
            counts[lang] = counts.get(lang, 0) + 1

    if not counts:
        return None, counts
    dominant = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return dominant, counts


def _detect_slurp_command() -> tuple[str, str]:
    """Locate the slurp binary and identify how it was installed.

    DECISION: the resolved absolute path is what goes into .mcp.json. MCP
    clients spawn the server without a login shell, so a bare "slurp" only
    works if PATH happens to be inherited — an absolute path works for both
    `uv tool install` and `pip install` layouts.

    Returns:
        Tuple of (command to launch slurp, human-readable install kind).
    """
    import shutil
    import sys

    exe = shutil.which("slurp")
    if exe is None:
        # Running via `uv run slurp init` may leave the venv bin dir off PATH.
        candidate = Path(sys.executable).parent / "slurp"
        if candidate.exists():
            exe = str(candidate)
    if exe is None:
        return "slurp", "not found on PATH"

    resolved = Path(exe).resolve()
    parts = [p.lower() for p in resolved.parts]
    if "uv" in parts and "tools" in parts:
        kind = "uv tool install"
    elif (resolved.parent.parent / "pyvenv.cfg").exists():
        kind = "pip install (virtualenv)"
    else:
        kind = "pip install"
    return exe, kind


def _build_mcp_config(command: str, graph_path: Path, existing: dict | None) -> dict:
    """Build the .mcp.json payload, preserving any unrelated MCP servers.

    Args:
        command: Command that launches slurp.
        graph_path: Absolute path to graph.json.
        existing: Previously parsed .mcp.json contents, or None.

    Returns:
        The full config dict to write.
    """
    config = dict(existing) if existing else {}
    servers = dict(config.get("mcpServers") or {})
    servers["slurp"] = {
        "command": command,
        "args": ["serve", "--graph", str(graph_path)],
    }
    config["mcpServers"] = servers
    return config


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
@click.option(
    "--version", is_flag=True, is_eager=True, expose_value=False,
    callback=_version_callback, help="Show version and exit.",
)
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
@click.option("--viz-output", "viz_output", type=click.Path(), default=None,
              help="Write the visualisation HTML to PATH. Creates parent directories. "
                   "Does not open a browser unless --viz is also given.")
@click.option("--ignore-file", "ignore_file", default=".slurpignore", show_default=True,
              help="Path to .slurpignore file for excluding nodes.")
@click.option("--backend", default="tfidf", show_default=True,
              type=click.Choice(["tfidf", "openai", "anthropic"]),
              help="Scoring backend. 'openai'/'anthropic' use real embeddings.")
@click.option("--inject-code", "inject_code_flag", is_flag=True, default=False,
              help="Append source code blocks for each selected node (≤30 nodes required).")
@click.option("--project-root", "project_root", type=click.Path(), default=None,
              help="Root directory for resolving source_file paths (default: graph.json directory).")
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
    viz_output: str | None,
    ignore_file: str,
    backend: str,
    inject_code_flag: bool,
    project_root: str | None,
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

    if inject_code_flag:
        from slurp.injector import inject_code
        root = Path(project_root) if project_root else graph_path.parent
        if subG.number_of_nodes() > 30:
            click.echo(
                f"Warning: --inject-code requires ≤30 nodes, but subgraph has "
                f"{subG.number_of_nodes()}. Increase --min-score or lower --budget to reduce.",
                err=True,
            )
        subG = inject_code(subG, root)

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

    if viz or viz_output:
        from slurp.viz import build_html
        _deliver_viz(build_html(subG, stats, scores, query), viz, viz_output)


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
@click.option("--viz-output", "viz_output", type=click.Path(), default=None,
              help="Write the visualisation HTML to PATH. Creates parent directories. "
                   "Does not open a browser unless --viz is also given.")
@click.option("--budget", "-b", default=None, type=int,
              help="Token budget; selects most relevant nodes from affected area.")
def diff_cmd(
    old_graph: str,
    new_graph: str,
    hops: int,
    viz: bool,
    viz_output: str | None,
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
    click.echo(format_diff(diff, G_new, hops=hops))

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

    if viz or viz_output:
        viz_G, viz_scores, viz_stats = build_diff_viz_graph(G_old, G_new, diff, hops)
        from slurp.viz import build_html
        query = (
            f"diff +{len(diff.added_nodes)} "
            f"-{len(diff.removed_nodes)} "
            f"~{len(diff.modified_nodes)}"
        )
        _deliver_viz(build_html(viz_G, viz_stats, viz_scores, query), viz, viz_output)

    if diff.has_changes:
        _echo_rich(_build_diff_panel(diff, old_graph, new_graph, hops, suggest_viz=not viz))


def _build_diff_panel(
    diff,
    old_graph: str,
    new_graph: str,
    hops: int,
    suggest_viz: bool,
) -> Panel:
    """Summary panel shown after a diff report.

    Args:
        diff: GraphDiff produced by diff_graphs.
        old_graph: Path string of the old graph, for the suggested command.
        new_graph: Path string of the new graph, for the suggested command.
        hops: Neighborhood depth the report was run with.
        suggest_viz: Whether to append the --viz suggestion.
    """
    from slurp.diff import impact_headline

    if diff.impact_score > 0.7:
        colour, border = "bold red", "red"
    elif diff.impact_score > 0.3:
        colour, border = "bold yellow", "yellow"
    else:
        colour, border = "bold green", "green"

    body = [
        f"[{colour}]{impact_headline(diff.impact_score)}[/]  "
        f"[dim](impact score {diff.impact_score:.4f})[/]",
        "",
        f"[green]+{len(diff.added_nodes)} added[/]  "
        f"[red]-{len(diff.removed_nodes)} removed[/]  "
        f"[yellow]~{len(diff.modified_nodes)} modified[/]",
    ]

    if suggest_viz:
        body.append("")
        body.append("See the full blast radius:")
        hops_arg = f" --hops {hops}" if hops != 2 else ""
        body.append(f"  [cyan]slurp diff {old_graph} {new_graph}{hops_arg} --viz[/]")

    return Panel(
        "\n".join(body),
        title="Impact Summary",
        border_style=border,
        padding=(1, 2),
    )


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
@click.option("--price-model", "model_price", default="default", show_default=True,
              type=click.Choice(sorted(MODEL_PRICES_PER_MTok)),
              help="Model whose input-token price is used for cost estimates.")
def benchmark_cmd(
    graph: str | None,
    queries: tuple[str, ...],
    budgets: tuple[int, ...],
    model: str,
    output: str | None,
    min_score: float,
    backend: str,
    model_price: str,
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
        model_price=model_price,
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
@click.option("--log/--no-log", "log", default=True, show_default=True,
              help="Append served queries to .slurp/session.log (only if .slurp/ exists).")
def serve(graph: str | None, log: bool) -> None:
    """Start an MCP stdio server exposing the slurp_query tool."""
    graph_path = Path(graph) if graph else _find_graph()
    if graph_path is None:
        raise click.ClickException(
            "No graph.json found. Pass --graph or run from a directory with graph.json."
        )
    import slurp.mcp as _mcp
    _mcp.serve(graph_path, session_log=log)


@cli.command("session")
@click.option("--last", "last", default=20, show_default=True,
              help="Number of most recent entries to show.")
@click.option("--tail", is_flag=True, default=False,
              help="Follow the log in real time (Ctrl+C to stop).")
@click.option("--log-dir", "log_dir", default=".slurp", show_default=True,
              help="Directory containing session.log.")
def session_cmd(last: int, tail: bool, log_dir: str) -> None:
    """Show queries your AI assistant sent to the slurp MCP server."""
    import time

    from slurp.mcp import SESSION_LOG_FILE, read_session_log

    log_path = Path(log_dir) / SESSION_LOG_FILE
    entries = read_session_log(Path(log_dir), last=last)

    if not entries:
        if not Path(log_dir).is_dir():
            click.echo(
                f"No session log: {log_dir}/ does not exist.\n"
                "Run a query manually first — that creates it — then let your "
                "assistant call slurp."
            )
        else:
            click.echo(
                f"No entries yet in {log_path}.\n"
                "Start the MCP server with 'slurp serve' and let your assistant query it."
            )
        if not tail:
            return

    if entries:
        _echo_rich(_build_session_table(entries, log_path))

    if not tail:
        return

    # DECISION: seek to EOF rather than re-reading, so following a long log costs
    # nothing and never re-prints what the table above already showed.
    click.echo(f"\nFollowing {log_path} (Ctrl+C to stop)...")
    try:
        with log_path.open("r", encoding="utf-8") as fh:
            fh.seek(0, 2)
            while True:
                line = fh.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                click.echo(_format_session_line(line))
    except FileNotFoundError:
        raise click.ClickException(f"{log_path} disappeared while following it.")
    except KeyboardInterrupt:
        click.echo("\nStopped.")


def _build_session_table(entries: list[dict], log_path: Path) -> Table:
    """Render session log entries as a rich table."""
    table = Table(
        title=f"MCP Session Log — last {len(entries)} ({log_path})",
        show_header=True,
    )
    table.add_column("Time")
    table.add_column("Query", max_width=36)
    table.add_column("Budget", justify="right")
    table.add_column("Nodes", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Savings", justify="right")

    for e in entries:
        table.add_row(
            str(e.get("ts", "")),
            str(e.get("query", "")),
            f"{e.get('budget', 0):,}",
            str(e.get("nodes", 0)),
            f"{e.get('tokens', 0):,}",
            f"{e.get('savings_pct', 0)}%",
        )
    return table


def _format_session_line(line: str) -> str:
    """Format one raw log line for --tail output; passes through unparseable lines."""
    import json as _json

    try:
        e = _json.loads(line)
    except ValueError:
        return line.rstrip("\n")
    return (
        f"{e.get('ts', '')}  {e.get('query', '')!r}  "
        f"{e.get('nodes', 0)} nodes · {e.get('tokens', 0):,} tokens · "
        f"{e.get('savings_pct', 0)}% saved"
    )


@cli.command("index")
@click.argument("path", default=".", type=click.Path(exists=True, file_okay=False))
@click.option("--output", "-o", "output_path", default=None, type=click.Path(),
              help="Output path for graph.json (default: <path>/graphify-out/graph.json).")
@click.option("--watch", is_flag=True, default=False,
              help="Re-index on file changes (requires watchdog).")
@click.option("--ignore-file", "ignore_file", default=".slurpignore", show_default=True,
              help="Path to .slurpignore rules.")
def index_cmd(path: str, output_path: str | None, watch: bool, ignore_file: str) -> None:
    """Generate graph.json by statically indexing the project.

    No graphify or LLM required. Supports Python (ast), TypeScript/JS
    (tree-sitter or regex), and Go (regex).
    """
    import json as _json

    from slurp.indexer import (
        _INDEXED_EXTENSIONS,
        format_parser_summary,
        index_project,
        iter_source_files,
        parser_summary,
    )

    root = Path(path).resolve()
    out = Path(output_path) if output_path else root / "graphify-out" / "graph.json"
    ignore = load_ignore(Path(ignore_file))

    def _run() -> None:
        click.echo(f"Indexing {root} ...")
        graph = index_project(root, ignore)
        n_nodes = len(graph["nodes"])
        n_edges = len(graph["links"])
        files_processed = len({n["source_file"] for n in graph["nodes"] if n.get("source_file")})
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_json.dumps(graph, indent=2), encoding="utf-8")
        click.echo(f"✓ {n_nodes} nodes · {n_edges} edges · {files_processed} files")

        extensions = {p.suffix.lower() for p in iter_source_files(root)}
        if summary := parser_summary(extensions):
            # DECISION: a degraded parser is shown in orange rather than hidden —
            # regex extracts strictly less than tree-sitter, and silently getting
            # a thinner graph is the failure mode this line exists to prevent.
            for line in format_parser_summary(summary):
                _echo_rich(Text.from_markup(line))
            if any(fallback for _, _, fallback in summary):
                click.echo("  Install the 'ts' extra for full TypeScript support: "
                           "uv sync --extra ts")

        click.echo(f"  Saved: {out}")
        click.echo(f'\nNext: slurp "your query" --graph {out}')

    _run()

    if watch:
        try:
            from watchdog.events import FileSystemEventHandler  # noqa: PLC0415
            from watchdog.observers import Observer  # noqa: PLC0415
        except ImportError:
            raise click.ClickException(
                "--watch requires watchdog. Install: pip install watchdog"
            )

        class _Handler(FileSystemEventHandler):  # type: ignore[misc]
            def on_modified(self, event) -> None:
                if not event.is_directory:
                    if Path(event.src_path).suffix.lower() in _INDEXED_EXTENSIONS:
                        safe_path = event.src_path.encode("unicode_escape").decode("ascii")
                        click.echo(f"  Changed: {safe_path} — re-indexing...")
                        _run()

        observer = Observer()
        observer.schedule(_Handler(), str(root), recursive=True)
        observer.start()
        click.echo(f"\nWatching {root} for changes (Ctrl+C to stop)...")
        try:
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()


@cli.command("init")
@click.option("--yes", "-y", is_flag=True, default=False,
              help="Accept every prompt (for CI and scripting).")
def init(yes: bool) -> None:
    """Set up slurp in this project — index it and write .mcp.json.

    Detects the project language, builds graph.json, and registers slurp as an
    MCP server so your AI coding assistant can query it. Needs no flags: run
    `slurp init` from the project root.
    """
    import json as _json
    import sys

    root = Path(".").resolve()
    mcp_path = Path(".mcp.json")

    language, counts = _detect_language(root)
    existing_graph = _find_graph()
    command, install_kind = _detect_slurp_command()

    # --- Plan ---------------------------------------------------------------
    plan: list[str] = []
    if language is None:
        plan.append("[yellow]Language:[/]  none detected (no .py/.ts/.js/.go sources)")
    else:
        breakdown = ", ".join(
            f"{lang} {n}" for lang, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        plan.append(f"[bold]Language:[/]  {language}  ([dim]{breakdown} files[/])")
    plan.append(f"[bold]Project:[/]   {root}")
    plan.append("")
    if existing_graph is not None:
        plan.append(f"  1. Reuse existing graph at [cyan]{existing_graph}[/] (or re-index)")
    else:
        plan.append("  1. Index this project into [cyan]graphify-out/graph.json[/]")
    verb = "Update" if mcp_path.exists() else "Create"
    plan.append(f"  2. {verb} [cyan].mcp.json[/] registering slurp as an MCP server")
    plan.append("")
    plan.append(f"[dim]slurp binary: {command}  ({install_kind})[/]")

    _echo_rich(Panel("\n".join(plan), title="slurp init", border_style="cyan", padding=(1, 2)))

    if language is None:
        click.echo(
            "Warning: no indexable source files found — the graph would be empty.\n"
            "         Run this from your project root."
        )

    if not yes and not click.confirm("Proceed?", default=True):
        click.echo("Cancelled — nothing was changed.")
        raise click.Abort()

    # --- Step 1: graph ------------------------------------------------------
    reuse = False
    if existing_graph is not None:
        reuse = yes or click.confirm(
            f"Reuse the existing graph at {existing_graph}?", default=True
        )

    if reuse and existing_graph is not None:
        graph_path = existing_graph.resolve()
        data = _json.loads(graph_path.read_text(encoding="utf-8"))
        n_nodes = len(data.get("nodes", []))
        n_edges = len(data.get("links", data.get("edges", [])))
        click.echo(f"Using existing graph: {graph_path}")
    else:
        from slurp.indexer import index_project, iter_source_files

        graph_path = root / "graphify-out" / "graph.json"
        ignore = load_ignore(Path(".slurpignore"))
        files = list(iter_source_files(root))

        # DECISION: the live progress bar is disabled off-TTY so piped output
        # and CliRunner captures stay free of ANSI control sequences.
        is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
        if not is_tty:
            click.echo(f"Indexing {len(files)} files...")

        from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn

        with Progress(
            TextColumn("[cyan]Indexing[/]"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("[dim]{task.completed}/{task.total} files[/]"),
            disable=not is_tty,
        ) as progress:
            task = progress.add_task("index", total=len(files))
            graph = index_project(root, ignore, on_file=lambda _p: progress.advance(task))

        n_nodes = len(graph["nodes"])
        n_edges = len(graph["links"])
        graph_path.parent.mkdir(parents=True, exist_ok=True)
        graph_path.write_text(_json.dumps(graph, indent=2), encoding="utf-8")

    # --- Step 2: .mcp.json --------------------------------------------------
    existing_cfg: dict | None = None
    write_mcp = True

    if mcp_path.exists():
        try:
            parsed = _json.loads(mcp_path.read_text(encoding="utf-8"))
            existing_cfg = parsed if isinstance(parsed, dict) else None
        except ValueError:
            existing_cfg = None

        others = [k for k in (existing_cfg or {}).get("mcpServers", {}) if k != "slurp"]
        if existing_cfg is None:
            prompt = ".mcp.json exists but is not valid JSON. Overwrite it?"
        elif others:
            prompt = (
                f".mcp.json exists. Update its 'slurp' entry? "
                f"({len(others)} other server(s) will be kept)"
            )
        else:
            prompt = ".mcp.json exists. Overwrite it?"
        write_mcp = yes or click.confirm(prompt, default=True)

    config = _build_mcp_config(command, graph_path, existing_cfg)
    config_json = _json.dumps(config, indent=2)
    if write_mcp:
        mcp_path.write_text(config_json + "\n", encoding="utf-8")

    # --- Summary ------------------------------------------------------------
    summary: list[str] = [
        f"[bold]Nodes:[/]   {n_nodes}",
        f"[bold]Edges:[/]   {n_edges}",
        f"[bold]Graph:[/]   {graph_path}",
        "",
    ]
    if write_mcp:
        summary.append(f"[bold].mcp.json[/] ({mcp_path.resolve()}):")
    else:
        summary.append("[yellow].mcp.json left untouched.[/] Add this entry manually:")
    # JSON contains square brackets, which rich would parse as markup tags.
    summary.append(escape(config_json))

    _echo_rich(Panel("\n".join(summary), title="slurp is ready", border_style="green",
                     padding=(1, 2)))
    click.echo("Next: restart your AI coding assistant to activate slurp")


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
