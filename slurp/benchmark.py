"""Measures real token savings of slurp vs injecting the full graph."""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from io import StringIO

import networkx as nx
from rich.console import Console
from rich.table import Table

from slurp.budget import (
    DEFAULT_MIN_SCORE,
    BudgetTooSmallError,
    build_context,
    full_graph_tokens,
)
from slurp.scorer import score_nodes

# DECISION: input-token list prices in USD per million tokens. This is a static
# snapshot for cost estimation only — slurp never calls these APIs. Prices drift,
# so treat the output as an estimate and refresh this table when they change.
MODEL_PRICES_PER_MTok: dict[str, float] = {
    "claude-opus-5": 5.0,
    "claude-sonnet-5": 3.0,
    "claude-haiku-4-5": 1.0,
    "gpt-4o": 5.0,
    "gpt-4o-mini": 0.15,
    "gpt-4.1": 2.0,
    "gemini-2.0-flash": 0.10,
    "default": 3.0,
}


def price_per_mtok(model_price: str) -> float:
    """Look up the input-token price for a pricing model.

    Args:
        model_price: Key into MODEL_PRICES_PER_MTok.

    Returns:
        USD per million input tokens.

    Raises:
        ValueError: If the model is not in the price table.
    """
    try:
        return MODEL_PRICES_PER_MTok[model_price]
    except KeyError:
        known = ", ".join(sorted(MODEL_PRICES_PER_MTok))
        raise ValueError(
            f"Unknown pricing model {model_price!r}. Known models: {known}"
        ) from None


def _cost_usd(tokens: int, price: float) -> float:
    """Cost in USD of *tokens* input tokens at *price* per million."""
    return tokens / 1_000_000 * price


@dataclass
class QueryBudgetResult:
    """Metrics for a single (query, budget) combination."""

    query: str
    budget: int
    tokens_slurp: int
    tokens_full: int
    savings_pct: float
    nodes_selected: int
    nodes_total: int
    coverage_pct: float
    cost_slurp_usd: float = 0.0
    cost_full_usd: float = 0.0
    # Set when the budget cannot hold even the header; the row is then left
    # out of every aggregate rather than counted as zero tokens.
    error: str = ""


