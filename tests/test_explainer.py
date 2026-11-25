"""Tests for slurp.explainer — natural-language node explanations."""

import json
import os
import stat
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
    _architectural_role,
    _explain_structural,
    _explore_further,
    _risk_level,
    _split_callers,
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
        assert "BLAST RADIUS: 8 production callers (risk HIGH)" in context

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
        assert "called by 8 places in the project" in _explain_structural(graph, "target", scores)

    def test_high_risk_warns_about_propagation(self, graph, scores):
        assert "utility" in _explain_structural(graph, "target", scores)

    def test_low_risk_says_so(self, scores):
        G = _graph_with_dependents(0)
        text = _explain_structural(G, "target", scores)
        assert any(r in text for r in ("Leaf", "Entry point", "Isolated"))

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


def _project_config(config_dir: Path, **values: str) -> Path:
    """Write a project-level config.json the way a cloned repo would ship it."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(values), encoding="utf-8")
    return config_dir


class TestEndpointKeyIsolation:
    """A key from the user's environment never travels to a project endpoint.

    The project config is whatever a cloned repository ships in .slurp/, so an
    endpoint read from it is not something the user chose.
    """

    @pytest.mark.parametrize(("provider", "env_var"), [
        ("openai", "OPENAI_API_KEY"),
        ("openai-compatible", "SLURP_LLM_API_KEY"),
    ])
    def test_env_key_refused_for_project_endpoint(self, tmp_path, provider, env_var):
        cfg = _project_config(tmp_path / ".slurp", provider=provider,
                              endpoint="https://attacker.invalid/v1")
        with pytest.raises(SlurpExplainError, match="endpoint"):
            resolve_config(config_dir=cfg, env={env_var: "sk-from-env"},
                           check_ollama=False)

    def test_autodetected_provider_is_also_refused(self, tmp_path):
        # No provider saved: OPENAI_API_KEY picks openai, the endpoint still
        # comes from the project.
        cfg = _project_config(tmp_path / ".slurp", endpoint="https://attacker.invalid/v1")
        with pytest.raises(SlurpExplainError):
            resolve_config(config_dir=cfg, env={"OPENAI_API_KEY": "sk-from-env"},
                           check_ollama=False)

    def test_error_never_echoes_the_key(self, tmp_path):
        cfg = _project_config(tmp_path / ".slurp", provider="openai",
                              endpoint="https://attacker.invalid/v1")
        with pytest.raises(SlurpExplainError) as info:
            resolve_config(config_dir=cfg, env={"OPENAI_API_KEY": "sk-from-env"},
                           check_ollama=False)
        assert "sk-from-env" not in str(info.value)

    def test_project_endpoint_with_project_key_is_allowed(self, tmp_path):
        cfg = _project_config(tmp_path / ".slurp", provider="openai",
                              endpoint="https://proxy.example/v1", api_key="sk-project")
        config = resolve_config(config_dir=cfg, env={"OPENAI_API_KEY": "sk-from-env"},
                                check_ollama=False)
        assert (config.endpoint, config.api_key) == ("https://proxy.example/v1", "sk-project")

    def test_endpoint_flag_with_env_key_is_allowed(self, tmp_path):
        cfg = _project_config(tmp_path / ".slurp", provider="openai",
                              endpoint="https://attacker.invalid/v1")
        config = resolve_config(endpoint="https://mine.example/v1", config_dir=cfg,
                                env={"OPENAI_API_KEY": "sk-from-env"}, check_ollama=False)
        assert (config.endpoint, config.api_key) == ("https://mine.example/v1", "sk-from-env")

    def test_env_endpoint_with_env_key_is_allowed(self, tmp_path):
        config = resolve_config(
            "openai-compatible", config_dir=tmp_path / "none",
            env={"SLURP_LLM_ENDPOINT": "http://localhost:1234/v1",
                 "SLURP_LLM_API_KEY": "sk-from-env"},
            check_ollama=False,
        )
        assert (config.endpoint, config.api_key) == ("http://localhost:1234/v1", "sk-from-env")

    def test_anthropic_ignores_endpoint_so_is_not_blocked(self, tmp_path):
        cfg = _project_config(tmp_path / ".slurp", provider="anthropic",
                              endpoint="https://attacker.invalid/v1")
        config = resolve_config(config_dir=cfg, env={"ANTHROPIC_API_KEY": "k"},
                                check_ollama=False)
        assert config.api_key == "k"

    def test_cli_explain_refuses_and_makes_no_call(self, tmp_path, monkeypatch):
        import slurp.explainer as ex

        calls: list[str] = []
        monkeypatch.setitem(ex._DISPATCH, "openai",
                            lambda prompt, config: calls.append(config.endpoint) or "x")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        graph = tmp_path / "graph.json"
        graph.write_text(json.dumps({"nodes": [{"id": "a", "label": "alpha"}],
                                     "links": []}), encoding="utf-8")
        cfg = _project_config(tmp_path / ".slurp", provider="openai",
                              endpoint="https://attacker.invalid/v1")

        res = CliRunner().invoke(cli, ["explain", "alpha", "--graph", str(graph),
                                       "--config-dir", str(cfg)])

        assert res.exit_code != 0
        assert calls == []
        assert "sk-from-env" not in res.output

    def test_config_show_reports_instead_of_crashing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        cfg = _project_config(tmp_path / ".slurp", provider="openai",
                              endpoint="https://attacker.invalid/v1")
        res = CliRunner().invoke(cli, ["config", "show", "--config-dir", str(cfg)])
        assert res.exit_code == 0, res.output
        assert "Traceback" not in res.output
        assert "sk-from-env" not in res.output


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

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_new_config_is_owner_only(self, tmp_path):
        write_config("api_key", "sk-secret", tmp_path)
        assert stat.S_IMODE((tmp_path / "config.json").stat().st_mode) == 0o600

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_existing_world_readable_config_is_tightened(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("{}", encoding="utf-8")
        path.chmod(0o644)
        write_config("api_key", "sk-secret", tmp_path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert read_config(tmp_path) == {"api_key": "sk-secret"}


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
        assert "2 production callers" in format_explanation(
            self._result()
        )

    def test_singular_dependent_agrees(self):
        out = format_explanation(self._result(callers=["a()"]))
        assert "1 production caller" in out and "callers" not in out.split("RISK")[1]

    def test_no_dependents_line(self):
        out = format_explanation(self._result(callers=[]))
        assert "Nothing in the project depends on this function" in out

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
        assert "8 production callers" in res.output

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


class TestConfigSetKeepsSlurpDirOutOfGit:
    """`config set` writes .slurp/config.json, which can hold an API key: it adds
    .slurp/ to .gitignore under the same rules as `slurp init`."""

    @staticmethod
    def _set(tmp_path, monkeypatch, *args):
        monkeypatch.chdir(tmp_path)
        return CliRunner().invoke(cli, ["config", "set", *args])

    @pytest.mark.parametrize("key, value", [("api_key", "sk-secret-value"),
                                            ("provider", "anthropic")])
    def test_existing_gitignore_gains_the_entry(self, tmp_path, monkeypatch, key, value):
        (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        res = self._set(tmp_path, monkeypatch, key, value)
        assert res.exit_code == 0, res.output
        assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == \
            "node_modules/\n.slurp/\n"
        assert ".gitignore" in res.output and "sk-secret-value" not in res.output

    def test_missing_trailing_newline_is_respected(self, tmp_path, monkeypatch):
        (tmp_path / ".gitignore").write_text("dist", encoding="utf-8")
        self._set(tmp_path, monkeypatch, "api_key", "sk-x")
        assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "dist\n.slurp/\n"

    @pytest.mark.parametrize("existing", [".slurp/", ".slurp", "/.slurp/", "**/.slurp/",
                                          "!.slurp/"])
    def test_an_existing_rule_or_negation_is_left_alone(self, tmp_path, monkeypatch,
                                                        existing):
        original = f"build/\n{existing}\n"
        (tmp_path / ".gitignore").write_text(original, encoding="utf-8")
        res = self._set(tmp_path, monkeypatch, "api_key", "sk-x")
        self._set(tmp_path, monkeypatch, "model", "m")
        assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == original
        assert ".gitignore" not in res.output

    def test_no_gitignore_is_created(self, tmp_path, monkeypatch):
        res = self._set(tmp_path, monkeypatch, "api_key", "sk-x")
        assert res.exit_code == 0, res.output
        assert not (tmp_path / ".gitignore").exists()

    def test_a_config_dir_the_entry_would_not_cover_is_left_alone(self, tmp_path,
                                                                 monkeypatch):
        (tmp_path / "proj").mkdir()
        (tmp_path / "proj" / ".gitignore").write_text("venv/\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path / "proj")
        for config_dir in (tmp_path / "elsewhere" / ".slurp", tmp_path / "proj" / "cfg"):
            res = CliRunner().invoke(cli, ["config", "set", "api_key", "sk-x",
                                           "--config-dir", str(config_dir)])
            assert res.exit_code == 0, res.output
        assert (tmp_path / "proj" / ".gitignore").read_text(encoding="utf-8") == "venv/\n"

    def test_a_refused_key_writes_nothing(self, tmp_path, monkeypatch):
        (tmp_path / ".gitignore").write_text("venv/\n", encoding="utf-8")
        res = self._set(tmp_path, monkeypatch, "bogus", "x")
        assert res.exit_code != 0
        assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "venv/\n"


def _call_graph(callers: int = 0, callees: int = 0, *, ntype: str = "function",
                tests: int = 0) -> nx.DiGraph:
    """A graph whose `target` has the requested shape."""
    G = nx.DiGraph()
    G.add_node("target", label="target", type=ntype, source_file="lib/core.py")
    G.add_node("mod", label="core", type="module", source_file="lib/core.py")
    G.add_edge("mod", "target", relation="contains")
    for i in range(callers):
        G.add_node(f"caller{i}", label=f"caller{i}", type="function",
                   source_file="lib/app.py")
        G.add_edge(f"caller{i}", "target", relation="calls")
    for i in range(tests):
        G.add_node(f"test_t{i}", label=f"test_t{i}", type="function",
                   source_file="tests/test_core.py")
        G.add_edge(f"test_t{i}", "target", relation="calls")
    for i in range(callees):
        G.add_node(f"dep{i}", label=f"dep{i}", type="function",
                   source_file="lib/dep.py")
        G.add_edge("target", f"dep{i}", relation="calls")
    return G


class TestArchitecturalRole:
    def test_many_callers_is_a_central_utility(self):
        assert "Central utility" in _architectural_role(_call_graph(callers=12), "target")

    def test_many_callers_and_callees_is_an_orchestrator(self):
        role = _architectural_role(_call_graph(callers=6, callees=6), "target")
        assert "Orchestrator" in role

    def test_many_callers_few_callees_is_a_shared_utility(self):
        """The shape the brief's rules left unnamed, and the commonest hub."""
        role = _architectural_role(_call_graph(callers=6, callees=1), "target")
        assert "Shared utility" in role

    def test_no_callers_but_many_callees_is_an_entry_point(self):
        assert "Entry point" in _architectural_role(_call_graph(callees=6), "target")

    def test_few_of_each_is_a_leaf(self):
        assert "Leaf" in _architectural_role(_call_graph(callers=1, callees=1), "target")

    def test_nothing_either_way_is_isolated(self):
        assert "Isolated" in _architectural_role(_call_graph(), "target")

    def test_module_reports_what_it_holds(self):
        G = _call_graph(callers=2)
        role = _architectural_role(G, "mod")
        assert "Core module" in role and "1 definition" in role

    def test_test_callers_do_not_inflate_the_role(self):
        """Sixty tests do not make a function a central utility."""
        role = _architectural_role(_call_graph(callers=1, callees=1, tests=60), "target")
        assert "Leaf" in role


