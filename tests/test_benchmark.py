"""Tests for slurp/benchmark.py."""

from __future__ import annotations

import json
import re

import networkx as nx
import pytest
from click.testing import CliRunner

from slurp.benchmark import (
    MODEL_PRICES_PER_MTok,
    BenchmarkResult,
    QueryBudgetResult,
    _cost_usd,
    _full_graph_tokens,
    _percentile,
    _savings_pct,
    format_benchmark,
    price_per_mtok,
    run_benchmark,
)
from slurp.cli import cli


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def simple_graph() -> nx.DiGraph:
    """Small graph with known structure for deterministic tests."""
    G = nx.DiGraph()
    nodes = [
        ("auth",    {"label": "authenticate_user", "type": "function",
                     "description": "Validates user credentials", "file_path": "src/auth.py"}),
        ("jwt",     {"label": "generate_token", "type": "function",
                     "description": "Creates JWT token", "file_path": "src/jwt.py"}),
        ("user",    {"label": "UserModel", "type": "class",
                     "description": "Database model for users", "file_path": "src/models.py"}),
        ("db",      {"label": "DatabasePool", "type": "class",
                     "description": "Manages connection pool", "file_path": "src/db.py"}),
        ("payment", {"label": "PaymentService", "type": "class",
                     "description": "Handles Stripe payments", "file_path": "src/payments.py"}),
    ]
    for nid, attrs in nodes:
        G.add_node(nid, **attrs)
    G.add_edge("auth", "jwt",  relation="calls")
    G.add_edge("auth", "user", relation="depends_on")
    G.add_edge("user", "db",   relation="depends_on")
    return G


@pytest.fixture
def single_query() -> list[str]:
    return ["auth flow"]


@pytest.fixture
def multi_queries() -> list[str]:
    return ["auth flow", "database", "payment"]


@pytest.fixture
def single_budget() -> list[int]:
    return [4000]


@pytest.fixture
def multi_budgets() -> list[int]:
    return [1000, 4000, 8000]


# ---------------------------------------------------------------------------
# _percentile
# ---------------------------------------------------------------------------

class TestPercentile:
    def test_empty_returns_zero(self):
        assert _percentile([], 50) == 0.0

    def test_single_value(self):
        assert _percentile([42.0], 50) == 42.0
        assert _percentile([42.0], 0)  == 42.0
        assert _percentile([42.0], 100) == 42.0

    def test_p0_is_minimum(self):
        assert _percentile([10.0, 20.0, 30.0], 0) == 10.0

    def test_p100_is_maximum(self):
        assert _percentile([10.0, 20.0, 30.0], 100) == 30.0

    def test_p50_median_odd(self):
        assert _percentile([10.0, 20.0, 30.0], 50) == 20.0

    def test_p50_median_even(self):
        result = _percentile([10.0, 20.0, 30.0, 40.0], 50)
        assert result == pytest.approx(25.0)

    def test_p90_interpolated(self):
        data = list(range(1, 11))  # 1..10
        result = _percentile([float(x) for x in data], 90)
        assert result == pytest.approx(9.1, abs=0.2)

    def test_unsorted_input_still_correct(self):
        assert _percentile([30.0, 10.0, 20.0], 50) == 20.0


# ---------------------------------------------------------------------------
# _full_graph_tokens
# ---------------------------------------------------------------------------

class TestFullGraphTokens:
    def test_returns_positive_int(self, simple_graph):
        count = _full_graph_tokens(simple_graph, "cl100k_base")
        assert isinstance(count, int)
        assert count > 0

    def test_larger_graph_more_tokens(self, simple_graph):
        G_small = nx.DiGraph()
        G_small.add_node("a", label="A", description="short")
        small = _full_graph_tokens(G_small, "cl100k_base")
        full  = _full_graph_tokens(simple_graph, "cl100k_base")
        assert full > small

    def test_same_graph_same_count(self, simple_graph):
        c1 = _full_graph_tokens(simple_graph, "cl100k_base")
        c2 = _full_graph_tokens(simple_graph, "cl100k_base")
        assert c1 == c2


# ---------------------------------------------------------------------------
# QueryBudgetResult dataclass
# ---------------------------------------------------------------------------

class TestQueryBudgetResult:
    def test_instantiation(self):
        r = QueryBudgetResult(
            query="auth", budget=4000,
            tokens_slurp=200, tokens_full=1000,
            savings_pct=80.0, nodes_selected=3, nodes_total=10,
            coverage_pct=30.0, precision=0.75,
        )
        assert r.query == "auth"
        assert r.savings_pct == 80.0
        assert r.precision == 0.75


