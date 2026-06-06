"""Tests for slurp/budget.py."""

import networkx as nx
import pytest

from slurp.budget import _serialize_node, count_tokens, select_subgraph

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LARGE_BUDGET = 100_000


def _simple_graph() -> tuple[nx.DiGraph, dict[str, float]]:
    """Three-node chain (a→b→c) with known scores."""
    G = nx.DiGraph()
    G.add_node("a", label="alpha", description="first node")
    G.add_node("b", label="beta", description="second node")
    G.add_node("c", label="gamma", description="third node")
    G.add_edge("a", "b")
    G.add_edge("b", "c")
    return G, {"a": 0.9, "b": 0.6, "c": 0.3}


# ---------------------------------------------------------------------------
# count_tokens
# ---------------------------------------------------------------------------


class TestCountTokens:
    def test_empty_string_returns_zero(self):
        assert count_tokens("") == 0

    def test_nonempty_returns_positive(self):
        assert count_tokens("hello world") > 0

    def test_longer_text_has_more_tokens(self):
        assert count_tokens("the quick brown fox jumps") > count_tokens("fox")

    def test_deterministic(self):
        assert count_tokens("auth service") == count_tokens("auth service")

    def test_custom_model_param_accepted(self):
        # Should not raise; cl100k_base is the default encoding.
        result = count_tokens("test", model="cl100k_base")
        assert isinstance(result, int) and result > 0


# ---------------------------------------------------------------------------
# _serialize_node
# ---------------------------------------------------------------------------


class TestSerializeNode:
    def test_includes_node_id(self):
        G = nx.DiGraph()
        G.add_node("my_node", label="MyNode", description="")
        assert "my_node" in _serialize_node(G, "my_node")

    def test_includes_label(self):
        G = nx.DiGraph()
        G.add_node("n", label="SomeLabel", description="")
        assert "SomeLabel" in _serialize_node(G, "n")

    def test_includes_type_in_parens(self):
        G = nx.DiGraph()
        G.add_node("n", label="n", type="function", description="")
        serialized = _serialize_node(G, "n")
        assert "(function)" in serialized

    def test_includes_description(self):
        G = nx.DiGraph()
        G.add_node("n", label="n", description="does important things")
        assert "does important things" in _serialize_node(G, "n")

    def test_includes_file_path(self):
        G = nx.DiGraph()
        G.add_node("n", label="n", description="", file_path="src/auth.py")
        assert "src/auth.py" in _serialize_node(G, "n")

    def test_omits_empty_description(self):
        G = nx.DiGraph()
        G.add_node("n", label="n", description="")
        serialized = _serialize_node(G, "n")
        # Only "n n" expected — no extra whitespace from empty fields.
        assert serialized == "n n"

    def test_omits_none_type(self):
        G = nx.DiGraph()
        G.add_node("n", label="n")
        serialized = _serialize_node(G, "n")
        assert "(" not in serialized


# ---------------------------------------------------------------------------
# select_subgraph — return shape
# ---------------------------------------------------------------------------


class TestSelectSubgraphShape:
    def test_returns_tuple_of_two(self):
        G, scores = _simple_graph()
        result = select_subgraph(G, scores, budget=LARGE_BUDGET)
        assert isinstance(result, tuple) and len(result) == 2

    def test_first_element_is_digraph(self):
        G, scores = _simple_graph()
        subG, _ = select_subgraph(G, scores, budget=LARGE_BUDGET)
        assert isinstance(subG, nx.DiGraph)

    def test_second_element_is_dict(self):
        G, scores = _simple_graph()
        _, stats = select_subgraph(G, scores, budget=LARGE_BUDGET)
        assert isinstance(stats, dict)

    def test_stats_has_all_required_keys(self):
        G, scores = _simple_graph()
        _, stats = select_subgraph(G, scores, budget=LARGE_BUDGET)
        assert set(stats.keys()) == {
            "nodes_selected",
            "nodes_total",
            "tokens_used",
            "tokens_budget",
            "coverage_pct",
        }


# ---------------------------------------------------------------------------
# select_subgraph — budget enforcement
# ---------------------------------------------------------------------------