class TestCallerSplit:
    def test_tests_are_separated_from_production(self):
        production, tests = _split_callers(_call_graph(callers=3, tests=5), "target")
        assert len(production) == 3
        assert len(tests) == 5

    def test_containing_module_is_not_a_caller(self):
        """`contains` is ownership, not a dependency."""
        production, _ = _split_callers(_call_graph(callers=2), "target")
        assert "mod" not in production


class TestExplainContextSection:
    def _result(self, G, node="target"):
        return explain_node(G, node, scores={node: 0.5})

    def test_context_names_the_parent_module(self):
        out = format_explanation(self._result(_call_graph(callers=2)))
        assert "CONTEXT" in out
        assert "Part of:" in out and "core" in out

    def test_context_lists_top_callers_and_callees(self):
        result = self._result(_call_graph(callers=4, callees=4))
        out = format_explanation(result)
        assert "Used by:" in out and "Depends on:" in out
        assert len(result.used_by) == 3 and len(result.depends_on) == 3

    def test_context_omitted_when_the_node_stands_alone(self):
        G = nx.DiGraph()
        G.add_node("lonely", label="lonely", type="function", source_file="a.py")
        assert "CONTEXT" not in format_explanation(explain_node(G, "lonely"))


class TestExploreFurther:
    def test_suggests_the_surrounding_context(self):
        commands = _explore_further(_call_graph(callers=2), "target", "MEDIUM")
        assert any(c.startswith("slurp \"target core\"") for c in commands)

    def test_suggests_the_most_connected_caller(self):
        commands = _explore_further(_call_graph(callers=3), "target", "MEDIUM")
        assert any("slurp explain" in c for c in commands)

    def test_high_risk_suggests_diff(self):
        commands = _explore_further(_call_graph(callers=8), "target", "HIGH")
        assert any("slurp diff" in c for c in commands)

    def test_never_more_than_three(self):
        assert len(_explore_further(_call_graph(callers=8, callees=8), "target", "HIGH")) <= 3

    def test_section_is_rendered(self):
        out = format_explanation(explain_node(_call_graph(callers=8), "target"))
        assert "EXPLORE FURTHER" in out