# ---------------------------------------------------------------------------
# BenchmarkResult dataclass
# ---------------------------------------------------------------------------

class TestBenchmarkResult:
    def test_default_empty(self):
        result = BenchmarkResult()
        assert result.rows == []
        assert result.best_case is None
        assert result.worst_case is None

    def test_to_dict_serializable(self, simple_graph, single_query, single_budget):
        result = run_benchmark(simple_graph, single_query, single_budget)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "rows" in d
        assert "mean_savings_pct" in d

    def test_to_dict_json_serializable(self, simple_graph, single_query, single_budget):
        import json
        result = run_benchmark(simple_graph, single_query, single_budget)
        # Should not raise
        json.dumps(result.to_dict())


# ---------------------------------------------------------------------------
# run_benchmark — basic correctness
# ---------------------------------------------------------------------------

class TestRunBenchmarkBasics:
    def test_row_count_equals_queries_times_budgets(
        self, simple_graph, multi_queries, multi_budgets
    ):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        assert len(result.rows) == len(multi_queries) * len(multi_budgets)

    def test_single_query_single_budget_one_row(
        self, simple_graph, single_query, single_budget
    ):
        result = run_benchmark(simple_graph, single_query, single_budget)
        assert len(result.rows) == 1

    def test_tokens_slurp_lte_tokens_full(
        self, simple_graph, multi_queries, multi_budgets
    ):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        for r in result.rows:
            assert r.tokens_slurp <= r.tokens_full, (
                f"{r.query} budget={r.budget}: slurp={r.tokens_slurp} > full={r.tokens_full}"
            )

    def test_tokens_full_same_for_all_rows(
        self, simple_graph, multi_queries, multi_budgets
    ):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        full_counts = {r.tokens_full for r in result.rows}
        assert len(full_counts) == 1

    def test_savings_pct_in_valid_range(
        self, simple_graph, multi_queries, multi_budgets
    ):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        for r in result.rows:
            assert 0.0 <= r.savings_pct <= 100.0, f"savings_pct={r.savings_pct} out of range"

    def test_precision_in_valid_range(
        self, simple_graph, multi_queries, multi_budgets
    ):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        for r in result.rows:
            assert 0.0 <= r.precision <= 1.0, f"precision={r.precision} out of range"

    def test_nodes_selected_lte_nodes_total(
        self, simple_graph, multi_queries, multi_budgets
    ):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        for r in result.rows:
            assert r.nodes_selected <= r.nodes_total

    def test_query_and_budget_recorded_correctly(
        self, simple_graph
    ):
        result = run_benchmark(simple_graph, ["auth"], [2000])
        assert result.rows[0].query == "auth"
        assert result.rows[0].budget == 2000

    def test_larger_budget_selects_more_or_equal_nodes(
        self, simple_graph, single_query
    ):
        result = run_benchmark(simple_graph, single_query, [500, 4000], min_score=0.0)
        small = next(r for r in result.rows if r.budget == 500)
        large = next(r for r in result.rows if r.budget == 4000)
        assert large.nodes_selected >= small.nodes_selected

    def test_larger_budget_has_lower_or_equal_savings(
        self, simple_graph, single_query
    ):
        result = run_benchmark(simple_graph, single_query, [500, 4000], min_score=0.0)
        small = next(r for r in result.rows if r.budget == 500)
        large = next(r for r in result.rows if r.budget == 4000)
        assert large.savings_pct <= small.savings_pct


# ---------------------------------------------------------------------------
# run_benchmark — edge cases
# ---------------------------------------------------------------------------

class TestRunBenchmarkEdgeCases:
    def test_empty_queries_returns_empty_result(self, simple_graph, single_budget):
        result = run_benchmark(simple_graph, [], single_budget)
        assert result.rows == []
        assert result.best_case is None

    def test_empty_budgets_returns_empty_result(self, simple_graph, single_query):
        result = run_benchmark(simple_graph, single_query, [])
        assert result.rows == []

    def test_very_small_budget_still_produces_row(self, simple_graph):
        result = run_benchmark(simple_graph, ["auth"], [10])
        assert len(result.rows) == 1
        # May select 0 nodes if nothing fits; savings should still be valid
        assert 0.0 <= result.rows[0].savings_pct <= 100.0

    def test_huge_budget_selects_all_nodes(self, simple_graph):
        result = run_benchmark(simple_graph, ["auth"], [1_000_000], min_score=0.0)
        assert result.rows[0].nodes_selected == result.rows[0].nodes_total


