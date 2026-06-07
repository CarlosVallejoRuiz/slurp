"""Tests for slurp/formatter.py."""

import json

import networkx as nx
import pytest

from slurp.formatter import format_subgraph


# ---------------------------------------------------------------------------
# Module-level fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_subgraph(sample_graph: nx.DiGraph) -> nx.DiGraph:
    """Three-node induced subgraph: authenticate_user, hash_password, JWTMiddleware."""
    nodes = ["authenticate_user", "hash_password", "JWTMiddleware"]
    return sample_graph.subgraph(nodes).copy()


@pytest.fixture
def partial_stats() -> dict:
    """Stats where 3 of 7 nodes were selected."""
    return {
        "nodes_selected": 3,
        "nodes_total": 7,
        "tokens_used": 58,
        "tokens_budget": 100,
        "coverage_pct": 42.9,
    }


@pytest.fixture
def full_stats() -> dict:
    """Stats where all 7 nodes were selected."""
    return {
        "nodes_selected": 7,
        "nodes_total": 7,
        "tokens_used": 130,
        "tokens_budget": 500,
        "coverage_pct": 100.0,
    }


@pytest.fixture
def auth_scores() -> dict:
    """Known scores for the three auth nodes (descending order: auth > jwt > hash)."""
    return {
        "authenticate_user": 0.94,
        "JWTMiddleware": 0.87,
        "hash_password": 0.72,
    }


# ---------------------------------------------------------------------------
# General
# ---------------------------------------------------------------------------


class TestFormatSubgraphGeneral:
    def test_returns_string(self, auth_subgraph, partial_stats):
        assert isinstance(format_subgraph(auth_subgraph, partial_stats), str)

    def test_default_format_is_markdown(self, auth_subgraph, partial_stats):
        assert format_subgraph(auth_subgraph, partial_stats) == format_subgraph(
            auth_subgraph, partial_stats, format="markdown"
        )

    def test_invalid_format_raises_value_error(self, auth_subgraph, partial_stats):
        with pytest.raises(ValueError, match="Unknown format"):
            format_subgraph(auth_subgraph, partial_stats, format="xml")

    def test_invalid_format_error_mentions_valid_options(self, auth_subgraph, partial_stats):
        with pytest.raises(ValueError, match="markdown"):
            format_subgraph(auth_subgraph, partial_stats, format="toml")

    def test_empty_subgraph_does_not_raise(self, partial_stats):
        result = format_subgraph(nx.DiGraph(), partial_stats)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Markdown — header
# ---------------------------------------------------------------------------


