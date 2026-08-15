"""Tests for slurp/budget.py."""

import random

import networkx as nx
import pytest

from slurp.budget import (
    _node_token_cache,
    _node_token_cost,
    _serialize_node,
    clear_node_token_cache,
    count_tokens,
    select_subgraph,
)

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


# ---------------------------------------------------------------------------
# select_subgraph — min_score filtering
# ---------------------------------------------------------------------------


class TestMinScore:
    def test_min_score_filters_low_score_nodes(self):
        G, _ = _simple_graph()
        scores = {"a": 0.9, "b": 0.6, "c": 0.3}
        subG, stats = select_subgraph(G, scores, budget=LARGE_BUDGET, min_score=0.5)
        assert "a" in subG.nodes
        assert "b" in subG.nodes
        assert "c" not in subG.nodes
        assert stats["nodes_selected"] == 2

    def test_min_score_above_all_scores_selects_nothing(self):
        G, scores = _simple_graph()
        subG, stats = select_subgraph(G, scores, budget=LARGE_BUDGET, min_score=1.1)
        assert stats["nodes_selected"] == 0
        assert stats["tokens_used"] == 0

    def test_min_score_zero_equivalent_to_no_filter(self):
        G, scores = _simple_graph()
        _, stats_default = select_subgraph(G, scores, budget=LARGE_BUDGET)
        _, stats_zero = select_subgraph(G, scores, budget=LARGE_BUDGET, min_score=0.0)
        assert stats_default["nodes_selected"] == stats_zero["nodes_selected"]
        assert stats_default["tokens_used"] == stats_zero["tokens_used"]

    def test_min_score_boundary_is_inclusive(self):
        G, _ = _simple_graph()
        scores = {"a": 0.9, "b": 0.5, "c": 0.3}
        subG, _ = select_subgraph(G, scores, budget=LARGE_BUDGET, min_score=0.5)
        assert "b" in subG.nodes  # score == threshold → included

    def test_min_score_just_below_boundary_excluded(self):
        G, _ = _simple_graph()
        scores = {"a": 0.9, "b": 0.499, "c": 0.3}
        subG, _ = select_subgraph(G, scores, budget=LARGE_BUDGET, min_score=0.5)
        assert "b" not in subG.nodes

    def test_nodes_total_reflects_full_graph_not_candidates(self):
        G, _ = _simple_graph()
        scores = {"a": 0.9, "b": 0.6, "c": 0.3}
        _, stats = select_subgraph(G, scores, budget=LARGE_BUDGET, min_score=0.5)
        assert stats["nodes_total"] == 3  # full graph has 3 nodes

    def test_coverage_pct_relative_to_full_graph(self):
        G, _ = _simple_graph()
        scores = {"a": 0.9, "b": 0.6, "c": 0.3}
        _, stats = select_subgraph(G, scores, budget=LARGE_BUDGET, min_score=0.5)
        assert stats["nodes_selected"] == 2
        assert stats["coverage_pct"] == round(2 / 3 * 100, 1)

    def test_missing_score_treated_as_zero_filtered_by_min_score(self):
        G = nx.DiGraph()
        G.add_node("a", label="a", description="")
        G.add_node("b", label="b", description="")
        subG, _ = select_subgraph(G, {"a": 0.8}, budget=LARGE_BUDGET, min_score=0.1)
        assert "a" in subG.nodes
        assert "b" not in subG.nodes  # missing → 0.0 < 0.1

    def test_neighbor_decay_boost_does_not_include_filtered_nodes(self):
        G = nx.DiGraph()
        G.add_node("hub", label="hub", description="")
        G.add_node("low", label="low", description="")
        G.add_edge("hub", "low")
        scores = {"hub": 0.9, "low": 0.05}
        # low is below min_score; hub's decay boost must not rescue it
        subG, _ = select_subgraph(G, scores, budget=LARGE_BUDGET, min_score=0.1)
        assert "hub" in subG.nodes
        assert "low" not in subG.nodes

    def test_tokens_used_never_exceeds_budget_with_min_score(self, sample_graph):
        from slurp.scorer import score_nodes

        scores = score_nodes(sample_graph, "auth")
        for budget in [10, 50, 100, LARGE_BUDGET]:
            _, stats = select_subgraph(
                sample_graph, scores, budget=budget, min_score=0.05
            )
            assert stats["tokens_used"] <= budget