# ---------------------------------------------------------------------------
# run_benchmark — aggregate statistics
# ---------------------------------------------------------------------------

class TestRunBenchmarkAggregates:
    def test_best_case_has_max_savings(
        self, simple_graph, multi_queries, multi_budgets
    ):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        max_savings = max(r.savings_pct for r in result.rows)
        assert result.best_case is not None
        assert result.best_case.savings_pct == max_savings

    def test_worst_case_has_min_savings(
        self, simple_graph, multi_queries, multi_budgets
    ):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        min_savings = min(r.savings_pct for r in result.rows)
        assert result.worst_case is not None
        assert result.worst_case.savings_pct == min_savings

    def test_mean_savings_matches_manual_calculation(
        self, simple_graph, multi_queries, multi_budgets
    ):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        manual_mean = round(
            sum(r.savings_pct for r in result.rows) / len(result.rows), 1
        )
        assert result.mean_savings_pct == manual_mean

    def test_p50_between_worst_and_best(
        self, simple_graph, multi_queries, multi_budgets
    ):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        assert result.worst_case.savings_pct <= result.p50_savings_pct <= result.best_case.savings_pct

    def test_p90_gte_p50(self, simple_graph, multi_queries, multi_budgets):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        assert result.p90_savings_pct >= result.p50_savings_pct

    def test_p95_gte_p90(self, simple_graph, multi_queries, multi_budgets):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        assert result.p95_savings_pct >= result.p90_savings_pct

    def test_mean_precision_in_range(
        self, simple_graph, multi_queries, multi_budgets
    ):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        assert 0.0 <= result.mean_precision <= 1.0

    def test_single_row_mean_equals_that_row(self, simple_graph):
        result = run_benchmark(simple_graph, ["auth"], [4000])
        assert result.mean_savings_pct == result.rows[0].savings_pct
        assert result.best_case is result.worst_case or (
            result.best_case.savings_pct == result.worst_case.savings_pct
        )


# ---------------------------------------------------------------------------
# format_benchmark
# ---------------------------------------------------------------------------

class TestFormatBenchmark:
    def test_empty_result_returns_no_results_message(self):
        result = BenchmarkResult()
        assert format_benchmark(result) == "No benchmark results."

    def test_contains_benchmark_results_header(
        self, simple_graph, single_query, single_budget
    ):
        result = run_benchmark(simple_graph, single_query, single_budget)
        output = format_benchmark(result)
        assert "Benchmark Results" in output

    def test_contains_summary_header(
        self, simple_graph, single_query, single_budget
    ):
        result = run_benchmark(simple_graph, single_query, single_budget)
        output = format_benchmark(result)
        assert "Summary" in output

    def test_contains_query_text(self, simple_graph, single_query, single_budget):
        result = run_benchmark(simple_graph, single_query, single_budget)
        output = format_benchmark(result)
        assert "auth flow" in output

    def test_contains_budget_value(self, simple_graph, single_query, single_budget):
        result = run_benchmark(simple_graph, single_query, single_budget)
        output = format_benchmark(result)
        assert "4,000" in output

    def test_contains_savings_column(self, simple_graph, single_query, single_budget):
        result = run_benchmark(simple_graph, single_query, single_budget)
        output = format_benchmark(result)
        assert "Savings" in output

    def test_contains_precision_column(self, simple_graph, single_query, single_budget):
        result = run_benchmark(simple_graph, single_query, single_budget)
        output = format_benchmark(result)
        assert "Precision" in output

    def test_contains_mean_savings(self, simple_graph, single_query, single_budget):
        result = run_benchmark(simple_graph, single_query, single_budget)
        output = format_benchmark(result)
        assert "Mean savings" in output

    def test_contains_best_and_worst_case(
        self, simple_graph, multi_queries, multi_budgets
    ):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        output = format_benchmark(result)
        assert "Best case" in output
        assert "Worst case" in output

    def test_contains_percentiles(self, simple_graph, multi_queries, multi_budgets):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        output = format_benchmark(result)
        assert "p50" in output
        assert "p90" in output
        assert "p95" in output

    def test_multiple_queries_all_present(self, simple_graph, multi_queries, single_budget):
        result = run_benchmark(simple_graph, multi_queries, single_budget)
        output = format_benchmark(result)
        for q in multi_queries:
            assert q in output

    def test_returns_string(self, simple_graph, single_query, single_budget):
        result = run_benchmark(simple_graph, single_query, single_budget)
        assert isinstance(format_benchmark(result), str)


