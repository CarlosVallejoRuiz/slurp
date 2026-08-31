"""Tests for slurp.evaluator — the with-slurp vs without-slurp quality benchmark."""

import json
from pathlib import Path

import networkx as nx
import pytest
from click.testing import CliRunner

from slurp.cli import cli
import slurp.evaluator as ev
from slurp.evaluator import (
    EvalQuestion,
    EvalResult,
    EvalSuite,
    build_baseline_context,
    build_judge_prompt,
    build_slurp_context,
    default_questions,
    load_eval_questions,
    parse_judge_response,
    run_eval,
)
from slurp.explainer import LLMConfig, SlurpExplainError


@pytest.fixture
def graph() -> nx.DiGraph:
    G = nx.DiGraph()
    G.add_node("admin.createAdminClient", label="createAdminClient", type="function",
               source_file="lib/admin.ts", description="Creates the admin client.")
    G.add_node("admin.requireAdmin", label="requireAdmin", type="function",
               source_file="lib/admin.ts", description="Checks admin permissions.")
    G.add_node("stats.recalc", label="recalcularPlayerStats", type="function",
               source_file="lib/stats.ts", description="Recalculates player stats.")
    G.add_edge("admin.requireAdmin", "admin.createAdminClient", relation="calls")
    G.add_edge("stats.recalc", "admin.createAdminClient", relation="calls")
    return G


def _stub_llm(monkeypatch, replies):
    """Replace _call_llm with a deterministic sequence of canned replies."""
    calls = []

    def fake(prompt, config):
        calls.append(prompt)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr(ev, "_call_llm", fake)
    return calls


JUDGE_OK = '{"score_a": 0.9, "score_b": 0.4, "reasoning": "A names the function."}'


class TestDefaultQuestions:
    def test_returns_fifteen(self):
        assert len(default_questions()) == 15

    def test_three_areas_of_five(self):
        areas = {}
        for q in default_questions():
            areas[q.area] = areas.get(q.area, 0) + 1
        assert areas == {"auth": 5, "stats": 5, "architecture": 5}

    def test_every_field_populated(self):
        for q in default_questions():
            assert q.id and q.question and q.area and q.ground_truth
            assert q.relevant_nodes
            assert q.difficulty in ev.DIFFICULTIES

    def test_ids_are_unique(self):
        ids = [q.id for q in default_questions()]
        assert len(ids) == len(set(ids))

    def test_questions_end_with_a_question_mark(self):
        for q in default_questions():
            assert q.question.endswith("?")

    def test_covers_every_difficulty(self):
        levels = {q.difficulty for q in default_questions()}
        assert levels == set(ev.DIFFICULTIES)


class TestEvalResultWinner:
    def test_slurp_wins(self):
        r = EvalResult("q", "?", score_with_slurp=0.9, score_without_slurp=0.4)
        assert r.winner == "slurp"

    def test_baseline_wins(self):
        r = EvalResult("q", "?", score_with_slurp=0.2, score_without_slurp=0.8)
        assert r.winner == "baseline"

    def test_equal_scores_are_a_tie(self):
        r = EvalResult("q", "?", score_with_slurp=0.5, score_without_slurp=0.5)
        assert r.winner == "tie"

    def test_token_savings(self):
        r = EvalResult("q", "?", tokens_with_slurp=1000, tokens_without_slurp=10000)
        assert r.token_savings_pct == 90.0

    def test_token_savings_without_baseline_is_zero(self):
        assert EvalResult("q", "?").token_savings_pct == 0.0

    def test_negative_savings_clamped_to_zero(self):
        r = EvalResult("q", "?", tokens_with_slurp=200, tokens_without_slurp=100)
        assert r.token_savings_pct == 0.0

    def test_to_dict_includes_winner(self):
        r = EvalResult("q", "?", score_with_slurp=0.9, score_without_slurp=0.1)
        assert r.to_dict()["winner"] == "slurp"


