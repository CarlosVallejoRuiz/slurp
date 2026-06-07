"""Tests for slurp/exporter.py."""

import pytest
import networkx as nx

from slurp.exporter import export_context, _strip_box, _meta_line


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def small_graph() -> nx.DiGraph:
    G = nx.DiGraph()
    G.add_node("auth", label="authenticate_user", type="function",
               description="Validates credentials", file_path="src/auth.py")
    G.add_node("token", label="generate_token", type="function",
               description="Creates JWT token", file_path="src/jwt.py")
    G.add_edge("auth", "token", relation="calls")
    return G


@pytest.fixture
def base_stats() -> dict:
    return {
        "nodes_selected": 2,
        "nodes_total": 10,
        "tokens_used": 120,
        "tokens_budget": 4000,
        "coverage_pct": 20.0,
    }


@pytest.fixture
def scores() -> dict[str, float]:
    return {"auth": 0.85, "token": 0.60}


# ---------------------------------------------------------------------------
# _strip_box
# ---------------------------------------------------------------------------

class TestStripBox:
    def test_removes_unicode_box_header(self, small_graph, base_stats):
        from slurp.formatter import format_subgraph
        body = format_subgraph(small_graph, base_stats, format="markdown", query="auth")
        stripped = _strip_box(body)
        assert "╭" not in stripped

    def test_preserves_relevant_nodes_section(self, small_graph, base_stats):
        from slurp.formatter import format_subgraph
        body = format_subgraph(small_graph, base_stats, format="markdown", query="auth")
        stripped = _strip_box(body)
        assert stripped.startswith("## Relevant Nodes")

    def test_no_box_returns_body_unchanged(self):
        body = "## Relevant Nodes\n\nsome content"
        assert _strip_box(body) == body

    def test_body_without_heading_returned_as_is(self):
        body = "no heading here"
        assert _strip_box(body) == body


# ---------------------------------------------------------------------------
# _meta_line
# ---------------------------------------------------------------------------

class TestMetaLine:
    def test_contains_query(self, base_stats):
        result = _meta_line(base_stats, "auth flow")
        assert 'query: "auth flow"' in result

    def test_contains_node_ratio(self, base_stats):
        result = _meta_line(base_stats, "auth")
        assert "nodes: 2/10" in result

    def test_contains_token_ratio(self, base_stats):
        result = _meta_line(base_stats, "auth")
        assert "tokens: 120/4,000" in result

    def test_contains_coverage(self, base_stats):
        result = _meta_line(base_stats, "auth")
        assert "coverage: 20.0%" in result


# ---------------------------------------------------------------------------
# export_context — claude format
# ---------------------------------------------------------------------------

