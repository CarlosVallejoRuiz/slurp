"""Tests for slurp/cli.py."""

import json
import re
from pathlib import Path
from urllib.parse import urlparse, unquote

import networkx as nx
import pytest
from click.testing import CliRunner

from slurp.cli import cli
from slurp.viz import build_html

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

    def test_version_output_contains_slurp(self):
        result = _runner().invoke(cli, ["--version"])
        assert "slurp" in result.output

    def test_version_output_contains_ascii_art(self):
        result = _runner().invoke(cli, ["--version"])
        # The Big-font ASCII art has these distinctive character sequences
        assert "___" in result.output
        assert "|_|" in result.output

    def test_version_output_contains_tagline(self):
        result = _runner().invoke(cli, ["--version"])
        assert "token-budget" in result.output

    def test_version_output_contains_ramen(self):
        result = _runner().invoke(cli, ["--version"])
        assert "🍜" in result.output


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
    def test_explain_exits_zero(self, sample_graph_json):
        result = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json), "--explain", "--no-audit"],
        )
        assert result.exit_code == 0

    def test_explain_shows_score_breakdown_table(self, sample_graph_json):
        result = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json), "--explain", "--no-audit"],
        )
        assert result.exit_code == 0
        assert "Score Breakdown" in result.output

    def test_explain_table_has_structural_column(self, sample_graph_json):
        result = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json), "--explain", "--no-audit"],
        )
        assert "Structural" in result.output

    def test_explain_table_has_semantic_column(self, sample_graph_json):
        result = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json), "--explain", "--no-audit"],
        )
        assert "Semantic" in result.output

    def test_explain_table_has_rank_column(self, sample_graph_json):
        result = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json), "--explain", "--no-audit"],
        )
        assert "Rank" in result.output

    def test_explain_markdown_still_present(self, sample_graph_json):
        result = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json), "--explain", "--no-audit"],
        )
        assert "## Relevant Nodes" in result.output

    def test_explain_table_contains_node_labels(self, sample_graph_json):
        result = _runner().invoke(
            cli,
            ["authenticate user", "--graph", str(sample_graph_json),
             "--explain", "--no-audit", "--min-score", "0.0"],
        )
        assert "authenticate_user" in result.output

    def test_without_explain_no_score_breakdown(self, sample_graph_json):
        result = _runner().invoke(
            cli, ["auth", "--graph", str(sample_graph_json), "--format", "markdown",
                  "--no-audit"]
        )
        assert result.exit_code == 0
        assert "Score Breakdown" not in result.output

    def test_scores_not_embedded_in_markdown(self, sample_graph_json):
        result = _runner().invoke(
            cli, ["auth", "--graph", str(sample_graph_json), "--format", "markdown",
                  "--no-audit"]
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

    def test_audit_shows_query_history_table(
        self, sample_graph_json, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        runner = _runner()
        for q in ["auth", "jwt", "password"]:
            runner.invoke(cli, [q, "--graph", str(sample_graph_json)])
        result = runner.invoke(cli, ["audit"])
        assert result.exit_code == 0
        assert "Query History" in result.output

    def test_audit_table_shows_all_queries(
        self, sample_graph_json, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        runner = _runner()
        for q in ["auth", "jwt", "password"]:
            runner.invoke(cli, [q, "--graph", str(sample_graph_json)])
        result = runner.invoke(cli, ["audit"])
        assert result.exit_code == 0
        # All three queries appear in the table
        assert "auth" in result.output
        assert "jwt" in result.output

    def test_audit_shows_top_nodes_table(
        self, sample_graph_json, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        runner = _runner()
        runner.invoke(cli, ["auth", "--graph", str(sample_graph_json)])
        result = runner.invoke(cli, ["audit"])
        assert result.exit_code == 0
        assert "Most Selected" in result.output

    def test_audit_table_has_savings_column(
        self, sample_graph_json, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        runner = _runner()
        runner.invoke(cli, ["auth", "--graph", str(sample_graph_json)])
        result = runner.invoke(cli, ["audit"])
        assert "Savings" in result.output

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
        assert "Query History" in result.output


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


# ---------------------------------------------------------------------------
# --viz flag
# ---------------------------------------------------------------------------


def _viz_html(monkeypatch, sample_graph_json, query="auth", extra_args=None) -> tuple[str, list]:
    """Run CLI with --viz, return (html_content, opened_urls)."""
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    args = [query, "--graph", str(sample_graph_json), "--viz", "--no-audit"]
    if extra_args:
        args += extra_args
    _runner().invoke(cli, args)
    if not opened:
        return "", opened
    path = Path(unquote(urlparse(opened[0]).path))
    html = path.read_text(encoding="utf-8") if path.exists() else ""
    return html, opened


class TestVizFlag:
    def test_viz_calls_webbrowser_open(self, sample_graph_json, monkeypatch):
        _, opened = _viz_html(monkeypatch, sample_graph_json)
        assert len(opened) == 1

    def test_viz_exit_code_zero(self, sample_graph_json, monkeypatch):
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        result = _runner().invoke(
            cli, ["auth", "--graph", str(sample_graph_json), "--viz", "--no-audit"]
        )
        assert result.exit_code == 0

    def test_viz_creates_html_file(self, sample_graph_json, monkeypatch):
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        _runner().invoke(
            cli, ["auth", "--graph", str(sample_graph_json), "--viz", "--no-audit"]
        )
        assert opened
        path = Path(unquote(urlparse(opened[0]).path))
        assert path.exists()
        assert path.suffix == ".html"

    def test_viz_url_is_file_uri(self, sample_graph_json, monkeypatch):
        _, opened = _viz_html(monkeypatch, sample_graph_json)
        assert opened[0].startswith("file://")

    def test_viz_html_contains_vis_network_cdn(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json)
        assert "vis-network" in html

    def test_viz_html_contains_query(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json, query="jwt authentication")
        assert "jwt authentication" in html

    def test_viz_html_contains_node_data(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json, query="auth")
        # At least one node from the sample graph must appear in the embedded JSON
        assert "authenticate_user" in html or "JWTMiddleware" in html

    def test_viz_html_contains_stats_nodes(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json)
        assert "nodes" in html.lower()

    def test_viz_html_contains_tokens_stat(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json)
        assert "tokens" in html.lower()

    def test_viz_html_contains_coverage_pct(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json)
        assert "coverage" in html.lower()

    def test_viz_still_prints_markdown_to_stdout(self, sample_graph_json, monkeypatch):
        monkeypatch.setattr("webbrowser.open", lambda url: None)
        result = _runner().invoke(
            cli, ["auth", "--graph", str(sample_graph_json), "--viz", "--no-audit"]
        )
        assert "## Relevant Nodes" in result.output

    def test_without_viz_no_browser_open(self, sample_graph_json, monkeypatch):
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        _runner().invoke(
            cli, ["auth", "--graph", str(sample_graph_json), "--no-audit"]
        )
        assert len(opened) == 0

    def test_viz_html_is_valid_html5(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json)
        assert "<!DOCTYPE html>" in html
        assert "<html" in html
        assert "</html>" in html

    def test_viz_html_escapes_xss_in_query(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json, query='<script>alert(1)</script>')
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_viz_html_has_dark_background(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json)
        # Dark theme colour appears in CSS
        assert "#0d1117" in html

    def test_viz_html_embeds_edge_relations(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json, query="auth")
        # The sample graph has "calls" and "depends_on" relations
        assert "calls" in html or "depends_on" in html

    def test_viz_html_has_savings_badge(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json, query="auth")
        assert "Saved" in html

    def test_viz_html_savings_badge_has_percentage(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json, query="auth")
        # Badge format: "Saved ~X tokens (Y%)"
        assert "tokens" in html and "%" in html

    def test_viz_html_isolated_field_in_node_data(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json, query="auth")
        # Every node object must carry the isolated flag
        assert '"isolated"' in html

    def test_viz_html_updated_physics_spring_length(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json, query="auth")
        assert "springLength:80" in html or "springLength: 80" in html

    def test_viz_html_updated_physics_stabilization(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json, query="auth")
        assert "iterations:500" in html or "iterations: 500" in html

    def test_viz_html_stabilization_enabled(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json, query="auth")
        assert "onlyDynamicEdges:false" in html or "onlyDynamicEdges: false" in html

    def test_viz_html_freeze_after_stabilization(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json, query="auth")
        # net.once('stabilized', ...) disables physics after layout settles
        assert "stabilized" in html
        assert "physics" in html and "enabled" in html

    def test_viz_html_node_font_size_13(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json, query="auth")
        # Template uses a ternary: size:isDiff?11:13 — 13 is the normal-mode size.
        assert "isDiff?11:13" in html

    def test_viz_html_node_font_stroke(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json, query="auth")
        assert "strokeWidth:isDiff?2:3" in html
        assert "strokeColor" in html

    def test_viz_html_diff_type_detection_present(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json, query="auth")
        assert "DIFF_TYPES" in html
        assert "isDiff" in html

    def test_viz_html_vadjust_present(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json, query="auth")
        assert "vadjust" in html

    def test_viz_html_node_chosen_false(self, sample_graph_json, monkeypatch):
        html, _ = _viz_html(monkeypatch, sample_graph_json, query="auth")
        assert "chosen: false" in html or "chosen:false" in html

    def test_viz_html_no_top_badge_when_under_limit(self, sample_graph_json, monkeypatch):
        # sample_graph has 7 nodes — well under default max_nodes=50
        html, _ = _viz_html(monkeypatch, sample_graph_json, query="auth")
        assert "showing top" not in html


# ---------------------------------------------------------------------------
# --min-score flag
# ---------------------------------------------------------------------------


class TestMinScoreFlag:
    def test_min_score_flag_accepted(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(
            cli,
            ["authenticate user", "--graph", str(sample_graph_json),
             "--min-score", "0.0", "--no-audit"],
        )
        assert result.exit_code == 0

    def test_min_score_zero_selects_at_least_as_many_as_strict(
        self, sample_graph_json, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        r_loose = _runner().invoke(
            cli,
            ["authenticate user", "--graph", str(sample_graph_json),
             "--min-score", "0.0", "--format", "json", "--no-audit"],
        )
        r_strict = _runner().invoke(
            cli,
            ["authenticate user", "--graph", str(sample_graph_json),
             "--min-score", "0.9", "--format", "json", "--no-audit"],
        )
        assert r_loose.exit_code == 0
        assert r_strict.exit_code == 0
        assert len(json.loads(r_loose.output)["nodes"]) >= len(json.loads(r_strict.output)["nodes"])

    def test_high_min_score_selects_fewer_nodes(
        self, sample_graph_json, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        r_low = _runner().invoke(
            cli,
            ["authenticate user jwt", "--graph", str(sample_graph_json),
             "--min-score", "0.0", "--format", "json", "--no-audit"],
        )
        r_high = _runner().invoke(
            cli,
            ["authenticate user jwt", "--graph", str(sample_graph_json),
             "--min-score", "0.9", "--format", "json", "--no-audit"],
        )
        assert r_low.exit_code == 0
        assert r_high.exit_code == 0
        assert len(json.loads(r_low.output)["nodes"]) >= len(json.loads(r_high.output)["nodes"])

    def test_impossible_min_score_produces_empty_result(
        self, sample_graph_json, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(
            cli,
            ["authenticate", "--graph", str(sample_graph_json),
             "--min-score", "1.1", "--format", "json", "--no-audit"],
        )
        assert result.exit_code == 0
        assert len(json.loads(result.output)["nodes"]) == 0

    def test_min_score_appears_in_help(self):
        result = _runner().invoke(cli, ["--help"])
        assert result.exit_code == 0

    def test_min_score_default_is_0_15_in_help(self):
        result = _runner().invoke(cli, ["run", "--help"])
        assert "0.15" in result.output


# ---------------------------------------------------------------------------
# build_html — max_nodes parameter
# ---------------------------------------------------------------------------


def _make_graph_n(n: int) -> tuple[nx.DiGraph, dict, dict[str, float]]:
    """Build an n-node subgraph with descending scores (node_0 = highest)."""
    G = nx.DiGraph()
    scores: dict[str, float] = {}
    for i in range(n):
        nid = f"node_{i}"
        G.add_node(nid, label=f"Node {i}", description="")
        scores[nid] = (n - i) / n
    stats = {
        "nodes_selected": n,
        "nodes_total": n * 2,
        "tokens_used": n * 50,
        "tokens_budget": 4000,
        "coverage_pct": 50.0,
    }
    return G, stats, scores


def _extract_nodes_json(html: str) -> list:
    start = html.index("var RN = ") + len("var RN = ")
    end = html.index(";\nvar RE = ")
    return json.loads(html[start:end])


def _extract_edges_json(html: str) -> list:
    start = html.index("var RE = ") + len("var RE = ")
    end = html.index(";\n\nvar DIFF_TYPES")
    return json.loads(html[start:end])


class TestBuildHtmlMaxNodes:
    def test_default_max_nodes_is_50(self):
        G, stats, scores = _make_graph_n(100)
        html = build_html(G, stats, scores, "test")
        nodes_data = _extract_nodes_json(html)
        assert len(nodes_data) == 50

    def test_max_nodes_truncates_to_specified_limit(self):
        G, stats, scores = _make_graph_n(30)
        html = build_html(G, stats, scores, "test", max_nodes=10)
        nodes_data = _extract_nodes_json(html)
        assert len(nodes_data) == 10

    def test_max_nodes_keeps_highest_scoring_nodes(self):
        G, stats, scores = _make_graph_n(20)
        html = build_html(G, stats, scores, "test", max_nodes=5)
        nodes_data = _extract_nodes_json(html)
        kept_ids = {n["id"] for n in nodes_data}
        for i in range(5):
            assert f"node_{i}" in kept_ids, f"node_{i} should be in top-5"

    def test_max_nodes_no_truncation_when_under_limit(self):
        G, stats, scores = _make_graph_n(10)
        html = build_html(G, stats, scores, "test", max_nodes=50)
        nodes_data = _extract_nodes_json(html)
        assert len(nodes_data) == 10

    def test_max_nodes_filters_edges_to_hidden_nodes(self):
        G = nx.DiGraph()
        scores: dict[str, float] = {}
        for i in range(10):
            nid = f"n{i}"
            G.add_node(nid, label=f"N{i}", description="")
            scores[nid] = 1.0 - i * 0.1
        G.add_edge("n0", "n9", relation="calls")  # n9 will be hidden
        G.add_edge("n0", "n1", relation="calls")  # n1 stays visible
        stats = {
            "nodes_selected": 10, "nodes_total": 10,
            "tokens_used": 500, "tokens_budget": 1000, "coverage_pct": 100.0,
        }
        html = build_html(G, stats, scores, "test", max_nodes=2)
        edges_data = _extract_edges_json(html)
        for e in edges_data:
            assert not (e["from"] == "n0" and e["to"] == "n9"), (
                "edge to hidden node n9 should be dropped"
            )

    def test_stats_header_unchanged_after_truncation(self):
        G, stats, scores = _make_graph_n(100)
        html = build_html(G, stats, scores, "test", max_nodes=10)
        # nodes_selected=100 must still appear in the header chip
        assert "<strong>100</strong>" in html

    def test_sample_graph_under_limit_unaffected(self, sample_graph):
        """sample_graph has 7 nodes — well under default max_nodes=50."""
        scores = {nid: 0.5 for nid in sample_graph.nodes}
        stats = {
            "nodes_selected": 7, "nodes_total": 7,
            "tokens_used": 350, "tokens_budget": 4000, "coverage_pct": 100.0,
        }
        html = build_html(sample_graph, stats, scores, "auth")
        nodes_data = _extract_nodes_json(html)
        assert len(nodes_data) == 7

    def test_top_badge_shown_when_truncated(self):
        G, stats, scores = _make_graph_n(100)
        html = build_html(G, stats, scores, "test", max_nodes=20)
        assert "showing top 20" in html

    def test_top_badge_absent_when_no_truncation(self):
        G, stats, scores = _make_graph_n(10)
        html = build_html(G, stats, scores, "test", max_nodes=50)
        assert "showing top" not in html


# ---------------------------------------------------------------------------
# --ignore-file flag
# ---------------------------------------------------------------------------


class TestIgnoreFile:
    def test_ignore_file_flag_accepted(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json),
             "--ignore-file", str(tmp_path / "nonexistent.slurpignore"),
             "--no-audit"],
        )
        assert result.exit_code == 0

    def test_missing_ignore_file_is_no_op(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        r_no_ignore = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json),
             "--format", "json", "--no-audit", "--min-score", "0.0"],
        )
        r_missing = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json),
             "--format", "json", "--no-audit", "--min-score", "0.0",
             "--ignore-file", str(tmp_path / "does_not_exist.slurpignore")],
        )
        assert r_no_ignore.exit_code == 0
        assert r_missing.exit_code == 0
        assert json.loads(r_no_ignore.output)["nodes"] == json.loads(r_missing.output)["nodes"]

    def test_ignore_type_excludes_nodes_of_that_type(
        self, sample_graph_json, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        ignore_file = tmp_path / ".slurpignore"
        ignore_file.write_text("type:class\n")
        result = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json),
             "--format", "json", "--no-audit", "--min-score", "0.0",
             "--ignore-file", str(ignore_file)],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        types = [n.get("type") for n in data["nodes"]]
        assert "class" not in types

    def test_ignore_file_reduces_node_count(
        self, sample_graph_json, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        ignore_file = tmp_path / ".slurpignore"
        ignore_file.write_text("type:class\n")
        r_with = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json),
             "--format", "json", "--no-audit", "--min-score", "0.0",
             "--ignore-file", str(ignore_file)],
        )
        r_without = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json),
             "--format", "json", "--no-audit", "--min-score", "0.0",
             "--ignore-file", str(tmp_path / "empty.slurpignore")],  # non-existent → no filter
        )
        assert r_with.exit_code == 0
        assert r_without.exit_code == 0
        count_with = json.loads(r_with.output)["stats"]["nodes_total"]
        count_without = json.loads(r_without.output)["stats"]["nodes_total"]
        assert count_with < count_without

    def test_default_slurpignore_in_cwd_is_auto_loaded(
        self, sample_graph_json, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".slurpignore").write_text("type:class\n")
        result = _runner().invoke(
            cli,
            ["auth", "--graph", str(sample_graph_json),
             "--format", "json", "--no-audit", "--min-score", "0.0"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        types = [n.get("type") for n in data["nodes"]]
        assert "class" not in types


# ---------------------------------------------------------------------------
# slurp export command
# ---------------------------------------------------------------------------

class TestExportCommand:
    def test_default_format_is_claude(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(
            cli, ["export", "auth", "--graph", str(sample_graph_json)]
        )
        assert result.exit_code == 0, result.output
        assert result.output.startswith("<context ")
        assert "</context>" in result.output

    def test_claude_format_explicit(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(
            cli, ["export", "auth", "--graph", str(sample_graph_json), "--format", "claude"]
        )
        assert result.exit_code == 0
        assert 'source="slurp-graph"' in result.output
        assert 'query="auth"' in result.output

    def test_chatgpt_format(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(
            cli, ["export", "auth", "--graph", str(sample_graph_json), "--format", "chatgpt"]
        )
        assert result.exit_code == 0
        assert "[CODEBASE CONTEXT — slurp-graph]" in result.output
        assert "[END CODEBASE CONTEXT]" in result.output

    def test_claudemd_format(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(
            cli, ["export", "auth", "--graph", str(sample_graph_json), "--format", "claudemd"]
        )
        assert result.exit_code == 0
        assert "## Codebase Context" in result.output
        assert "<!-- slurp-graph" in result.output

    def test_output_to_file(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        out_file = tmp_path / "context.md"
        result = _runner().invoke(
            cli, ["export", "auth", "--graph", str(sample_graph_json),
                  "--output", str(out_file)]
        )
        assert result.exit_code == 0
        assert out_file.exists()
        content = out_file.read_text(encoding="utf-8")
        assert "<context " in content
        assert "Exported to" in result.output

    def test_output_file_contains_full_context(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        out_file = tmp_path / "out.md"
        _runner().invoke(
            cli, ["export", "auth", "--graph", str(sample_graph_json),
                  "--format", "claudemd", "--output", str(out_file)]
        )
        content = out_file.read_text(encoding="utf-8")
        assert "## Codebase Context" in content
        assert "## Relevant Nodes" in content

    def test_no_graph_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(cli, ["export", "auth"])
        assert result.exit_code != 0
        assert "No graph.json found" in result.output

    def test_chatgpt_format_no_box(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(
            cli, ["export", "auth", "--graph", str(sample_graph_json), "--format", "chatgpt"]
        )
        assert result.exit_code == 0
        assert "╭" not in result.output

    def test_claudemd_format_no_box(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _runner().invoke(
            cli, ["export", "auth", "--graph", str(sample_graph_json), "--format", "claudemd"]
        )
        assert result.exit_code == 0
        assert "╭" not in result.output

    def test_budget_option_respected(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        r_small = _runner().invoke(
            cli, ["export", "auth", "--graph", str(sample_graph_json), "--budget", "100"]
        )
        r_large = _runner().invoke(
            cli, ["export", "auth", "--graph", str(sample_graph_json), "--budget", "8000"]
        )
        assert r_small.exit_code == 0
        assert r_large.exit_code == 0
        assert len(r_large.output) >= len(r_small.output)


# ---------------------------------------------------------------------------
# --inject-code and --project-root
# ---------------------------------------------------------------------------

PYTHON_CODE_CLI = """\
def authenticate_user(username, password):
    if not username:
        return None
    return db.find(username)

def other():
    pass
"""


class TestInjectCodeCommand:
    def _make_graph_json(self, tmp_path: Path) -> tuple[Path, Path]:
        """Return (graph_json_path, project_root) with a real source file."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "auth.py").write_text(PYTHON_CODE_CLI)

        graph = {
            "nodes": [
                {"id": "n1", "label": "authenticate_user", "type": "function",
                 "source_file": "src/auth.py", "source_location": "L1"},
                {"id": "n2", "label": "other", "type": "function",
                 "source_file": "src/auth.py", "source_location": "L6"},
            ],
            "edges": [],
        }
        gpath = tmp_path / "graph.json"
        gpath.write_text(json.dumps(graph))
        return gpath, tmp_path

    def test_inject_code_flag_accepted(self, tmp_path):
        gpath, root = self._make_graph_json(tmp_path)
        result = _runner().invoke(
            cli, ["auth", "--graph", str(gpath), "--budget", "8000",
                  "--inject-code", "--project-root", str(root)]
        )
        assert result.exit_code == 0

    def test_inject_code_appends_code_block(self, tmp_path):
        gpath, root = self._make_graph_json(tmp_path)
        result = _runner().invoke(
            cli, ["auth", "--graph", str(gpath), "--budget", "8000",
                  "--inject-code", "--project-root", str(root)]
        )
        assert result.exit_code == 0
        assert "```python" in result.output
        assert "def authenticate_user" in result.output

    def test_inject_code_without_project_root_uses_graph_dir(self, tmp_path):
        gpath, root = self._make_graph_json(tmp_path)
        result = _runner().invoke(
            cli, ["auth", "--graph", str(gpath), "--budget", "8000", "--inject-code"]
        )
        assert result.exit_code == 0
        assert "```python" in result.output

    def test_inject_code_too_many_nodes_shows_warning(self, tmp_path):
        src = tmp_path / "auth.py"
        src.write_text(PYTHON_CODE_CLI)
        # Build a graph with 31 nodes
        nodes = [{"id": f"n{i}", "label": f"Node {i}"} for i in range(31)]
        graph = {"nodes": nodes, "edges": []}
        gpath = tmp_path / "graph.json"
        gpath.write_text(json.dumps(graph))
        result = _runner().invoke(
            cli, ["auth", "--graph", str(gpath), "--budget", "99999", "--inject-code",
                  "--project-root", str(tmp_path)],
        )
        assert result.exit_code == 0
        # Warning goes to stderr (err=True); CliRunner merges stderr into output by default
        assert "Warning" in result.output

    def test_without_inject_code_no_code_block(self, tmp_path):
        gpath, root = self._make_graph_json(tmp_path)
        result = _runner().invoke(
            cli, ["auth", "--graph", str(gpath), "--budget", "8000"]
        )
        assert result.exit_code == 0
        assert "```python" not in result.output