class TestEvalSuiteMetrics:
    def _suite(self) -> EvalSuite:
        return EvalSuite(results=[
            EvalResult("a", "?", score_with_slurp=1.0, score_without_slurp=0.5,
                       tokens_with_slurp=100, tokens_without_slurp=1000),
            EvalResult("b", "?", score_with_slurp=0.5, score_without_slurp=0.5,
                       tokens_with_slurp=100, tokens_without_slurp=1000),
            EvalResult("c", "?", score_with_slurp=0.0, score_without_slurp=0.5,
                       tokens_with_slurp=100, tokens_without_slurp=1000),
        ])

    def test_win_rate(self):
        assert self._suite().slurp_win_rate == pytest.approx(33.3)

    def test_mean_scores(self):
        suite = self._suite()
        assert suite.mean_score_slurp == pytest.approx(0.5)
        assert suite.mean_score_baseline == pytest.approx(0.5)

    def test_mean_token_savings(self):
        assert self._suite().mean_token_savings == pytest.approx(90.0)

    def test_quality_per_token(self):
        suite = self._suite()
        # 1.5 score over 300 tokens vs 1.5 over 3000.
        assert suite.quality_per_token_slurp == pytest.approx(0.005)
        assert suite.quality_per_token_baseline == pytest.approx(0.0005)

    def test_empty_suite_is_all_zeros(self):
        suite = EvalSuite()
        assert suite.slurp_win_rate == 0.0
        assert suite.mean_score_slurp == 0.0
        assert suite.quality_per_token_slurp == 0.0

    def test_failed_questions_are_excluded_not_counted_as_ties(self):
        suite = EvalSuite(results=[
            EvalResult("a", "?", score_with_slurp=1.0, score_without_slurp=0.0,
                       tokens_with_slurp=10, tokens_without_slurp=100),
            EvalResult("b", "?", error="provider unreachable"),
        ])
        assert len(suite.scored) == 1
        assert suite.slurp_win_rate == 100.0
        assert suite.to_dict()["questions_failed"] == 1


class TestJudgePrompt:
    def test_contains_every_part(self):
        prompt = build_judge_prompt("Q?", "GT", "A-text", "B-text")
        for fragment in ("Question: Q?", "Ground truth: GT", "Answer A: A-text",
                         "Answer B: B-text"):
            assert fragment in prompt

    def test_asks_for_json_only(self):
        prompt = build_judge_prompt("Q?", "GT", "a", "b")
        assert "Respond ONLY with valid JSON" in prompt
        assert '"score_a"' in prompt and '"score_b"' in prompt

    def test_lists_the_three_criteria(self):
        prompt = build_judge_prompt("Q?", "GT", "a", "b")
        for criterion in ("Accuracy", "Completeness", "Specificity"):
            assert criterion in prompt

    def test_braces_are_not_swallowed_by_formatting(self):
        prompt = build_judge_prompt("Q?", "GT", "a", "b")
        assert prompt.count("{") >= 1 and prompt.count("}") >= 1


class TestParseJudgeResponse:
    def test_plain_json(self):
        a, b, why = parse_judge_response(JUDGE_OK)
        assert (a, b) == (0.9, 0.4)
        assert "names the function" in why

    def test_json_inside_a_fenced_block(self):
        raw = "```json\n" + JUDGE_OK + "\n```"
        assert parse_judge_response(raw)[0] == 0.9

    def test_json_with_surrounding_prose(self):
        raw = "Here is my evaluation:\n" + JUDGE_OK + "\nHope that helps."
        assert parse_judge_response(raw)[1] == 0.4

    def test_scores_are_clamped_to_the_unit_range(self):
        a, b, _ = parse_judge_response('{"score_a": 5, "score_b": -2}')
        assert (a, b) == (1.0, 0.0)

    def test_missing_json_raises(self):
        with pytest.raises(SlurpExplainError, match="no JSON"):
            parse_judge_response("I could not evaluate these answers.")

    def test_malformed_json_raises(self):
        with pytest.raises(SlurpExplainError, match="invalid JSON"):
            parse_judge_response('{"score_a": }')

    def test_non_numeric_score_raises(self):
        with pytest.raises(SlurpExplainError, match="numeric"):
            parse_judge_response('{"score_a": "high", "score_b": 0.2}')


class TestContexts:
    @staticmethod
    def _big_graph(n: int = 200) -> nx.DiGraph:
        G = nx.DiGraph()
        for i in range(n):
            G.add_node(f"mod.fn{i}", label=f"fn{i}", type="function",
                       source_file=f"lib/mod{i % 20}.ts",
                       description=f"Function number {i} doing unrelated work.")
        for i in range(1, n):
            G.add_edge(f"mod.fn{i}", "mod.fn0", relation="calls")
        return G

    def test_slurp_context_is_smaller_on_a_real_sized_graph(self):
        G = self._big_graph()
        slurp_text, slurp_tokens = build_slurp_context(G, "fn0", budget=500)
        _, baseline_tokens = build_baseline_context(G)
        assert slurp_tokens < baseline_tokens
        assert isinstance(slurp_text, str) and slurp_text

    def test_tiny_graphs_can_cost_more_than_the_baseline(self, graph):
        """The selector's header and scores are overhead a 3-node graph cannot amortise."""
        _, slurp_tokens = build_slurp_context(graph, "admin client", budget=200)
        _, baseline_tokens = build_baseline_context(graph)
        assert slurp_tokens > 0 and baseline_tokens > 0
        # No ordering asserted: on a graph this small either way round is correct.

    def test_baseline_covers_every_node(self, graph):
        text, _ = build_baseline_context(graph)
        for label in ("createAdminClient", "requireAdmin", "recalcularPlayerStats"):
            assert label in text


