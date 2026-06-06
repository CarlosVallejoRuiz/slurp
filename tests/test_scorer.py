"""Tests for slurp/scorer.py."""

import networkx as nx
import pytest

from slurp.scorer import _tokenize, score_nodes, score_nodes_detailed


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

    def test_camelcase_split_into_parts(self):
        tokens = _tokenize("JWTMiddleware")
        assert "jwt" in tokens
        assert "middleware" in tokens

    def test_camelcase_original_identifier_also_present(self):
        # Full compound form kept alongside split parts for exact-match searches.
        assert "jwtmiddleware" in _tokenize("JWTMiddleware")

    def test_camelcase_lowercase_to_uppercase_boundary(self):
        tokens = _tokenize("recalcularPlayerStats")
        assert "recalcular" in tokens
        assert "player" in tokens
        assert "stats" in tokens

    def test_snake_plus_camelcase_mixed_label(self):
        tokens = _tokenize("supabase_admin_recalcularPlayerStats")
        assert "supabase" in tokens
        assert "admin" in tokens
        assert "recalcular" in tokens
        assert "player" in tokens
        assert "stats" in tokens

    def test_pascalcase_split(self):
        tokens = _tokenize("UserModel")
        assert "user" in tokens
        assert "model" in tokens

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


# ---------------------------------------------------------------------------
# score_nodes — camelCase label matching
# ---------------------------------------------------------------------------


class TestCamelCaseScoring:
    def test_camelcase_label_scores_higher_for_matching_query(self):
        # Simulates a real graphify label like "recalcularPlayerStats".
        # With improved _tokenize, 'player' and 'stats' are extracted as separate
        # tokens, so the query "player stats" matches while "dashboard_layout" does not.
        G = nx.DiGraph()
        G.add_node("relevant", label="recalcularPlayerStats", type="function", description="")
        G.add_node("unrelated", label="dashboard_layout", type="function", description="")
        scores = score_nodes(G, "player stats")
        assert scores["relevant"] > scores["unrelated"], (
            f"camelCase node should score higher: "
            f"relevant={scores['relevant']:.4f}, unrelated={scores['unrelated']:.4f}"
        )

    def test_snake_camelcase_compound_matches_english_subwords(self):
        G = nx.DiGraph()
        G.add_node("a", label="supabase_admin_recalcularPlayerStats", description="")
        G.add_node("b", label="dashboard_layout", description="")
        scores = score_nodes(G, "player stats")
        assert scores["a"] > scores["b"]

    def test_pascalcase_label_matches_individual_words(self):
        G = nx.DiGraph()
        G.add_node("a", label="UserModel", description="")
        G.add_node("b", label="send_email", description="")
        scores = score_nodes(G, "user model")
        assert scores["a"] > scores["b"]

    def test_acronym_camelcase_matches_acronym_query(self):
        # JWTMiddleware → tokens include 'jwt', so query "jwt" scores it above unrelated.
        G = nx.DiGraph()
        G.add_node("a", label="JWTMiddleware", description="")
        G.add_node("b", label="PaymentService", description="")
        scores = score_nodes(G, "jwt")
        assert scores["a"] > scores["b"]


# ---------------------------------------------------------------------------
# score_nodes — file_type="code" boost
# ---------------------------------------------------------------------------