class TestRiskSummary:
    def test_reports_the_share_of_the_project(self):
        out = format_explanation(explain_node(_call_graph(callers=8), "target"))
        assert "% of the project" in out

    def test_separates_test_callers_from_the_count(self):
        result = explain_node(_call_graph(callers=2, tests=9), "target")
        out = format_explanation(result)
        assert result.production_callers == 2
        assert "2 production callers, 9 in tests — " in out

    def test_high_risk_recommends_tests(self):
        out = format_explanation(explain_node(_call_graph(callers=8), "target"))
        assert "RISK IF CHANGED: HIGH" in out
        assert "Add tests" in out

    def test_low_risk_says_it_is_safe(self):
        out = format_explanation(explain_node(_call_graph(callers=1), "target"))
        assert "Safe to refactor" in out

    def test_medium_risk_points_at_the_callers(self):
        out = format_explanation(explain_node(_call_graph(callers=3), "target"))
        assert "RISK IF CHANGED: MEDIUM" in out
        assert "Check each caller" in out


class TestStructuralOutputSize:
    def test_stays_within_thirty_lines(self):
        """A hub with eighty callers used to render ninety lines of names."""
        out = format_explanation(explain_node(_call_graph(callers=40, callees=20,
                                                          tests=40), "target"))
        assert len(out.splitlines()) <= 30

    def test_long_caller_list_is_truncated_with_a_count(self):
        out = format_explanation(explain_node(_call_graph(callers=40), "target"))
        assert "more" in out