class TestExportClaude:
    def test_starts_with_context_tag(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth", format="claude")
        assert result.startswith("<context ")

    def test_ends_with_closing_tag(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth", format="claude")
        assert result.endswith("</context>")

    def test_has_source_attr(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth", format="claude")
        assert 'source="slurp-graph"' in result

    def test_has_query_attr(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth flow", format="claude")
        assert 'query="auth flow"' in result

    def test_has_nodes_attr(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth", format="claude")
        assert 'nodes="2/10"' in result

    def test_has_tokens_attr(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth", format="claude")
        assert 'tokens="120/4000"' in result

    def test_has_coverage_attr(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth", format="claude")
        assert 'coverage=' in result

    def test_body_contains_relevant_nodes(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth", format="claude")
        assert "## Relevant Nodes" in result

    def test_body_contains_unicode_box(self, small_graph, base_stats):
        # claude keeps the full body including the box header
        result = export_context(small_graph, base_stats, "auth", format="claude")
        assert "╭" in result

    def test_default_format_is_claude(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth")
        assert result.startswith("<context ")

    def test_scores_reflected_in_body(self, small_graph, base_stats, scores):
        result = export_context(small_graph, base_stats, "auth", format="claude", scores=scores)
        assert "score:" in result

    def test_relationships_included_when_edges_present(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth", format="claude")
        assert "## Key Relationships" in result


# ---------------------------------------------------------------------------
# export_context — chatgpt format
# ---------------------------------------------------------------------------

class TestExportChatgpt:
    def test_starts_with_delimiter(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth", format="chatgpt")
        assert result.startswith("[CODEBASE CONTEXT — slurp-graph]")

    def test_ends_with_delimiter(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth", format="chatgpt")
        assert result.endswith("[END CODEBASE CONTEXT]")

    def test_has_query_in_metadata(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth flow", format="chatgpt")
        assert 'query: "auth flow"' in result

    def test_has_node_ratio(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth", format="chatgpt")
        assert "nodes: 2/10" in result

    def test_has_token_ratio(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth", format="chatgpt")
        assert "tokens: 120/4,000" in result

    def test_no_unicode_box(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth", format="chatgpt")
        assert "╭" not in result

    def test_has_relevant_nodes_section(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth", format="chatgpt")
        assert "## Relevant Nodes" in result

    def test_scores_reflected(self, small_graph, base_stats, scores):
        result = export_context(small_graph, base_stats, "auth", format="chatgpt", scores=scores)
        assert "score:" in result


# ---------------------------------------------------------------------------
# export_context — claudemd format
# ---------------------------------------------------------------------------

class TestExportClaudemd:
    def test_starts_with_h2_heading(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth", format="claudemd")
        assert result.startswith("## Codebase Context")

    def test_has_html_comment_with_metadata(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth", format="claudemd")
        assert "<!-- slurp-graph ·" in result
        assert "-->" in result

    def test_comment_contains_query(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth flow", format="claudemd")
        assert 'query: "auth flow"' in result

    def test_no_unicode_box(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth", format="claudemd")
        assert "╭" not in result

    def test_has_relevant_nodes_section(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth", format="claudemd")
        assert "## Relevant Nodes" in result

    def test_no_codebase_context_end_marker(self, small_graph, base_stats):
        # claudemd is an open section — no end delimiter
        result = export_context(small_graph, base_stats, "auth", format="claudemd")
        assert "[END" not in result
        assert "</context>" not in result

    def test_scores_reflected(self, small_graph, base_stats, scores):
        result = export_context(small_graph, base_stats, "auth", format="claudemd", scores=scores)
        assert "score:" in result


# ---------------------------------------------------------------------------
# export_context — unknown format
# ---------------------------------------------------------------------------

class TestExportUnknownFormat:
    def test_raises_value_error(self, small_graph, base_stats):
        with pytest.raises(ValueError, match="Unknown export format"):
            export_context(small_graph, base_stats, "auth", format="xml")

    def test_error_message_shows_valid_formats(self, small_graph, base_stats):
        with pytest.raises(ValueError, match="claude.*chatgpt.*claudemd"):
            export_context(small_graph, base_stats, "auth", format="bad")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestExportEdgeCases:
    def test_empty_query(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "", format="claude")
        assert result.startswith("<context ")

    def test_graph_with_no_edges(self, base_stats):
        G = nx.DiGraph()
        G.add_node("a", label="Alpha", type="function", description="desc")
        stats = {**base_stats, "nodes_selected": 1}
        result = export_context(G, stats, "alpha", format="claude")
        assert "## Relevant Nodes" in result
        assert "## Key Relationships" not in result

    def test_scores_none_no_error(self, small_graph, base_stats):
        result = export_context(small_graph, base_stats, "auth", format="claude", scores=None)
        assert "## Relevant Nodes" in result

    def test_all_three_formats_contain_node_labels(self, small_graph, base_stats):
        for fmt in ("claude", "chatgpt", "claudemd"):
            result = export_context(small_graph, base_stats, "auth", format=fmt)
            assert "authenticate_user" in result, f"Missing node label in format '{fmt}'"