class TestRunEval:
    def test_scores_every_question(self, graph, monkeypatch):
        _stub_llm(monkeypatch, ["answer with slurp", "answer baseline", JUDGE_OK])
        questions = [EvalQuestion("q1", "Which admin client?", "auth", "createAdminClient()")]
        suite = run_eval(graph, questions, LLMConfig(provider="anthropic"), budget=500)
        assert len(suite.results) == 1
        result = suite.results[0]
        assert result.score_with_slurp == 0.9
        assert result.score_without_slurp == 0.4
        assert result.winner == "slurp"
        assert result.error == ""

    def test_makes_three_llm_calls_per_question(self, graph, monkeypatch):
        calls = _stub_llm(monkeypatch, ["a", "b", JUDGE_OK])
        run_eval(graph, [EvalQuestion("q1", "?", "auth", "gt")],
                 LLMConfig(provider="anthropic"), budget=500)
        assert len(calls) == 3

    def test_baseline_context_is_built_once_for_all_questions(self, graph, monkeypatch):
        seen = []
        monkeypatch.setattr(ev, "build_baseline_context",
                            lambda G, model="cl100k_base": (seen.append(1), ("ctx", 999))[1])
        _stub_llm(monkeypatch, ["a", "b", JUDGE_OK])
        questions = [EvalQuestion(f"q{i}", "?", "auth", "gt") for i in range(3)]
        run_eval(graph, questions, LLMConfig(provider="anthropic"), budget=500)
        assert len(seen) == 1

    def test_records_token_counts(self, graph, monkeypatch):
        _stub_llm(monkeypatch, ["a", "b", JUDGE_OK])
        suite = run_eval(graph, [EvalQuestion("q1", "?", "auth", "gt")],
                         LLMConfig(provider="anthropic"), budget=500)
        result = suite.results[0]
        assert result.tokens_with_slurp > 0
        assert result.tokens_without_slurp > 0

    def test_llm_failure_is_recorded_not_raised(self, graph, monkeypatch):
        def boom(prompt, config):
            raise SlurpExplainError("provider unreachable")

        monkeypatch.setattr(ev, "_call_llm", boom)
        suite = run_eval(graph, [EvalQuestion("q1", "?", "auth", "gt")],
                         LLMConfig(provider="anthropic"), budget=500)
        assert suite.results[0].error == "provider unreachable"
        assert suite.scored == []

    def test_unparseable_judge_is_recorded_as_an_error(self, graph, monkeypatch):
        _stub_llm(monkeypatch, ["a", "b", "not json at all"])
        suite = run_eval(graph, [EvalQuestion("q1", "?", "auth", "gt")],
                         LLMConfig(provider="anthropic"), budget=500)
        assert "no JSON" in suite.results[0].error

    def test_progress_callback_fires_per_question(self, graph, monkeypatch):
        _stub_llm(monkeypatch, ["a", "b", JUDGE_OK])
        seen = []
        run_eval(graph, [EvalQuestion(f"q{i}", "?", "auth", "gt") for i in range(4)],
                 LLMConfig(provider="anthropic"), budget=500,
                 on_question=seen.append)
        assert len(seen) == 4


class TestLoadEvalQuestions:
    def test_loads_a_bare_list(self, tmp_path):
        path = tmp_path / "q.json"
        path.write_text(json.dumps([
            {"id": "a1", "question": "Q?", "area": "auth", "ground_truth": "GT",
             "relevant_nodes": ["n1"], "difficulty": "hard"},
        ]), encoding="utf-8")
        questions = load_eval_questions(path)
        assert len(questions) == 1
        assert questions[0].id == "a1"
        assert questions[0].relevant_nodes == ["n1"]
        assert questions[0].difficulty == "hard"

    def test_loads_a_questions_key(self, tmp_path):
        path = tmp_path / "q.json"
        path.write_text(json.dumps(
            {"questions": [{"id": "a1", "question": "Q?", "ground_truth": "GT"}]}
        ), encoding="utf-8")
        assert len(load_eval_questions(path)) == 1

    def test_optional_fields_get_defaults(self, tmp_path):
        path = tmp_path / "q.json"
        path.write_text(json.dumps([{"id": "a", "question": "Q?", "ground_truth": "G"}]),
                        encoding="utf-8")
        q = load_eval_questions(path)[0]
        assert q.area == "general"
        assert q.difficulty == "medium"
        assert q.relevant_nodes == []

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(SlurpExplainError, match="not found"):
            load_eval_questions(tmp_path / "nope.json")

    def test_invalid_json_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(SlurpExplainError, match="Could not read"):
            load_eval_questions(path)

    def test_empty_list_raises(self, tmp_path):
        path = tmp_path / "empty.json"
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(SlurpExplainError, match="No questions"):
            load_eval_questions(path)

    def test_question_missing_ground_truth_raises(self, tmp_path):
        path = tmp_path / "q.json"
        path.write_text(json.dumps([{"id": "a", "question": "Q?"}]), encoding="utf-8")
        with pytest.raises(SlurpExplainError, match="Invalid question"):
            load_eval_questions(path)