# ---------------------------------------------------------------------------
# CLI — slurp benchmark
# ---------------------------------------------------------------------------


def _runner() -> CliRunner:
    return CliRunner()


class TestBenchmarkCLI:
    def test_basic_run(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(cli, [
            "benchmark",
            "--graph", str(sample_graph_json),
            "--queries", "auth",
            "--budget", "4000",
        ])
        assert result.exit_code == 0, result.output
        assert "Benchmark Results" in result.output

    def test_multiple_queries_and_budgets(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(cli, [
            "benchmark",
            "--graph", str(sample_graph_json),
            "--queries", "auth",
            "--queries", "database",
            "--budget", "2000",
            "--budget", "4000",
        ])
        assert result.exit_code == 0
        assert "auth" in result.output
        assert "database" in result.output

    def test_output_flag_saves_json(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        out_file = tmp_path / "bench.json"
        result = _runner().invoke(cli, [
            "benchmark",
            "--graph", str(sample_graph_json),
            "--queries", "auth",
            "--budget", "4000",
            "--output", str(out_file),
        ])
        assert result.exit_code == 0
        assert out_file.exists()
        import json
        data = json.loads(out_file.read_text())
        assert "rows" in data
        assert "mean_savings_pct" in data

    def test_output_json_has_correct_row_count(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        out_file = tmp_path / "bench.json"
        _runner().invoke(cli, [
            "benchmark",
            "--graph", str(sample_graph_json),
            "--queries", "auth",
            "--queries", "jwt",
            "--budget", "2000",
            "--budget", "4000",
            "--output", str(out_file),
        ])
        import json
        data = json.loads(out_file.read_text())
        assert len(data["rows"]) == 4  # 2 queries × 2 budgets

    def test_no_graph_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(cli, [
            "benchmark", "--queries", "auth", "--budget", "4000"
        ])
        assert result.exit_code != 0
        assert "No graph.json found" in result.output

    def test_summary_in_output(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(cli, [
            "benchmark",
            "--graph", str(sample_graph_json),
            "--queries", "auth",
            "--budget", "4000",
        ])
        assert result.exit_code == 0
        assert "Summary" in result.output
        assert "Mean savings" in result.output

    def test_savings_percentage_shown(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(cli, [
            "benchmark",
            "--graph", str(sample_graph_json),
            "--queries", "auth",
            "--budget", "4000",
        ])
        assert result.exit_code == 0
        assert "%" in result.output


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


class TestPriceTable:
    def test_default_key_exists(self):
        assert "default" in MODEL_PRICES_PER_MTok

    def test_every_price_is_positive(self):
        assert all(p > 0 for p in MODEL_PRICES_PER_MTok.values())

    @pytest.mark.parametrize("model,expected", [
        ("claude-opus-5", 5.0),
        ("claude-sonnet-5", 3.0),
        ("claude-haiku-4-5", 1.0),
        ("gpt-4o", 5.0),
        ("gpt-4o-mini", 0.15),
        ("gpt-4.1", 2.0),
        ("gemini-2.0-flash", 0.10),
        ("default", 3.0),
    ])
    def test_price_lookup(self, model, expected):
        assert price_per_mtok(model) == expected

    def test_unknown_model_raises(self):
        with pytest.raises(ValueError, match="Unknown pricing model"):
            price_per_mtok("gpt-9-turbo-ultra")

    def test_unknown_model_error_lists_known_models(self):
        with pytest.raises(ValueError, match="claude-sonnet-5"):
            price_per_mtok("nope")


class TestCostCalculation:
    def test_one_million_tokens_costs_exactly_the_price(self):
        assert _cost_usd(1_000_000, 3.0) == pytest.approx(3.0)

    def test_zero_tokens_costs_nothing(self):
        assert _cost_usd(0, 15.0) == 0.0

    def test_cost_scales_linearly(self):
        assert _cost_usd(500_000, 3.0) == pytest.approx(1.5)

    def test_cost_uses_the_selected_model_price(self, simple_graph, single_query, single_budget):
        cheap = run_benchmark(simple_graph, single_query, single_budget,
                              model_price="gemini-2.0-flash")
        pricey = run_benchmark(simple_graph, single_query, single_budget,
                               model_price="claude-opus-5")
        assert pricey.total_cost_slurp_usd > cheap.total_cost_slurp_usd

    def test_cost_ratio_matches_price_ratio(self, simple_graph, single_query, single_budget):
        a = run_benchmark(simple_graph, single_query, single_budget, model_price="gpt-4.1")
        b = run_benchmark(simple_graph, single_query, single_budget, model_price="gpt-4o")
        assert b.total_cost_full_usd / a.total_cost_full_usd == pytest.approx(5.0 / 2.0)


class TestQueryBudgetResultCostFields:
    def test_has_cost_slurp_field(self, simple_graph, single_query, single_budget):
        result = run_benchmark(simple_graph, single_query, single_budget)
        assert hasattr(result.rows[0], "cost_slurp_usd")

    def test_has_cost_full_field(self, simple_graph, single_query, single_budget):
        result = run_benchmark(simple_graph, single_query, single_budget)
        assert hasattr(result.rows[0], "cost_full_usd")

    def test_cost_fields_default_to_zero(self):
        row = QueryBudgetResult(
            query="q", budget=1000, tokens_slurp=10, tokens_full=100,
            savings_pct=90.0, nodes_selected=1, nodes_total=5,
            coverage_pct=20.0, precision=1.0,
        )
        assert row.cost_slurp_usd == 0.0
        assert row.cost_full_usd == 0.0

    def test_row_cost_matches_its_token_count(self, simple_graph, single_query, single_budget):
        result = run_benchmark(simple_graph, single_query, single_budget, model_price="gpt-4o")
        row = result.rows[0]
        assert row.cost_slurp_usd == pytest.approx(_cost_usd(row.tokens_slurp, 5.0))

    def test_full_cost_is_at_least_slurp_cost(self, simple_graph, multi_queries, multi_budgets):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        assert all(r.cost_full_usd >= r.cost_slurp_usd for r in result.rows)


class TestBenchmarkResultCostTotals:
    def test_totals_default_to_zero(self):
        result = BenchmarkResult()
        assert result.total_cost_slurp_usd == 0.0
        assert result.total_cost_full_usd == 0.0
        assert result.total_savings_usd == 0.0

    def test_default_model_price_is_default(self):
        assert BenchmarkResult().model_price == "default"

    def test_totals_sum_the_rows(self, simple_graph, multi_queries, multi_budgets):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        assert result.total_cost_slurp_usd == pytest.approx(
            sum(r.cost_slurp_usd for r in result.rows)
        )
        assert result.total_cost_full_usd == pytest.approx(
            sum(r.cost_full_usd for r in result.rows)
        )

    def test_savings_is_the_difference(self, simple_graph, multi_queries, multi_budgets):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        assert result.total_savings_usd == pytest.approx(
            result.total_cost_full_usd - result.total_cost_slurp_usd
        )

    def test_savings_is_positive(self, simple_graph, multi_queries, multi_budgets):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        assert result.total_savings_usd > 0

    def test_model_price_is_recorded(self, simple_graph, single_query, single_budget):
        result = run_benchmark(simple_graph, single_query, single_budget,
                               model_price="claude-haiku-4-5")
        assert result.model_price == "claude-haiku-4-5"

    def test_costs_serialize_to_dict(self, simple_graph, single_query, single_budget):
        d = run_benchmark(simple_graph, single_query, single_budget).to_dict()
        for key in ("total_cost_slurp_usd", "total_cost_full_usd",
                    "total_savings_usd", "model_price"):
            assert key in d

    def test_empty_run_records_model_price(self, simple_graph):
        result = run_benchmark(simple_graph, [], [], model_price="gpt-4o")
        assert result.model_price == "gpt-4o"
        assert result.total_savings_usd == 0.0

    def test_unknown_price_model_raises_before_running(self, simple_graph, single_query,
                                                       single_budget):
        with pytest.raises(ValueError, match="Unknown pricing model"):
            run_benchmark(simple_graph, single_query, single_budget, model_price="bogus")


class TestFormatBenchmarkCosts:
    def test_shows_cost_slurp_column(self, simple_graph, single_query, single_budget):
        out = format_benchmark(run_benchmark(simple_graph, single_query, single_budget))
        assert "Cost (slurp)" in out

    def test_shows_cost_full_column(self, simple_graph, single_query, single_budget):
        out = format_benchmark(run_benchmark(simple_graph, single_query, single_budget))
        assert "Cost (full)" in out

    def test_costs_are_dollar_formatted(self, simple_graph, single_query, single_budget):
        out = format_benchmark(run_benchmark(simple_graph, single_query, single_budget))
        assert re.search(r"\$\d+\.\d{4}", out)

    def test_summary_has_cost_with_slurp(self, simple_graph, single_query, single_budget):
        out = format_benchmark(run_benchmark(simple_graph, single_query, single_budget))
        assert "Cost with slurp" in out

    def test_summary_has_cost_without_slurp(self, simple_graph, single_query, single_budget):
        out = format_benchmark(run_benchmark(simple_graph, single_query, single_budget))
        assert "Cost without slurp" in out

    def test_summary_has_money_saved(self, simple_graph, single_query, single_budget):
        out = format_benchmark(run_benchmark(simple_graph, single_query, single_budget))
        assert "Money saved" in out

    def test_money_saved_shows_a_percentage(self, simple_graph, single_query, single_budget):
        out = format_benchmark(run_benchmark(simple_graph, single_query, single_budget))
        assert re.search(r"Money saved.*%", out, re.DOTALL)

    def test_header_names_the_pricing_model(self, simple_graph, single_query, single_budget):
        out = format_benchmark(
            run_benchmark(simple_graph, single_query, single_budget,
                          model_price="claude-haiku-4-5")
        )
        assert "claude-haiku-4-5" in out

    def test_header_shows_the_price(self, simple_graph, single_query, single_budget):
        out = format_benchmark(
            run_benchmark(simple_graph, single_query, single_budget, model_price="gpt-4o")
        )
        assert "$5/MTok" in out

    def test_empty_result_still_returns_message(self):
        assert format_benchmark(BenchmarkResult()) == "No benchmark results."

    def test_savings_pct_is_zero_when_no_cost(self):
        assert _savings_pct(BenchmarkResult()) == 0.0

    def test_savings_pct_matches_the_totals(self, simple_graph, multi_queries, multi_budgets):
        result = run_benchmark(simple_graph, multi_queries, multi_budgets)
        expected = result.total_savings_usd / result.total_cost_full_usd * 100
        assert _savings_pct(result) == pytest.approx(expected)


class TestPriceModelCLI:
    def test_price_model_flag_accepted(self, sample_graph_json):
        result = _runner().invoke(cli, [
            "benchmark", "--graph", str(sample_graph_json),
            "-q", "auth", "--budget", "2000", "--price-model", "gpt-4o",
        ])
        assert result.exit_code == 0, result.output

    def test_price_model_appears_in_output(self, sample_graph_json):
        result = _runner().invoke(cli, [
            "benchmark", "--graph", str(sample_graph_json),
            "-q", "auth", "--budget", "2000", "--price-model", "claude-haiku-4-5",
        ])
        assert "claude-haiku-4-5" in result.output

    def test_default_price_model_is_default(self, sample_graph_json):
        result = _runner().invoke(cli, [
            "benchmark", "--graph", str(sample_graph_json),
            "-q", "auth", "--budget", "2000",
        ])
        assert result.exit_code == 0
        assert "pricing: default" in result.output

    def test_invalid_price_model_rejected(self, sample_graph_json):
        result = _runner().invoke(cli, [
            "benchmark", "--graph", str(sample_graph_json),
            "-q", "auth", "--budget", "2000", "--price-model", "gpt-9",
        ])
        assert result.exit_code != 0

    def test_cost_columns_render_in_cli_output(self, sample_graph_json):
        result = _runner().invoke(cli, [
            "benchmark", "--graph", str(sample_graph_json),
            "-q", "auth", "--budget", "2000",
        ])
        assert "Cost (slurp)" in result.output
        assert "Money saved" in result.output

    def test_costs_saved_to_json_output(self, sample_graph_json, tmp_path):
        out_path = tmp_path / "bench.json"
        _runner().invoke(cli, [
            "benchmark", "--graph", str(sample_graph_json),
            "-q", "auth", "--budget", "2000", "--price-model", "gpt-4o",
            "--output", str(out_path),
        ])
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert data["model_price"] == "gpt-4o"
        assert data["total_cost_full_usd"] > 0

    def test_price_model_does_not_shadow_tiktoken_model(self, sample_graph_json):
        """--model stays the tiktoken encoding; --price-model is separate."""
        result = _runner().invoke(cli, [
            "benchmark", "--graph", str(sample_graph_json),
            "-q", "auth", "--budget", "2000",
            "--model", "cl100k_base", "--price-model", "gpt-4o",
        ])
        assert result.exit_code == 0, result.output
        assert "pricing: gpt-4o" in result.output
