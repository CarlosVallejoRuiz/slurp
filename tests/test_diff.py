"""Tests for slurp/diff.py."""

import json

import networkx as nx
import pytest
from click.testing import CliRunner

from slurp.cli import cli
from slurp.diff import (
    GraphDiff,
    _centrality,
    affected_subgraph,
    build_diff_viz_graph,
    diff_graphs,
    format_diff,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def graphs():
    """(G_old, G_new) with known changes:
    - "d" added
    - "c" removed
    - "a" label changed  → modified
    - edge (a→d) added
    - edge (b→c) removed
    - edge (a→b) unchanged
    """
    G_old = nx.DiGraph()
    G_old.add_node("a", label="NodeA", type="function")
    G_old.add_node("b", label="NodeB", type="class")
    G_old.add_node("c", label="NodeC", type="function")
    G_old.add_edge("a", "b", relation="calls")
    G_old.add_edge("b", "c", relation="depends_on")

    G_new = nx.DiGraph()
    G_new.add_node("a", label="NodeA_v2", type="function")  # label changed
    G_new.add_node("b", label="NodeB", type="class")         # unchanged
    G_new.add_node("d", label="NodeD", type="module")        # added
    G_new.add_edge("a", "b", relation="calls")               # unchanged
    G_new.add_edge("a", "d", relation="imports")             # added

    return G_old, G_new


@pytest.fixture
def chain_graph():
    """Linear chain a→b→c→d→e. Used for hop expansion tests."""
    G = nx.DiGraph()
    for node in "abcde":
        G.add_node(node, label=node, type="function")
    for u, v in zip("abcd", "bcde"):
        G.add_edge(u, v)
    return G


@pytest.fixture
def old_graph_json(tmp_path, graphs):
    G_old, _ = graphs
    data = {
        "nodes": [{"id": n, **G_old.nodes[n]} for n in G_old.nodes],
        "edges": [{"source": u, "target": v, **G_old.edges[u, v]} for u, v in G_old.edges],
    }
    p = tmp_path / "old.json"
    p.write_text(json.dumps(data))
    return p


@pytest.fixture
def new_graph_json(tmp_path, graphs):
    _, G_new = graphs
    data = {
        "nodes": [{"id": n, **G_new.nodes[n]} for n in G_new.nodes],
        "edges": [{"source": u, "target": v, **G_new.edges[u, v]} for u, v in G_new.edges],
    }
    p = tmp_path / "new.json"
    p.write_text(json.dumps(data))
    return p


def _runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# _centrality
# ---------------------------------------------------------------------------


class TestCentrality:
    def test_empty_graph_returns_empty(self):
        assert _centrality(nx.DiGraph()) == {}

    def test_single_node_is_zero(self):
        G = nx.DiGraph()
        G.add_node("a")
        assert _centrality(G)["a"] == 0.0

    def test_values_in_0_1(self, chain_graph):
        for v in _centrality(chain_graph).values():
            assert 0.0 <= v <= 1.0

    def test_hub_node_higher_than_leaf(self):
        G = nx.DiGraph()
        for n in "abcd":
            G.add_node(n)
        # hub connects to all others
        G.add_edge("hub", "a")
        G.add_edge("hub", "b")
        G.add_edge("hub", "c")
        G.add_node("hub")
        cent = _centrality(G)
        assert cent["hub"] > cent["a"]


# ---------------------------------------------------------------------------
# diff_graphs
# ---------------------------------------------------------------------------


class TestDiffGraphs:
    def test_identical_graphs_no_changes(self, graphs):
        G_old, _ = graphs
        d = diff_graphs(G_old, G_old)
        assert d.added_nodes == []
        assert d.removed_nodes == []
        assert d.modified_nodes == []
        assert d.added_edges == []
        assert d.removed_edges == []

    def test_identical_graphs_impact_zero(self, graphs):
        G_old, _ = graphs
        d = diff_graphs(G_old, G_old)
        assert d.impact_score == 0.0

    def test_added_nodes(self, graphs):
        G_old, G_new = graphs
        d = diff_graphs(G_old, G_new)
        assert d.added_nodes == ["d"]

    def test_removed_nodes(self, graphs):
        G_old, G_new = graphs
        d = diff_graphs(G_old, G_new)
        assert d.removed_nodes == ["c"]

    def test_modified_nodes_label_change(self, graphs):
        G_old, G_new = graphs
        d = diff_graphs(G_old, G_new)
        assert "a" in d.modified_nodes

    def test_modified_nodes_type_change(self):
        G_old = nx.DiGraph()
        G_old.add_node("x", label="X", type="function")
        G_new = nx.DiGraph()
        G_new.add_node("x", label="X", type="class")  # type changed
        d = diff_graphs(G_old, G_new)
        assert "x" in d.modified_nodes

    def test_unchanged_node_not_modified(self, graphs):
        G_old, G_new = graphs
        d = diff_graphs(G_old, G_new)
        assert "b" not in d.modified_nodes

    def test_description_change_not_tracked_as_modified(self):
        G_old = nx.DiGraph()
        G_old.add_node("x", label="X", type="function", description="old")
        G_new = nx.DiGraph()
        G_new.add_node("x", label="X", type="function", description="new")
        d = diff_graphs(G_old, G_new)
        assert d.modified_nodes == []

    def test_added_edges(self, graphs):
        G_old, G_new = graphs
        d = diff_graphs(G_old, G_new)
        assert ("a", "d") in d.added_edges

    def test_removed_edges(self, graphs):
        G_old, G_new = graphs
        d = diff_graphs(G_old, G_new)
        assert ("b", "c") in d.removed_edges

    def test_unchanged_edge_not_in_diff(self, graphs):
        G_old, G_new = graphs
        d = diff_graphs(G_old, G_new)
        assert ("a", "b") not in d.added_edges
        assert ("a", "b") not in d.removed_edges

    def test_impact_score_nonzero_for_changes(self, graphs):
        G_old, G_new = graphs
        d = diff_graphs(G_old, G_new)
        assert d.impact_score > 0.0

    def test_impact_score_between_0_and_1(self, graphs):
        G_old, G_new = graphs
        d = diff_graphs(G_old, G_new)
        assert 0.0 <= d.impact_score <= 1.0

    def test_has_changes_true_when_diff(self, graphs):
        G_old, G_new = graphs
        d = diff_graphs(G_old, G_new)
        assert d.has_changes

    def test_has_changes_false_identical(self, graphs):
        G_old, _ = graphs
        d = diff_graphs(G_old, G_old)
        assert not d.has_changes

    def test_results_are_sorted(self, graphs):
        G_old, G_new = graphs
        d = diff_graphs(G_old, G_new)
        assert d.added_nodes == sorted(d.added_nodes)
        assert d.removed_nodes == sorted(d.removed_nodes)
        assert d.modified_nodes == sorted(d.modified_nodes)

    def test_empty_old_all_added(self):
        G_old = nx.DiGraph()
        G_new = nx.DiGraph()
        G_new.add_node("x", label="X", type="function")
        d = diff_graphs(G_old, G_new)
        assert d.added_nodes == ["x"]
        assert d.removed_nodes == []

    def test_empty_new_all_removed(self):
        G_old = nx.DiGraph()
        G_old.add_node("x", label="X", type="function")
        G_new = nx.DiGraph()
        d = diff_graphs(G_old, G_new)
        assert d.removed_nodes == ["x"]
        assert d.added_nodes == []


# ---------------------------------------------------------------------------
# affected_subgraph
# ---------------------------------------------------------------------------


class TestAffectedSubgraph:
    def test_hops_0_only_seed_nodes(self, chain_graph):
        diff = GraphDiff(modified_nodes=["c"])
        sub = affected_subgraph(chain_graph, diff, hops=0)
        assert set(sub.nodes) == {"c"}

    def test_hops_1_direct_neighbors(self, chain_graph):
        diff = GraphDiff(modified_nodes=["c"])
        sub = affected_subgraph(chain_graph, diff, hops=1)
        assert set(sub.nodes) == {"b", "c", "d"}

    def test_hops_2_two_hops(self, chain_graph):
        diff = GraphDiff(modified_nodes=["c"])
        sub = affected_subgraph(chain_graph, diff, hops=2)
        assert set(sub.nodes) == {"a", "b", "c", "d", "e"}

    def test_added_nodes_are_seeds(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        sub = affected_subgraph(G_new, diff, hops=0)
        # "d" (added) and "a" (modified) are seeds; "b" from removed edge
        assert "d" in sub.nodes
        assert "a" in sub.nodes

    def test_removed_nodes_not_in_result(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        sub = affected_subgraph(G_new, diff, hops=2)
        assert "c" not in sub.nodes  # "c" was removed, not in G_new

    def test_empty_diff_empty_subgraph(self, chain_graph):
        diff = GraphDiff()
        sub = affected_subgraph(chain_graph, diff, hops=2)
        assert sub.number_of_nodes() == 0

    def test_returns_copy_not_view(self, chain_graph):
        diff = GraphDiff(modified_nodes=["c"])
        sub = affected_subgraph(chain_graph, diff, hops=0)
        sub.add_node("injected")
        assert "injected" not in chain_graph.nodes

    def test_edges_preserved_within_subgraph(self, chain_graph):
        diff = GraphDiff(modified_nodes=["c"])
        sub = affected_subgraph(chain_graph, diff, hops=1)
        assert ("b", "c") in sub.edges
        assert ("c", "d") in sub.edges

    def test_edges_outside_subgraph_excluded(self, chain_graph):
        diff = GraphDiff(modified_nodes=["c"])
        sub = affected_subgraph(chain_graph, diff, hops=1)
        assert ("a", "b") not in sub.edges  # a is outside the 1-hop neighborhood


# ---------------------------------------------------------------------------
# format_diff
# ---------------------------------------------------------------------------


class TestFormatDiff:
    def test_output_starts_with_header(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        out = format_diff(diff, G_new)
        assert out.startswith("# Slurp Diff")

    def test_summary_section_present(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        out = format_diff(diff, G_new)
        assert "## Summary" in out

    def test_impact_score_in_output(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        out = format_diff(diff, G_new)
        assert "Impact score" in out
        assert str(diff.impact_score) in out

    def test_added_nodes_section(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        out = format_diff(diff, G_new)
        assert "Added Nodes" in out

    def test_removed_nodes_section(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        out = format_diff(diff, G_new)
        assert "Removed Nodes" in out

    def test_modified_nodes_section(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        out = format_diff(diff, G_new)
        assert "Modified Nodes" in out

    def test_added_node_label_in_output(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        out = format_diff(diff, G_new)
        assert "NodeD" in out  # label of added node "d"

    def test_removed_node_id_in_output(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        out = format_diff(diff, G_new)
        assert "c" in out  # removed node id

    def test_affected_edges_section(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        out = format_diff(diff, G_new)
        assert "Affected Edges" in out

    def test_nodes_at_risk_section(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        out = format_diff(diff, G_new)
        assert "Nodes at Risk" in out

    def test_no_changes_message(self, graphs):
        G_old, _ = graphs
        diff = diff_graphs(G_old, G_old)
        out = format_diff(diff, G_old)
        assert "No changes" in out

    def test_no_changes_no_added_section(self, graphs):
        G_old, _ = graphs
        diff = diff_graphs(G_old, G_old)
        out = format_diff(diff, G_old)
        assert "Added Nodes" not in out

    def test_centrality_values_in_at_risk(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        out = format_diff(diff, G_new)
        assert "centrality:" in out

    def test_impact_label_high_for_large_score(self):
        G_old = nx.DiGraph()
        G_new = nx.DiGraph()
        G_new.add_node("x", label="X", type="function")
        diff = GraphDiff(added_nodes=["x"], impact_score=0.8)
        out = format_diff(diff, G_new)
        assert "high" in out

    def test_impact_label_low_for_small_score(self):
        G_old = nx.DiGraph()
        G_new = nx.DiGraph()
        G_new.add_node("x", label="X", type="function")
        diff = GraphDiff(added_nodes=["x"], impact_score=0.05)
        out = format_diff(diff, G_new)
        assert "low" in out


# ---------------------------------------------------------------------------
# build_diff_viz_graph
# ---------------------------------------------------------------------------


class TestBuildDiffVizGraph:
    def test_added_nodes_have_added_type(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        viz_G, _, _ = build_diff_viz_graph(G_old, G_new, diff, hops=0)
        assert viz_G.nodes["d"]["type"] == "added"

    def test_removed_nodes_have_removed_type(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        viz_G, _, _ = build_diff_viz_graph(G_old, G_new, diff, hops=0)
        assert viz_G.nodes["c"]["type"] == "removed"

    def test_modified_nodes_have_modified_type(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        viz_G, _, _ = build_diff_viz_graph(G_old, G_new, diff, hops=0)
        assert viz_G.nodes["a"]["type"] == "modified"

    def test_unchanged_neighbors_have_unchanged_type(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        viz_G, _, _ = build_diff_viz_graph(G_old, G_new, diff, hops=1)
        if "b" in viz_G.nodes:
            assert viz_G.nodes["b"]["type"] == "unchanged"

    def test_removed_nodes_included_as_isolated(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        viz_G, _, _ = build_diff_viz_graph(G_old, G_new, diff, hops=0)
        assert "c" in viz_G.nodes  # removed, but added back from G_old

    def test_direct_affected_score_is_1(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        _, scores, _ = build_diff_viz_graph(G_old, G_new, diff, hops=1)
        assert scores["d"] == 1.0  # added
        assert scores["c"] == 1.0  # removed
        assert scores["a"] == 1.0  # modified

    def test_neighborhood_score_less_than_direct(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        _, scores, _ = build_diff_viz_graph(G_old, G_new, diff, hops=1)
        if "b" in scores:
            assert scores["b"] < 1.0

    def test_stats_nodes_total_matches_new_graph(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        _, _, stats = build_diff_viz_graph(G_old, G_new, diff, hops=2)
        assert stats["nodes_total"] == G_new.number_of_nodes()

    def test_empty_diff_no_affected_nodes(self, graphs):
        G_old, _ = graphs
        diff = diff_graphs(G_old, G_old)
        viz_G, scores, _ = build_diff_viz_graph(G_old, G_old, diff, hops=0)
        # No seeds → empty viz graph
        assert viz_G.number_of_nodes() == 0


# ---------------------------------------------------------------------------
# CLI — slurp diff
# ---------------------------------------------------------------------------


class TestDiffCLI:
    def test_diff_identical_exits_zero(self, sample_graph_json):
        result = _runner().invoke(
            cli, ["diff", str(sample_graph_json), str(sample_graph_json)]
        )
        assert result.exit_code == 0

    def test_diff_identical_no_changes_message(self, sample_graph_json):
        result = _runner().invoke(
            cli, ["diff", str(sample_graph_json), str(sample_graph_json)]
        )
        assert "No changes" in result.output

    def test_diff_changed_shows_summary(self, old_graph_json, new_graph_json):
        result = _runner().invoke(
            cli, ["diff", str(old_graph_json), str(new_graph_json)]
        )
        assert result.exit_code == 0
        assert "Summary" in result.output

    def test_diff_changed_shows_added(self, old_graph_json, new_graph_json):
        result = _runner().invoke(
            cli, ["diff", str(old_graph_json), str(new_graph_json)]
        )
        assert "Added Nodes" in result.output

    def test_diff_changed_shows_removed(self, old_graph_json, new_graph_json):
        result = _runner().invoke(
            cli, ["diff", str(old_graph_json), str(new_graph_json)]
        )
        assert "Removed Nodes" in result.output

    def test_diff_with_budget_exits_zero(self, old_graph_json, new_graph_json):
        result = _runner().invoke(
            cli, ["diff", str(old_graph_json), str(new_graph_json), "--budget", "4000"]
        )
        assert result.exit_code == 0

    def test_diff_with_budget_shows_budgeted_section(self, old_graph_json, new_graph_json):
        result = _runner().invoke(
            cli, ["diff", str(old_graph_json), str(new_graph_json), "--budget", "4000"]
        )
        assert "Token-Budgeted" in result.output

    def test_diff_hops_flag_accepted(self, old_graph_json, new_graph_json):
        result = _runner().invoke(
            cli, ["diff", str(old_graph_json), str(new_graph_json), "--hops", "1"]
        )
        assert result.exit_code == 0

    def test_diff_missing_file_exits_nonzero(self, sample_graph_json, tmp_path):
        result = _runner().invoke(
            cli, ["diff", str(sample_graph_json), str(tmp_path / "missing.json")]
        )
        assert result.exit_code != 0

    def test_diff_viz_calls_webbrowser(self, old_graph_json, new_graph_json, monkeypatch):
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        result = _runner().invoke(
            cli, ["diff", str(old_graph_json), str(new_graph_json), "--viz"]
        )
        assert result.exit_code == 0
        assert len(opened) == 1

    def test_diff_viz_html_contains_diff_colors(
        self, old_graph_json, new_graph_json, monkeypatch, tmp_path
    ):
        from pathlib import Path
        from urllib.parse import unquote, urlparse

        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        _runner().invoke(
            cli, ["diff", str(old_graph_json), str(new_graph_json), "--viz"]
        )
        if not opened:
            return
        path = Path(unquote(urlparse(opened[0]).path))
        html = path.read_text(encoding="utf-8") if path.exists() else ""
        assert "added" in html
        assert "removed" in html
