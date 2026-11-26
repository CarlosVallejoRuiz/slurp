"""CLI entry point for Slurp."""

import json
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
from slurp import graphfile as _graphfile
from slurp._graphutils import _LANGUAGE_BY_EXT, _left_out
from slurp.audit import log_query, read_audit, top_nodes_from_audit
# Imported at module level because the --price-model choices are built at decoration time.
from slurp.benchmark import MODEL_PRICES_PER_MTok
from slurp.budget import (
    DEFAULT_MIN_SCORE,
    BudgetTooSmallError,
    build_context,
    count_tokens,
    fit_to_budget,
    left_out_from_stats,
    resolve_token_factor,
)
from slurp.explainer import PROVIDERS as _EXPLAIN_PROVIDERS
from slurp.formatter import format_subgraph
from slurp.ignore import apply_ignore, load_ignore
from slurp.loader import SlurpLoadError, build_graph, load_graph
from slurp.scorer import score_nodes, score_nodes_detailed

# DECISION (v1.3): graphify-out/slurp-graph.json comes before graphify-out/graph.json.
# That file only exists when the user (or `slurp init`) chose to build slurp's own,
# always-fresh graph beside graphify's, so finding it expresses a preference.
_GRAPH_SEARCH_PATHS = [
    Path("graph.json"),
    Path("graphify-out/slurp-graph.json"),
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


_NO_GRAPH = "No graph.json found. Pass --graph or run from a directory with graph.json."

_FINISHING = "finishing graph refresh… (Ctrl+C to skip; the graph stays as it was)"


def _refresh_options(fn):
    """--no-refresh and --refresh-timeout, on every command that answers from a graph."""
    fn = click.option(
        "--refresh-timeout", "refresh_timeout", type=float, default=None,
        help="Seconds to wait for a refresh of slurp's own graph before answering from "
             "the previous one (default: $SLURP_REFRESH_TIMEOUT, else 2; 0 never waits).")(fn)
    return click.option(
        "--no-refresh", "no_refresh", is_flag=True, default=False,
        help="Do not refresh slurp's own graph first; only warn if it is out of date "
             "(also SLURP_REFRESH=0).")(fn)


class _Graphs:
    """The graphs a command answers from, made current first (design §3).

    DECISION: everything about the refresh goes to stderr — stdout carries the
    answer, which may be piped, JSON, or `export --output`. A refresh that
    outlives the time limit is finished before the process exits, with a
    message: killing it would throw the work away and the next command would
    start over. Ctrl+C is safe thanks to the atomic write.
    """

    def __init__(self, paths: list[Path], *, no_refresh: bool = False,
                 timeout: float | None = None, measuring: bool = False) -> None:
        from slurp.refresh import GraphTracker, Refresher, resolve_refresh

        try:
            enabled, seconds = resolve_refresh(no_refresh, timeout)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
        reason = None
        if measuring:
            enabled, reason = False, "benchmark and eval measure the graph as it is"
        try:
            self.trackers = [GraphTracker(p) for p in paths]
        except SlurpLoadError as exc:
            raise click.ClickException(str(exc)) from None
        kwargs = {"disabled_reason": reason} if reason else {}
        self.refresher = Refresher(self.trackers, seconds, enabled, **kwargs)
        self.freshness = self.refresher.prepare()
        for line in self.freshness.warnings:
            click.echo(line, err=True)

    @property
    def graph(self):
        return self.trackers[0].graph

    def dicts(self) -> list[dict]:
        """Each graph as its parsed dict, for a federation (JSON graphs only)."""
        out = []
        for tracker in self.trackers:
            if tracker.data is None:
                raise click.ClickException(
                    f"{tracker.path}: a federation reads graph.json files only")
            out.append(tracker.data)
        return out

    def __enter__(self) -> "_Graphs":
        return self

    def __exit__(self, *exc: object) -> None:
        self.refresher.finish(on_wait=lambda: click.echo(_FINISHING, err=True))


def _graph_path(graph: str | None) -> Path:
    path = Path(graph) if graph else _find_graph()
    if path is None:
        raise click.ClickException(_NO_GRAPH)
    return path


# Ownership lives in slurp.graphfile since v1.3: the refresh of the server and
# of every command needs it too. The CLI names are kept for its callers.
_SLURP_GRAPH_KEYS = _graphfile._SLURP_GRAPH_KEYS
_is_foreign_graph = _graphfile.is_foreign_graph


def _confirm_replacing_foreign_graph(path: Path, keep_both_hint: str) -> None:
    """Ask before replacing a graph another tool wrote; abort on anything but yes.

    The hint goes out before the prompt, so a non-interactive run — where the
    prompt reads EOF and aborts — still says how to proceed.

    Args:
        path: The foreign graph about to be replaced.
        keep_both_hint: Command that writes slurp's graph somewhere else instead.
    """
    click.echo(
        f"{path} exists and was not written by slurp — it looks like graphify's "
        "graph, which slurp cannot regenerate.\n"
        f"  Keep both:  {keep_both_hint}\n"
        "  Replace it: run again with --force",
        err=True,
    )
    if not click.confirm("Overwrite it?", default=False, err=True):
        raise click.ClickException(f"Left {path} untouched.")


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


def _mentions_slurp_dir(line: str) -> bool:
    """Whether a .gitignore line already decides what happens to .slurp/.

    A negation (`!.slurp/`) counts too: it is the user deliberately choosing to
    track the directory, and appending `.slurp/` after it would override them.
    """
    pattern = line.strip().lstrip("!")
    pattern = pattern.removeprefix("**/").removeprefix("/")
    return pattern.rstrip("*").rstrip("/") == ".slurp"


def _ensure_gitignored(gitignore: Path) -> bool:
    """Append `.slurp/` to an existing .gitignore that does not mention it yet.

    DECISION: .slurp/ holds the audit log, the session log and config.json,
    which may contain an API key — none of it belongs in a commit. A missing
    .gitignore is left missing: creating one is a project decision, not slurp's.

    Args:
        gitignore: Path to the project's .gitignore.

    Returns:
        True if the entry was appended.
    """
    if not gitignore.is_file():
        return False
    text = gitignore.read_text(encoding="utf-8")
    if any(_mentions_slurp_dir(line) for line in text.splitlines()):
        return False
    separator = "" if not text or text.endswith("\n") else "\n"
    gitignore.write_text(f"{text}{separator}.slurp/\n", encoding="utf-8")
    return True


def _gitignore_config_dir(config_dir: Path) -> bool:
    """Add `.slurp/` to ./.gitignore after `slurp config set`, as `slurp init` does.

    DECISION: config.json can hold an API key, and a user who only ever ran
    `config set` never had init write the entry. Same rules as init (see
    _ensure_gitignored), plus one: the entry is only written when it would
    cover the config — a directory called .slurp at or below the current one.

    Returns:
        True if the entry was appended.
    """
    if config_dir.name != ".slurp":
        return False
    try:
        config_dir.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        return False
    return _ensure_gitignored(Path(".gitignore"))


# DECISION: --backend anthropic is refused, not removed silently. Anthropic
# offers no embedding model — POST /v1/embeddings answers 404 and the official
# docs point to Voyage AI — so the backend could never have worked. A bare
# click.Choice would answer "not one of 'tfidf', 'openai'", leaving anyone who
# used it to guess why; this says what happened and what to use instead. The
# library refuses it too (scorer._check_backend).
_ANTHROPIC_BACKEND_RETIRED = (
    "'anthropic' is not available: Anthropic does not offer an embeddings "
    "endpoint or model, so this backend could never return results. Use "
    "--backend openai for real embeddings (needs OPENAI_API_KEY and the "
    "llm-openai extra), or the default --backend tfidf."
)


class _BackendChoice(click.Choice):
    """--backend values, explaining the one that was retired."""

    def __init__(self) -> None:
        super().__init__(["tfidf", "openai"])

    def convert(self, value, param, ctx):  # type: ignore[override]
        if isinstance(value, str) and value.strip().lower() == "anthropic":
            self.fail(_ANTHROPIC_BACKEND_RETIRED, param, ctx)
        return super().convert(value, param, ctx)


_BACKEND = _BackendChoice()

# Tests and import nodes do not compete for a place by default (see
# _graphutils._left_out); these let each kind back in, wherever a subgraph is
# selected.
_INCLUDE_TESTS = click.option(
    "--include-tests", "include_tests", is_flag=True, default=False,
    help="Let test code compete for a place in the subgraph (left out by default).")
_INCLUDE_IMPORTS = click.option(
    "--include-imports", "include_imports", is_flag=True, default=False,
    help="Let import nodes compete for a place in the subgraph (left out by default).")


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
@click.option("--graph", "-g", type=click.Path(), multiple=True,
              help="Path to graph.json (auto-discovered if omitted). "
                   "Repeat to query several projects as one federated graph.")
@click.option("--graph-label", "graph_labels", multiple=True,
              help="Name for each --graph, in the same order. Used to namespace "
                   "node ids. Defaults to a name derived from each path.")
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
@click.option("--suggest", is_flag=True, default=False,
              help="Suggest related queries worth exploring next.")
# Two spellings: `--n` as specified, `--suggest-n` because a bare `--n` gives
# no clue what it counts among two dozen other options.
@click.option("--n", "--suggest-n", "suggest_n", default=3, show_default=True,
              help="How many suggestions --suggest may show.")
@click.option("--neighbor-decay", "neighbor_decay", default=0.7,
              show_default=True, help="Score decay factor for neighbor nodes.")
@click.option("--min-score", "min_score", default=DEFAULT_MIN_SCORE, show_default=True,
              help="Minimum relevance score threshold; nodes below this are excluded.")
@click.option("--viz", is_flag=True, default=False,
              help="Open an interactive graph visualisation in the browser.")
@click.option("--viz-output", "viz_output", type=click.Path(), default=None,
              help="Write the visualisation HTML to PATH. Creates parent directories. "
                   "Does not open a browser unless --viz is also given.")
@click.option("--ignore-file", "ignore_file", default=".slurpignore", show_default=True,
              help="Path to .slurpignore file for excluding nodes.")
@click.option("--backend", default="tfidf", show_default=True,
              type=_BACKEND,
              help="Scoring backend: 'tfidf', or 'openai' for real embeddings.")
@click.option("--inject-code", "inject_code_flag", is_flag=True, default=False,
              help="Append source code blocks for each selected node (≤30 nodes required).")
@click.option("--project-root", "project_root", type=click.Path(), default=None,
              help="Root directory for resolving source_file paths (default: graph.json directory).")
@click.option("--token-factor", "token_factor", type=float, default=None,
              help="Multiply token counts (e.g. 1.56 to estimate Claude tokens). "
                   "Default: $SLURP_TOKEN_FACTOR, else 1.0.")
@_INCLUDE_TESTS
@_INCLUDE_IMPORTS
@_refresh_options
def run(
    query: str,
    graph: tuple[str, ...],
    graph_labels: tuple[str, ...],
    budget: int,
    fmt: str,
    model: str,
    explain: bool,
    no_audit: bool,
    suggest: bool,
    suggest_n: int,
    neighbor_decay: float,
    min_score: float,
    viz: bool,
    viz_output: str | None,
    ignore_file: str,
    backend: str,
    inject_code_flag: bool,
    project_root: str | None,
    token_factor: float | None,
    include_tests: bool,
    include_imports: bool,
    no_refresh: bool,
    refresh_timeout: float | None,
) -> None:
    """Run a query against the graph."""
    factor = _token_factor(token_factor)
    graph_paths = [Path(g) for g in graph]
    if not graph_paths:
        found = _find_graph()
        if found is None:
            raise click.ClickException(
                "No graph.json found. Pass --graph or run from a directory "
                "with graph.json."
            )
        graph_paths = [found]

    if graph_labels and len(graph_labels) != len(graph_paths):
        raise click.ClickException(
            f"Got {len(graph_labels)} --graph-label values for "
            f"{len(graph_paths)} --graph paths — pass one label per graph."
        )

    # The audit log records a single graph path; for a federation, the first one
    # identifies the query well enough to keep the log's shape unchanged.
    graph_path = graph_paths[0]
    with _Graphs(graph_paths, no_refresh=no_refresh, timeout=refresh_timeout) as graphs:
        _run_query(graphs, graph_path, graph_paths, graph_labels, query, budget, fmt, model,
                   explain, no_audit, suggest, suggest_n, neighbor_decay, min_score, viz,
                   viz_output, ignore_file, backend, inject_code_flag, project_root, factor,
                   include_tests, include_imports)


def _run_query(graphs: "_Graphs", graph_path: Path, graph_paths: list[Path],
               graph_labels: tuple[str, ...], query: str, budget: int, fmt: str, model: str,
               explain: bool, no_audit: bool, suggest: bool, suggest_n: int,
               neighbor_decay: float, min_score: float, viz: bool, viz_output: str | None,
               ignore_file: str, backend: str, inject_code_flag: bool,
               project_root: str | None, factor: float, include_tests: bool,
               include_imports: bool) -> None:
    """The body of `slurp QUERY`, once its graphs are current."""
    fed_stats = None
    try:
        if len(graph_paths) == 1:
            G = graphs.graph
        else:
            from slurp.federation import derive_labels, merge_graphs_with_stats
            labels = list(graph_labels) or derive_labels(graph_paths)
            merged, fed_stats = merge_graphs_with_stats(graphs.dicts(), labels)
            G = build_graph(merged)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None

    if fed_stats is not None:
        from slurp.federation import format_federation_header
        click.echo(format_federation_header(fed_stats))
        if fed_stats.collision_count:
            click.echo(
                f"  {fed_stats.collision_count} node "
                f"{'id' if fed_stats.collision_count == 1 else 'ids'} appeared in "
                "more than one graph — namespaced by project, none lost."
            )
        click.echo()

    G = apply_ignore(G, load_ignore(Path(ignore_file)))

    if explain:
        scores, structural, semantic = score_nodes_detailed(G, query, backend=backend)
    else:
        scores = score_nodes(G, query, backend=backend)
        structural = semantic = {}

    try:
        result, subG, stats = build_context(
            G, scores, budget=budget, model=model,
            neighbor_decay=neighbor_decay, min_score=min_score,
            format=fmt, query=query, token_factor=factor,
            include_tests=include_tests, include_imports=include_imports,
        )
    except BudgetTooSmallError as exc:
        raise click.ClickException(str(exc)) from None

    if inject_code_flag:
        from slurp.injector import inject_code
        root = Path(project_root) if project_root else graph_path.parent
        if subG.number_of_nodes() > 30:
            click.echo(
                f"Warning: --inject-code requires ≤30 nodes, but subgraph has "
                f"{subG.number_of_nodes()}. Increase --min-score or lower --budget to reduce.",
                err=True,
            )
        injected = inject_code(subG, root)
        if injected is not subG:
            # The code blocks count too: trim back under the budget, lowest
            # scores first, now that the real text is longer.
            from slurp.formatter import item_text
            result, subG, stats = fit_to_budget(
                injected, budget,
                lambda sub, st: format_subgraph(sub, st, format=fmt, scores=scores, query=query),
                _item_values(injected, scores), G.number_of_nodes(), model,
                item_cost=lambda item: count_tokens(item_text(injected, item, fmt), model),
                token_factor=factor, left_out=left_out_from_stats(stats))

    click.echo(result)

    if suggest:
        from slurp.suggester import format_suggestions, suggest_queries
        # No --graph in the rendered commands: the reader just ran one, and an
        # absolute path doubles the line length for nothing.
        text = format_suggestions(
            suggest_queries(G, query, scores, subG, n=suggest_n,
                            exclude=_left_out(G, include_tests, include_imports)))
        # Silence is the honest answer when the budget covered the neighbourhood.
        click.echo(f"\n{text}" if text else "\nSUGGESTED QUERIES\n  (none — the "
                   "budget already covers this neighbourhood)")

    if not no_audit:
        top = sorted(subG.nodes, key=lambda n: scores.get(n, 0.0), reverse=True)
        log_query(
            query=query,
            graph_path=graph_path,
            stats=stats,
            top_nodes=top,
            graph_state=graphs.freshness.status,
        )

    if explain:
        _echo_rich(_build_explain_table(subG, scores, structural, semantic))

    if viz or viz_output:
        from slurp.budget import full_graph_tokens
        from slurp.viz import build_html
        # The badge compares two measured texts: what was printed, and the whole
        # graph printed the same way.
        viz_stats = {**stats, "tokens_full": full_graph_tokens(G, scores, query, fmt, model,
                                                                token_factor=factor)}
        _deliver_viz(build_html(subG, viz_stats, scores, query), viz, viz_output)


def _token_factor(flag: float | None) -> float:
    """--token-factor, else $SLURP_TOKEN_FACTOR, else 1.0 — as a clean CLI error."""
    try:
        return resolve_token_factor(flag)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None


def _item_values(G, scores: dict[str, float]) -> dict[tuple, float]:
    """Trimming order for fit_to_budget: a node by its score, an edge by its weaker end."""
    values: dict[tuple, float] = {("n", n): scores.get(n, 0.0) for n in G.nodes}
    for u, v in G.edges:
        values[("e", u, v)] = min(scores.get(u, 0.0), scores.get(v, 0.0))
    return values


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
@_refresh_options
def stats(graph: str | None, no_refresh: bool, refresh_timeout: float | None) -> None:
    """Show statistics for a graph file."""
    graph_path = _graph_path(graph)
    with _Graphs([graph_path], no_refresh=no_refresh, timeout=refresh_timeout) as graphs:
        G = graphs.graph
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
            try:
                text, _subG, _stats = build_context(sub, scores, budget=budget, query=query,
                                                    token_factor=_token_factor(None))
            except BudgetTooSmallError as exc:
                raise click.ClickException(str(exc)) from None
            click.echo("\n## Token-Budgeted Affected Subgraph\n")
            click.echo(text)

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
    from slurp.diff import diff_headline, diff_severity

    # One band decides both the headline and the colour: they used different
    # thresholds (0.5/0.2 vs 0.7/0.3) and could disagree on the same score.
    colour, border = {
        "high": ("bold red", "red"),
        "medium": ("bold yellow", "yellow"),
        "low": ("bold green", "green"),
        "unknown": ("bold magenta", "magenta"),
    }[diff_severity(diff)]

    body = [
        f"[{colour}]{diff_headline(diff)}[/]  "
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
@click.option("--min-score", "min_score", default=DEFAULT_MIN_SCORE, show_default=True,
              help="Minimum relevance score threshold.")
@click.option("--neighbor-decay", "neighbor_decay", default=0.7, show_default=True,
              help="Score decay factor for neighbor nodes.")
@click.option("--ignore-file", "ignore_file", default=".slurpignore", show_default=True,
              help="Path to .slurpignore file for excluding nodes.")
@click.option("--backend", default="tfidf", show_default=True,
              type=_BACKEND,
              help="Scoring backend: 'tfidf', or 'openai' for real embeddings.")
@click.option("--token-factor", "token_factor", type=float, default=None,
              help="Multiply token counts (e.g. 1.56 to estimate Claude tokens). "
                   "Default: $SLURP_TOKEN_FACTOR, else 1.0.")
@_INCLUDE_TESTS
@_INCLUDE_IMPORTS
@_refresh_options
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
    token_factor: float | None,
    include_tests: bool,
    include_imports: bool,
    no_refresh: bool,
    refresh_timeout: float | None,
) -> None:
    """Export a context block ready to paste into a Claude/ChatGPT/CLAUDE.md system prompt."""
    factor = _token_factor(token_factor)
    graph_path = _graph_path(graph)
    with _Graphs([graph_path], no_refresh=no_refresh, timeout=refresh_timeout) as graphs:
        _export(graphs.graph, query, budget, fmt, output, model, min_score, neighbor_decay,
                ignore_file, backend, factor, include_tests, include_imports)


def _export(G, query: str, budget: int, fmt: str, output: str | None, model: str,
            min_score: float, neighbor_decay: float, ignore_file: str, backend: str,
            factor: float, include_tests: bool, include_imports: bool) -> None:
    from slurp.exporter import export_context

    G = apply_ignore(G, load_ignore(Path(ignore_file)))
    scores = score_nodes(G, query, backend=backend)
    # The export wrapper is part of what gets pasted, so it is what gets budgeted.
    try:
        result, _subG, _stats = build_context(
            G, scores, budget=budget, model=model,
            neighbor_decay=neighbor_decay, min_score=min_score, query=query,
            render=lambda sub, st: export_context(sub, st, query, format=fmt, scores=scores),
            token_factor=factor, include_tests=include_tests, include_imports=include_imports,
        )
    except BudgetTooSmallError as exc:
        raise click.ClickException(str(exc)) from None

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
@click.option("--min-score", "min_score", default=DEFAULT_MIN_SCORE, show_default=True,
              help="Minimum relevance score threshold, as in a query.")
@click.option("--backend", default="tfidf", show_default=True,
              type=_BACKEND,
              help="Scoring backend: 'tfidf', or 'openai' for real embeddings.")
@click.option("--price-model", "model_price", default="default", show_default=True,
              type=click.Choice(sorted(MODEL_PRICES_PER_MTok)),
              help="Model whose input-token price is used for cost estimates.")
@click.option("--token-factor", "token_factor", type=float, default=None,
              help="Multiply token counts (e.g. 1.56 to estimate Claude tokens). "
                   "Default: $SLURP_TOKEN_FACTOR, else 1.0.")
@_INCLUDE_TESTS
@_INCLUDE_IMPORTS
def benchmark_cmd(
    graph: str | None,
    queries: tuple[str, ...],
    budgets: tuple[int, ...],
    model: str,
    output: str | None,
    min_score: float,
    backend: str,
    model_price: str,
    token_factor: float | None,
    include_tests: bool,
    include_imports: bool,
) -> None:
    """Measure token savings of slurp vs injecting the full graph."""
    factor = _token_factor(token_factor)
    from slurp.benchmark import format_benchmark, run_benchmark

    # DECISION: a measurement runs on a fixed snapshot; refreshing would move
    # the floor under it. An out-of-date graph is only reported, on stderr.
    G = _Graphs([_graph_path(graph)], measuring=True).graph

    result = run_benchmark(
        G,
        queries=list(queries),
        budgets=list(budgets),
        model=model,
        min_score=min_score,
        backend=backend,
        model_price=model_price,
        token_factor=factor,
        include_tests=include_tests,
        include_imports=include_imports,
    )

    click.echo(format_benchmark(result))

    if output:
        import json as _json
        out_path = Path(output)
        out_path.write_text(_json.dumps(result.to_dict(), indent=2), encoding="utf-8")
        click.echo(f"Results saved to {out_path}")


@cli.command()
@click.option("--graph", "-g", type=click.Path(), multiple=True,
              help="Path to graph.json (auto-discovered if omitted). "
                   "Repeat to serve several projects as one federated graph.")
@click.option("--graph-label", "graph_labels", multiple=True,
              help="Name for each --graph, in the same order.")
@click.option("--log/--no-log", "log", default=True, show_default=True,
              help="Append served queries to .slurp/session.log (only if .slurp/ exists).")
@_refresh_options
def serve(graph: tuple[str, ...], graph_labels: tuple[str, ...], log: bool,
          no_refresh: bool, refresh_timeout: float | None) -> None:
    """Start an MCP stdio server exposing the five slurp tools (query, explain, diff, suggest, impact)."""
    graph_paths = [Path(g) for g in graph]
    if not graph_paths:
        found = _find_graph()
        if found is None:
            raise click.ClickException(
                "No graph.json found. Pass --graph or run from a directory "
                "with graph.json."
            )
        graph_paths = [found]

    if graph_labels and len(graph_labels) != len(graph_paths):
        raise click.ClickException(
            f"Got {len(graph_labels)} --graph-label values for "
            f"{len(graph_paths)} --graph paths — pass one label per graph."
        )

    from slurp.refresh import resolve_refresh

    try:
        enabled, seconds = resolve_refresh(no_refresh, refresh_timeout)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None

    import slurp.mcp as _mcp
    _mcp.serve(
        graph_paths[0] if len(graph_paths) == 1 else graph_paths,
        session_log=log,
        labels=list(graph_labels) or None,
        refresh=enabled,
        refresh_timeout=seconds,
    )


@cli.command("eval")
@click.option("--graph", "-g", type=click.Path(), default=None,
              help="Path to graph.json (auto-discovered if omitted).")
@click.option("--budget", "-b", default=4000, show_default=True,
              help="Token budget for the slurp context.")
@click.option("--questions", "questions_path", type=click.Path(), default=None,
              help="JSON file of questions (uses the built-in set if omitted).")
@click.option("--provider", type=click.Choice(list(_EXPLAIN_PROVIDERS)), default=None,
              help="LLM provider (auto-detected from env vars if omitted).")
@click.option("--model", "-m", default=None,
              help="Model name (provider default if omitted).")
@click.option("--endpoint", default=None,
              help="Base URL for the openai-compatible provider.")
@click.option("--output", "-o", "output_path", type=click.Path(), default=None,
              help="Write full results as JSON to PATH.")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="List the questions and exit without calling any LLM.")
