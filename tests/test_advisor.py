"""Tests for slurp.advisor — budget recommendations from query history."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from slurp.advisor import (
    BUDGET_PERCENTILE,
    DEFAULT_BUDGET,
    SIMILARITY_THRESHOLD,
    BudgetAdvice,
    _expected_coverage,
    find_similar_queries,
    format_advice,
    query_similarities,
    recommend_budget,
)
from slurp.benchmark import MODEL_PRICES_PER_MTok
from slurp.cli import cli


def _entry(query: str, tokens_used: int, nodes_selected: int = 40,
           nodes_total: int = 2111) -> dict:
    return {
        "timestamp": "2026-08-17T10:00:00",
        "query": query,
        "budget": 4000,
        "tokens_used": tokens_used,
        "nodes_selected": nodes_selected,
        "nodes_total": nodes_total,
        "graph_path": "graph.json",
        "top_nodes": [],
    }


def _write_audit(audit_dir: Path, entries: list[dict]) -> Path:
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / "audit.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return path


def _auth_history(n: int, base_tokens: int = 2600) -> list[dict]:
    """n distinct 'auth flow ...' queries with spread-out token usage."""
    suffixes = [
        "login", "middleware", "token", "session", "jwt", "guard",
        "refresh", "cookie", "oauth", "logout", "scope", "claims",
    ]
    return [
        _entry(f"auth flow {suffixes[i % len(suffixes)]}{i // len(suffixes) or ''}",
               base_tokens + i * 50)
        for i in range(n)
    ]


@pytest.fixture
def audit_dir(tmp_path: Path) -> Path:
    return tmp_path / ".slurp"


class TestNoHistory:
    def test_missing_audit_dir_recommends_default(self, tmp_path):
        advice = recommend_budget("auth flow", audit_dir=tmp_path / "nope")
        assert advice.recommended_budget == DEFAULT_BUDGET
        assert advice.confidence == "NONE"
        assert advice.similar == []

    def test_missing_audit_dir_explains_why(self, tmp_path):
        advice = recommend_budget("auth flow", audit_dir=tmp_path / "nope")
        assert advice.has_history is False
        assert "No query history yet" in advice.note

    def test_history_without_similar_queries(self, audit_dir):
        _write_audit(audit_dir, [_entry("database pool", 5100)])
        advice = recommend_budget("auth flow", audit_dir=audit_dir)
        assert advice.recommended_budget == DEFAULT_BUDGET
        assert advice.confidence == "NONE"
        assert advice.note == "No historical data for this query type"
        assert advice.has_history is True

    def test_empty_audit_file(self, audit_dir):
        _write_audit(audit_dir, [])
        advice = recommend_budget("auth flow", audit_dir=audit_dir)
        assert advice.confidence == "NONE"
        assert advice.recommended_budget == DEFAULT_BUDGET

    def test_corrupt_audit_file_does_not_crash(self, audit_dir):
        audit_dir.mkdir(parents=True)
        (audit_dir / "audit.jsonl").write_text("{not json\n", encoding="utf-8")
        advice = recommend_budget("auth flow", audit_dir=audit_dir)
        assert advice.recommended_budget == DEFAULT_BUDGET

    def test_entries_missing_fields_are_skipped(self, audit_dir):
        _write_audit(audit_dir, [
            {"query": "auth flow login"},          # no tokens_used
            {"tokens_used": 2000},                  # no query
            _entry("", 2000),                       # blank query
        ])
        advice = recommend_budget("auth flow", audit_dir=audit_dir)
        assert advice.similar == []


class TestSimilarity:
    def test_identical_query_scores_one(self):
        assert query_similarities("auth flow", ["auth flow"])[0] == pytest.approx(1.0)

    def test_unrelated_query_scores_zero(self):
        assert query_similarities("auth flow", ["database pool"])[0] == 0.0

    def test_partial_overlap_is_between(self):
        score = query_similarities("auth flow", ["auth flow login"])[0]
        assert 0.0 < score < 1.0

    def test_empty_history_returns_empty(self):
        assert query_similarities("auth flow", []) == []

    def test_tokenizer_matches_scorer_on_case_styles(self):
        """authFlow, auth_flow and 'auth flow' must be the same ask."""
        for variant in ("authFlow", "auth_flow", "AuthFlow", "auth-flow"):
            assert query_similarities("auth flow", [variant])[0] > SIMILARITY_THRESHOLD

    def test_threshold_filters_matches(self, audit_dir):
        entries = [_entry("auth flow login", 2650), _entry("database pool", 5100)]
        matches = find_similar_queries("auth flow", entries)
        assert [m.query for m in matches] == ["auth flow login"]

    def test_matches_sorted_by_similarity(self):
        entries = [_entry("auth flow login extras here", 2650), _entry("auth flow", 2700)]
        matches = find_similar_queries("auth flow", entries)
        assert matches[0].query == "auth flow"
        assert matches[0].similarity >= matches[1].similarity

    def test_custom_threshold_widens_matches(self):
        entries = [_entry("login validation", 2700)]
        assert find_similar_queries("auth flow", entries) == []
        widened = find_similar_queries("auth flow", entries, threshold=0.0)
        assert [m.query for m in widened] == ["login validation"]

    def test_savings_pct_derived_from_nodes_total(self):
        # baseline = 100 nodes * 50 tokens = 5000; used 1000 → 80% saved
        matches = find_similar_queries(
            "auth flow", [_entry("auth flow login", 1000, nodes_total=100)]
        )
        assert matches[0].savings_pct == 80.0

    def test_savings_pct_zero_when_nodes_total_missing(self):
        matches = find_similar_queries(
            "auth flow", [_entry("auth flow login", 1000, nodes_total=0)]
        )
        assert matches[0].savings_pct == 0.0


class TestOptimalBudget:
    def test_uses_p75_of_similar_token_usage(self, audit_dir):
        # Four similar queries at 1000/2000/3000/4000 → p75 = 3250.
        _write_audit(audit_dir, [
            _entry("auth flow login", 1000), _entry("auth flow token", 2000),
            _entry("auth flow jwt", 3000), _entry("auth flow guard", 4000),
        ])
        advice = recommend_budget("auth flow", audit_dir=audit_dir)
        assert advice.similar_count == 4
        assert advice.recommended_budget == 3250

    def test_percentile_constant_is_75(self):
        assert BUDGET_PERCENTILE == 75

    def test_single_similar_query_uses_its_value(self, audit_dir):
        _write_audit(audit_dir, [_entry("auth flow login", 2650)])
        assert recommend_budget("auth flow", audit_dir=audit_dir).recommended_budget == 2650

    def test_dissimilar_entries_excluded_from_calculation(self, audit_dir):
        """A huge unrelated query must not inflate the recommendation."""
        _write_audit(audit_dir, [
            _entry("auth flow login", 1000), _entry("auth flow token", 1000),
            _entry("database pool", 50000),
        ])
        assert recommend_budget("auth flow", audit_dir=audit_dir).recommended_budget == 1000

    def test_budget_is_always_positive(self, audit_dir):
        _write_audit(audit_dir, [_entry("auth flow login", 1)])
        assert recommend_budget("auth flow", audit_dir=audit_dir).recommended_budget >= 1


class TestConfidenceLevels:
    @pytest.mark.parametrize(("count", "expected"), [
        (0, "NONE"), (1, "LOW"), (2, "LOW"), (3, "MEDIUM"),
        (9, "MEDIUM"), (10, "HIGH"), (12, "HIGH"),
    ])
    def test_thresholds(self, audit_dir, count, expected):
        _write_audit(audit_dir, _auth_history(count))
        advice = recommend_budget("auth flow", audit_dir=audit_dir)
        assert advice.similar_count == count
        assert advice.confidence == expected

    def test_low_confidence_still_recommends(self, audit_dir):
        _write_audit(audit_dir, _auth_history(2))
        advice = recommend_budget("auth flow", audit_dir=audit_dir)
        assert advice.confidence == "LOW"
        assert advice.recommended_budget != DEFAULT_BUDGET
        assert "starting point" in advice.note

    def test_min_similar_raises_the_bar(self, audit_dir):
        _write_audit(audit_dir, _auth_history(5))
        assert recommend_budget("auth flow", audit_dir=audit_dir).confidence == "MEDIUM"
        strict = recommend_budget("auth flow", audit_dir=audit_dir, min_similar=8)
        assert strict.confidence == "LOW"
        assert "need 8" in strict.note

    def test_min_similar_does_not_change_the_number(self, audit_dir):
        _write_audit(audit_dir, _auth_history(5))
        relaxed = recommend_budget("auth flow", audit_dir=audit_dir)
        strict = recommend_budget("auth flow", audit_dir=audit_dir, min_similar=8)
        assert relaxed.recommended_budget == strict.recommended_budget


class TestEstimatedCost:
    def test_cost_matches_price_table(self, audit_dir):
        _write_audit(audit_dir, [_entry("auth flow login", 1_000_000)])
        advice = recommend_budget("auth flow", audit_dir=audit_dir,
                                  price_model="claude-sonnet-5")
        assert advice.recommended_budget == 1_000_000
        assert advice.estimated_cost_usd == pytest.approx(
            MODEL_PRICES_PER_MTok["claude-sonnet-5"]
        )

    @pytest.mark.parametrize("model", sorted(MODEL_PRICES_PER_MTok))
    def test_every_known_model_is_accepted(self, audit_dir, model):
        _write_audit(audit_dir, [_entry("auth flow login", 2650)])
        advice = recommend_budget("auth flow", audit_dir=audit_dir, price_model=model)
        assert advice.price_model == model
        assert advice.estimated_cost_usd > 0

    def test_unknown_model_raises(self, audit_dir):
        with pytest.raises(ValueError, match="Unknown pricing model"):
            recommend_budget("auth flow", audit_dir=audit_dir, price_model="gpt-9")

    def test_cheaper_model_costs_less(self, audit_dir):
        _write_audit(audit_dir, [_entry("auth flow login", 100_000)])
        opus = recommend_budget("auth flow", audit_dir=audit_dir,
                                price_model="claude-opus-5")
        mini = recommend_budget("auth flow", audit_dir=audit_dir,
                                price_model="gpt-4o-mini")
        assert mini.estimated_cost_usd < opus.estimated_cost_usd

    def test_no_history_still_prices_the_default(self, tmp_path):
        advice = recommend_budget("auth flow", audit_dir=tmp_path,
                                  price_model="claude-sonnet-5")
        assert advice.estimated_cost_usd == pytest.approx(
            DEFAULT_BUDGET / 1_000_000 * MODEL_PRICES_PER_MTok["claude-sonnet-5"]
        )


class TestExpectedCoverage:
    def test_budget_above_all_usage_is_full_coverage(self):
        similar = find_similar_queries(
            "auth flow", [_entry("auth flow login", 1000), _entry("auth flow jwt", 2000)]
        )
        assert _expected_coverage(5000, similar) == 100.0

    def test_budget_below_usage_is_partial(self):
        similar = find_similar_queries("auth flow", [_entry("auth flow login", 1000)])
        assert _expected_coverage(500, similar) == 50.0

    def test_no_similar_queries_is_zero(self):
        assert _expected_coverage(4000, []) == 0.0

    def test_p75_budget_covers_most_of_the_history(self, audit_dir):
        _write_audit(audit_dir, _auth_history(8))
        advice = recommend_budget("auth flow", audit_dir=audit_dir)
        assert advice.expected_coverage_pct >= 90.0


class TestOutputFormat:
    def _advice(self, audit_dir: Path, n: int = 12) -> BudgetAdvice:
        _write_audit(audit_dir, _auth_history(n))
        return recommend_budget("auth flow", audit_dir=audit_dir,
                                price_model="claude-sonnet-5")

    def test_contains_all_headline_fields(self, audit_dir):
        out = format_advice(self._advice(audit_dir), Path("graph.json"))
        for label in ("Recommended budget:", "Expected coverage:",
                      "Estimated cost:", "Confidence:"):
            assert label in out

    def test_budget_uses_thousands_separator(self, audit_dir):
        out = format_advice(self._advice(audit_dir), Path("graph.json"))
        assert "tokens" in out
        assert any(f"{c}," in out for c in "123456789")

    def test_lists_similar_queries_with_savings(self, audit_dir):
        out = format_advice(self._advice(audit_dir), Path("graph.json"))
        assert "Similar past queries:" in out
        assert "→" in out
        assert "% savings" in out

    def test_caps_the_similar_list(self, audit_dir):
        out = format_advice(self._advice(audit_dir, n=12), Path("graph.json"))
        assert "… and 7 more" in out

    def test_runnable_command_carries_budget_and_graph(self, audit_dir):
        advice = self._advice(audit_dir)
        out = format_advice(advice, Path("graph.json"))
        assert "Run with recommended budget:" in out
        assert f'slurp "auth flow" --graph graph.json --budget {advice.recommended_budget}' in out

    def test_command_omits_graph_when_unknown(self, audit_dir):
        out = format_advice(self._advice(audit_dir), None)
        assert "--graph" not in out
        assert 'slurp "auth flow" --budget' in out

    def test_no_history_output_omits_similar_section(self, tmp_path):
        advice = recommend_budget("auth flow", audit_dir=tmp_path)
        out = format_advice(advice, Path("graph.json"))
        assert "Similar past queries:" not in out
        assert "Expected coverage:" not in out
        assert f"{DEFAULT_BUDGET:,} tokens" in out

    def test_low_confidence_note_is_shown(self, audit_dir):
        _write_audit(audit_dir, _auth_history(2))
        out = format_advice(recommend_budget("auth flow", audit_dir=audit_dir), None)
        assert "Note:" in out

    def test_singular_wording_for_one_query(self, audit_dir):
        _write_audit(audit_dir, _auth_history(1))
        out = format_advice(recommend_budget("auth flow", audit_dir=audit_dir), None)
        assert "(1 similar query)" in out


class TestAdvisorCommand:
    def test_runs_and_shows_panel(self, tmp_path):
        _write_audit(tmp_path / ".slurp", _auth_history(12))
        res = CliRunner().invoke(cli, [
            "advisor", "auth flow", "--graph", "graph.json",
            "--audit-dir", str(tmp_path / ".slurp"),
        ])
        assert res.exit_code == 0, res.output
        assert 'Budget Advisor — "auth flow"' in res.output
        assert "Based on 12 similar past queries" in res.output
        assert "Confidence:          HIGH" in res.output

    def test_no_history_message(self, tmp_path):
        res = CliRunner().invoke(cli, [
            "advisor", "auth flow", "--audit-dir", str(tmp_path / "nothing"),
        ])
        assert res.exit_code == 0, res.output
        assert "No query history yet" in res.output
        assert f"{DEFAULT_BUDGET:,} tokens" in res.output

    def test_no_similar_message(self, tmp_path):
        _write_audit(tmp_path / ".slurp", [_entry("database pool", 5100)])
        res = CliRunner().invoke(cli, [
            "advisor", "auth flow", "--audit-dir", str(tmp_path / ".slurp"),
        ])
        assert "No historical data for this query type" in res.output

    def test_price_model_flag(self, tmp_path):
        _write_audit(tmp_path / ".slurp", _auth_history(5))
        res = CliRunner().invoke(cli, [
            "advisor", "auth flow", "--audit-dir", str(tmp_path / ".slurp"),
            "--price-model", "gpt-4o-mini",
        ])
        assert "(gpt-4o-mini)" in res.output

    def test_unknown_price_model_errors_cleanly(self, tmp_path):
        res = CliRunner().invoke(cli, [
            "advisor", "auth flow", "--audit-dir", str(tmp_path),
            "--price-model", "nope",
        ])
        assert res.exit_code != 0
        assert "Unknown pricing model" in res.output

    def test_min_similar_flag(self, tmp_path):
        _write_audit(tmp_path / ".slurp", _auth_history(5))
        res = CliRunner().invoke(cli, [
            "advisor", "auth flow", "--audit-dir", str(tmp_path / ".slurp"),
            "--min-similar", "8",
        ])
        assert "Confidence:          LOW" in res.output

    def test_help_lists_flags(self):
        res = CliRunner().invoke(cli, ["advisor", "--help"])
        for flag in ("--graph", "--audit-dir", "--price-model", "--min-similar"):
            assert flag in res.output

    def test_appears_in_top_level_help(self):
        res = CliRunner().invoke(cli, ["--help"])
        assert "advisor" in res.output