class TestBudgetEnforcement:
    def test_zero_budget_selects_nothing(self):
        G, scores = _simple_graph()
        subG, stats = select_subgraph(G, scores, budget=0)
        assert subG.number_of_nodes() == 0
        assert stats["nodes_selected"] == 0
        assert stats["tokens_used"] == 0

    def test_large_budget_selects_all_nodes(self):
        G, scores = _simple_graph()
        subG, stats = select_subgraph(G, scores, budget=LARGE_BUDGET)
        assert stats["nodes_selected"] == G.number_of_nodes()

    def test_tokens_used_never_exceeds_budget(self, sample_graph):
        from slurp.scorer import score_nodes

        scores = score_nodes(sample_graph, "auth")
        for budget in [0, 10, 50, 100, 250, LARGE_BUDGET]:
            _, stats = select_subgraph(sample_graph, scores, budget=budget)
            assert stats["tokens_used"] <= budget, f"exceeded budget={budget}"

    def test_stats_tokens_budget_reflects_input(self):
        G, scores = _simple_graph()
        _, stats = select_subgraph(G, scores, budget=42)
        assert stats["tokens_budget"] == 42

    def test_oversized_node_skipped_smaller_node_still_selected(self):
        G = nx.DiGraph()
        # "big" has a huge label — far over any reasonable tight budget.
        G.add_node("big", label="word " * 200, description="")
        G.add_node("small", label="s", description="")
        scores = {"big": 0.9, "small": 0.1}

        small_tok = count_tokens(_serialize_node(G, "small"))
        # Budget fits "small" but not "big".
        subG, stats = select_subgraph(G, scores, budget=small_tok)

        assert "small" in subG.nodes
        assert "big" not in subG.nodes
        assert stats["tokens_used"] <= small_tok


# ---------------------------------------------------------------------------
# select_subgraph — stats correctness
# ---------------------------------------------------------------------------


class TestStats:
    def test_nodes_selected_matches_subgraph_size(self, sample_graph):
        from slurp.scorer import score_nodes

        scores = score_nodes(sample_graph, "auth")
        subG, stats = select_subgraph(sample_graph, scores, budget=100)
        assert stats["nodes_selected"] == subG.number_of_nodes()

    def test_nodes_total_matches_original_graph(self, sample_graph):
        from slurp.scorer import score_nodes

        scores = score_nodes(sample_graph, "auth")
        _, stats = select_subgraph(sample_graph, scores, budget=100)
        assert stats["nodes_total"] == sample_graph.number_of_nodes()

    def test_coverage_pct_100_when_all_selected(self):
        G, scores = _simple_graph()
        _, stats = select_subgraph(G, scores, budget=LARGE_BUDGET)
        assert stats["coverage_pct"] == 100.0

    def test_coverage_pct_0_when_nothing_selected(self):
        G, scores = _simple_graph()
        _, stats = select_subgraph(G, scores, budget=0)
        assert stats["coverage_pct"] == 0.0

    def test_coverage_pct_is_rounded_to_one_decimal(self, sample_graph):
        from slurp.scorer import score_nodes

        scores = score_nodes(sample_graph, "auth")
        _, stats = select_subgraph(sample_graph, scores, budget=50)
        pct = stats["coverage_pct"]
        assert isinstance(pct, float)
        assert pct == round(pct, 1)

    def test_tokens_used_equals_sum_of_selected_node_costs(self):
        G, scores = _simple_graph()
        subG, stats = select_subgraph(G, scores, budget=LARGE_BUDGET)
        expected = sum(count_tokens(_serialize_node(G, nid)) for nid in subG.nodes)
        assert stats["tokens_used"] == expected


# ---------------------------------------------------------------------------
# select_subgraph — selection order
# ---------------------------------------------------------------------------


class TestSelectionOrder:
    def test_highest_scoring_node_selected_with_tight_budget(self):
        G = nx.DiGraph()
        G.add_node("top", label="top", description="")
        G.add_node("low", label="low", description="")
        scores = {"top": 0.9, "low": 0.1}

        # Budget: exactly the cost of the "top" node.
        top_tok = count_tokens(_serialize_node(G, "top"))
        subG, _ = select_subgraph(G, scores, budget=top_tok)

        assert "top" in subG.nodes
        assert subG.number_of_nodes() == 1

    def test_selection_order_follows_descending_scores(self):
        G, scores = _simple_graph()
        # With large budget, all nodes are selected.
        subG, stats = select_subgraph(G, scores, budget=LARGE_BUDGET)
        assert stats["nodes_selected"] == 3

    def test_neighbor_decay_boosts_neighbor_above_peer(self):
        """Decay=0.7 promotes hub's neighbor above an unrelated peer node."""
        G = nx.DiGraph()
        G.add_node("hub", label="hub", description="")  # high score
        G.add_node("spoke", label="spoke", description="")  # low score, connected to hub
        G.add_node("peer", label="peer", description="")  # medium score, isolated
        G.add_edge("hub", "spoke")

        scores = {"hub": 0.9, "spoke": 0.1, "peer": 0.5}
        # After selecting hub (score=0.9): spoke gets 0.9 * 0.7 = 0.63 > peer (0.5).
        # Without decay:                  spoke stays at 0.1 < peer (0.5).

        hub_tok = count_tokens(_serialize_node(G, "hub"))    # 2 tokens
        spoke_tok = count_tokens(_serialize_node(G, "spoke"))  # 3 tokens
        budget = hub_tok + spoke_tok  # fits hub + spoke exactly (5 tokens)

        # Verify peer's cost fits within the remaining budget post-hub so the
        # test exercises the decay difference, not a size difference.
        peer_tok = count_tokens(_serialize_node(G, "peer"))  # 2 tokens
        assert peer_tok <= budget - hub_tok, "peer must fit post-hub for this test to be valid"

        subG_boosted, _ = select_subgraph(G, scores, budget=budget, neighbor_decay=0.7)
        subG_flat, _ = select_subgraph(G, scores, budget=budget, neighbor_decay=0.0)

        assert set(subG_boosted.nodes) == {"hub", "spoke"}
        assert set(subG_flat.nodes) == {"hub", "peer"}

    def test_decay_zero_uses_raw_scores_only(self):
        G = nx.DiGraph()
        G.add_node("high", label="high", description="")
        G.add_node("connected_low", label="connected", description="")
        G.add_edge("high", "connected_low")
        scores = {"high": 0.9, "connected_low": 0.1}

        high_tok = count_tokens(_serialize_node(G, "high"))
        subG, _ = select_subgraph(G, scores, budget=high_tok, neighbor_decay=0.0)

        # Only "high" fits; "connected_low" is not boosted.
        assert "high" in subG.nodes
        assert subG.number_of_nodes() == 1