@click.option("--retrieval", is_flag=True, default=False,
              help="No LLM: measure recall and MRR of each question's relevant_nodes.")
@click.option("--min-score", "min_score", default=None, type=float,
              help="Minimum score for --retrieval (default: the CLI query default).")
@click.option("--format", "-f", "fmt", type=click.Choice(["markdown", "json", "yaml"]),
              default=None,
              help="Output format the --retrieval subgraph is budgeted and measured in "
                   "(default: markdown, as `slurp QUERY` and the MCP server send).")
@click.option("--token-factor", "token_factor", type=float, default=None,
              help="Multiply token counts (e.g. 1.56 to estimate Claude tokens). "
                   "Default: $SLURP_TOKEN_FACTOR, else 1.0.")
@_INCLUDE_TESTS
@_INCLUDE_IMPORTS
@click.option("--config-dir", "config_dir", default=".slurp", show_default=True,
              help="Directory holding config.json.")
def eval_cmd(
    graph: str | None,
    budget: int,
    questions_path: str | None,
    provider: str | None,
    model: str | None,
    endpoint: str | None,
    output_path: str | None,
    dry_run: bool,
    retrieval: bool,
    min_score: float | None,
    fmt: str | None,
    token_factor: float | None,
    include_tests: bool,
    include_imports: bool,
    config_dir: str,
) -> None:
    """Measure whether answers are better with slurp than with the full graph."""
    # The LLM eval sends markdown: a format it would ignore must not pass silently.
    if fmt is not None and not retrieval:
        raise click.ClickException("--format applies to --retrieval only; the LLM eval "
                                   "sends markdown, as `slurp QUERY` does by default.")
    factor = _token_factor(token_factor)
    from slurp.evaluator import (
        default_questions,
        load_eval_questions,
        run_eval,
        run_retrieval_eval,
    )
    from slurp.explainer import SlurpExplainError, resolve_config

    try:
        questions = (
            load_eval_questions(Path(questions_path)) if questions_path
            else default_questions()
        )
    except SlurpExplainError as exc:
        raise click.ClickException(str(exc)) from None

    if dry_run:
        _echo_rich(_build_eval_questions_table(questions))
        click.echo(f"\n{len(questions)} questions · no LLM calls made (--dry-run).")
        return

    graph_path = _graph_path(graph)
    G = _Graphs([graph_path], measuring=True).graph   # a snapshot: see benchmark

    if retrieval:
        # DECISION: no provider is resolved at all — the metric must run
        # offline, cost nothing and give the same numbers on every run.
        try:
            suite = run_retrieval_eval(
                G, questions, budget=budget,
                min_score=DEFAULT_MIN_SCORE if min_score is None else min_score,
                token_factor=factor, format=fmt or "markdown",
                include_tests=include_tests, include_imports=include_imports)
        except BudgetTooSmallError as exc:
            raise click.ClickException(str(exc)) from None
        if not suite.scored:
            raise click.ClickException(
                f"None of the {len(questions)} questions' relevant_nodes exist in "
                f"{graph_path}. The built-in set targets a `slurp index` graph of "
                "PrismaStats; pass --questions with node ids from this graph.")
        _echo_rich(_build_retrieval_table(suite))
        _echo_rich(_build_retrieval_panel(suite))
        if output_path:
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(suite.to_dict(), indent=2), encoding="utf-8")
            click.echo(f"\nResults saved to {out}")
        return

    try:
        config = resolve_config(provider, model, endpoint, Path(config_dir))
    except (ValueError, SlurpExplainError) as exc:
        raise click.ClickException(str(exc)) from None
    if config.provider in ("structural", ""):
        raise click.ClickException(
            "slurp eval needs an LLM. Set ANTHROPIC_API_KEY or OPENAI_API_KEY, "
            "run Ollama locally, or use `slurp config set provider ...`."
        )

    click.echo(
        f"Running eval — {len(questions)} questions · budget {budget:,} · "
        f"provider: {config.provider}"
    )

    from rich.progress import BarColumn, Progress, TextColumn

    buf = StringIO()
    console = Console(file=buf, no_color=True, width=100)
    with Progress(BarColumn(bar_width=40), TextColumn("{task.completed}/{task.total}"),
                  console=console, transient=False) as progress:
        task = progress.add_task("eval", total=len(questions))
        suite = run_eval(
            G, questions, config, budget=budget, graph_path=graph_path,
            token_factor=factor, include_tests=include_tests,
            include_imports=include_imports,
            on_question=lambda _r: progress.advance(task),
        )
    click.echo(buf.getvalue(), nl=False)

    _echo_rich(_build_eval_panel(suite))

    failed = len(suite.results) - len(suite.scored)
    if failed:
        click.echo(f"\n{failed} question(s) could not be scored:")
        for result in suite.results:
            if result.error:
                click.echo(f"  {result.question_id}: {result.error}")

    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(suite.to_dict(), indent=2), encoding="utf-8")
        click.echo(f"\nResults saved to {out}")