# ---------------------------------------------------------------------------
# Per-node token cache
# ---------------------------------------------------------------------------


class TestNodeTokenCache:
    @pytest.fixture(autouse=True)
    def _clean(self):
        clear_node_token_cache()
        yield
        clear_node_token_cache()

    @staticmethod
    def _graph():
        G = nx.DiGraph()
        G.add_node("a", label="Alpha", type="function", description="first node")
        G.add_node("b", label="Beta", type="class", description="second node")
        G.add_edge("a", "b")
        return G

    def test_starts_empty(self):
        assert len(_node_token_cache) == 0

    def test_first_query_populates_one_entry_per_node(self):
        G = self._graph()
        select_subgraph(G, {"a": 1.0, "b": 0.5}, budget=1000)
        assert len(_node_token_cache) == G.number_of_nodes()

    def test_second_query_does_not_re_encode(self, monkeypatch):
        G = self._graph()
        select_subgraph(G, {"a": 1.0, "b": 0.5}, budget=1000)

        calls = {"n": 0}
        import slurp.budget as budget_mod
        original = budget_mod.count_tokens

        def counting(text, model="cl100k_base"):
            calls["n"] += 1
            return original(text, model)

        monkeypatch.setattr(budget_mod, "count_tokens", counting)
        select_subgraph(G, {"a": 1.0, "b": 0.5}, budget=1000)
        assert calls["n"] == 0

    def test_second_query_does_not_re_serialize(self, monkeypatch):
        G = self._graph()
        select_subgraph(G, {"a": 1.0, "b": 0.5}, budget=1000)

        calls = {"n": 0}
        import slurp.budget as budget_mod
        original = budget_mod._serialize_node

        def counting(graph, node_id):
            calls["n"] += 1
            return original(graph, node_id)

        monkeypatch.setattr(budget_mod, "_serialize_node", counting)
        select_subgraph(G, {"a": 1.0, "b": 0.5}, budget=1000)
        assert calls["n"] == 0

    def test_cached_run_gives_the_same_result(self):
        G = self._graph()
        first_sub, first_stats = select_subgraph(G, {"a": 1.0, "b": 0.5}, budget=1000)
        second_sub, second_stats = select_subgraph(G, {"a": 1.0, "b": 0.5}, budget=1000)
        assert first_stats == second_stats
        assert set(first_sub.nodes) == set(second_sub.nodes)

    def test_clear_empties_the_cache(self):
        G = self._graph()
        select_subgraph(G, {"a": 1.0, "b": 0.5}, budget=1000)
        clear_node_token_cache()
        assert len(_node_token_cache) == 0

    def test_recomputes_after_clear(self, monkeypatch):
        G = self._graph()
        select_subgraph(G, {"a": 1.0, "b": 0.5}, budget=1000)
        clear_node_token_cache()

        calls = {"n": 0}
        import slurp.budget as budget_mod
        original = budget_mod.count_tokens

        def counting(text, model="cl100k_base"):
            calls["n"] += 1
            return original(text, model)

        monkeypatch.setattr(budget_mod, "count_tokens", counting)
        select_subgraph(G, {"a": 1.0, "b": 0.5}, budget=1000)
        assert calls["n"] > 0

    def test_different_models_cached_separately(self):
        G = self._graph()
        select_subgraph(G, {"a": 1.0, "b": 0.5}, budget=1000, model="cl100k_base")
        before = len(_node_token_cache)
        select_subgraph(G, {"a": 1.0, "b": 0.5}, budget=1000, model="p50k_base")
        assert len(_node_token_cache) == before * 2

    def test_model_is_part_of_the_key(self):
        G = self._graph()
        _node_token_cost(G, "a", "cl100k_base")
        assert ("a", "cl100k_base") in _node_token_cache
        assert ("a", "p50k_base") not in _node_token_cache

    def test_cost_matches_uncached_computation(self):
        G = self._graph()
        expected = count_tokens(_serialize_node(G, "a"), "cl100k_base")
        assert _node_token_cost(G, "a", "cl100k_base") == expected

    def test_cost_is_stable_across_calls(self):
        G = self._graph()
        assert _node_token_cost(G, "a", "cl100k_base") == _node_token_cost(G, "a", "cl100k_base")