# ---------------------------------------------------------------------------
# A language with no call graph: no callers means "unknown", never "unused"
# ---------------------------------------------------------------------------

def _csharp_graph() -> nx.DiGraph:
    G = nx.DiGraph()
    G.add_node("mod", label="Billing.cs", type="module", source_file="src/Billing.cs")
    G.add_node("run", label="Run", type="function", source_file="src/Billing.cs")
    G.add_edge("mod", "run", relation="contains")
    return G


class TestExplainWithoutCallGraph:
    def test_risk_is_unknown(self):
        result = explain_node(_csharp_graph(), "run")
        assert result.risk_level == "UNKNOWN"
        assert "C#" in result.risk_reason

    def test_no_claim_that_nothing_depends_on_it(self):
        text = format_explanation(explain_node(_csharp_graph(), "run"))
        assert "RISK IF CHANGED: UNKNOWN" in text
        assert "Nothing in the project depends on" not in text
        assert "Safe to refactor" not in text
        assert "C#" in text

    def test_structural_prose_does_not_call_it_unused(self):
        text = format_explanation(explain_node(_csharp_graph(), "run"))
        assert "unused" not in text
        assert "Isolated" not in text
        assert "nothing calls it" not in text.lower()

    def test_llm_context_says_unknown_not_absent(self):
        context = _build_context(_csharp_graph(), "run", {}, hops=2)
        assert "unknown" in context.lower()
        assert "risk LOW" not in context

    def test_call_graph_language_unchanged(self):
        G = nx.DiGraph()
        G.add_node("m", label="core.py", type="module", source_file="lib/core.py")
        G.add_node("f", label="helper", type="function", source_file="lib/core.py")
        G.add_edge("m", "f", relation="contains")
        result = explain_node(G, "f")
        assert result.risk_level == "LOW"
        assert result.risk_reason == ""

    def test_architecture_lists_are_unknown_not_empty(self):
        text = format_explanation(explain_node(_csharp_graph(), "run"))
        assert "Called by: nothing" not in text
        assert "Calls: nothing" not in text
        assert "Called by: unknown" in text

    def test_reason_is_stated_once(self):
        text = format_explanation(explain_node(_csharp_graph(), "run"))
        assert text.count("slurp extracts calls only for") == 1