@dataclass
class BenchmarkResult:
    """Aggregated results of a full benchmark run."""

    rows: list[QueryBudgetResult] = field(default_factory=list)
    mean_savings_pct: float = 0.0
    p50_savings_pct: float = 0.0
    p90_savings_pct: float = 0.0
    p95_savings_pct: float = 0.0
    best_case: QueryBudgetResult | None = None
    worst_case: QueryBudgetResult | None = None
    total_cost_slurp_usd: float = 0.0
    total_cost_full_usd: float = 0.0
    total_savings_usd: float = 0.0
    model_price: str = "default"

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict."""
        d = asdict(self)
        return d


def _percentile(data: list[float], p: float) -> float:
    """Linear-interpolation percentile (0 <= p <= 100)."""
    if not data:
        return 0.0
    s = sorted(data)
    idx = (p / 100.0) * (len(s) - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= len(s):
        return s[-1]
    return s[lo] + (idx - lo) * (s[hi] - s[lo])


def _full_graph_tokens(G: nx.DiGraph, scores: dict[str, float], query: str, model: str,
                       token_factor: float = 1.0) -> int:
    """Tokens of the whole graph sent the same way slurp's answer is sent.

    DECISION: the baseline is measured by the same pipeline as the subgraph —
    same formatter, same header with the same query, same scores — so savings
    compare two texts produced alike. v1.0.7 measured the baseline without the
    query in its header, so a subgraph holding the whole graph came out larger
    than "the whole graph".
    """
    return full_graph_tokens(G, scores, query, model=model, token_factor=token_factor)


def run_benchmark(
    G: nx.DiGraph,
    queries: list[str],
    budgets: list[int],
    model: str = "cl100k_base",
    min_score: float = DEFAULT_MIN_SCORE,
    neighbor_decay: float = 0.7,
    backend: str = "tfidf",
    model_price: str = "default",
    token_factor: float = 1.0,
) -> BenchmarkResult:
    """Run a battery of queries across multiple budgets and measure token savings.

    Args:
        G: Graph to benchmark against.
        queries: Query strings to test.
        budgets: Token budgets to test (one run per query×budget combination).
        model: Tiktoken encoding for token counting.
        min_score: Minimum score filter passed to select_subgraph.
        neighbor_decay: Neighbor decay passed to select_subgraph.
        backend: Scoring backend — 'tfidf' (default), 'openai', or 'anthropic'.
        model_price: Key into MODEL_PRICES_PER_MTok used to price token counts.

    Returns:
        BenchmarkResult with per-row data and aggregate statistics.

    Raises:
        ValueError: If model_price is not in the price table.
    """
    price = price_per_mtok(model_price)

    if not queries or not budgets:
        return BenchmarkResult(model_price=model_price)

    rows: list[QueryBudgetResult] = []

    for query in queries:
        scores = score_nodes(G, query, backend=backend)
        tokens_full = _full_graph_tokens(G, scores, query, model, token_factor)

        for budget in budgets:
            try:
                _text, subG, stats = build_context(
                    G, scores, budget=budget, model=model,
                    neighbor_decay=neighbor_decay, min_score=min_score, query=query,
                    token_factor=token_factor,
                )
            except BudgetTooSmallError as exc:
                rows.append(QueryBudgetResult(
                    query=query, budget=budget, tokens_slurp=0, tokens_full=tokens_full,
                    savings_pct=0.0, nodes_selected=0, nodes_total=G.number_of_nodes(),
                    coverage_pct=0.0, error=str(exc)))
                continue
            # The real size of the text sent, measured — no longer an estimate.
            used = stats["tokens_used"]
            savings = (
                max(0.0, (tokens_full - used) / tokens_full * 100)
                if tokens_full > 0 else 0.0
            )
            rows.append(QueryBudgetResult(
                query=query,
                budget=budget,
                tokens_slurp=used,
                tokens_full=tokens_full,
                savings_pct=round(savings, 1),
                nodes_selected=stats["nodes_selected"],
                nodes_total=stats["nodes_total"],
                coverage_pct=stats["coverage_pct"],
                cost_slurp_usd=_cost_usd(used, price),
                cost_full_usd=_cost_usd(tokens_full, price),
            ))

    measured = [r for r in rows if not r.error]
    if not measured:
        return BenchmarkResult(rows=rows, model_price=model_price)

    sv = [r.savings_pct for r in measured]

    # DECISION: totals accumulate across every query×budget run, so they answer
    # "what would this benchmark's workload have cost" — not per-query cost.
    total_slurp = sum(r.cost_slurp_usd for r in measured)
    total_full = sum(r.cost_full_usd for r in measured)

    return BenchmarkResult(
        rows=rows,
        mean_savings_pct=round(statistics.mean(sv), 1),
        p50_savings_pct=round(_percentile(sv, 50), 1),
        p90_savings_pct=round(_percentile(sv, 90), 1),
        p95_savings_pct=round(_percentile(sv, 95), 1),
        best_case=max(measured, key=lambda r: r.savings_pct),
        worst_case=min(measured, key=lambda r: r.savings_pct),
        total_cost_slurp_usd=total_slurp,
        total_cost_full_usd=total_full,
        total_savings_usd=total_full - total_slurp,
        model_price=model_price,
    )


def _savings_pct(result: BenchmarkResult) -> float:
    """Percentage of the full-graph cost saved by slurp."""
    if result.total_cost_full_usd <= 0:
        return 0.0
    return result.total_savings_usd / result.total_cost_full_usd * 100


def format_benchmark(result: BenchmarkResult) -> str:
    """Render a BenchmarkResult as rich tables.

    Args:
        result: BenchmarkResult from run_benchmark.

    Returns:
        Plain-text string with a per-run table and an aggregate summary.
    """
    if not result.rows:
        return "No benchmark results."
    if result.best_case is None:
        return "No benchmark results: every budget was below the header's minimum.\n" + \
            "\n".join(f"  {r.query!r} @ {r.budget:,}: {r.error}" for r in result.rows)

    price = MODEL_PRICES_PER_MTok.get(result.model_price, MODEL_PRICES_PER_MTok["default"])
    runs_table = Table(
        title=f"Benchmark Results — pricing: {result.model_price} (${price:g}/MTok input)",
        show_header=True,
    )
    runs_table.add_column("Query", max_width=24)
    runs_table.add_column("Budget", justify="right")
    runs_table.add_column("Tokens used", justify="right")
    runs_table.add_column("Full graph", justify="right")
    runs_table.add_column("Savings", justify="right")
    runs_table.add_column("Cost (slurp)", justify="right")
    runs_table.add_column("Cost (full)", justify="right")
    runs_table.add_column("Nodes sel.", justify="right")
    runs_table.add_column("Nodes total", justify="right")
    runs_table.add_column("Coverage", justify="right")

    for r in result.rows:
        if r.error:
            runs_table.add_row(r.query, f"{r.budget:,}", "—", f"{r.tokens_full:,}",
                               "skipped", "—", "—", "—", str(r.nodes_total), "—")
            continue
        runs_table.add_row(
            r.query,
            f"{r.budget:,}",
            f"{r.tokens_slurp:,}",
            f"{r.tokens_full:,}",
            f"{r.savings_pct:.1f}%",
            f"${r.cost_slurp_usd:.4f}",
            f"${r.cost_full_usd:.4f}",
            str(r.nodes_selected),
            str(r.nodes_total),
            f"{r.coverage_pct}%",
        )

    summary_table = Table(title="Summary", show_header=True)
    summary_table.add_column("Metric")
    summary_table.add_column("Value", justify="right")

    assert result.best_case is not None
    assert result.worst_case is not None
    summary_rows = [
        ("Mean savings", f"{result.mean_savings_pct:.1f}%"),
        ("p50 savings",  f"{result.p50_savings_pct:.1f}%"),
        ("p90 savings",  f"{result.p90_savings_pct:.1f}%"),
        ("p95 savings",  f"{result.p95_savings_pct:.1f}%"),
        (
            "Best case",
            f"{result.best_case.savings_pct:.1f}% "
            f"({result.best_case.query!r}, budget {result.best_case.budget:,})",
        ),
        (
            "Worst case",
            f"{result.worst_case.savings_pct:.1f}% "
            f"({result.worst_case.query!r}, budget {result.worst_case.budget:,})",
        ),
        ("Cost with slurp", f"${result.total_cost_slurp_usd:.4f}"),
        ("Cost without slurp", f"${result.total_cost_full_usd:.4f}"),
        (
            "Money saved",
            f"${result.total_savings_usd:.4f} ({_savings_pct(result):.1f}%)",
        ),
    ]
    for metric, value in summary_rows:
        summary_table.add_row(metric, value)

    buf = StringIO()
    # DECISION: 150 columns, not 120 — the two cost columns push the runs table
    # past 120, and rich silently drops columns that don't fit rather than wrapping.
    Console(file=buf, no_color=True, width=150).print(runs_table, summary_table)
    # DECISION: no quality figure here. The former "precision" compared the
    # selection with the scorer's own idea of a relevant node (score >= 0.3) —
    # agreement between two halves of slurp, and on `slurp index` graphs, where
    # every node scores >= 0.3, just the coverage again. Retrieval quality is
    # measured against labelled nodes by `slurp eval --retrieval`.
    buf.write("Retrieval quality is not measured here: see `slurp eval --retrieval`.\n")
    skipped = [r for r in result.rows if r.error]
    if skipped:
        buf.write(f"\n{len(skipped)} run(s) skipped: {skipped[0].error}\n")
    return buf.getvalue()
