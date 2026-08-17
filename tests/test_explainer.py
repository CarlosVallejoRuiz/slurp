"""Tests for slurp.explainer — natural-language node explanations."""

import json
from pathlib import Path

import networkx as nx
import pytest
from click.testing import CliRunner

from slurp.cli import cli
from slurp.explainer import (
    DEFAULT_MODELS,
    PROVIDERS,
    ExplainResult,
    LLMConfig,
    SlurpExplainError,
    _build_context,
    _call_llm,
    _explain_structural,
    _risk_level,
    detect_provider,
    explain_node,
    find_node,
    format_explanation,
    read_config,
    resolve_config,
    write_config,
)


def _graph_with_dependents(n: int) -> nx.DiGraph:
    """A central node with *n* direct dependents and one dependency."""
    G = nx.DiGraph()
    G.add_node("target", label="recalcStats", type="function",
               source_file="lib/admin.ts", description="Recalculates stats.")
    G.add_node("helper", label="createClient", type="function",
               source_file="lib/client.ts")
    G.add_edge("target", "helper", relation="calls")
    for i in range(n):
        G.add_node(f"caller{i}", label=f"handler{i}", type="function",
                   source_file="lib/handlers.ts")
        G.add_edge(f"caller{i}", "target", relation="calls")
    return G


@pytest.fixture
def graph() -> nx.DiGraph:
    return _graph_with_dependents(8)


@pytest.fixture
def scores() -> dict[str, float]:
    return {"target": 0.702, "helper": 0.31}


class TestRiskLevel:
    @pytest.mark.parametrize(("dependents", "expected"), [
        (0, "LOW"), (1, "LOW"),
        (2, "MEDIUM"), (3, "MEDIUM"), (5, "MEDIUM"),
        (6, "HIGH"), (8, "HIGH"), (20, "HIGH"),
    ])
    def test_thresholds(self, dependents, expected):
        G = _graph_with_dependents(dependents)
        assert _risk_level(G, "target") == expected

    def test_zero_dependents_is_low(self):
        assert _risk_level(_graph_with_dependents(0), "target") == "LOW"

    def test_three_dependents_is_medium(self):
        assert _risk_level(_graph_with_dependents(3), "target") == "MEDIUM"

    def test_eight_dependents_is_high(self):
        assert _risk_level(_graph_with_dependents(8), "target") == "HIGH"

    def test_outgoing_edges_do_not_raise_risk(self):
        """Risk is about who depends on you, not who you depend on."""
        G = nx.DiGraph()
        G.add_node("a", label="a", type="function")
        for i in range(10):
            G.add_node(f"dep{i}", label=f"dep{i}", type="function")
            G.add_edge("a", f"dep{i}", relation="calls")
        assert _risk_level(G, "a") == "LOW"

    def test_missing_node_is_low(self):
        assert _risk_level(nx.DiGraph(), "nope") == "LOW"


class TestBuildContext:
    def test_includes_callers(self, graph, scores):
        context = _build_context(graph, "target", scores)
        assert "CALLED BY (8):" in context
        assert "handler0()" in context

    def test_includes_callees(self, graph, scores):
        context = _build_context(graph, "target", scores)
        assert "CALLS (1):" in context
        assert "createClient()" in context

    def test_includes_node_facts(self, graph, scores):
        context = _build_context(graph, "target", scores)
        assert "recalcStats()" in context
        assert "type: function" in context
        assert "source file: lib/admin.ts" in context
        assert "Recalculates stats." in context

    def test_includes_relevance_score(self, graph, scores):
        assert "relevance score: 0.702" in _build_context(graph, "target", scores)

    def test_includes_blast_radius(self, graph, scores):
        context = _build_context(graph, "target", scores)
        assert "BLAST RADIUS: 8 direct dependents (risk HIGH)" in context

    def test_reports_empty_relationships(self):
        G = nx.DiGraph()
        G.add_node("lonely", label="lonely", type="function")
        context = _build_context(G, "lonely")
        assert "(nothing depends on this node)" in context
        assert "(this node calls nothing)" in context

    def test_two_hop_neighbours_included(self):
        G = nx.DiGraph()
        for n in ("a", "b", "c"):
            G.add_node(n, label=n, type="function")
        G.add_edge("a", "b", relation="calls")
        G.add_edge("b", "c", relation="calls")
        context = _build_context(G, "a", hops=2)
        assert "NEIGHBOURHOOD (within 2 hops)" in context
        assert "c" in context

    def test_hops_zero_omits_neighbourhood(self, graph):
        assert "NEIGHBOURHOOD" not in _build_context(graph, "target", hops=0)

    def test_long_caller_lists_are_truncated(self):
        context = _build_context(_graph_with_dependents(40), "target")
        assert "and 28 more" in context

    def test_missing_node_raises(self, graph):
        with pytest.raises(SlurpExplainError, match="Node not in graph"):
            _build_context(graph, "nope")

    def test_project_shown_for_federated_nodes(self):
        G = nx.DiGraph()
        G.add_node("x", label="x", type="function", _project="auth")
        assert "project: auth" in _build_context(G, "x")