class TestMarkdownHeader:
    def test_header_has_unicode_box_top(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown")
        assert "╭" in out and "╮" in out

    def test_header_has_unicode_box_bottom(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown")
        assert "╰" in out and "╯" in out

    def test_header_shows_nodes_selected_over_total(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown")
        assert "3/7" in out

    def test_header_shows_tokens_used_over_budget(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown")
        assert "58" in out
        assert "100" in out

    def test_header_shows_coverage_pct(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown")
        assert "42.9" in out

    def test_header_shows_budget_value(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown")
        first_line = out.split("\n")[0]
        assert "100" in first_line

    def test_query_appears_in_header_when_provided(self, auth_subgraph, partial_stats):
        out = format_subgraph(
            auth_subgraph, partial_stats, format="markdown", query="auth flow"
        )
        header_lines = out.split("\n")[:3]
        assert any("auth flow" in line for line in header_lines)

    def test_query_not_in_header_when_empty(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown", query="")
        first_line = out.split("\n")[0]
        assert 'for: ""' not in first_line
        assert "Slurp" in first_line


# ---------------------------------------------------------------------------
# Markdown — nodes section
# ---------------------------------------------------------------------------


class TestMarkdownNodes:
    def test_relevant_nodes_section_header_present(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown")
        assert "## Relevant Nodes" in out

    def test_all_node_labels_appear(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown")
        assert "authenticate_user" in out
        assert "hash_password" in out
        assert "JWTMiddleware" in out

    def test_node_type_rendered_in_parens(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown")
        assert "(function)" in out
        assert "(class)" in out

    def test_node_description_rendered(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown")
        assert "Validates user credentials" in out
        assert "Hashes password using bcrypt" in out

    def test_file_path_rendered_with_arrow(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown")
        assert "→ File: src/auth/service.py" in out
        assert "→ File: src/middleware/jwt.py" in out

    def test_score_shown_when_scores_provided(self, auth_subgraph, partial_stats, auth_scores):
        out = format_subgraph(
            auth_subgraph, partial_stats, format="markdown", scores=auth_scores
        )
        assert "score: 0.94" in out
        assert "score: 0.87" in out
        assert "score: 0.72" in out

    def test_score_not_shown_when_scores_omitted(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown", scores=None)
        assert "score:" not in out

    def test_nodes_sorted_by_score_descending(self, auth_subgraph, partial_stats, auth_scores):
        out = format_subgraph(
            auth_subgraph, partial_stats, format="markdown", scores=auth_scores
        )
        # Expected order: authenticate_user (0.94) → JWTMiddleware (0.87) → hash_password (0.72)
        pos = {n: out.index(f"### {n}") for n in auth_scores}
        assert pos["authenticate_user"] < pos["JWTMiddleware"] < pos["hash_password"]

    def test_node_heading_format(self, auth_subgraph, partial_stats, auth_scores):
        out = format_subgraph(
            auth_subgraph, partial_stats, format="markdown", scores=auth_scores
        )
        assert "### authenticate_user (function) · score: 0.94" in out

    def test_no_score_prefix_without_scores(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown")
        # Heading must still include type but no "· score:" suffix
        assert "### authenticate_user (function)" in out
        assert "· score:" not in out


# ---------------------------------------------------------------------------
# Markdown — relationships section
# ---------------------------------------------------------------------------


class TestMarkdownRelationships:
    def test_key_relationships_section_present(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown")
        assert "## Key Relationships" in out

    def test_edge_rendered_with_arrows_and_relation(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown")
        assert "- authenticate_user → calls → hash_password" in out

    def test_all_induced_edges_rendered(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown")
        # auth_subgraph has edges: authenticate_user→hash_password, JWTMiddleware→authenticate_user
        assert "authenticate_user → calls → hash_password" in out
        assert "JWTMiddleware → calls → authenticate_user" in out

    def test_no_relationships_section_when_subgraph_has_no_edges(self, partial_stats):
        G = nx.DiGraph()
        G.add_node("solo", label="solo", type="function", description="alone")
        out = format_subgraph(G, {**partial_stats, "nodes_selected": 1}, format="markdown")
        assert "## Key Relationships" not in out

    def test_edges_not_in_subgraph_not_rendered(self, sample_graph, partial_stats):
        # subgraph only has authenticate_user and hash_password (no JWTMiddleware)
        subG = sample_graph.subgraph(["authenticate_user", "hash_password"]).copy()
        out = format_subgraph(subG, partial_stats, format="markdown")
        # JWTMiddleware→authenticate_user edge must NOT appear
        assert "JWTMiddleware" not in out


# ---------------------------------------------------------------------------
# Markdown — tip
# ---------------------------------------------------------------------------


class TestMarkdownTip:
    def test_tip_shown_when_nodes_excluded(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown")
        assert "💡" in out

    def test_tip_shows_correct_excluded_count(self, auth_subgraph, partial_stats):
        # 7 - 3 = 4 excluded
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown")
        assert "4 additional" in out

    def test_tip_mentions_budget_flag(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="markdown")
        assert "--budget" in out

    def test_tip_not_shown_when_all_nodes_selected(self, auth_subgraph, full_stats):
        out = format_subgraph(auth_subgraph, full_stats, format="markdown")
        assert "💡" not in out

    def test_tip_not_shown_when_stats_total_equals_selected(self, auth_subgraph):
        stats = {
            "nodes_selected": 3,
            "nodes_total": 3,
            "tokens_used": 58,
            "tokens_budget": 100,
            "coverage_pct": 100.0,
        }
        out = format_subgraph(auth_subgraph, stats, format="markdown")
        assert "💡" not in out


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


class TestJsonFormat:
    def _parse(self, auth_subgraph, stats, **kwargs):
        return json.loads(format_subgraph(auth_subgraph, stats, format="json", **kwargs))

    def test_returns_valid_json(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="json")
        json.loads(out)  # raises if invalid

    def test_has_stats_nodes_edges_keys(self, auth_subgraph, partial_stats):
        parsed = self._parse(auth_subgraph, partial_stats)
        assert set(parsed.keys()) == {"stats", "nodes", "edges"}

    def test_stats_values_match_input(self, auth_subgraph, partial_stats):
        parsed = self._parse(auth_subgraph, partial_stats)
        assert parsed["stats"]["nodes_selected"] == 3
        assert parsed["stats"]["nodes_total"] == 7
        assert parsed["stats"]["tokens_budget"] == 100
        assert parsed["stats"]["coverage_pct"] == pytest.approx(42.9)

    def test_node_count_matches_subgraph(self, auth_subgraph, partial_stats):
        parsed = self._parse(auth_subgraph, partial_stats)
        assert len(parsed["nodes"]) == auth_subgraph.number_of_nodes()

    def test_edge_count_matches_subgraph(self, auth_subgraph, partial_stats):
        parsed = self._parse(auth_subgraph, partial_stats)
        assert len(parsed["edges"]) == auth_subgraph.number_of_edges()

    def test_every_node_has_id_and_label(self, auth_subgraph, partial_stats):
        parsed = self._parse(auth_subgraph, partial_stats)
        for node in parsed["nodes"]:
            assert "id" in node
            assert "label" in node

    def test_every_edge_has_source_and_target(self, auth_subgraph, partial_stats):
        parsed = self._parse(auth_subgraph, partial_stats)
        for edge in parsed["edges"]:
            assert "source" in edge
            assert "target" in edge

    def test_score_present_when_scores_provided(self, auth_subgraph, partial_stats, auth_scores):
        parsed = self._parse(auth_subgraph, partial_stats, scores=auth_scores)
        by_id = {n["id"]: n for n in parsed["nodes"]}
        assert by_id["authenticate_user"]["score"] == pytest.approx(0.94, abs=1e-4)
        assert by_id["JWTMiddleware"]["score"] == pytest.approx(0.87, abs=1e-4)

    def test_score_absent_when_scores_not_provided(self, auth_subgraph, partial_stats):
        parsed = self._parse(auth_subgraph, partial_stats)
        for node in parsed["nodes"]:
            assert "score" not in node

    def test_nodes_sorted_by_score_descending(self, auth_subgraph, partial_stats, auth_scores):
        parsed = self._parse(auth_subgraph, partial_stats, scores=auth_scores)
        scores_list = [n["score"] for n in parsed["nodes"]]
        assert scores_list == sorted(scores_list, reverse=True)

    def test_node_attributes_included(self, auth_subgraph, partial_stats):
        parsed = self._parse(auth_subgraph, partial_stats)
        auth_node = next(n for n in parsed["nodes"] if n["id"] == "authenticate_user")
        assert auth_node["type"] == "function"
        assert "credentials" in auth_node["description"]
        assert auth_node["file_path"] == "src/auth/service.py"

    def test_edge_relation_included(self, auth_subgraph, partial_stats):
        parsed = self._parse(auth_subgraph, partial_stats)
        auth_hash_edge = next(
            e for e in parsed["edges"]
            if e["source"] == "authenticate_user" and e["target"] == "hash_password"
        )
        assert auth_hash_edge["relation"] == "calls"

    def test_empty_subgraph_has_empty_nodes_and_edges(self, partial_stats):
        parsed = json.loads(format_subgraph(nx.DiGraph(), partial_stats, format="json"))
        assert parsed["nodes"] == []
        assert parsed["edges"] == []


# ---------------------------------------------------------------------------
# YAML
# ---------------------------------------------------------------------------


class TestYamlFormat:
    def test_returns_string(self, auth_subgraph, partial_stats):
        assert isinstance(format_subgraph(auth_subgraph, partial_stats, format="yaml"), str)

    def test_starts_with_stats_block(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="yaml")
        assert out.startswith("stats:")

    def test_contains_nodes_block(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="yaml")
        assert "\nnodes:" in out

    def test_contains_edges_block(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="yaml")
        assert "\nedges:" in out

    def test_stats_values_present(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="yaml")
        assert "nodes_selected: 3" in out
        assert "nodes_total: 7" in out
        assert "tokens_budget: 100" in out

    def test_node_ids_present(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="yaml")
        assert "id: authenticate_user" in out
        assert "id: hash_password" in out
        assert "id: JWTMiddleware" in out

    def test_node_list_uses_dash_prefix(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="yaml")
        assert "  - id:" in out

    def test_edge_source_target_present(self, auth_subgraph, partial_stats):
        out = format_subgraph(auth_subgraph, partial_stats, format="yaml")
        assert "source: authenticate_user" in out
        assert "target: hash_password" in out

    def test_score_present_when_provided(self, auth_subgraph, partial_stats, auth_scores):
        out = format_subgraph(
            auth_subgraph, partial_stats, format="yaml", scores=auth_scores
        )
        assert "score: 0.94" in out

    def test_empty_edges_rendered(self, partial_stats):
        G = nx.DiGraph()
        G.add_node("a", label="a", description="")
        out = format_subgraph(G, partial_stats, format="yaml")
        assert "edges:" in out


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_full_pipeline_markdown(self, sample_graph):
        from slurp.budget import select_subgraph
        from slurp.scorer import score_nodes

        scores = score_nodes(sample_graph, "authentication jwt")
        subG, stats = select_subgraph(sample_graph, scores, budget=10_000)
        out = format_subgraph(
            subG, stats, format="markdown", scores=scores, query="authentication jwt"
        )

        assert "## Relevant Nodes" in out
        assert "## Key Relationships" in out
        assert "authentication jwt" in out
        assert "100.0" in out  # full coverage when budget is generous

    def test_full_pipeline_json(self, sample_graph):
        from slurp.budget import select_subgraph
        from slurp.scorer import score_nodes

        scores = score_nodes(sample_graph, "payment stripe")
        subG, stats = select_subgraph(sample_graph, scores, budget=10_000)
        parsed = json.loads(
            format_subgraph(subG, stats, format="json", scores=scores)
        )

        assert len(parsed["nodes"]) == sample_graph.number_of_nodes()
        assert all("score" in n for n in parsed["nodes"])
        node_scores = [n["score"] for n in parsed["nodes"]]
        assert node_scores == sorted(node_scores, reverse=True)

    def test_full_pipeline_yaml(self, sample_graph):
        from slurp.budget import select_subgraph
        from slurp.scorer import score_nodes

        scores = score_nodes(sample_graph, "database pool connection")
        subG, stats = select_subgraph(sample_graph, scores, budget=10_000)
        out = format_subgraph(subG, stats, format="yaml", scores=scores)

        assert out.startswith("stats:")
        assert "nodes:" in out
        assert "edges:" in out
        assert "score:" in out

    def test_partial_budget_shows_tip_and_correct_count(self, sample_graph):
        from slurp.budget import select_subgraph
        from slurp.scorer import score_nodes

        scores = score_nodes(sample_graph, "auth")
        subG, stats = select_subgraph(sample_graph, scores, budget=60)
        out = format_subgraph(subG, stats, format="markdown", scores=scores)

        if stats["nodes_selected"] < stats["nodes_total"]:
            excluded = stats["nodes_total"] - stats["nodes_selected"]
            assert "💡" in out
            assert str(excluded) in out


# ---------------------------------------------------------------------------
# code_block rendering in markdown
# ---------------------------------------------------------------------------

class TestCodeBlockMarkdown:
    def _stats(self, G):
        n = G.number_of_nodes()
        return {"nodes_selected": n, "nodes_total": n,
                "tokens_used": 0, "tokens_budget": 9999, "coverage_pct": 100.0}

    def test_code_block_rendered_in_markdown(self):
        G = nx.DiGraph()
        G.add_node("fn", label="my_func", type="function", file_path="src/auth.py",
                   code_block="def my_func():\n    return 42")
        out = format_subgraph(G, self._stats(G), format="markdown")
        assert "```python" in out
        assert "def my_func():" in out
        assert "return 42" in out
        assert "```" in out

    def test_language_detected_from_file_path(self):
        G = nx.DiGraph()
        G.add_node("fn", label="fn", type="function", file_path="utils.ts",
                   code_block="function fn() { return 1; }")
        out = format_subgraph(G, self._stats(G), format="markdown")
        assert "```typescript" in out

    def test_language_detected_from_source_file_fallback(self):
        G = nx.DiGraph()
        G.add_node("fn", label="fn", type="function", source_file="main.go",
                   code_block="func Foo() {}")
        out = format_subgraph(G, self._stats(G), format="markdown")
        assert "```go" in out

    def test_unknown_extension_renders_empty_lang(self):
        G = nx.DiGraph()
        G.add_node("fn", label="fn", type="function", file_path="script.rb",
                   code_block="def hello; end")
        out = format_subgraph(G, self._stats(G), format="markdown")
        assert "```\n" in out

    def test_no_code_block_attr_renders_nothing(self):
        G = nx.DiGraph()
        G.add_node("fn", label="fn", type="function", file_path="src/auth.py")
        out = format_subgraph(G, self._stats(G), format="markdown")
        assert "```" not in out

    def test_code_block_appears_after_file_line(self):
        G = nx.DiGraph()
        G.add_node("fn", label="fn", type="function", file_path="src/auth.py",
                   code_block="def fn(): pass")
        out = format_subgraph(G, self._stats(G), format="markdown")
        file_idx = out.index("→ File:")
        code_idx = out.index("```python")
        assert code_idx > file_idx

    def test_code_block_not_in_json_format(self):
        G = nx.DiGraph()
        G.add_node("fn", label="fn", type="function",
                   code_block="def fn(): pass")
        out = format_subgraph(G, self._stats(G), format="json")
        # JSON format does not emit fenced code blocks
        assert "```" not in out