class TestCodeFileTypeBoost:
    def test_code_node_scores_higher_than_untyped(self):
        # Partial-match query: 3 terms, nodes only contain 2 → TF-IDF ≈ 0.56,
        # base score ≈ 0.73.  Boost pushes code to 1.0, plain stays at 0.73.
        G = nx.DiGraph()
        G.add_node("code",  label="auth login", description="", file_type="code")
        G.add_node("plain", label="auth login", description="")
        scores = score_nodes(G, "auth login token")
        assert scores["code"] > scores["plain"]

    def test_code_node_scores_higher_than_document(self):
        G = nx.DiGraph()
        G.add_node("code", label="auth service", description="", file_type="code")
        G.add_node("doc",  label="auth service", description="", file_type="document")
        scores = score_nodes(G, "auth service")
        assert scores["code"] > scores["doc"]

    def test_code_boost_clamped_to_one(self):
        G = nx.DiGraph()
        G.add_node("a", label="auth", description="auth", file_type="code")
        scores = score_nodes(G, "auth")
        assert scores["a"] <= 1.0

    def test_document_file_type_not_boosted(self):
        G = nx.DiGraph()
        G.add_node("a", label="auth", description="", file_type="document")
        G.add_node("b", label="auth", description="", file_type="config")
        G.add_node("c", label="auth", description="")
        scores = score_nodes(G, "auth")
        # None of these have file_type="code" — scores must be equal.
        assert abs(scores["a"] - scores["b"]) < 1e-9
        assert abs(scores["b"] - scores["c"]) < 1e-9

    def test_code_boost_applies_regardless_of_query_match(self):
        # Even with zero TF-IDF a code node should beat an untyped one
        # because PageRank is equal (isolated nodes) and boost adds 0.3.
        G = nx.DiGraph()
        G.add_node("code",  label="irrelevant_func", description="", file_type="code")
        G.add_node("plain", label="irrelevant_func", description="")
        scores = score_nodes(G, "xyzzy")  # no TF-IDF match for either
        assert scores["code"] > scores["plain"]

    def test_scores_still_between_0_and_1_with_boost(self, sample_graph):
        G = sample_graph.copy()
        G.add_node("code_node", label="auth", description="auth service", file_type="code")
        scores = score_nodes(G, "auth service")
        for nid, s in scores.items():
            assert 0.0 <= s <= 1.0, f"{nid}: {s}"


# ---------------------------------------------------------------------------
# score_nodes_detailed — component exposure
# ---------------------------------------------------------------------------


class TestScoreNodesDetailed:
    def test_returns_three_dicts(self, sample_graph):
        result = score_nodes_detailed(sample_graph, "auth")
        assert len(result) == 3
        final, structural, semantic = result
        assert isinstance(final, dict)
        assert isinstance(structural, dict)
        assert isinstance(semantic, dict)

    def test_all_three_cover_same_nodes(self, sample_graph):
        final, structural, semantic = score_nodes_detailed(sample_graph, "auth")
        assert set(final) == set(structural) == set(semantic) == set(sample_graph.nodes)

    def test_final_scores_match_score_nodes(self, sample_graph):
        final, _, _ = score_nodes_detailed(sample_graph, "auth flow")
        plain = score_nodes(sample_graph, "auth flow")
        for nid in sample_graph.nodes:
            assert abs(final[nid] - plain[nid]) < 1e-12

    def test_structural_scores_between_0_and_1(self, sample_graph):
        _, structural, _ = score_nodes_detailed(sample_graph, "auth")
        for nid, s in structural.items():
            assert 0.0 <= s <= 1.0, f"{nid} structural={s}"

    def test_semantic_scores_between_0_and_1(self, sample_graph):
        _, _, semantic = score_nodes_detailed(sample_graph, "auth")
        for nid, s in semantic.items():
            assert 0.0 <= s <= 1.0, f"{nid} semantic={s}"

    def test_empty_graph_returns_three_empty_dicts(self):
        final, structural, semantic = score_nodes_detailed(nx.DiGraph(), "auth")
        assert final == {} and structural == {} and semantic == {}

    def test_semantic_nonzero_for_matching_query(self, sample_graph):
        _, _, semantic = score_nodes_detailed(sample_graph, "authenticate credentials")
        assert semantic["authenticate_user"] > 0.0

    def test_structural_nonzero_for_connected_nodes(self, sample_graph):
        _, structural, _ = score_nodes_detailed(sample_graph, "auth")
        # hash_password is pointed to by authenticate_user — has incoming edges → PR > 0.
        assert structural["hash_password"] > 0.0