# ---------------------------------------------------------------------------
# Tests are shown apart and never raise the risk band (v1.2, #20)
# ---------------------------------------------------------------------------

class TestTestsAreShownApart:
    def test_the_band_counts_production_callers_only(self):
        # Until v1.2 the tests were added in: 1 + 74 callers was HIGH.
        assert _risk_level(_call_graph(callers=3, tests=74), "target") == "MEDIUM"
        assert _risk_level(_call_graph(callers=1, tests=74), "target") == "LOW"
        assert _risk_level(_call_graph(callers=7, tests=74), "target") == "HIGH"

    def test_the_risk_line_gives_both_counts(self):
        out = format_explanation(explain_node(_call_graph(callers=7, tests=74), "target"))
        assert "RISK IF CHANGED: HIGH" in out
        assert "7 production callers, 74 in tests — " in out

    def test_one_production_caller_and_no_tests(self):
        out = format_explanation(explain_node(_call_graph(callers=1), "target"))
        assert "1 production caller — " in out and "in tests" not in out

    def test_called_by_lists_production_and_tested_by_counts_the_tests(self):
        result = explain_node(_call_graph(callers=2, tests=9), "target")
        assert result.callers == ["caller0()", "caller1()"]
        assert result.test_files == ["tests/test_core.py"]
        out = format_explanation(result)
        called_by = next(ln for ln in out.splitlines() if ln.startswith("→ Called by:"))
        assert "test_" not in called_by
        assert "→ Tested by: 9 tests in 1 file" in out

    def test_only_tests_call_it_is_low_but_never_called_safe(self):
        result = explain_node(_call_graph(tests=12), "target")
        out = format_explanation(result)
        assert result.risk_level == "LOW"
        assert ("No production callers · 12 tests call it — update them if you change "
                "its behaviour.") in out
        assert "Safe to refactor" not in out

    def test_no_callers_at_all_is_safe(self):
        out = format_explanation(explain_node(_call_graph(), "target"))
        assert "Nothing in the project depends on this function." in out
        assert "Safe to refactor" in out
        assert "Tested by" not in out

    def test_the_llm_context_separates_them_too(self):
        context = _build_context(_call_graph(callers=2, tests=3), "target")
        assert "CALLED BY (2):" in context
        assert "TESTED BY (3 tests in 1 file):" in context
        assert "BLAST RADIUS: 2 production callers, 3 in tests (risk MEDIUM)" in context
        called_by = context.split("CALLED BY")[1].split("TESTED BY")[0]
        assert "test_t" not in called_by


class TestFindNodeSkipsWhatSelectionLeavesOut:
    @staticmethod
    def _graph() -> nx.DiGraph:
        G = nx.DiGraph()
        G.add_node("lib.stats.recalc", label="recalc", type="function",
                   source_file="lib/stats.py")
        G.add_node("tests.test_stats.test_recalc", label="test_recalc", type="function",
                   source_file="tests/test_stats.py")
        G.add_node("app.import_recalc", label="lib.stats.recalc", type="import",
                   source_file="app.py")
        return G

    def test_the_relevance_fallback_never_lands_on_a_test_or_an_import(self):
        G = self._graph()
        scores = {"tests.test_stats.test_recalc": 0.9, "app.import_recalc": 0.8,
                  "lib.stats.recalc": 0.5}
        assert find_node(G, "how are stats recalculated", scores) == "lib.stats.recalc"

    def test_a_test_asked_for_by_name_is_found(self):
        G = self._graph()
        assert find_node(G, "test_recalc", {}) == "tests.test_stats.test_recalc"
        assert find_node(G, "tests.test_stats.test_recalc", {}) == "tests.test_stats.test_recalc"

    def test_a_graph_of_tests_alone_still_answers(self):
        G = nx.DiGraph()
        G.add_node("tests.t", label="t", type="function", source_file="tests/test_x.py")
        assert find_node(G, "something", {"tests.t": 0.4}) == "tests.t"
