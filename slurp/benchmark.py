"""Measures real token savings of slurp vs injecting the full graph."""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from io import StringIO

import networkx as nx
from rich.console import Console
from rich.table import Table

from slurp.budget import count_tokens, select_subgraph
from slurp.formatter import format_subgraph
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
    precision: float  # fraction of relevant nodes (score >= threshold) that were selected
    cost_slurp_usd: float = 0.0
    cost_full_usd: float = 0.0


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
    mean_precision: float = 0.0
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


def _full_graph_tokens(G: nx.DiGraph, model: str) -> int:
    """Token count for serializing the entire graph to markdown (baseline)."""
    n = G.number_of_nodes()
    stats = {
        "nodes_selected": n,
        "nodes_total": n,
        "tokens_used": 0,
        "tokens_budget": 1_000_000,
        "coverage_pct": 100.0,
    }
    text = format_subgraph(G, stats, format="markdown", query="")
    return count_tokens(text, model=model)


def run_benchmark(
    G: nx.DiGraph,
    queries: list[str],
    budgets: list[int],
    model: str = "cl100k_base",
    min_score: float = 0.0,
    neighbor_decay: float = 0.7,
    relevance_threshold: float = 0.3,
    backend: str = "tfidf",
    model_price: str = "default",
) -> BenchmarkResult:
    """Run a battery of queries across multiple budgets and measure token savings.

    Args:
        G: Graph to benchmark against.
        queries: Query strings to test.
        budgets: Token budgets to test (one run per query×budget combination).
        model: Tiktoken encoding for token counting.
        min_score: Minimum score filter passed to select_subgraph.
        neighbor_decay: Neighbor decay passed to select_subgraph.
        relevance_threshold: Score cutoff that defines a "relevant" node for
            precision calculation.
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

    tokens_full = _full_graph_tokens(G, model)
    rows: list[QueryBudgetResult] = []

    for query in queries:
        scores = score_nodes(G, query, backend=backend)
        relevant_ids = {nid for nid, s in scores.items() if s >= relevance_threshold}

        for budget in budgets:
            subG, stats = select_subgraph(
                G, scores, budget=budget, model=model,
                neighbor_decay=neighbor_decay, min_score=min_score,
            )
            used = stats["tokens_used"]
            savings = (
                max(0.0, (tokens_full - used) / tokens_full * 100)
                if tokens_full > 0 else 0.0
            )
            selected_set = set(subG.nodes)
            precision = (
                len(selected_set & relevant_ids) / len(relevant_ids)
                if relevant_ids else 1.0
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
                precision=round(precision, 4),
                cost_slurp_usd=_cost_usd(used, price),
                cost_full_usd=_cost_usd(tokens_full, price),
            ))

    if not rows:
        return BenchmarkResult(model_price=model_price)

    sv = [r.savings_pct for r in rows]
    pv = [r.precision for r in rows]

    # DECISION: totals accumulate across every query×budget run, so they answer
    # "what would this benchmark's workload have cost" — not per-query cost.
    total_slurp = sum(r.cost_slurp_usd for r in rows)
    total_full = sum(r.cost_full_usd for r in rows)

    return BenchmarkResult(
        rows=rows,
        mean_savings_pct=round(statistics.mean(sv), 1),
        p50_savings_pct=round(_percentile(sv, 50), 1),
        p90_savings_pct=round(_percentile(sv, 90), 1),
        p95_savings_pct=round(_percentile(sv, 95), 1),
        best_case=max(rows, key=lambda r: r.savings_pct),
        worst_case=min(rows, key=lambda r: r.savings_pct),
        mean_precision=round(statistics.mean(pv), 4),
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
    runs_table.add_column("Precision", justify="right")

    for r in result.rows:
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
            f"{r.precision:.2%}",
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
        ("Mean precision", f"{result.mean_precision:.2%}"),
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
    return buf.getvalue()