def _build_retrieval_table(suite) -> Table:
    """Per-question recall and rank, for `slurp eval --retrieval`."""
    table = Table(title="Retrieval (no LLM)", show_header=True, header_style="bold")
    table.add_column("ID", style="dim")
    table.add_column("Found", justify="right")
    table.add_column("Recall", justify="right")
    table.add_column("Rank", justify="right")
    table.add_column("Subgraph", justify="right")
    table.add_column("Tokens", justify="right")
    for r in suite.results:
        if not r.scorable:
            table.add_row(r.question_id, "—", "—", "—", "not in graph", "—")
            continue
        present = len(r.relevant) - len(r.missing)
        table.add_row(r.question_id, f"{len(r.found)}/{present}", f"{r.recall:.2f}",
                      str(r.first_rank), f"{r.subgraph_nodes} nodes", f"{r.tokens_used:,}")
    return table


def _build_retrieval_panel(suite) -> Panel:
    """Summary panel of `slurp eval --retrieval`."""
    skipped = len(suite.results) - len(suite.scored)
    lines = [
        f"Recall (mean):  {suite.mean_recall:.3f}",
        f"Hit rate:       {suite.hit_rate:.3f}",
        f"MRR:            {suite.mrr:.3f}"
        + (f" · {suite.mrr_all:.3f} ranking every node, tests and imports included"
           if suite.mrr_all != suite.mrr else ""),
        f"Tokens sent:    mean {suite.mean_tokens:,.0f} · max {suite.max_tokens:,} "
        f"({suite.format}, measured)",
        f"Questions:      {len(suite.scored)} scored"
        + (f", {skipped} not in this graph" if skipped else ""),
        f"Budget {suite.budget:,} · min score {suite.min_score}",
    ]
    return Panel("\n".join(lines), title="Retrieval Results", border_style="cyan")


