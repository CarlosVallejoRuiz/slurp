"""Tests for slurp/suggester.py."""

import networkx as nx
import pytest

from slurp.budget import select_subgraph
from slurp.scorer import score_nodes
from slurp.suggester import (
    SuggestedQuery,
    _frontier,
    format_suggestions,
    suggest_queries,
)


def _node(G, nid, label, ntype="function", source="lib/app.py"):
    G.add_node(nid, label=label, type=ntype, source_file=source, description="")


def _graph_with_frontier() -> nx.DiGraph:
    """A selected core plus a themed cluster hanging just off it."""
    G = nx.DiGraph()
    _node(G, "core", "playerStats")
    _node(G, "helper", "playerStatsHelper")
    G.add_edge("core", "helper", relation="calls")
    # The frontier: a database-shaped cluster one hop out.
    for i in range(4):
        _node(G, f"db{i}", f"databaseSchemaTable{i}", source="lib/db.py")
        G.add_edge("core", f"db{i}", relation="calls")
    return G


def _selected(G: nx.DiGraph, *nodes: str) -> nx.DiGraph:
    return G.subgraph(nodes).copy()


class TestSuggestQueries:
    def test_returns_at_most_n(self):
        G = _graph_with_frontier()
        out = suggest_queries(G, "player stats", score_nodes(G, "player stats"),
                              _selected(G, "core"), n=2)
        assert len(out) <= 2

    def test_zero_n_returns_nothing(self):
        G = _graph_with_frontier()
        assert suggest_queries(G, "player stats", {}, _selected(G, "core"), n=0) == []

    def test_empty_subgraph_returns_nothing(self):
        G = _graph_with_frontier()
        assert suggest_queries(G, "player stats", {}, nx.DiGraph()) == []

    def test_no_frontier_returns_nothing(self):
        """Everything selected means nothing left to suggest."""
        G = _graph_with_frontier()
        whole = G.copy()
        assert suggest_queries(G, "player stats", score_nodes(G, "x"), whole) == []

    def test_suggestions_do_not_repeat_the_original_query(self):
        G = _graph_with_frontier()
        out = suggest_queries(G, "player stats", score_nodes(G, "player stats"),
                              _selected(G, "core"))
        from slurp.scorer import _tokenize
        original = set(_tokenize("player stats"))
        for suggestion in out:
            assert not set(_tokenize(suggestion.query)) <= original

    def test_expected_nodes_are_real_nodes(self):
        G = _graph_with_frontier()
        out = suggest_queries(G, "player stats", score_nodes(G, "player stats"),
                              _selected(G, "core"))
        assert out
        for suggestion in out:
            for nid in suggestion.expected_nodes:
                assert nid in G

    def test_expected_nodes_are_never_already_selected(self):
        G = _graph_with_frontier()
        selected = _selected(G, "core")
        out = suggest_queries(G, "player stats", score_nodes(G, "player stats"),
                              selected)
        for suggestion in out:
            assert not set(suggestion.expected_nodes) & set(selected.nodes)

    def test_ordered_by_relevance(self):
        G = _graph_with_frontier()
        out = suggest_queries(G, "player stats", score_nodes(G, "player stats"),
                              _selected(G, "core"))
        assert out == sorted(out, key=lambda s: -s.relevance_score)

    def test_relevance_is_a_fraction(self):
        G = _graph_with_frontier()
        for suggestion in suggest_queries(G, "player stats",
                                          score_nodes(G, "player stats"),
                                          _selected(G, "core")):
            assert 0.0 <= suggestion.relevance_score <= 1.0


class TestDrillDown:
    def test_fires_when_the_top_node_has_unexplored_neighbours(self):
        G = _graph_with_frontier()
        scores = {"core": 1.0}
        out = suggest_queries(G, "unrelated", scores, _selected(G, "core"))
        assert any("drill down" in s.reason for s in out)

    def test_names_the_top_node(self):
        G = _graph_with_frontier()
        out = suggest_queries(G, "unrelated", {"core": 1.0}, _selected(G, "core"))
        drill = [s for s in out if "drill down" in s.reason]
        assert drill and "playerStats" in drill[0].query

    def test_absent_when_the_node_stands_alone(self):
        G = nx.DiGraph()
        _node(G, "core", "playerStats")
        _node(G, "far", "unrelatedThing")
        out = suggest_queries(G, "player", score_nodes(G, "player"),
                              _selected(G, "core"))
        assert not any("drill down" in s.reason for s in out)

    def test_prefers_production_over_tests(self):
        """A test can score highest, but drilling into one is not advice."""
        G = nx.DiGraph()
        _node(G, "t", "test_player_stats", source="tests/test_app.py")
        _node(G, "prod", "playerStats", source="lib/app.py")
        _node(G, "n1", "databaseSchema", source="lib/db.py")
        _node(G, "n2", "databaseTable", source="lib/db.py")
        G.add_edge("prod", "n1", relation="calls")
        G.add_edge("prod", "n2", relation="calls")
        G.add_edge("t", "prod", relation="calls")
        out = suggest_queries(G, "zzz", {"t": 1.0, "prod": 0.9},
                              _selected(G, "t", "prod"))
        drill = [s for s in out if "drill down" in s.reason]
        assert drill and "test_player_stats" not in drill[0].query