class TestExplainStructural:
    def test_generates_text_without_llm(self, graph, scores):
        text = _explain_structural(graph, "target", scores)
        assert isinstance(text, str) and len(text) > 50

    def test_names_the_node_and_file(self, graph, scores):
        text = _explain_structural(graph, "target", scores)
        assert "recalcStats()" in text
        assert "lib/admin.ts" in text

    def test_reports_dependents(self, graph, scores):
        assert "referenced by 8 nodes" in _explain_structural(graph, "target", scores)

    def test_high_risk_warns_about_propagation(self, graph, scores):
        assert "propagate widely" in _explain_structural(graph, "target", scores)

    def test_low_risk_says_so(self, scores):
        G = _graph_with_dependents(0)
        assert "little blast radius" in _explain_structural(G, "target", scores)

    def test_orphan_node_is_described(self):
        G = nx.DiGraph()
        G.add_node("x", label="x", type="function")
        text = _explain_structural(G, "x")
        assert "entry point" in text or "unused" in text

    def test_includes_description_when_present(self, graph):
        assert "Recalculates stats" in _explain_structural(graph, "target")

    def test_missing_node_raises(self, graph):
        with pytest.raises(SlurpExplainError):
            _explain_structural(graph, "nope")


class TestFindNode:
    def test_exact_id_wins(self, graph, scores):
        assert find_node(graph, "target", scores) == "target"

    def test_label_match(self, graph, scores):
        assert find_node(graph, "recalcStats", scores) == "target"

    def test_label_match_tolerates_parens(self, graph, scores):
        assert find_node(graph, "recalcStats()", scores) == "target"

    def test_label_match_is_case_insensitive(self, graph, scores):
        assert find_node(graph, "RECALCSTATS", scores) == "target"

    def test_falls_back_to_best_score(self, graph):
        assert find_node(graph, "something vague", {"helper": 0.9}) == "helper"

    def test_dotted_suffix_match(self):
        G = nx.DiGraph()
        G.add_node("pkg.mod.thing", label="thing", type="function")
        assert find_node(G, "thing", {}) == "pkg.mod.thing"

    def test_empty_graph_raises(self):
        with pytest.raises(SlurpExplainError, match="no nodes"):
            find_node(nx.DiGraph(), "x", {})

    def test_no_match_raises(self, graph):
        with pytest.raises(SlurpExplainError, match="No node matches"):
            find_node(graph, "definitely-absent", {})


