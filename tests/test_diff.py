"""Tests for slurp/diff.py."""

import json
import re

import networkx as nx
import pytest
from click.testing import CliRunner

from slurp.cli import cli
from slurp.diff import (
    _HEADLINES,
    _SCORE_LABELS,
    GraphDiff,
    _centrality,
    affected_subgraph,
    build_diff_viz_graph,
    diff_graphs,
    format_diff,
    impact_headline,
    impact_severity,
    review_candidates,
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
        G_new = nx.DiGraph()
        G_new.add_node("x", label="X", type="function")
        diff = GraphDiff(added_nodes=["x"], impact_score=0.8)
        out = format_diff(diff, G_new)
        assert "high" in out

    def test_impact_label_low_for_small_score(self):
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


# ---------------------------------------------------------------------------
# impact_headline
# ---------------------------------------------------------------------------


class TestImpactHeadline:
    @pytest.mark.parametrize("score", [0.5, 0.75, 1.0])
    def test_high_from_point_five(self, score):
        assert impact_headline(score) == "⚠️ High impact change"

    @pytest.mark.parametrize("score", [0.2, 0.35, 0.49])
    def test_medium_between_point_two_and_point_five(self, score):
        assert impact_headline(score) == "⚡ Medium impact change"

    @pytest.mark.parametrize("score", [0.0, 0.1, 0.19])
    def test_low_below_point_two(self, score):
        assert impact_headline(score) == "✅ Low impact change"

    def test_boundaries_are_inclusive(self):
        """0.5 and 0.2 belong to the upper band, matching the score label."""
        assert impact_headline(0.5) == "⚠️ High impact change"
        assert impact_headline(0.2) == "⚡ Medium impact change"


class TestImpactSeverity:
    @pytest.mark.parametrize("score,band", [
        (0.0, "low"), (0.19, "low"),
        (0.2, "medium"), (0.49, "medium"),
        (0.5, "high"), (1.0, "high"),
    ])
    def test_bands(self, score, band):
        assert impact_severity(score) == band

    @pytest.mark.parametrize("score", [0.0, 0.19, 0.2, 0.3, 0.49, 0.5, 0.7, 1.0])
    def test_headline_and_score_label_always_agree(self, score, graphs):
        """The whole point of unifying the scales: no contradictory summary."""
        _, G_new = graphs
        diff = diff_graphs(*graphs)
        diff.impact_score = score
        out = format_diff(diff, G_new)
        band = impact_severity(score)
        assert _HEADLINES[band] in out
        assert _SCORE_LABELS[band] in out

    @pytest.mark.parametrize("score", [0.5, 0.2])
    def test_no_contradiction_at_the_boundaries(self, score, graphs):
        """A strict > would have made these two lines disagree."""
        _, G_new = graphs
        diff = diff_graphs(*graphs)
        diff.impact_score = score
        out = format_diff(diff, G_new)
        summary = out.split("## Summary")[1].split("##")[0]
        highs = ("⚠️ High impact change" in summary, "🔴 high" in summary)
        mediums = ("⚡ Medium impact change" in summary, "🟡 medium" in summary)
        assert highs[0] == highs[1]
        assert mediums[0] == mediums[1]


# ---------------------------------------------------------------------------
# review_candidates
# ---------------------------------------------------------------------------


class TestReviewCandidates:
    def test_returns_list_of_pairs(self, graphs):
        G_old, G_new = graphs
        out = review_candidates(G_new, diff_graphs(G_old, G_new))
        assert all(isinstance(nid, str) and isinstance(c, float) for nid, c in out)

    def test_limits_to_five_by_default(self):
        G_old = nx.DiGraph()
        G_old.add_node("hub", label="Hub")
        G_new = nx.DiGraph()
        G_new.add_node("hub", label="Hub")
        for i in range(20):
            G_new.add_node(f"n{i}", label=f"N{i}")
            G_new.add_edge("hub", f"n{i}")
        out = review_candidates(G_new, diff_graphs(G_old, G_new))
        assert len(out) <= 5

    def test_limit_is_configurable(self, graphs):
        G_old, G_new = graphs
        out = review_candidates(G_new, diff_graphs(G_old, G_new), limit=1)
        assert len(out) <= 1

    def test_ordered_by_centrality_descending(self):
        G_old = nx.DiGraph()
        G_old.add_node("seed", label="Seed")
        G_new = nx.DiGraph()
        G_new.add_node("seed", label="Seed")
        G_new.add_node("hub", label="Hub")
        G_new.add_node("leaf", label="Leaf")
        G_new.add_edge("seed", "hub")
        G_new.add_edge("hub", "leaf")
        G_new.add_edge("hub", "seed")
        out = review_candidates(G_new, diff_graphs(G_old, G_new))
        scores = [c for _, c in out]
        assert scores == sorted(scores, reverse=True)

    def test_empty_when_no_changes(self, graphs):
        _, G_new = graphs
        assert review_candidates(G_new, diff_graphs(G_new, G_new)) == []

    def test_empty_graph_returns_empty(self):
        empty = nx.DiGraph()
        assert review_candidates(empty, diff_graphs(empty, empty)) == []

    def test_only_includes_affected_nodes(self, graphs):
        G_old, G_new = graphs
        diff = diff_graphs(G_old, G_new)
        affected = set(affected_subgraph(G_new, diff).nodes)
        assert all(nid in affected for nid, _ in review_candidates(G_new, diff))

    def test_hops_widens_the_candidate_pool(self, chain_graph):
        G_old = chain_graph.copy()
        G_old.remove_node("a")
        G_new = chain_graph
        diff = diff_graphs(G_old, G_new)
        near = review_candidates(G_new, diff, hops=1, limit=99)
        far = review_candidates(G_new, diff, hops=3, limit=99)
        assert len(far) >= len(near)

    def test_deterministic_for_tied_centrality(self):
        G_old = nx.DiGraph()
        G_old.add_node("seed", label="Seed")
        G_new = nx.DiGraph()
        G_new.add_node("seed", label="Seed")
        for name in ("z", "y", "x"):
            G_new.add_node(name, label=name.upper())
            G_new.add_edge("seed", name)
        first = review_candidates(G_new, diff_graphs(G_old, G_new))
        second = review_candidates(G_new, diff_graphs(G_old, G_new))
        assert first == second


# ---------------------------------------------------------------------------
# format_diff — impact headline and "What to review"
# ---------------------------------------------------------------------------


class TestFormatDiffImpactHeadline:
    def test_headline_present_in_summary(self, graphs):
        G_old, G_new = graphs
        out = format_diff(diff_graphs(G_old, G_new), G_new)
        assert "impact change" in out

    def test_headline_appears_before_impact_score(self, graphs):
        G_old, G_new = graphs
        out = format_diff(diff_graphs(G_old, G_new), G_new)
        assert out.index("impact change") < out.index("**Impact score:**")

    def test_high_score_shows_high_headline(self, graphs):
        _, G_new = graphs
        diff = diff_graphs(*graphs)
        diff.impact_score = 0.95
        assert "⚠️ High impact change" in format_diff(diff, G_new)

    def test_medium_score_shows_medium_headline(self, graphs):
        _, G_new = graphs
        diff = diff_graphs(*graphs)
        diff.impact_score = 0.35
        assert "⚡ Medium impact change" in format_diff(diff, G_new)

    def test_low_score_shows_low_headline(self, graphs):
        _, G_new = graphs
        diff = diff_graphs(*graphs)
        diff.impact_score = 0.01
        assert "✅ Low impact change" in format_diff(diff, G_new)

    def test_headline_shown_even_with_no_changes(self, graphs):
        _, G_new = graphs
        out = format_diff(diff_graphs(G_new, G_new), G_new)
        assert "impact change" in out


class TestFormatDiffWhatToReview:
    def test_section_present_when_changes_exist(self, graphs):
        G_old, G_new = graphs
        out = format_diff(diff_graphs(G_old, G_new), G_new)
        assert "## What to review" in out

    def test_section_absent_when_no_changes(self, graphs):
        _, G_new = graphs
        out = format_diff(diff_graphs(G_new, G_new), G_new)
        assert "## What to review" not in out

    def test_section_has_explanatory_line(self, graphs):
        G_old, G_new = graphs
        out = format_diff(diff_graphs(G_old, G_new), G_new)
        assert "blast radius" in out

    def test_entries_are_numbered(self, graphs):
        G_old, G_new = graphs
        out = format_diff(diff_graphs(G_old, G_new), G_new)
        section = out.split("## What to review")[1]
        assert re.search(r"^1\. ", section, re.MULTILINE)

    def test_lists_at_most_five(self):
        G_old = nx.DiGraph()
        G_old.add_node("hub", label="Hub")
        G_new = nx.DiGraph()
        G_new.add_node("hub", label="Hub")
        for i in range(20):
            G_new.add_node(f"n{i}", label=f"Node{i}")
            G_new.add_edge("hub", f"n{i}")
        out = format_diff(diff_graphs(G_old, G_new), G_new)
        section = out.split("## What to review")[1]
        assert len(re.findall(r"^\d+\. ", section, re.MULTILINE)) <= 5

    def test_entries_show_centrality(self, graphs):
        G_old, G_new = graphs
        out = format_diff(diff_graphs(G_old, G_new), G_new)
        section = out.split("## What to review")[1]
        assert "centrality:" in section

    def test_entries_use_labels_not_ids(self, graphs):
        G_old, G_new = graphs
        out = format_diff(diff_graphs(G_old, G_new), G_new)
        section = out.split("## What to review")[1]
        assert "NodeD" in section or "NodeA_v2" in section or "NodeB" in section

    def test_entries_show_node_type(self, graphs):
        G_old, G_new = graphs
        out = format_diff(diff_graphs(G_old, G_new), G_new)
        section = out.split("## What to review")[1]
        assert "(module)" in section or "(function)" in section or "(class)" in section

    def test_entries_show_file_path_when_present(self):
        G_old = nx.DiGraph()
        G_old.add_node("seed", label="Seed")
        G_new = nx.DiGraph()
        G_new.add_node("seed", label="Seed")
        G_new.add_node("new", label="NewNode", type="function", file_path="src/new.py")
        G_new.add_edge("seed", "new")
        out = format_diff(diff_graphs(G_old, G_new), G_new)
        assert "src/new.py" in out.split("## What to review")[1]

    def test_section_comes_last(self, graphs):
        G_old, G_new = graphs
        out = format_diff(diff_graphs(G_old, G_new), G_new)
        assert out.index("## What to review") > out.index("## Summary")
        for section in ("## Added Nodes", "## Affected Edges"):
            if section in out:
                assert out.index("## What to review") > out.index(section)

    def test_hops_parameter_is_honoured(self, chain_graph):
        G_old = chain_graph.copy()
        G_old.remove_node("a")
        G_new = chain_graph
        diff = diff_graphs(G_old, G_new)
        near = format_diff(diff, G_new, hops=1).split("## What to review")[1]
        far = format_diff(diff, G_new, hops=4).split("## What to review")[1]
        assert len(re.findall(r"^\d+\. ", far, re.MULTILINE)) >= len(
            re.findall(r"^\d+\. ", near, re.MULTILINE)
        )

    def test_default_hops_is_two(self, chain_graph):
        G_old = chain_graph.copy()
        G_old.remove_node("a")
        G_new = chain_graph
        diff = diff_graphs(G_old, G_new)
        assert format_diff(diff, G_new) == format_diff(diff, G_new, hops=2)


class TestDiffCLIPanel:
    def test_panel_shown_when_changes(self, old_graph_json, new_graph_json):
        result = CliRunner().invoke(cli, ["diff", str(old_graph_json), str(new_graph_json)])
        assert result.exit_code == 0, result.output
        assert "Impact Summary" in result.output

    def test_panel_absent_when_no_changes(self, new_graph_json):
        result = CliRunner().invoke(cli, ["diff", str(new_graph_json), str(new_graph_json)])
        assert "Impact Summary" not in result.output

    def test_panel_shows_counts(self, old_graph_json, new_graph_json):
        result = CliRunner().invoke(cli, ["diff", str(old_graph_json), str(new_graph_json)])
        assert "added" in result.output
        assert "removed" in result.output
        assert "modified" in result.output

    def test_panel_shows_headline(self, old_graph_json, new_graph_json):
        result = CliRunner().invoke(cli, ["diff", str(old_graph_json), str(new_graph_json)])
        assert "impact change" in result.output

    def test_suggests_viz_when_not_used(self, old_graph_json, new_graph_json):
        result = CliRunner().invoke(cli, ["diff", str(old_graph_json), str(new_graph_json)])
        assert "See the full blast radius" in result.output
        assert "--viz" in result.output

    def test_no_suggestion_when_viz_used(self, old_graph_json, new_graph_json, monkeypatch):
        monkeypatch.setattr("webbrowser.open", lambda url: None)
        result = CliRunner().invoke(
            cli, ["diff", str(old_graph_json), str(new_graph_json), "--viz"]
        )
        assert "See the full blast radius" not in result.output

    def test_viz_output_alone_still_suggests_viz(self, old_graph_json, new_graph_json,
                                                 tmp_path):
        result = CliRunner().invoke(cli, [
            "diff", str(old_graph_json), str(new_graph_json),
            "--viz-output", str(tmp_path / "d.html"),
        ])
        assert "See the full blast radius" in result.output

    def test_suggestion_includes_hops_when_non_default(self, old_graph_json, new_graph_json):
        result = CliRunner().invoke(cli, [
            "diff", str(old_graph_json), str(new_graph_json), "--hops", "3",
        ])
        assert "--hops 3" in result.output