class TestRiskExploration:
    def _risky_graph(self, dependents: int) -> nx.DiGraph:
        G = nx.DiGraph()
        _node(G, "core", "playerStats")
        _node(G, "risky", "createAdminClient", source="lib/client.py")
        G.add_edge("core", "risky", relation="calls")
        for i in range(dependents):
            _node(G, f"dep{i}", f"consumer{i}", source="lib/api.py")
            G.add_edge(f"dep{i}", "risky", relation="calls")
        return G

    def test_fires_for_a_high_risk_neighbour(self):
        G = self._risky_graph(8)
        out = suggest_queries(G, "player stats", score_nodes(G, "player stats"),
                              _selected(G, "core"))
        risk = [s for s in out if "high-risk" in s.reason]
        assert risk and risk[0].query == "explain:createAdminClient"

    def test_silent_below_the_high_threshold(self):
        G = self._risky_graph(2)
        out = suggest_queries(G, "player stats", score_nodes(G, "player stats"),
                              _selected(G, "core"))
        assert not any("high-risk" in s.reason for s in out)

    def test_reason_reports_the_dependent_count(self):
        G = self._risky_graph(9)
        out = suggest_queries(G, "player stats", score_nodes(G, "player stats"),
                              _selected(G, "core"))
        risk = [s for s in out if "high-risk" in s.reason]
        # Nine consumers plus `core`, which also calls it.
        assert risk and "10 nodes depend on it" in risk[0].reason


class TestFrontier:
    def test_excludes_selected_nodes(self):
        G = _graph_with_frontier()
        assert "core" not in _frontier(G, _selected(G, "core"))

    def test_excludes_import_nodes(self):
        """Import placeholders name what they import and flood the tokens."""
        G = nx.DiGraph()
        _node(G, "core", "playerStats")
        _node(G, "imp", "lib.db", ntype="import")
        G.add_edge("core", "imp", relation="imports_from")
        assert _frontier(G, _selected(G, "core")) == set()

    def test_empty_for_an_empty_subgraph(self):
        assert _frontier(_graph_with_frontier(), nx.DiGraph()) == set()


class TestFormatSuggestions:
    def test_empty_input_renders_nothing(self):
        assert format_suggestions([]) == ""

    def test_renders_a_runnable_query_command(self):
        out = format_suggestions([SuggestedQuery("player stats db", "because")])
        assert 'slurp "player stats db"' in out
        assert "— because" in out

    def test_explain_prefix_becomes_an_explain_command(self):
        out = format_suggestions([SuggestedQuery("explain:createAdminClient", "risky")])
        assert "slurp explain 'createAdminClient'" in out
        assert "explain:" not in out

    def test_commands_are_aligned(self):
        out = format_suggestions([
            SuggestedQuery("short", "a"),
            SuggestedQuery("a much longer query here", "b"),
        ])
        dashes = [line.index("—") for line in out.splitlines()[1:]]
        assert len(set(dashes)) == 1

    def test_graph_flag_is_included_when_given(self):
        out = format_suggestions([SuggestedQuery("x y", "r")], graph_flag="g.json")
        assert "--graph g.json" in out


class TestSuggestOnRealGraph:
    @pytest.fixture
    def graph(self, sample_graph_json):
        from slurp.loader import load_graph
        return load_graph(sample_graph_json)

    def test_runs_without_error(self, graph):
        scores = score_nodes(graph, "auth flow")
        sub, _ = select_subgraph(graph, scores, budget=200, min_score=0.15)
        out = suggest_queries(graph, "auth flow", scores, sub)
        assert isinstance(out, list)
        for suggestion in out:
            assert suggestion.query
            assert suggestion.reason