def _build_eval_questions_table(questions) -> Table:
    """Table of the eval set, for --dry-run."""
    table = Table(title=f"Eval questions ({len(questions)})",
                  show_header=True, header_style="bold")
    table.add_column("ID", style="dim")
    table.add_column("Area")
    table.add_column("Diff", style="dim")
    table.add_column("Question")
    for q in questions:
        table.add_row(q.id, q.area, q.difficulty, q.question)
    return table


def _build_eval_panel(suite) -> Panel:
    """Summary panel of an eval run."""
    scored = len(suite.scored)
    wins = sum(1 for r in suite.scored if r.winner == "slurp")
    lines = [
        f"slurp win rate:         {suite.slurp_win_rate}% ({wins}/{scored})",
        f"Mean score with slurp:  {suite.mean_score_slurp:.3f}",
        f"Mean score baseline:    {suite.mean_score_baseline:.3f}",
        f"Token savings:          {suite.mean_token_savings:.1f}%",
        f"Quality/token slurp:    {suite.quality_per_token_slurp:.6f}",
        f"Quality/token baseline: {suite.quality_per_token_baseline:.6f}",
    ]
    return Panel("\n".join(lines), title="Eval Results", border_style="cyan")


@cli.command("explain")
@click.argument("query")
@click.option("--graph", "-g", type=click.Path(), default=None,
              help="Path to graph.json (auto-discovered if omitted).")
@click.option("--provider", type=click.Choice(list(_EXPLAIN_PROVIDERS)), default=None,
              help="LLM provider (auto-detected from env vars if omitted).")
@click.option("--model", "-m", default=None,
              help="Model name (provider default if omitted).")
@click.option("--endpoint", default=None,
              help="Base URL for the openai-compatible provider.")
@click.option("--no-llm", "no_llm", is_flag=True, default=False,
              help="Skip the LLM and show the structural analysis only.")
@click.option("--hops", default=2, show_default=True,
              help="Neighbourhood radius used to build the explanation context.")
@click.option("--config-dir", "config_dir", default=".slurp", show_default=True,
              help="Directory holding config.json.")
