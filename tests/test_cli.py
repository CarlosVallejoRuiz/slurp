"""Tests for slurp/cli.py."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from slurp.cli import cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# --version
# ---------------------------------------------------------------------------


class TestVersion:
    def test_version_flag_exits_zero(self):
        result = _runner().invoke(cli, ["--version"])
        assert result.exit_code == 0

    def test_version_output_contains_version_string(self):
        result = _runner().invoke(cli, ["--version"])
        assert "0.1.0" in result.output


# ---------------------------------------------------------------------------
# --help
# ---------------------------------------------------------------------------


class TestHelp:
    def test_help_flag_exits_zero(self):
        result = _runner().invoke(cli, ["--help"])
        assert result.exit_code == 0

    def test_help_mentions_budget(self):
        result = _runner().invoke(cli, ["--help"])
        assert "budget" in result.output.lower()

    def test_no_args_shows_help(self):
        result = _runner().invoke(cli, [])
        assert result.exit_code == 0
        assert "Usage" in result.output


# ---------------------------------------------------------------------------
# Main query command — basic invocation
# ---------------------------------------------------------------------------


class TestMainCommand:
    def test_basic_query_exits_zero(self, sample_graph_json):
        result = _runner().invoke(
            cli, ["auth flow", "--graph", str(sample_graph_json)]
        )
        assert result.exit_code == 0, result.output

    def test_output_is_nonempty(self, sample_graph_json):
        result = _runner().invoke(
            cli, ["auth flow", "--graph", str(sample_graph_json)]
        )
        assert len(result.output.strip()) > 0

    def test_default_format_is_markdown(self, sample_graph_json):
        result = _runner().invoke(
            cli, ["auth flow", "--graph", str(sample_graph_json)]
        )
        assert "## Relevant Nodes" in result.output

    def test_short_graph_flag(self, sample_graph_json):
        result = _runner().invoke(
            cli, ["auth flow", "-g", str(sample_graph_json)]
        )
        assert result.exit_code == 0

    def test_budget_flag_accepted(self, sample_graph_json):
        result = _runner().invoke(
            cli, ["auth", "--graph", str(sample_graph_json), "--budget", "500"]
        )
        assert result.exit_code == 0

    def test_short_budget_flag(self, sample_graph_json):
        result = _runner().invoke(
            cli, ["auth", "-g", str(sample_graph_json), "-b", "500"]
        )
        assert result.exit_code == 0

    def test_model_flag_accepted(self, sample_graph_json):
        result = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json), "--model", "cl100k_base"],
        )
        assert result.exit_code == 0

    def test_neighbor_decay_flag_accepted(self, sample_graph_json):
        result = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json), "--neighbor-decay", "0.5"],
        )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# --format flag
# ---------------------------------------------------------------------------


class TestFormatFlag:
    def test_format_markdown(self, sample_graph_json):
        result = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json), "--format", "markdown"],
        )
        assert result.exit_code == 0
        assert "## Relevant Nodes" in result.output

    def test_format_json(self, sample_graph_json):
        result = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json), "--format", "json"],
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert "nodes" in parsed
        assert "stats" in parsed

    def test_format_yaml(self, sample_graph_json):
        result = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json), "--format", "yaml"],
        )
        assert result.exit_code == 0
        assert "nodes:" in result.output
        assert "stats:" in result.output

    def test_short_format_flag(self, sample_graph_json):
        result = _runner().invoke(
            cli, ["auth", "-g", str(sample_graph_json), "-f", "json"]
        )
        assert result.exit_code == 0

    def test_invalid_format_exits_nonzero(self, sample_graph_json):
        result = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json), "--format", "toml"],
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# --explain flag
# ---------------------------------------------------------------------------


class TestExplainFlag:
    def test_explain_shows_scores(self, sample_graph_json):
        result = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json), "--explain"],
        )
        assert result.exit_code == 0
        assert "score:" in result.output

    def test_without_explain_no_scores_in_markdown(self, sample_graph_json):
        result = _runner().invoke(
            cli, ["auth", "--graph", str(sample_graph_json), "--format", "markdown"]
        )
        assert result.exit_code == 0
        assert "score:" not in result.output


# ---------------------------------------------------------------------------
# --no-audit flag
# ---------------------------------------------------------------------------


class TestNoAuditFlag:
    def test_no_audit_skips_audit_file(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json), "--no-audit"],
        )
        assert result.exit_code == 0
        assert not (tmp_path / ".slurp" / "audit.jsonl").exists()

    def test_without_no_audit_creates_audit_file(
        self, sample_graph_json, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(
            cli, ["auth", "--graph", str(sample_graph_json)]
        )
        assert result.exit_code == 0
        assert (tmp_path / ".slurp" / "audit.jsonl").exists()

    def test_audit_entry_contains_query(
        self, sample_graph_json, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        _runner().invoke(cli, ["jwt token", "--graph", str(sample_graph_json)])
        audit_file = tmp_path / ".slurp" / "audit.jsonl"
        entry = json.loads(audit_file.read_text().strip())
        assert entry["query"] == "jwt token"


# ---------------------------------------------------------------------------
# Missing graph error
# ---------------------------------------------------------------------------


class TestMissingGraph:
    def test_missing_graph_file_exits_nonzero(self, tmp_path):
        result = _runner().invoke(
            cli, ["auth", "--graph", str(tmp_path / "nonexistent.json")]
        )
        assert result.exit_code != 0

    def test_missing_graph_error_message(self, tmp_path):
        result = _runner().invoke(
            cli, ["auth", "--graph", str(tmp_path / "nonexistent.json")]
        )
        assert "Error" in result.output

    def test_no_graph_flag_no_autodiscovery_exits_nonzero(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(cli, ["auth query"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------


class TestAutoDiscovery:
    def test_discovers_graph_json_in_cwd(self, sample_graph_json, monkeypatch):
        monkeypatch.chdir(sample_graph_json.parent)
        result = _runner().invoke(cli, ["auth"])
        assert result.exit_code == 0

    def test_discovers_graphify_out_graph_json(self, tmp_path, sample_graph_json, monkeypatch):
        monkeypatch.chdir(tmp_path)
        graphify_dir = tmp_path / "graphify-out"
        graphify_dir.mkdir()
        import shutil
        shutil.copy(sample_graph_json, graphify_dir / "graph.json")
        result = _runner().invoke(cli, ["auth"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# stats subcommand
# ---------------------------------------------------------------------------


class TestStatsCommand:
    def test_stats_exits_zero(self, sample_graph_json):
        result = _runner().invoke(cli, ["stats", "--graph", str(sample_graph_json)])
        assert result.exit_code == 0

    def test_stats_shows_node_count(self, sample_graph_json):
        result = _runner().invoke(cli, ["stats", "--graph", str(sample_graph_json)])
        assert "Nodes:" in result.output
        assert "7" in result.output

    def test_stats_shows_edge_count(self, sample_graph_json):
        result = _runner().invoke(cli, ["stats", "--graph", str(sample_graph_json)])
        assert "Edges:" in result.output
        assert "6" in result.output

    def test_stats_shows_graph_path(self, sample_graph_json):
        result = _runner().invoke(cli, ["stats", "--graph", str(sample_graph_json)])
        assert "Graph:" in result.output

    def test_stats_short_graph_flag(self, sample_graph_json):
        result = _runner().invoke(cli, ["stats", "-g", str(sample_graph_json)])
        assert result.exit_code == 0

    def test_stats_missing_graph_exits_nonzero(self, tmp_path):
        result = _runner().invoke(
            cli, ["stats", "--graph", str(tmp_path / "missing.json")]
        )
        assert result.exit_code != 0

    def test_stats_auto_discovers_cwd(self, sample_graph_json, monkeypatch):
        monkeypatch.chdir(sample_graph_json.parent)
        result = _runner().invoke(cli, ["stats"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# audit subcommand
# ---------------------------------------------------------------------------


class TestAuditCommand:
    def test_audit_no_entries_exits_zero(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(cli, ["audit"])
        assert result.exit_code == 0

    def test_audit_no_entries_says_no_entries(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(cli, ["audit"])
        assert "No audit entries" in result.output

    def test_audit_shows_total_query_count(
        self, sample_graph_json, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        runner = _runner()
        for q in ["auth", "jwt", "password"]:
            runner.invoke(cli, [q, "--graph", str(sample_graph_json)])
        result = runner.invoke(cli, ["audit"])
        assert result.exit_code == 0
        assert "3" in result.output

    def test_audit_top_nodes_flag(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = _runner()
        runner.invoke(cli, ["auth", "--graph", str(sample_graph_json)])
        result = runner.invoke(cli, ["audit", "--top-nodes", "5"])
        assert result.exit_code == 0

    def test_audit_custom_audit_dir(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        custom_dir = tmp_path / "custom_audit"
        from slurp.audit import log_query
        from slurp.loader import load_graph
        from slurp.scorer import score_nodes
        from slurp.budget import select_subgraph

        G = load_graph(sample_graph_json)
        sc = score_nodes(G, "auth")
        _, st = select_subgraph(G, sc, budget=10_000)
        log_query("auth", sample_graph_json, st, list(G.nodes)[:3], audit_dir=custom_dir)

        result = _runner().invoke(
            cli, ["audit", "--audit-dir", str(custom_dir)]
        )
        assert result.exit_code == 0
        assert "1" in result.output


# ---------------------------------------------------------------------------
# Integration — full pipeline
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_full_pipeline_markdown(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(
            cli,
            [
                "authentication jwt",
                "--graph", str(sample_graph_json),
                "--budget", "10000",
                "--format", "markdown",
            ],
        )
        assert result.exit_code == 0
        assert "authenticate_user" in result.output or "JWTMiddleware" in result.output

    def test_full_pipeline_json_valid(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(
            cli,
            [
                "auth",
                "--graph", str(sample_graph_json),
                "--format", "json",
                "--no-audit",
            ],
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert isinstance(parsed["nodes"], list)
        assert len(parsed["nodes"]) > 0

    def test_query_then_audit_round_trip(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = _runner()
        runner.invoke(cli, ["auth flow", "--graph", str(sample_graph_json)])
        result = runner.invoke(cli, ["audit"])
        assert result.exit_code == 0
        assert "1" in result.output

    def test_tight_budget_selects_fewer_nodes(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = _runner()
        r_small = runner.invoke(
            cli,
            ["auth", "-g", str(sample_graph_json), "-b", "50", "-f", "json", "--no-audit"],
        )
        r_large = runner.invoke(
            cli,
            ["auth", "-g", str(sample_graph_json), "-b", "100000", "-f", "json", "--no-audit"],
        )
        assert r_small.exit_code == 0
        assert r_large.exit_code == 0
        small_count = len(json.loads(r_small.output)["nodes"])
        large_count = len(json.loads(r_large.output)["nodes"])
        assert small_count <= large_count