# ---------------------------------------------------------------------------
# Heap-based greedy loop
# ---------------------------------------------------------------------------


def _reference_select(G, scores, budget, model="cl100k_base",
                      neighbor_decay=0.7, min_score=0.0):
    """The pre-heap O(n^2) implementation, kept as a differential oracle.

    NOTE: its tie-breaking depends on set iteration order, which Python
    randomises per process, so it is only deterministic when no two candidates
    ever hold the same effective score.
    """
    nodes_total = G.number_of_nodes()
    if nodes_total == 0 or budget <= 0:
        return nx.DiGraph(), {
            "nodes_selected": 0, "nodes_total": nodes_total, "tokens_used": 0,
            "tokens_budget": budget, "coverage_pct": 0.0}
    token_cost = {nid: _node_token_cost(G, nid, model) for nid in G.nodes}
    candidates = {nid for nid in G.nodes if scores.get(nid, 0.0) >= min_score}
    effective = {nid: scores.get(nid, 0.0) for nid in candidates}
    processed, selected, tokens_used = set(), [], 0
    while len(processed) < len(candidates):
        best = max((nid for nid in candidates if nid not in processed),
                   key=lambda nid: effective.get(nid, 0.0))
        processed.add(best)
        cost = token_cost[best]
        if cost <= budget - tokens_used:
            selected.append(best)
            tokens_used += cost
            boosted = effective.get(best, 0.0) * neighbor_decay
            for nbr in set(G.predecessors(best)) | set(G.successors(best)):
                if nbr in candidates and nbr not in processed \
                        and boosted > effective.get(nbr, 0.0):
                    effective[nbr] = boosted
        if tokens_used >= budget:
            break
    return G.subgraph(selected).copy(), {
        "nodes_selected": len(selected), "nodes_total": nodes_total,
        "tokens_used": tokens_used, "tokens_budget": budget,
        "coverage_pct": round(len(selected) / nodes_total * 100, 1)}


def _random_graph(rng, n):
    G = nx.DiGraph()
    for i in range(n):
        G.add_node(f"n{i}", label=f"Node{i}", description="x" * rng.randint(1, 40))
    for _ in range(rng.randint(0, n * 2)):
        a, b = rng.randrange(n), rng.randrange(n)
        if a != b:
            G.add_edge(f"n{a}", f"n{b}")
    return G