class TestExplainNodeFallback:
    def test_no_config_uses_structural(self, graph, scores):
        result = explain_node(graph, "target", scores, None)
        assert result.provider_used == "structural"
        assert result.explanation

    def test_structural_provider_skips_llm(self, graph, scores):
        result = explain_node(graph, "target", scores, LLMConfig(provider="structural"))
        assert result.provider_used == "structural"

    def test_result_fields_populated(self, graph, scores):
        result = explain_node(graph, "target", scores, None)
        assert result.node_id == "target"
        assert result.label == "recalcStats()"
        assert result.risk_level == "HIGH"
        assert result.callers == [f"handler{i}()" for i in range(8)]
        assert result.callees == ["createClient()"]
        assert result.context_tokens > 0
        assert result.node_type == "function"
        assert result.source_file == "lib/admin.ts"
        assert result.score == 0.702

    def test_llm_failure_falls_back_with_warning(self, graph, scores, monkeypatch):
        import slurp.explainer as ex

        def boom(prompt, config):
            raise SlurpExplainError("no API key")

        monkeypatch.setattr(ex, "_call_llm", boom)
        result = explain_node(graph, "target", scores, LLMConfig(provider="anthropic"))
        assert result.provider_used == "structural"
        assert "no API key" in result.warning

    def test_unexpected_llm_error_also_falls_back(self, graph, scores, monkeypatch):
        import slurp.explainer as ex

        monkeypatch.setattr(ex, "_call_llm", lambda p, c: 1 / 0)
        result = explain_node(graph, "target", scores, LLMConfig(provider="openai"))
        assert result.provider_used == "structural"
        assert "ZeroDivisionError" in result.warning

    def test_llm_success_replaces_explanation(self, graph, scores, monkeypatch):
        import slurp.explainer as ex

        monkeypatch.setattr(ex, "_call_llm", lambda p, c: "LLM prose here.")
        config = LLMConfig(provider="anthropic", model="claude-sonnet-5")
        result = explain_node(graph, "target", scores, config)
        assert result.explanation == "LLM prose here."
        assert result.provider_used == "anthropic"
        assert result.model_used == "claude-sonnet-5"

    def test_empty_llm_reply_keeps_structural(self, graph, scores, monkeypatch):
        import slurp.explainer as ex

        monkeypatch.setattr(ex, "_call_llm", lambda p, c: "   ")
        result = explain_node(graph, "target", scores, LLMConfig(provider="anthropic"))
        assert result.provider_used == "structural"

    def test_missing_node_raises(self, graph, scores):
        with pytest.raises(SlurpExplainError):
            explain_node(graph, "nope", scores, None)


class TestCallLLM:
    def test_unknown_provider_raises(self):
        with pytest.raises(SlurpExplainError, match="Unknown provider"):
            _call_llm("hi", LLMConfig(provider="mystery"))

    def test_missing_anthropic_sdk_message(self, monkeypatch):
        import builtins
        real = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "anthropic":
                raise ImportError("no module")
            return real(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked)
        with pytest.raises(SlurpExplainError, match="pip install anthropic"):
            _call_llm("hi", LLMConfig(provider="anthropic", api_key="k"))

    def test_missing_openai_sdk_message(self, monkeypatch):
        import builtins
        real = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "openai":
                raise ImportError("no module")
            return real(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked)
        with pytest.raises(SlurpExplainError, match="pip install openai"):
            _call_llm("hi", LLMConfig(provider="openai", api_key="k"))

    def test_missing_anthropic_key_message(self, monkeypatch):
        pytest.importorskip("anthropic")
        with pytest.raises(SlurpExplainError, match="No Anthropic API key"):
            _call_llm("hi", LLMConfig(provider="anthropic", api_key=None))

    def test_openai_compatible_needs_endpoint(self):
        config = LLMConfig(provider="openai-compatible", endpoint=None)
        with pytest.raises(SlurpExplainError, match="needs an endpoint"):
            _call_llm("hi", config)

    def test_ollama_unreachable_is_reported(self, monkeypatch):
        import slurp.explainer as ex

        def boom(url, payload, headers):
            raise SlurpExplainError(f"Could not reach {url}: refused")

        monkeypatch.setattr(ex, "_post_json", boom)
        with pytest.raises(SlurpExplainError, match="Could not reach"):
            _call_llm("hi", LLMConfig(provider="ollama"))

    def test_ollama_posts_to_generate_endpoint(self, monkeypatch):
        import slurp.explainer as ex
        seen: dict = {}

        def fake(url, payload, headers):
            seen["url"] = url
            seen["payload"] = payload
            return {"response": "ollama says hi"}

        monkeypatch.setattr(ex, "_post_json", fake)
        text = _call_llm("hi", LLMConfig(provider="ollama", model="llama3.2"))
        assert text == "ollama says hi"
        assert seen["url"].endswith("/api/generate")
        assert seen["payload"]["model"] == "llama3.2"
        assert seen["payload"]["stream"] is False

    def test_openai_compatible_posts_chat_completions(self, monkeypatch):
        import slurp.explainer as ex
        seen: dict = {}

        def fake(url, payload, headers):
            seen["url"] = url
            seen["headers"] = headers
            return {"choices": [{"message": {"content": "local model reply"}}]}

        monkeypatch.setattr(ex, "_post_json", fake)
        config = LLMConfig(provider="openai-compatible",
                           endpoint="http://localhost:1234/v1", api_key="secret")
        assert _call_llm("hi", config) == "local model reply"
        assert seen["url"] == "http://localhost:1234/v1/chat/completions"
        assert seen["headers"]["Authorization"] == "Bearer secret"

    def test_openai_compatible_bad_shape_raises(self, monkeypatch):
        import slurp.explainer as ex

        monkeypatch.setattr(ex, "_post_json", lambda u, p, h: {"unexpected": True})
        config = LLMConfig(provider="openai-compatible", endpoint="http://x/v1")
        with pytest.raises(SlurpExplainError, match="Unexpected response shape"):
            _call_llm("hi", config)


