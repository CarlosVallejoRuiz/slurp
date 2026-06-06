"""Tests for slurp/scorer.py."""

import networkx as nx
import pytest

from slurp.scorer import _tokenize, score_nodes


# ---------------------------------------------------------------------------
# _tokenize
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_lowercases(self):
        assert _tokenize("Hello World") == ["hello", "world"]

    def test_splits_on_underscore(self):
        assert _tokenize("authenticate_user") == ["authenticate", "user"]

    def test_splits_on_punctuation(self):
        assert _tokenize("foo.bar, baz!") == ["foo", "bar", "baz"]

    def test_camel_case_kept_as_one_token(self):
        assert _tokenize("JWTMiddleware") == ["jwtmiddleware"]

    def test_empty_string_returns_empty(self):
        assert _tokenize("") == []

    def test_numbers_preserved(self):
        assert _tokenize("v0.1.0") == ["v0", "1", "0"]

    def test_multiple_spaces_ignored(self):
        assert _tokenize("foo  bar") == ["foo", "bar"]


# ---------------------------------------------------------------------------
# score_nodes — return shape and value range
# ---------------------------------------------------------------------------


class TestScoreNodesShape:
    def test_returns_dict_for_all_nodes(self, sample_graph):
        scores = score_nodes(sample_graph, "auth")
        assert set(scores.keys()) == set(sample_graph.nodes)

    def test_all_scores_between_0_and_1(self, sample_graph):
        scores = score_nodes(sample_graph, "user authentication jwt")
        for node_id, score in scores.items():
            assert 0.0 <= score <= 1.0, f"{node_id} score {score} out of range"

    def test_empty_graph_returns_empty_dict(self):
        assert score_nodes(nx.DiGraph(), "anything") == {}

    def test_single_node_graph(self):
        G = nx.DiGraph()
        G.add_node("only", label="only node", description="")
        scores = score_nodes(G, "")
        assert "only" in scores
        assert 0.0 <= scores["only"] <= 1.0


# ---------------------------------------------------------------------------
# score_nodes — structural component (PageRank)
# ---------------------------------------------------------------------------


class TestStructuralComponent:
    def test_no_match_query_max_score_bounded_by_structural_weight(self, sample_graph):
        # With zero TF-IDF, max possible score = 0.4 * 1.0 (top-PageRank node).
        scores = score_nodes(sample_graph, "xyzzy foobar")
        assert max(scores.values()) <= 0.4 + 1e-9

    def test_empty_query_max_score_bounded_by_structural_weight(self, sample_graph):
        scores = score_nodes(sample_graph, "")
        assert max(scores.values()) <= 0.4 + 1e-9

    def test_empty_query_all_scores_non_negative(self, sample_graph):
        scores = score_nodes(sample_graph, "")
        for score in scores.values():
            assert score >= 0.0

    def test_graph_with_no_edges_uniform_pagerank(self):
        G = nx.DiGraph()
        for nid in ["a", "b", "c"]:
            G.add_node(nid, label=nid, description="")
        scores = score_nodes(G, "")
        # Without edges PageRank is uniform; after max-normalization all = 1.0,
        # so all structural scores are equal.
        vals = list(scores.values())
        assert abs(vals[0] - vals[1]) < 1e-9
        assert abs(vals[1] - vals[2]) < 1e-9

    def test_highly_connected_node_gets_higher_structural_score(self, sample_graph):
        # With a neutral query, structural weight dominates.
        # hash_password is pointed to by authenticate_user (weight=3),
        # which itself is pointed to by JWTMiddleware (weight=3) — high PR chain.
        # JWTMiddleware and PaymentService have no incoming edges — low PR.
        scores = score_nodes(sample_graph, "xyzzy")
        assert scores["hash_password"] > scores["JWTMiddleware"]
        assert scores["hash_password"] > scores["PaymentService"]


# ---------------------------------------------------------------------------
# score_nodes — semantic component (TF-IDF)
# ---------------------------------------------------------------------------


class TestSemanticComponent:
    def test_auth_query_ranks_authenticate_user_above_payment(self, sample_graph):
        scores = score_nodes(sample_graph, "authenticate credentials")
        assert scores["authenticate_user"] > scores["PaymentService"]

    def test_auth_query_ranks_authenticate_user_above_email(self, sample_graph):
        scores = score_nodes(sample_graph, "authenticate credentials")
        assert scores["authenticate_user"] > scores["send_email"]

    def test_email_query_ranks_send_email_above_authenticate_user(self, sample_graph):
        # "email" appears in send_email label and description; not in authenticate_user.
        scores = score_nodes(sample_graph, "email sendgrid")
        assert scores["send_email"] > scores["authenticate_user"]

    def test_payment_query_ranks_payment_service_above_hash_password(self, sample_graph):
        scores = score_nodes(sample_graph, "payment stripe")
        assert scores["PaymentService"] > scores["hash_password"]

    def test_database_query_ranks_database_pool_above_send_email(self, sample_graph):
        scores = score_nodes(sample_graph, "database postgresql pool")
        assert scores["DatabasePool"] > scores["send_email"]

    def test_jwt_query_ranks_middleware_above_payment(self, sample_graph):
        scores = score_nodes(sample_graph, "jwt middleware authorization")
        assert scores["JWTMiddleware"] > scores["PaymentService"]

    def test_exact_label_term_gives_nonzero_semantic_score(self, sample_graph):
        # "email" is a token of send_email's label — must produce > 0.
        scores = score_nodes(sample_graph, "email")
        assert scores["send_email"] > 0.0

    def test_no_matching_term_gives_only_structural_contribution(self, sample_graph):
        scores_neutral = score_nodes(sample_graph, "xyzzy")
        scores_empty = score_nodes(sample_graph, "")
        # Both should produce identical scores (TF-IDF = 0 in both cases).
        for nid in sample_graph.nodes:
            assert abs(scores_neutral[nid] - scores_empty[nid]) < 1e-9


# ---------------------------------------------------------------------------
# score_nodes — combined behavior
# ---------------------------------------------------------------------------


class TestCombinedScores:
    def test_top_node_for_auth_query_is_auth_related(self, sample_graph):
        scores = score_nodes(sample_graph, "authenticate user jwt")
        top = max(scores, key=scores.__getitem__)
        assert top in {"authenticate_user", "JWTMiddleware", "hash_password"}

    def test_scores_are_deterministic(self, sample_graph):
        s1 = score_nodes(sample_graph, "auth")
        s2 = score_nodes(sample_graph, "auth")
        assert s1 == s2

    def test_all_scores_finite(self, sample_graph):
        import math

        scores = score_nodes(sample_graph, "database pool connection")
        for score in scores.values():
            assert math.isfinite(score)