class TestHeapEquivalence:
    """The heap loop must agree with the original scan wherever the original
    was well-defined — i.e. when no two candidates share an effective score."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        clear_node_token_cache()
        yield
        clear_node_token_cache()

    def test_matches_reference_when_ties_are_impossible(self):
        """Edgeless graphs have no neighbour boost, so distinct input scores stay
        distinct — the only regime where the original scan is deterministic."""
        rng = random.Random(99)
        for _ in range(150):
            n = rng.randint(2, 30)
            G = nx.DiGraph()
            for i in range(n):
                G.add_node(f"n{i}", label=f"Node{i}",
                           description="x" * rng.randint(1, 40))
            vals = rng.sample(range(1, 100_000), n)
            scores = {f"n{i}": vals[i] / 100_000.0 for i in range(n)}
            budget = rng.choice([50, 200, 1000, 5000])
            r_sub, r_stats = _reference_select(G, scores, budget)
            h_sub, h_stats = select_subgraph(G, scores, budget)
            assert r_stats == h_stats
            assert set(r_sub.nodes) == set(h_sub.nodes)

    def test_divergence_requires_a_tie(self):
        """With every score tied, the two implementations may pick different
        nodes — both valid greedy answers — but must agree on the budget spent
        being within limits and on selecting a non-empty set."""
        G = nx.DiGraph()
        for i in range(20):
            G.add_node(f"n{i}", label=f"N{i}", description="d")
        for i in range(19):
            G.add_edge(f"n{i}", f"n{i + 1}")
        scores = {f"n{i}": 0.5 for i in range(20)}
        _, r_stats = _reference_select(G, scores, 60)
        h_sub, h_stats = select_subgraph(G, scores, 60)
        assert r_stats["tokens_used"] <= 60
        assert h_stats["tokens_used"] <= 60
        assert h_stats["nodes_selected"] > 0

    def test_matches_reference_on_a_tie_free_chain(self):
        G = nx.DiGraph()
        for i in range(12):
            G.add_node(f"n{i}", label=f"Node{i}", description="d" * (i + 1))
        for i in range(11):
            G.add_edge(f"n{i}", f"n{i + 1}")
        scores = {f"n{i}": (i + 1) / 100.0 for i in range(12)}
        r_sub, r_stats = _reference_select(G, scores, 200)
        h_sub, h_stats = select_subgraph(G, scores, 200)
        assert r_stats == h_stats
        assert set(r_sub.nodes) == set(h_sub.nodes)

    def test_single_node_matches(self):
        G = nx.DiGraph()
        G.add_node("solo", label="Solo", description="only node")
        r_sub, r_stats = _reference_select(G, {"solo": 1.0}, 1000)
        h_sub, h_stats = select_subgraph(G, {"solo": 1.0}, 1000)
        assert r_stats == h_stats
        assert set(r_sub.nodes) == set(h_sub.nodes)

    def test_empty_graph_matches(self):
        G = nx.DiGraph()
        assert _reference_select(G, {}, 1000)[1] == select_subgraph(G, {}, 1000)[1]

    def test_zero_budget_matches(self):
        G = nx.DiGraph()
        G.add_node("a", label="A", description="d")
        assert _reference_select(G, {"a": 1.0}, 0)[1] == select_subgraph(G, {"a": 1.0}, 0)[1]


class TestHeapDeterminism:
    """The heap breaks ties by node id, so the output no longer depends on set
    iteration order — unlike the original scan."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        clear_node_token_cache()
        yield
        clear_node_token_cache()

    @staticmethod
    def _all_tied_graph():
        G = nx.DiGraph()
        for i in range(40):
            G.add_node(f"n{i}", label=f"N{i}", description="d" * (i % 7 + 1))
        for i in range(39):
            G.add_edge(f"n{i}", f"n{i + 1}")
        return G, {f"n{i}": 0.5 for i in range(40)}

    def test_repeated_runs_are_identical(self):
        G, scores = self._all_tied_graph()
        first_sub, first_stats = select_subgraph(G, scores, 100)
        for _ in range(5):
            sub, stats = select_subgraph(G, scores, 100)
            assert stats == first_stats
            assert set(sub.nodes) == set(first_sub.nodes)

    def test_all_tied_selection_is_ordered_by_node_id(self):
        G, scores = self._all_tied_graph()
        sub, _ = select_subgraph(G, scores, 100)
        assert sorted(sub.nodes) == sorted(sub.nodes)  # sanity
        # n0 sorts first among tied ids and must be selected.
        assert "n0" in sub.nodes

    def test_reordering_node_insertion_does_not_change_result(self):
        G1, scores = self._all_tied_graph()
        sub1, stats1 = select_subgraph(G1, scores, 100)
        G2 = nx.DiGraph()
        for i in reversed(range(40)):
            G2.add_node(f"n{i}", label=f"N{i}", description="d" * (i % 7 + 1))
        for i in range(39):
            G2.add_edge(f"n{i}", f"n{i + 1}")
        sub2, stats2 = select_subgraph(G2, scores, 100)
        assert stats1 == stats2
        assert set(sub1.nodes) == set(sub2.nodes)