class TestProviderDetection:
    def test_anthropic_key_wins(self):
        env = {"ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "j"}
        assert detect_provider(env, check_ollama=False) == "anthropic"

    def test_openai_key_detected(self):
        assert detect_provider({"OPENAI_API_KEY": "j"}, check_ollama=False) == "openai"

    def test_endpoint_detected(self):
        env = {"SLURP_LLM_ENDPOINT": "http://localhost:1234/v1"}
        assert detect_provider(env, check_ollama=False) == "openai-compatible"

    def test_nothing_configured_is_structural(self):
        assert detect_provider({}, check_ollama=False) == "structural"

    def test_ollama_detected_when_running(self, monkeypatch):
        import slurp.explainer as ex

        monkeypatch.setattr(ex, "_ollama_running", lambda host: True)
        assert detect_provider({}, check_ollama=True) == "ollama"

    def test_ollama_skipped_when_not_running(self, monkeypatch):
        import slurp.explainer as ex

        monkeypatch.setattr(ex, "_ollama_running", lambda host: False)
        assert detect_provider({}, check_ollama=True) == "structural"

    def test_every_provider_has_a_default_model(self):
        assert set(DEFAULT_MODELS) == set(PROVIDERS)


class TestResolveConfig:
    def test_flag_beats_saved_config(self, tmp_path):
        write_config("provider", "openai", tmp_path)
        config = resolve_config("anthropic", config_dir=tmp_path,
                                env={}, check_ollama=False)
        assert config.provider == "anthropic"

    def test_saved_config_beats_autodetect(self, tmp_path):
        write_config("provider", "ollama", tmp_path)
        config = resolve_config(config_dir=tmp_path,
                                env={"ANTHROPIC_API_KEY": "k"}, check_ollama=False)
        assert config.provider == "ollama"

    def test_autodetect_when_nothing_saved(self, tmp_path):
        config = resolve_config(config_dir=tmp_path,
                                env={"OPENAI_API_KEY": "j"}, check_ollama=False)
        assert config.provider == "openai"

    def test_api_key_from_env(self, tmp_path):
        config = resolve_config(config_dir=tmp_path,
                                env={"ANTHROPIC_API_KEY": "secret"}, check_ollama=False)
        assert config.api_key == "secret"

    def test_default_model_per_provider(self, tmp_path):
        config = resolve_config("anthropic", config_dir=tmp_path,
                                env={}, check_ollama=False)
        assert config.model == DEFAULT_MODELS["anthropic"]

    def test_endpoint_from_env(self, tmp_path):
        config = resolve_config(config_dir=tmp_path,
                                env={"SLURP_LLM_ENDPOINT": "http://x/v1"},
                                check_ollama=False)
        assert config.endpoint == "http://x/v1"

    def test_bad_temperature_falls_back(self, tmp_path):
        write_config("temperature", "not-a-number", tmp_path)
        assert resolve_config(config_dir=tmp_path, env={},
                              check_ollama=False).temperature == 0.3


class TestConfigFile:
    def test_write_then_read(self, tmp_path):
        write_config("provider", "anthropic", tmp_path)
        assert read_config(tmp_path)["provider"] == "anthropic"

    def test_creates_directory(self, tmp_path):
        target = tmp_path / "nested" / ".slurp"
        write_config("model", "gpt-4o-mini", target)
        assert (target / "config.json").is_file()

    def test_updates_preserve_other_keys(self, tmp_path):
        write_config("provider", "openai", tmp_path)
        write_config("model", "gpt-4o", tmp_path)
        config = read_config(tmp_path)
        assert config == {"provider": "openai", "model": "gpt-4o"}

    def test_unknown_key_raises(self, tmp_path):
        with pytest.raises(SlurpExplainError, match="Unknown config key"):
            write_config("nonsense", "x", tmp_path)

    def test_unknown_provider_raises(self, tmp_path):
        with pytest.raises(SlurpExplainError, match="Unknown provider"):
            write_config("provider", "skynet", tmp_path)

    def test_missing_file_reads_empty(self, tmp_path):
        assert read_config(tmp_path / "absent") == {}

    def test_corrupt_file_reads_empty(self, tmp_path):
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / "config.json").write_text("{broken", encoding="utf-8")
        assert read_config(tmp_path) == {}