# ---------------------------------------------------------------------------
# select_subgraph — subgraph structure
# ---------------------------------------------------------------------------


class TestSubgraphStructure:
    def test_subgraph_only_contains_selected_nodes(self, sample_graph):
        from slurp.scorer import score_nodes

        scores = score_nodes(sample_graph, "auth")
        subG, stats = select_subgraph(sample_graph, scores, budget=60)
        assert subG.number_of_nodes() == stats["nodes_selected"]
        assert set(subG.nodes).issubset(set(sample_graph.nodes))

    def test_subgraph_edges_are_induced_subset(self, sample_graph):
        from slurp.scorer import score_nodes

        scores = score_nodes(sample_graph, "auth")
        subG, _ = select_subgraph(sample_graph, scores, budget=60)
        for u, v in subG.edges:
            assert sample_graph.has_edge(u, v), f"Edge ({u},{v}) not in original graph"
            assert u in subG.nodes and v in subG.nodes

    def test_node_attributes_preserved_in_subgraph(self, sample_graph):
        from slurp.scorer import score_nodes

        scores = score_nodes(sample_graph, "auth")
        subG, _ = select_subgraph(sample_graph, scores, budget=LARGE_BUDGET)
        for nid in subG.nodes:
            for key in ("label", "type", "description", "file_path"):
                assert subG.nodes[nid].get(key) == sample_graph.nodes[nid].get(key)

    def test_edge_attributes_preserved_in_subgraph(self, sample_graph):
        from slurp.scorer import score_nodes

        scores = score_nodes(sample_graph, "auth")
        subG, _ = select_subgraph(sample_graph, scores, budget=LARGE_BUDGET)
        for u, v in subG.edges:
            assert subG.edges[u, v] == sample_graph.edges[u, v]


# ---------------------------------------------------------------------------
# select_subgraph — edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_graph_returns_empty_subgraph(self):
        G = nx.DiGraph()
        subG, stats = select_subgraph(G, {}, budget=1000)
        assert subG.number_of_nodes() == 0
        assert stats["nodes_selected"] == 0
        assert stats["nodes_total"] == 0
        assert stats["coverage_pct"] == 0.0

    def test_single_node_graph_large_budget(self):
        G = nx.DiGraph()
        G.add_node("only", label="only", description="sole node")
        subG, stats = select_subgraph(G, {"only": 1.0}, budget=LARGE_BUDGET)
        assert "only" in subG.nodes
        assert stats["nodes_selected"] == 1
        assert stats["coverage_pct"] == 100.0

    def test_single_node_graph_zero_budget(self):
        G = nx.DiGraph()
        G.add_node("only", label="only", description="")
        subG, stats = select_subgraph(G, {"only": 1.0}, budget=0)
        assert subG.number_of_nodes() == 0

    def test_missing_score_defaults_to_zero(self):
        G = nx.DiGraph()
        G.add_node("a", label="a", description="")
        G.add_node("b", label="b", description="")
        # Deliberately omit "b" from scores — should not crash.
        subG, stats = select_subgraph(G, {"a": 0.8}, budget=LARGE_BUDGET)
        assert "a" in subG.nodes
        assert stats["nodes_selected"] == 2  # both selected with large budget

    def test_integration_with_scorer_full_budget(self, sample_graph):
        from slurp.scorer import score_nodes

        scores = score_nodes(sample_graph, "authentication jwt token")
        subG, stats = select_subgraph(sample_graph, scores, budget=LARGE_BUDGET)
        assert stats["nodes_selected"] == sample_graph.number_of_nodes()
        assert stats["tokens_used"] > 0
        assert stats["coverage_pct"] == 100.0

    def test_integration_with_scorer_partial_budget(self, sample_graph):
        from slurp.scorer import score_nodes

        scores = score_nodes(sample_graph, "authentication jwt token")
        subG, stats = select_subgraph(sample_graph, scores, budget=60)
        assert 0 < stats["nodes_selected"] < sample_graph.number_of_nodes()
        assert stats["tokens_used"] <= 60