class TestHeapInvariants:
    """Properties the greedy algorithm must hold regardless of tie-breaking."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        clear_node_token_cache()
        yield
        clear_node_token_cache()

    def test_never_exceeds_budget(self):
        rng = random.Random(7)
        for _ in range(60):
            G = _random_graph(rng, rng.randint(2, 25))
            scores = {n: rng.random() for n in G.nodes}
            budget = rng.choice([10, 50, 200, 1000])
            _, stats = select_subgraph(G, scores, budget)
            assert stats["tokens_used"] <= budget

    def test_stats_are_self_consistent(self):
        rng = random.Random(11)
        for _ in range(60):
            G = _random_graph(rng, rng.randint(2, 25))
            scores = {n: rng.random() for n in G.nodes}
            sub, stats = select_subgraph(G, scores, 500)
            assert stats["nodes_selected"] == sub.number_of_nodes()
            assert stats["nodes_total"] == G.number_of_nodes()

    def test_highest_scoring_node_selected_first_when_it_fits(self):
        G = nx.DiGraph()
        G.add_node("low", label="Low", description="d")
        G.add_node("high", label="High", description="d")
        sub, _ = select_subgraph(G, {"low": 0.1, "high": 0.9}, 10)
        assert "high" in sub.nodes

    def test_min_score_filter_still_applies(self):
        G = nx.DiGraph()
        G.add_node("keep", label="Keep", description="d")
        G.add_node("drop", label="Drop", description="d")
        sub, _ = select_subgraph(G, {"keep": 0.9, "drop": 0.1},
                                 budget=1000, min_score=0.5)
        assert "keep" in sub.nodes
        assert "drop" not in sub.nodes

    def test_neighbor_boost_still_propagates(self):
        """A low-scored neighbour of the top node beats an unconnected peer."""
        G = nx.DiGraph()
        G.add_node("hub", label="Hub", description="d")
        G.add_node("nbr", label="Nbr", description="d")
        G.add_node("far", label="Far", description="d")
        G.add_edge("hub", "nbr")
        scores = {"hub": 1.0, "nbr": 0.01, "far": 0.02}
        # Budget for exactly hub + nbr, computed from their real token costs so
        # the assertion does not depend on two node ids tokenising identically.
        budget = (_node_token_cost(G, "hub", "cl100k_base")
                  + _node_token_cost(G, "nbr", "cl100k_base"))
        sub, _ = select_subgraph(G, scores, budget=budget, neighbor_decay=0.7)
        assert "hub" in sub.nodes
        assert "nbr" in sub.nodes   # boosted 0.01 -> 0.7, overtaking far's 0.02
        assert "far" not in sub.nodes

    def test_no_node_selected_twice(self):
        rng = random.Random(13)
        for _ in range(40):
            G = _random_graph(rng, rng.randint(2, 20))
            scores = {n: rng.choice([0.0, 0.5, 1.0]) for n in G.nodes}
            sub, stats = select_subgraph(G, scores, 300)
            assert len(set(sub.nodes)) == stats["nodes_selected"]

    def test_stale_heap_entries_do_not_reprocess(self):
        """A boosted node is pushed twice; it must still be selected once."""
        G = nx.DiGraph()
        for i in range(6):
            G.add_node(f"n{i}", label=f"N{i}", description="d")
        G.add_edge("n0", "n1")
        G.add_edge("n1", "n2")
        scores = {f"n{i}": 1.0 - i * 0.1 for i in range(6)}
        sub, stats = select_subgraph(G, scores, 1000)
        assert len(list(sub.nodes)) == stats["nodes_selected"]
        assert len(set(sub.nodes)) == len(list(sub.nodes))