@_refresh_options
def explain_cmd(
    query: str,
    graph: str | None,
    provider: str | None,
    model: str | None,
    endpoint: str | None,
    no_llm: bool,
    hops: int,
    config_dir: str,
    no_refresh: bool,
    refresh_timeout: float | None,
) -> None:
    """Explain what a node does, who depends on it, and what breaks if it changes."""
    graph_path = _graph_path(graph)
    with _Graphs([graph_path], no_refresh=no_refresh, timeout=refresh_timeout) as graphs:
        _explain(graphs.graph, query, provider, model, endpoint, no_llm, hops, config_dir)


def _explain(G, query: str, provider: str | None, model: str | None, endpoint: str | None,
             no_llm: bool, hops: int, config_dir: str) -> None:
    from slurp.explainer import (
        LLMConfig,
        SlurpExplainError,
        explain_node,
        find_node,
        format_explanation,
        resolve_config,
    )

    scores = score_nodes(G, query)

    try:
        node_id = find_node(G, query, scores)
        config = (
            LLMConfig(provider="structural") if no_llm
            else resolve_config(provider, model, endpoint, Path(config_dir))
        )
        result = explain_node(G, node_id, scores, config, hops=hops)
    except SlurpExplainError as exc:
        raise click.ClickException(str(exc)) from None

    subtitle = " · ".join(filter(None, [
        result.node_type,
        result.source_file,
        f"score: {result.score:.3f}" if result.score else "",
    ]))
    _echo_rich(Panel(subtitle or result.node_id,
                     title=f"slurp explain — {result.label}",
                     border_style="cyan"))
    click.echo()
    click.echo(format_explanation(result))

    if result.provider_used == "structural" and not no_llm and not result.warning:
        click.echo(
            "\nNo LLM provider configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY, "
            "run Ollama locally, or use `slurp config set provider ...`."
        )


@cli.group("config")
def config_group() -> None:
    """View and change slurp's saved configuration."""


@config_group.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--config-dir", "config_dir", default=".slurp", show_default=True,
              help="Directory holding config.json.")
def config_set(key: str, value: str, config_dir: str) -> None:
    """Save KEY = VALUE to .slurp/config.json."""
    from slurp.explainer import SlurpExplainError, config_path, write_config

    try:
        write_config(key, value, Path(config_dir))
    except SlurpExplainError as exc:
        raise click.ClickException(str(exc)) from None
    except OSError as exc:
        raise click.ClickException(f"Could not write config: {exc}") from None

    shown = "********" if key == "api_key" else value
    click.echo(f"Set {key} = {shown}")
    click.echo(f"  Saved: {config_path(Path(config_dir))}")
    if _gitignore_config_dir(Path(config_dir)):
        click.echo("  .gitignore: added .slurp/ (the config can hold an API key)")


@config_group.command("show")
@click.option("--config-dir", "config_dir", default=".slurp", show_default=True,
              help="Directory holding config.json.")
def config_show(config_dir: str) -> None:
    """Show the saved configuration and what slurp would use right now."""
    from slurp.explainer import SlurpExplainError, config_path, read_config, resolve_config

    path = config_path(Path(config_dir))
    saved = read_config(Path(config_dir))

    table = Table(title=f"slurp config — {path}", show_header=True, header_style="bold")
    table.add_column("Key")
    table.add_column("Value")
    if saved:
        for key in sorted(saved):
            value = "********" if key == "api_key" else str(saved[key])
            table.add_row(key, value)
    else:
        table.add_row("(empty)", "no config file yet")
    _echo_rich(table)

    try:
        resolved = resolve_config(config_dir=Path(config_dir))
    except SlurpExplainError as exc:
        # `show` exists to diagnose the config, so a refusal is reported, not raised.
        click.echo(f"\nEffective provider: none — {exc}")
        return
    click.echo(f"\nEffective provider: {resolved.provider}")
    click.echo(f"Effective model:    {resolved.model or '(none)'}")
    if resolved.endpoint:
        click.echo(f"Effective endpoint: {resolved.endpoint}")
    if resolved.provider == "structural":
        click.echo(
            "\nNo LLM provider detected — `slurp explain` will use structural "
            "analysis. Set an API key or run `slurp config set provider ...`."
        )


@cli.command("impact")
@click.argument("file_path")
@click.option("--graph", "-g", type=click.Path(), default=None,
              help="Path to graph.json (auto-discovered if omitted).")
@click.option("--hops", default=2, show_default=True,
              help="How far to follow dependants outward.")
@click.option("--output", "output_fmt", type=click.Choice(["text", "json"]),
              default="text", show_default=True,
              help="Output format; json is meant for CI gating.")
@click.option("--viz", is_flag=True, default=False,
              help="Open an interactive view of the affected nodes.")
@click.option("--viz-output", "viz_output", type=click.Path(), default=None,
              help="Write the visualisation to a file instead of opening it.")
@_refresh_options
def impact_cmd(
    file_path: str,
    graph: str | None,
    hops: int,
    output_fmt: str,
    viz: bool,
    viz_output: str | None,
    no_refresh: bool,
    refresh_timeout: float | None,
) -> None:
    """Show what could break if you edit FILE_PATH, before you edit it."""
    if hops < 1:
        raise click.ClickException("--hops must be at least 1")
    graph_path = _graph_path(graph)
    with _Graphs([graph_path], no_refresh=no_refresh, timeout=refresh_timeout) as graphs:
        _impact(graphs.graph, file_path, hops, output_fmt, viz, viz_output)


def _impact(G, file_path: str, hops: int, output_fmt: str, viz: bool,
            viz_output: str | None) -> None:
    import json as _json

    from slurp.impact import (
        affected_subgraph,
        analyze_impact,
        format_impact,
        impact_to_dict,
    )

    result = analyze_impact(G, file_path, hops=hops)

    if output_fmt == "json":
        click.echo(_json.dumps(impact_to_dict(result), indent=2))
    else:
        click.echo(format_impact(result, G))

    if viz or viz_output:
        if not result.nodes_in_file:
            raise click.ClickException(
                f"Nothing to visualise: no nodes found for {file_path}."
            )
        from slurp.viz import build_html
        sub = affected_subgraph(G, result)
        # Nodes in the edited file lead; everything else is shaded by distance.
        scores = {
            n: 1.0 if n in set(result.nodes_in_file)
            else 0.6 if n in set(result.direct_dependents) else 0.3
            for n in sub.nodes
        }
        stats = {
            "nodes_selected": sub.number_of_nodes(),
            "nodes_total": G.number_of_nodes(),
            "tokens_used": 0,
            "tokens_budget": 0,
            "coverage_pct": round(
                sub.number_of_nodes() / (G.number_of_nodes() or 1) * 100, 1),
        }
        html = build_html(sub, stats, scores, f"impact: {file_path}")
        _deliver_viz(html, viz, viz_output)


@cli.command("advisor")
@click.argument("query")
@click.option("--graph", "-g", type=click.Path(), default=None,
              help="Path to graph.json (auto-discovered if omitted).")
@click.option("--audit-dir", "audit_dir", default=".slurp", show_default=True,
              help="Directory containing audit.jsonl.")
@click.option("--price-model", "price_model", default="claude-sonnet-5",
              show_default=True,
              help="Pricing model for the cost estimate (same models as benchmark).")
@click.option("--min-similar", "min_similar", default=3, show_default=True,
              help="Similar queries needed before a recommendation is confident.")