class TestFormatExplanation:
    def _result(self, **kwargs) -> ExplainResult:
        base = {
            "node_id": "target", "label": "recalcStats()",
            "explanation": "It recalculates things.", "risk_level": "HIGH",
            "callers": ["a()", "b()"], "callees": ["c()"],
            "provider_used": "structural", "context_tokens": 1240,
            "node_type": "function",
        }
        return ExplainResult(**{**base, **kwargs})

    def test_sections_present(self):
        out = format_explanation(self._result())
        assert "EXPLANATION" in out
        assert "ARCHITECTURE" in out
        assert "RISK IF CHANGED: HIGH" in out

    def test_arrows_for_callers_and_callees(self):
        out = format_explanation(self._result())
        assert "→ Called by: a(), b()" in out
        assert "← Calls: c()" in out

    def test_empty_lists_say_nothing(self):
        out = format_explanation(self._result(callers=[], callees=[]))
        assert "→ Called by: nothing" in out
        assert "← Calls: nothing" in out

    def test_dependent_count_line(self):
        assert "2 nodes depend on this function directly." in format_explanation(
            self._result()
        )

    def test_singular_dependent_agrees(self):
        out = format_explanation(self._result(callers=["a()"]))
        assert "1 node depends on this function directly." in out

    def test_no_dependents_line(self):
        out = format_explanation(self._result(callers=[]))
        assert "Nothing depends on this function directly." in out

    def test_provider_footer(self):
        out = format_explanation(
            self._result(provider_used="anthropic", model_used="claude-sonnet-5")
        )
        assert "Provider: anthropic (claude-sonnet-5) · Context: 1,240 tokens" in out

    def test_warning_shown(self):
        out = format_explanation(self._result(warning="ollama unavailable"))
        assert "Note: ollama unavailable" in out

    def test_long_caller_list_wraps_aligned(self):
        callers = [f"someRatherLongHandlerName{i}()" for i in range(6)]
        out = format_explanation(self._result(callers=callers))
        arch = [ln for ln in out.splitlines() if "someRatherLong" in ln]
        assert len(arch) > 1
        assert arch[1].startswith(" " * len("→ Called by: "))