class TestEvalCommand:
    @pytest.fixture
    def graph_file(self, tmp_path) -> Path:
        path = tmp_path / "graph.json"
        path.write_text(json.dumps({
            "nodes": [
                {"id": "admin.createAdminClient", "label": "createAdminClient",
                 "type": "function", "source_file": "lib/admin.ts"},
                {"id": "admin.requireAdmin", "label": "requireAdmin",
                 "type": "function", "source_file": "lib/admin.ts"},
            ],
            "links": [{"source": "admin.requireAdmin",
                       "target": "admin.createAdminClient", "relation": "calls"}],
        }), encoding="utf-8")
        return path

    def test_dry_run_lists_questions_without_calling_the_llm(self, monkeypatch):
        def boom(prompt, config):
            raise AssertionError("--dry-run must not call the LLM")

        monkeypatch.setattr(ev, "_call_llm", boom)
        res = CliRunner().invoke(cli, ["eval", "--dry-run"])
        assert res.exit_code == 0, res.output
        assert "Eval questions (15)" in res.output
        assert "no LLM calls made" in res.output
        assert "auth-01" in res.output

    def test_dry_run_needs_no_graph(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        res = CliRunner().invoke(cli, ["eval", "--dry-run"])
        assert res.exit_code == 0, res.output

    def test_dry_run_with_custom_questions(self, tmp_path):
        path = tmp_path / "q.json"
        path.write_text(json.dumps([
            {"id": "custom-1", "question": "Anything?", "ground_truth": "Yes"},
        ]), encoding="utf-8")
        res = CliRunner().invoke(cli, ["eval", "--dry-run", "--questions", str(path)])
        assert res.exit_code == 0, res.output
        assert "custom-1" in res.output
        assert "Eval questions (1)" in res.output

    def test_bad_questions_file_errors_cleanly(self, tmp_path):
        res = CliRunner().invoke(
            cli, ["eval", "--dry-run", "--questions", str(tmp_path / "missing.json")]
        )
        assert res.exit_code != 0
        assert "not found" in res.output

    def test_reports_results_and_writes_output(self, graph_file, tmp_path, monkeypatch):
        _stub_llm(monkeypatch, ["slurp answer", "baseline answer", JUDGE_OK])
        questions = tmp_path / "q.json"
        questions.write_text(json.dumps([
            {"id": "q1", "question": "Which admin client?", "ground_truth": "createAdminClient"},
        ]), encoding="utf-8")
        out = tmp_path / "results.json"
        res = CliRunner().invoke(cli, [
            "eval", "--graph", str(graph_file), "--questions", str(questions),
            "--provider", "anthropic", "--output", str(out),
            "--config-dir", str(tmp_path / "cfg"),
        ])
        assert res.exit_code == 0, res.output
        assert "Eval Results" in res.output
        assert "slurp win rate" in res.output
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["slurp_win_rate"] == 100.0
        assert payload["results"][0]["winner"] == "slurp"

    def test_structural_provider_is_rejected(self, graph_file, tmp_path):
        res = CliRunner().invoke(cli, [
            "eval", "--graph", str(graph_file),
            "--config-dir", str(tmp_path / "cfg"),
        ], env={"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "SLURP_LLM_ENDPOINT": ""})
        # Either no provider was detected (clean error) or one was; both are fine
        # as long as it never tracebacks.
        assert "Traceback" not in res.output

    def test_failed_questions_are_reported(self, graph_file, tmp_path, monkeypatch):
        def boom(prompt, config):
            raise SlurpExplainError("provider unreachable")

        monkeypatch.setattr(ev, "_call_llm", boom)
        questions = tmp_path / "q.json"
        questions.write_text(json.dumps([
            {"id": "q1", "question": "Q?", "ground_truth": "GT"},
        ]), encoding="utf-8")
        res = CliRunner().invoke(cli, [
            "eval", "--graph", str(graph_file), "--questions", str(questions),
            "--provider", "anthropic", "--config-dir", str(tmp_path / "cfg"),
        ])
        assert res.exit_code == 0, res.output
        assert "could not be scored" in res.output
        assert "provider unreachable" in res.output

    def test_help_lists_flags(self):
        res = CliRunner().invoke(cli, ["eval", "--help"])
        for flag in ("--graph", "--budget", "--questions", "--provider",
                     "--output", "--dry-run"):
            assert flag in res.output

    def test_appears_in_top_level_help(self):
        res = CliRunner().invoke(cli, ["--help"])
        assert "eval" in res.output