@_refresh_options
def advisor_cmd(
    query: str,
    graph: str | None,
    audit_dir: str,
    price_model: str,
    min_similar: int,
    no_refresh: bool,
    refresh_timeout: float | None,
) -> None:
    """Recommend a token budget for QUERY from your past query history."""
    from slurp.advisor import format_advice, recommend_budget

    try:
        advice = recommend_budget(
            query,
            audit_dir=Path(audit_dir),
            price_model=price_model,
            min_similar=min_similar,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None

    graph_path = Path(graph) if graph else _find_graph()
    # The advice comes from the audit log; the graph it names is kept current
    # like every graph a command names (design §3), for the query it suggests.
    refreshing = (_Graphs([graph_path], no_refresh=no_refresh, timeout=refresh_timeout)
                  if graph_path is not None and graph_path.exists() else None)

    if advice.similar:
        headline = (
            f"Based on {advice.similar_count} similar past "
            f"{'query' if advice.similar_count == 1 else 'queries'}"
        )
        border = "cyan"
    else:
        headline = advice.note
        border = "yellow"

    _echo_rich(Panel(headline, title=f'Budget Advisor — "{query}"',
                     border_style=border))
    click.echo()
    click.echo(format_advice(advice, graph_path))
    if refreshing is not None:
        refreshing.__exit__(None, None, None)


@cli.command("session")
@click.option("--last", "last", default=20, show_default=True,
              help="Number of most recent entries to show.")
@click.option("--tail", is_flag=True, default=False,
              help="Follow the log in real time (Ctrl+C to stop).")
@click.option("--sessions", "sessions", is_flag=True, default=False,
              help="One line per server session: client, calls per tool, edits, refresh time.")
@click.option("--log-dir", "log_dir", default=".slurp", show_default=True,
              help="Directory containing session.log.")
def session_cmd(last: int, tail: bool, sessions: bool, log_dir: str) -> None:
    """Show what your AI assistant asked the slurp MCP server."""
    import time

    from slurp.session import SESSION_LOG_FILE, read_session_log, summarize_sessions

    log_path = Path(log_dir) / SESSION_LOG_FILE
    if sessions:
        summaries = summarize_sessions(read_session_log(Path(log_dir)))[-last:]
        if not summaries:
            click.echo(f"No sessions in {log_path} yet (lines written before v1.3 carry none).")
            return
        _echo_rich(_build_sessions_table(summaries, log_path))
        return
    entries = [e for e in read_session_log(Path(log_dir))
               if e.get("event") in (None, "call")][-last:] if last > 0 else []

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


def _asked(entry: dict) -> str:
    """What a session log line asked, in one cell: the query, the file, the node."""
    args = entry.get("args") or {}
    for key in ("query", "file_path", "node_name"):
        if args.get(key) or entry.get(key):
            return str(args.get(key) or entry.get(key))
    if args.get("new_graph"):
        return f"{args.get('old_graph')} → {args.get('new_graph')}"
    return ""


def _build_session_table(entries: list[dict], log_path: Path) -> Table:
    """Render session log entries as a rich table."""
    table = Table(
        title=f"MCP Session Log — last {len(entries)} ({log_path})",
        show_header=True,
    )
    table.add_column("Time")
    table.add_column("Tool")
    table.add_column("Asked", max_width=36)
    table.add_column("Budget", justify="right")
    table.add_column("Nodes", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Graph")

    # No savings: see log_session. A savings_pct left by a pre-1.1.0 log was a
    # fixed-cost-per-node guess and is not shown. Lines without a tool are v1
    # lines: slurp_query was the only tool logged.
    for e in entries:
        budget = e.get("budget")
        table.add_row(
            str(e.get("ts", "")),
            str(e.get("tool", "slurp_query")),
            _asked(e),
            f"{budget:,}" if isinstance(budget, int) else "",
            str(e["nodes"]) if "nodes" in e else "",
            f"{e['tokens']:,}" if "tokens" in e else "",
            str(e.get("graph", "")) + (" ✗" if e.get("error") else ""),
        )
    return table


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def _build_sessions_table(summaries: list, log_path: Path) -> Table:
    """`slurp session --sessions`: one row per server process (design §7)."""
    table = Table(title=f"MCP Sessions — {len(summaries)} ({log_path})", show_header=True)
    for name, justify in (("Started", "left"), ("Client", "left"), ("Duration", "right"),
                          ("Calls", "left"), ("1st call", "right"), ("Changed before", "right"),
                          ("Edited after impact/explain", "right"), ("Warned", "right"),
                          ("p95 refresh", "right")):
        table.add_column(name, justify=justify)  # type: ignore[arg-type]
    for s in summaries:
        calls = " · ".join(f"{tool.removeprefix('slurp_')} {n}"
                           for tool, n in sorted(s.calls.items(), key=lambda kv: (-kv[1], kv[0])))
        table.add_row(
            s.started,
            s.client or "—",
            _duration(s.duration_s),
            calls or "none",
            f"+{_duration(s.first_call_s)}" if s.first_call_s is not None else "—",
            str(s.changed_before_first_call),
            f"{s.edited_after_impact_or_explain} of {s.edited}",
            str(s.warned),
            f"{s.p95_refresh_ms:.0f} ms" if s.p95_refresh_ms is not None else "—",
        )
    return table


def _format_session_line(line: str) -> str:
    """Format one raw log line for --tail output; passes through unparseable lines."""
    import json as _json

    try:
        e = _json.loads(line)
    except ValueError:
        return line.rstrip("\n")
    if e.get("event") in ("start", "end"):
        return f"{e.get('ts', '')}  — session {e.get('event')} —"
    nodes = f"  {e['nodes']} nodes · {e.get('tokens', 0):,} tokens" if "nodes" in e else ""
    graph = f"  [{e['graph']}]" if e.get("graph") else ""
    return f"{e.get('ts', '')}  {e.get('tool', 'slurp_query')}  {_asked(e)!r}{nodes}{graph}"


def _rel_display(path: Path, root: Path) -> str:
    """Render *path* relative to *root* when possible, for compact output."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _run_smart(root: Path, out: Path, ignore, dry_run: bool,
               ignore_path: Path | None = None) -> bool:
    """Run one incremental re-index and report it.

    Args:
        root: Project root.
        out: Graph path to update.
        ignore: SlurpIgnore rules.
        dry_run: List the work without writing the graph.

    Returns:
        True if the incremental path handled the request. False means the caller
        must fall back to a full index; the reason has already been printed.
    """
    from slurp.smart import smart_reindex

    mode = "smart mode, dry run" if dry_run else "smart mode"
    click.echo(f"Indexing {root} ({mode}) ...")

    result = smart_reindex(root, out, ignore, dry_run=dry_run, ignore_path=ignore_path)

    if not result.is_git_repo:
        click.echo("  No git repository found — running full index")
        return False
    if not result.graph_found:
        click.echo(f"  No existing graph at {out} — running full index")
        return False

    if result.fallback_reason and not result.changed_files:
        # No usable build commit: there is nothing to compare against.
        click.echo(f"  Full index needed: {result.fallback_reason}")
        if dry_run:
            click.echo("\nNo changes written (--dry-run).")
            return True
        return False

    if not result.changed_files:
        click.echo("✓ No changes since the graph was built — index is up to date")
        return True

    click.echo(f"→ Detected {len(result.changed_files)} changed "
               f"{_plural(len(result.changed_files), 'file')} since the graph was built")

    if result.fallback_reason:
        # DECISION: an edit that adds, removes or renames a file or a symbol can
        # change how unchanged files resolve their calls, so --smart replaces
        # the graph with a full index rather than return a graph a full index
        # would not produce.
        click.echo(f"  Full index needed: {result.fallback_reason}")
        if dry_run:
            for path in result.changed_files:
                click.echo(f"  {_rel_display(path, root)}")
            click.echo("\nNo changes written (--dry-run).")
            return True
        return False

    if dry_run:
        click.echo(f"\nWould re-index {len(result.expanded_files)} "
                   f"{_plural(len(result.expanded_files), 'file')}:")
        for path in result.expanded_files:
            click.echo(f"  {_rel_display(path, root)}")
        click.echo("\nNo changes written (--dry-run).")
        return True

    click.echo(f"✓ Updated {result.updated_nodes} nodes · "
               f"{result.updated_edges} edges in {result.elapsed_ms / 1000:.1f}s")
    if result.speedup >= 1.0:
        click.echo(f"  Full index would take: "
                   f"~{result.full_index_ms_estimate / 1000:.1f}s "
                   f"({result.speedup:.0f}× faster)")
    click.echo(f"  Saved: {out}")
    click.echo(f'\nNext: slurp "your query" --graph {out}')
    return True


def _explain_empty_graph(root: Path) -> None:
    """Say why indexing produced 0 nodes, on stderr.

    An empty graph is otherwise indistinguishable from a broken one, and the
    usual cause — every source file under vendor/, dist/ or build/ — has a
    one-line fix.
    """
    from slurp.indexer import _INDEXED_EXTENSIONS, skipped_source_files

    skipped = skipped_source_files(root)
    if skipped:
        listed = ", ".join(f"{d}/ ({n})" for d, n in sorted(skipped.items(),
                                                           key=lambda kv: -kv[1]))
        first = max(skipped, key=lambda d: skipped[d])
        click.echo(
            f"Warning: 0 nodes — {sum(skipped.values())} source files were skipped "
            f"because they sit under directories slurp never indexes: {listed}.\n"
            f"  To index one anyway, make it the root: slurp index {root / first}",
            err=True,
        )
    else:
        exts = " ".join(sorted(_INDEXED_EXTENSIONS))
        click.echo(
            f"Warning: 0 nodes — no source files with an indexable extension under "
            f"{root}.\n  slurp indexes: {exts}",
            err=True,
        )


def _plural(count: int, word: str) -> str:
    return word if count == 1 else f"{word}s"


@cli.command("index")
@click.argument("path", default=".", type=click.Path(exists=True, file_okay=False))
@click.option("--output", "-o", "output_path", default=None, type=click.Path(),
              help="Output path for graph.json (default: <path>/graphify-out/graph.json).")
@click.option("--watch", is_flag=True, default=False,
              help="Keep the graph current until Ctrl+C: polls every second, refreshes "
                   "after a quiet second.")
@click.option("--ignore-file", "ignore_file", default=".slurpignore", show_default=True,
              help="Path to .slurpignore rules.")
@click.option("--smart", is_flag=True, default=False,
              help="Re-index only files changed since the graph was built. Falls back to a full "
                   "index when a file or symbol is added, removed or renamed.")
@click.option("--dry-run", is_flag=True, default=False,
              help="With --smart, list the files that would be re-indexed and exit.")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite the output even if another tool (e.g. graphify) wrote it.")
def index_cmd(
    path: str,
    output_path: str | None,
    watch: bool,
    ignore_file: str,
    smart: bool,
    dry_run: bool,
    force: bool,
) -> None:
    """Generate graph.json by statically indexing the project.

    No graphify or LLM required. Supports Python (ast), TypeScript/JS
    (tree-sitter or regex), and Go (regex).
    """
    from slurp.graphfile import GraphChangedError, GraphLock, graph_identity, write_graph
    from slurp.indexer import (
        format_parser_summary,
        iter_source_files,
        parser_summary,
    )
    from slurp.refresh import index_with_manifest

    root = Path(path).resolve()
    out = Path(output_path) if output_path else root / "graphify-out" / "graph.json"
    ignore_path = Path(ignore_file)
    ignore = load_ignore(ignore_path)

    def _overtaken() -> click.ClickException:
        return click.ClickException(
            f"{out} changed while slurp was indexing — another tool wrote it. "
            "Nothing was written; run `slurp index` again.")

    def _run() -> None:
        click.echo(f"Indexing {root} ...")
        expected = graph_identity(out)
        graph = index_with_manifest(root, out, ignore_path)
        n_nodes = len(graph["nodes"])
        n_edges = len(graph["links"])
        files_processed = len({n["source_file"] for n in graph["nodes"] if n.get("source_file")})
        try:
            write_graph(out, graph, expected=expected)
        except GraphChangedError:
            raise _overtaken() from None
        click.echo(f"✓ {n_nodes} nodes · {n_edges} edges · {files_processed} files")
        if not n_nodes:
            _explain_empty_graph(root)

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

    if dry_run and not smart:
        raise click.ClickException("--dry-run only applies with --smart.")

    if dry_run:
        _run_smart(root, out, ignore, dry_run=True, ignore_path=ignore_path)
        return

    foreign = _is_foreign_graph(out)
    if foreign and not force:
        _confirm_replacing_foreign_graph(
            out, f"slurp index {path} -o {out.parent / 'slurp-graph.json'}")
    if foreign and smart:
        # --smart edits the existing graph in place; merging slurp's nodes into
        # another tool's graph would produce neither graph. Replace it whole.
        click.echo("  Existing graph was not written by slurp — running a full "
                   "index instead of --smart")

    # DECISION: the lock saves two slurp processes from indexing the same
    # project at once (the MCP server refreshing while this runs); it is held
    # for one write at a time, so `--watch` never keeps the server out.
    lock = GraphLock(out)
    lock.acquire(on_wait=lambda: click.echo(
        f"waiting for another slurp process to finish writing {out}…", err=True))
    try:
        if not foreign and _is_foreign_graph(out) and not force:
            raise _overtaken()
        # _run_smart returns False when it could not run incrementally (no git
        # repo, no existing graph); the reason is reported and a full index
        # takes over.
        try:
            done = (not foreign and smart
                    and _run_smart(root, out, ignore, dry_run=False, ignore_path=ignore_path))
        except GraphChangedError:
            raise _overtaken() from None
        if not done:
            _run()
    finally:
        lock.release()

    if watch:
        _watch(out)


# Seconds between two looks at the manifest in `slurp index --watch`.
_WATCH_INTERVAL_S = 1.0


def _watch_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _watch(graph_path: Path) -> None:
    """Keep *graph_path* current until Ctrl+C: a poll over its manifest, debounced.

    DECISION (v1.3, design §4): every second the manifest is checked (0.4 ms on
    PrismaStats); a change is acted on only once a whole poll passed with the
    disk as it was, so a save that writes twice or a checkout of a hundred
    files is one refresh. The refresh is the server's — fast path or full
    index, atomic write under the lock — so new, deleted and renamed files and
    .slurpignore are all seen, skipped directories never wake it, and nothing
    depends on inotify, FSEvents or the watchdog package (no longer a
    dependency). The v1.2 watcher re-indexed on every modify event of any
    indexable file, node_modules/ included, and missed creations and renames.
    """
    import time

    from slurp.refresh import GraphTracker

    try:
        tracker = GraphTracker(graph_path)
    except SlurpLoadError as exc:
        raise click.ClickException(str(exc)) from None
    if not tracker.refreshable:
        raise click.ClickException(f"--watch keeps slurp's own graphs current; {graph_path} "
                                   f"is not one ({tracker.note or tracker.kind}).")
    click.echo(f"\nWatching {tracker.root} for changes (Ctrl+C to stop)...")
    pending = None
    try:
        while True:
            _watch_sleep(_WATCH_INTERVAL_S)
            if tracker.disk_changed():
                tracker.load()          # another slurp wrote it: start from that
                pending = None
                continue
            changes = tracker.check()
            if not changes.any or tracker.failed_signature == changes.signature():
                pending = None
                continue
            if changes.signature() != pending:
                pending = changes.signature()   # still moving: wait for a quiet poll
                continue
            pending = None
            started = time.perf_counter()
            try:
                rebuilt = tracker.rebuild(changes)
            except Exception as exc:
                tracker.failed_signature = changes.signature()
                click.echo(f"✗ refresh failed: {type(exc).__name__}: {exc}", err=True)
                continue
            if rebuilt.reloaded:
                tracker.load()
                continue
            tracker.apply(rebuilt)
            what = changes.describe().replace(" since this graph was built", "")
            how = ("smart update" if rebuilt.way == "smart"
                   else f"full index ({rebuilt.reason})")
            click.echo(f"↻ {what} — {how} in {time.perf_counter() - started:.1f}s")
    except KeyboardInterrupt:
        click.echo("Stopped.")


@cli.command("init")
@click.option("--yes", "-y", is_flag=True, default=False,
              help="Accept every prompt (for CI and scripting).")
@click.option("--keep-graphify", "keep_graphify", is_flag=True, default=False,
              help="With a graph slurp did not build (graphify's), serve that one instead of "
                   "building slurp's own beside it. slurp will not refresh it.")
@click.option("--instructions", "instructions",
              type=click.Choice(["local", "versioned", "none"]), default="local",
              show_default=True,
              help="Where the paragraph telling your agent when to use slurp goes: "
                   "CLAUDE.local.md (local), CLAUDE.md/AGENTS.md (versioned), or nowhere.")
def init(yes: bool, keep_graphify: bool, instructions: str) -> None:
    """Set up slurp in this project — index it and register it with your agent.

    Detects the project language, builds slurp's own graph (kept current before
    every answer), registers slurp as an MCP server in .mcp.json, approves it
    for Claude Code in .claude/settings.local.json and tells the agent when to
    use it. Needs no flags: run `slurp init` from the project root.
    """
    import json as _json

    from slurp import clients
    from slurp.refresh import GraphTracker

    root = Path(".").resolve()
    mcp_path = Path(".mcp.json")

    language, counts = _detect_language(root)
    existing_graph = _find_graph()
    command, install_kind = _detect_slurp_command()

    # DECISION (v1.3, design §5.2, decision 4): slurp's own graph by default,
    # because only that one stays current by itself. A graph slurp built is
    # reused; one it did not build (graphify's) is left as it is and slurp's
    # goes beside it, unless --keep-graphify serves that one, read-only.
    existing_kind = ""
    existing_owner = ""
    if existing_graph is not None:
        try:
            tracker = GraphTracker(existing_graph)
            existing_kind = "own" if tracker.refreshable else "foreign"
            existing_owner = tracker.kind
        except SlurpLoadError:
            existing_kind = "foreign"
    reuse = existing_kind == "own" or (existing_kind == "foreign" and keep_graphify)
    if reuse:
        assert existing_graph is not None
        graph_path = existing_graph.resolve()
    elif existing_kind == "foreign":
        graph_path = root / "graphify-out" / "slurp-graph.json"
    else:
        graph_path = root / "graphify-out" / "graph.json"
    variant = "graphify" if existing_kind == "foreign" and keep_graphify else "own"

    settings_root = clients.git_root(root) or root
    settings_path = settings_root / ".claude" / "settings.local.json"
    instruction_plan = clients.plan_instructions(root, instructions, variant)

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
    if existing_kind == "own":
        plan.append(f"  1. Reuse slurp's graph at [cyan]{existing_graph}[/] "
                    "(refreshed before every answer)")
    elif existing_kind == "foreign" and keep_graphify:
        plan.append(f"  1. Serve the existing graph at [cyan]{existing_graph}[/] — slurp "
                    "won't refresh it")
    elif existing_kind == "foreign" and existing_graph is not None \
            and existing_graph.resolve() == graph_path.resolve():
        # A slurp ≤ 1.2 graph whose root did not validate (another tool's graph
        # here stops init before anything is written, below).
        plan.append(f"  1. Rebuild slurp's graph at [cyan]{_rel_display(graph_path, root)}[/] "
                    "(refreshed before every answer)")
        plan.append("     The one there was written by slurp ≤ 1.2 and fewer than nine in ten "
                    "of its files exist here.")
    elif existing_kind == "foreign":
        plan.append(f"  1. Build slurp's own graph at [cyan]{_rel_display(graph_path, root)}[/] "
                    "(refreshed before every answer)")
        who = "graphify" if existing_owner == "graphify" else "another tool"
        plan.append(f"     {existing_graph} ({who}'s) stays as it is.")
        plan.append("     [dim]Serve that graph instead (slurp won't refresh it): "
                    "--keep-graphify[/]")
    else:
        plan.append("  1. Index this project into [cyan]graphify-out/graph.json[/] "
                    "(refreshed before every answer)")
    verb = "Update" if mcp_path.exists() else "Create"
    plan.append(f"  2. {verb} [cyan].mcp.json[/] registering slurp as an MCP server")
    plan.append(f"  3. Approve the slurp server and its tools for Claude Code in "
                f"[cyan]{_rel_display(settings_path, root)}[/]")
    if instructions == "none":
        plan.append("  4. No instructions for the agent (--instructions none)")
    else:
        where = ", ".join(_rel_display(p, root) for p in instruction_plan.targets) or "—"
        kind = "local, not versioned" if instructions == "local" else "versioned"
        plan.append(f"  4. Tell the agent when to use slurp in [cyan]{where}[/] ({kind})")
    plan.append("     [dim]Or: --instructions versioned (CLAUDE.md/AGENTS.md) | none[/]")
    plan.append("  5. Keep .slurp/, settings.local.json, CLAUDE.local.md out of git "
                "[dim](.gitignore)[/]")
    plan.append("")
    plan.append(f"[dim]slurp binary: {command}  ({install_kind})[/]")

    _echo_rich(Panel("\n".join(plan), title="slurp init", border_style="cyan", padding=(1, 2)))

    if language is None:
        click.echo(
            "Warning: no indexable source files found — the graph would be empty.\n"
            "         Run this from your project root."
        )

    if not yes and not click.confirm("Proceed?", default=True):
        click.echo("Cancelled — nothing was changed. Re-run with --keep-graphify or "
                   "--instructions local|versioned|none to change the plan.")
        raise click.Abort()

    # --- Step 1: graph ------------------------------------------------------
    if not reuse and _is_foreign_graph(graph_path):
        # Another tool's graph where slurp's would go: never replaced unasked.
        if yes:
            raise click.ClickException(
                f"{graph_path} holds a graph slurp did not write. Nothing was changed; "
                "move it, or re-run with --keep-graphify to serve it as it is.")
        _confirm_replacing_foreign_graph(graph_path, "re-run with --keep-graphify")
    if reuse:
        data = _json.loads(graph_path.read_text(encoding="utf-8"))
        n_nodes = len(data.get("nodes", []))
        n_edges = len(data.get("links", data.get("edges", [])))
        click.echo(f"Using existing graph: {graph_path}")
    else:
        n_nodes, n_edges = _init_index(root, graph_path)

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

    # --- Step 3: Claude Code approvals ---------------------------------------
    settings = clients.merge_claude_settings(settings_path)

    # --- Step 4: the paragraph for the agent ---------------------------------
    written = clients.write_instructions(instruction_plan)

    # --- Step 5: keep local files out of git ---------------------------------
    ignored_now: list[str] = []
    unversioned_risk: list[str] = []
    if _ensure_gitignored(Path(".gitignore")):
        ignored_now.append(".slurp/")
    local_files = [(settings_path, settings_root, ".claude/settings.local.json")]
    local_files += [(p, root, p.name) for p in written if p.name == "CLAUDE.local.md"]
    in_git = clients.git_root(root) is not None
    for path, base, entry in local_files:
        if not in_git or not path.exists():
            continue
        if clients.git_tracked(path):
            unversioned_risk.append(f"{_rel_display(path, root)} is tracked by git — "
                                    f"`git rm --cached {_rel_display(path, root)}` to stop "
                                    "versioning it")
            continue
        if clients.git_ignored(path):
            continue
        if clients.ensure_ignored(base / ".gitignore", entry):
            ignored_now.append(entry)
        else:
            unversioned_risk.append(f"{_rel_display(path, root)} is not ignored by git and "
                                    f"there is no .gitignore to add it to")

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
    summary.append("")
    if settings.invalid:
        summary.append(f"[yellow]{escape(settings.warnings[0])}[/]")
        summary.append(escape(settings.fragment))
    else:
        state = "updated" if settings.written else "already approves slurp"
        summary.append(f"[bold]Claude Code[/]: {escape(_rel_display(settings_path, root))} "
                       f"{state} (enabledMcpjsonServers, permissions.allow mcp__slurp__*)")
        summary.extend(f"[yellow]{escape(w)}[/]" for w in settings.warnings)
    if written:
        summary.append(f"[bold]Instructions[/]: {', '.join(_rel_display(p, root) for p in written)}")
        if any(p.name == "CLAUDE.local.md" for p in written):
            summary.append("[dim]  An ignored CLAUDE.local.md exists only in this worktree; "
                           "run `slurp init` in other worktrees too.[/]")
    summary.extend(f"[yellow]{escape(w)}[/]" for w in instruction_plan.warnings)
    if ignored_now:
        summary.append(f"[bold].gitignore[/]: added {', '.join(ignored_now)}")
    summary.extend(f"[yellow]{escape(w)}[/]" for w in unversioned_risk)

    _echo_rich(Panel("\n".join(summary), title="slurp is ready", border_style="green",
                     padding=(1, 2)))
    click.echo("Other agents (printed, not written — copy what you use):")
    for name, fragment in clients.client_fragments(command, graph_path).items():
        click.echo(f"\n{name}:\n{fragment}")
    click.echo("\nNext: restart your AI coding assistant to activate slurp")


def _init_index(root: Path, graph_path: Path) -> tuple[int, int]:
    """Build slurp's graph for `slurp init`, with a progress bar on a terminal."""
    import sys

    from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn

    from slurp.graphfile import GraphChangedError, GraphLock, graph_identity, write_graph
    from slurp.indexer import iter_source_files
    from slurp.refresh import index_with_manifest

    files = list(iter_source_files(root))
    # DECISION: the live progress bar is disabled off-TTY so piped output
    # and CliRunner captures stay free of ANSI control sequences.
    is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    if not is_tty:
        click.echo(f"Indexing {len(files)} files...")

    with Progress(
        TextColumn("[cyan]Indexing[/]"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("[dim]{task.completed}/{task.total} files[/]"),
        disable=not is_tty,
    ) as progress:
        task = progress.add_task("index", total=len(files))
        lock = GraphLock(graph_path)
        lock.acquire(on_wait=lambda: click.echo(
            f"waiting for another slurp process to finish writing {graph_path}…", err=True))
        try:
            expected = graph_identity(graph_path)
            graph = index_with_manifest(root, graph_path, Path(".slurpignore"),
                                        on_file=lambda _p: progress.advance(task))
            try:
                write_graph(graph_path, graph, expected=expected)
            except GraphChangedError:
                raise click.ClickException(
                    f"{graph_path} changed while slurp was indexing — another tool "
                    "wrote it. Nothing was written; run `slurp init` again.") from None
        finally:
            lock.release()
    return len(graph["nodes"]), len(graph["links"])


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

    # DECISION: no savings column. Savings are what `slurp benchmark` measures —
    # the whole graph printed the same way and counted — and the audit log has
    # no such measurement. Until v1.1.0 this column guessed a fixed cost per node.
    for i, entry in enumerate(recent, 1):
        queries_table.add_row(
            str(i),
            entry.get("timestamp", ""),
            entry.get("query", ""),
            str(entry.get("nodes_selected", "")),
            f"{entry.get('tokens_used', 0):,}",
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