class TestExplainCommand:
    @pytest.fixture
    def graph_file(self, tmp_path) -> Path:
        nodes = [
            {"id": "target", "label": "recalcStats", "type": "function",
             "source_file": "lib/admin.ts", "description": "Recalculates stats."},
            {"id": "helper", "label": "createClient", "type": "function",
             "source_file": "lib/client.ts"},
        ]
        links = [{"source": "target", "target": "helper", "relation": "calls"}]
        for i in range(8):
            nodes.append({"id": f"caller{i}", "label": f"handler{i}",
                          "type": "function", "source_file": "lib/h.ts"})
            links.append({"source": f"caller{i}", "target": "target",
                          "relation": "calls"})
        path = tmp_path / "graph.json"
        path.write_text(json.dumps({"nodes": nodes, "links": links}), encoding="utf-8")
        return path

    def test_no_llm_forces_structural(self, graph_file, tmp_path):
        res = CliRunner().invoke(cli, [
            "explain", "recalcStats", "--graph", str(graph_file), "--no-llm",
            "--config-dir", str(tmp_path / "cfg"),
        ])
        assert res.exit_code == 0, res.output
        assert "Provider: structural" in res.output

    def test_output_sections(self, graph_file, tmp_path):
        res = CliRunner().invoke(cli, [
            "explain", "recalcStats", "--graph", str(graph_file), "--no-llm",
            "--config-dir", str(tmp_path / "cfg"),
        ])
        assert "slurp explain — recalcStats()" in res.output
        assert "EXPLANATION" in res.output
        assert "ARCHITECTURE" in res.output
        assert "RISK IF CHANGED: HIGH" in res.output
        assert "8 nodes depend on this function directly." in res.output

    def test_panel_shows_type_and_file(self, graph_file, tmp_path):
        res = CliRunner().invoke(cli, [
            "explain", "recalcStats", "--graph", str(graph_file), "--no-llm",
            "--config-dir", str(tmp_path / "cfg"),
        ])
        assert "function" in res.output
        assert "lib/admin.ts" in res.output

    def test_no_llm_suppresses_configure_hint(self, graph_file, tmp_path):
        res = CliRunner().invoke(cli, [
            "explain", "recalcStats", "--graph", str(graph_file), "--no-llm",
            "--config-dir", str(tmp_path / "cfg"),
        ])
        assert "No LLM provider configured" not in res.output

    def test_unmatched_query_errors(self, graph_file, tmp_path):
        res = CliRunner().invoke(cli, [
            "explain", "zzz-nothing-like-this", "--graph", str(graph_file),
            "--no-llm", "--config-dir", str(tmp_path / "cfg"),
        ])
        # Either it resolves to the best-scoring node or reports no match, but
        # it must never traceback.
        assert res.exit_code in (0, 1)
        assert "Traceback" not in res.output

    def test_hops_flag_accepted(self, graph_file, tmp_path):
        res = CliRunner().invoke(cli, [
            "explain", "recalcStats", "--graph", str(graph_file), "--no-llm",
            "--hops", "1", "--config-dir", str(tmp_path / "cfg"),
        ])
        assert res.exit_code == 0, res.output

    def test_help_lists_flags(self):
        res = CliRunner().invoke(cli, ["explain", "--help"])
        for flag in ("--graph", "--provider", "--model", "--endpoint",
                     "--no-llm", "--hops"):
            assert flag in res.output


class TestConfigCommand:
    def test_set_and_show(self, tmp_path):
        runner = CliRunner()
        res = runner.invoke(cli, ["config", "set", "provider", "anthropic",
                                  "--config-dir", str(tmp_path)])
        assert res.exit_code == 0, res.output
        assert "Set provider = anthropic" in res.output

        res = runner.invoke(cli, ["config", "show", "--config-dir", str(tmp_path)])
        assert res.exit_code == 0, res.output
        assert "anthropic" in res.output

    def test_set_model(self, tmp_path):
        res = CliRunner().invoke(cli, ["config", "set", "model", "claude-sonnet-5",
                                       "--config-dir", str(tmp_path)])
        assert res.exit_code == 0, res.output
        assert read_config(tmp_path)["model"] == "claude-sonnet-5"

    def test_set_endpoint(self, tmp_path):
        res = CliRunner().invoke(cli, [
            "config", "set", "endpoint", "http://localhost:1234/v1",
            "--config-dir", str(tmp_path),
        ])
        assert res.exit_code == 0, res.output
        assert read_config(tmp_path)["endpoint"] == "http://localhost:1234/v1"

    def test_api_key_is_masked_on_set(self, tmp_path):
        res = CliRunner().invoke(cli, ["config", "set", "api_key", "sk-secret-value",
                                       "--config-dir", str(tmp_path)])
        assert "sk-secret-value" not in res.output
        assert "********" in res.output

    def test_api_key_is_masked_on_show(self, tmp_path):
        write_config("api_key", "sk-secret-value", tmp_path)
        res = CliRunner().invoke(cli, ["config", "show", "--config-dir", str(tmp_path)])
        assert "sk-secret-value" not in res.output
        assert "********" in res.output

    def test_show_empty_config(self, tmp_path):
        res = CliRunner().invoke(cli, ["config", "show",
                                       "--config-dir", str(tmp_path / "none")])
        assert res.exit_code == 0, res.output
        assert "no config file yet" in res.output

    def test_unknown_key_errors(self, tmp_path):
        res = CliRunner().invoke(cli, ["config", "set", "bogus", "x",
                                       "--config-dir", str(tmp_path)])
        assert res.exit_code != 0
        assert "Unknown config key" in res.output

    def test_unknown_provider_errors(self, tmp_path):
        res = CliRunner().invoke(cli, ["config", "set", "provider", "skynet",
                                       "--config-dir", str(tmp_path)])
        assert res.exit_code != 0
        assert "Unknown provider" in res.output

    def test_config_in_top_level_help(self):
        res = CliRunner().invoke(cli, ["--help"])
        assert "config" in res.output
