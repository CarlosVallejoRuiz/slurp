"""Tests for slurp/scorer.py."""

import sys
from unittest.mock import patch

import networkx as nx
import pytest

from slurp.scorer import (
    _cosine_sim,
    _embed_anthropic,
    _embed_openai,
    _node_text,
    _tokenize,
    score_nodes,
    score_nodes_detailed,
)


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


# ---------------------------------------------------------------------------
# _node_text
# ---------------------------------------------------------------------------

class TestNodeText:
    def test_combines_label_type_description(self):
        attrs = {"label": "MyFunc", "type": "function", "description": "Does things"}
        result = _node_text(attrs)
        assert "MyFunc" in result
        assert "function" in result
        assert "Does things" in result

    def test_uses_file_type_if_no_type(self):
        attrs = {"label": "Foo", "file_type": "class", "description": ""}
        assert "class" in _node_text(attrs)

    def test_ignores_empty_parts(self):
        attrs = {"label": "X", "type": "", "description": ""}
        assert _node_text(attrs) == "X"

    def test_empty_attrs_returns_empty_string(self):
        assert _node_text({}) == ""


# ---------------------------------------------------------------------------
# _cosine_sim
# ---------------------------------------------------------------------------

class TestCosineSimHelper:
    def test_identical_vectors_return_1(self):
        assert _cosine_sim([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_return_0(self):
        assert _cosine_sim([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors_clamped_to_0(self):
        # -1 cosine sim is clamped to 0.0
        assert _cosine_sim([1.0, 0.0], [-1.0, 0.0]) == 0.0

    def test_zero_vector_returns_0(self):
        assert _cosine_sim([0.0, 0.0], [1.0, 0.0]) == 0.0
        assert _cosine_sim([1.0, 0.0], [0.0, 0.0]) == 0.0

    def test_similar_vectors_return_high_score(self):
        result = _cosine_sim([0.9, 0.1], [0.8, 0.2])
        assert result > 0.95

    def test_result_always_in_0_1(self):
        import random
        rng = random.Random(42)
        for _ in range(20):
            a = [rng.uniform(-1, 1) for _ in range(5)]
            b = [rng.uniform(-1, 1) for _ in range(5)]
            result = _cosine_sim(a, b)
            assert 0.0 <= result <= 1.0


# ---------------------------------------------------------------------------
# _embed_openai — ImportError behavior
# ---------------------------------------------------------------------------

class TestEmbedOpenAIImport:
    def test_raises_import_error_if_not_installed(self):
        with patch.dict(sys.modules, {"openai": None}):
            with pytest.raises(ImportError, match="pip install openai"):
                _embed_openai(["hello"])

    def test_error_message_mentions_openai(self):
        with patch.dict(sys.modules, {"openai": None}):
            with pytest.raises(ImportError) as exc_info:
                _embed_openai(["hello"])
            assert "openai" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# _embed_anthropic — ImportError behavior
# ---------------------------------------------------------------------------

class TestEmbedAnthropicImport:
    def test_raises_import_error_if_not_installed(self):
        with patch.dict(sys.modules, {"anthropic": None}):
            with pytest.raises(ImportError, match="pip install anthropic"):
                _embed_anthropic(["hello"])

    def test_error_message_mentions_anthropic(self):
        with patch.dict(sys.modules, {"anthropic": None}):
            with pytest.raises(ImportError) as exc_info:
                _embed_anthropic(["hello"])
            assert "anthropic" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# score_nodes with embedding backends (mocked)
# ---------------------------------------------------------------------------

def _fake_embeddings(n: int, dim: int = 8) -> list[list[float]]:
    """Returns n unit vectors where vector i has 1.0 at position i%dim."""
    result = []
    for i in range(n):
        v = [0.0] * dim
        v[i % dim] = 1.0
        result.append(v)
    return result


class TestEmbeddingBackendScoreNodes:
    def test_openai_backend_calls_embed_openai(self, sample_graph):
        n_nodes = sample_graph.number_of_nodes()
        fake = _fake_embeddings(n_nodes + 1)  # query + nodes
        with patch("slurp.scorer._embed_openai", return_value=fake) as mock_fn:
            scores = score_nodes(sample_graph, "auth", backend="openai")
            mock_fn.assert_called_once()
        assert set(scores.keys()) == set(sample_graph.nodes)

    def test_anthropic_backend_calls_embed_anthropic(self, sample_graph):
        n_nodes = sample_graph.number_of_nodes()
        fake = _fake_embeddings(n_nodes + 1)
        with patch("slurp.scorer._embed_anthropic", return_value=fake) as mock_fn:
            scores = score_nodes(sample_graph, "auth", backend="anthropic")
            mock_fn.assert_called_once()
        assert set(scores.keys()) == set(sample_graph.nodes)

    def test_embedding_scores_in_valid_range(self, sample_graph):
        n_nodes = sample_graph.number_of_nodes()
        fake = _fake_embeddings(n_nodes + 1)
        with patch("slurp.scorer._embed_openai", return_value=fake):
            scores = score_nodes(sample_graph, "auth", backend="openai")
        for nid, s in scores.items():
            assert 0.0 <= s <= 1.0, f"{nid}: score={s}"

    def test_tfidf_backend_is_unchanged_default(self, sample_graph):
        scores_default = score_nodes(sample_graph, "auth")
        scores_tfidf = score_nodes(sample_graph, "auth", backend="tfidf")
        for nid in sample_graph.nodes:
            assert abs(scores_default[nid] - scores_tfidf[nid]) < 1e-12

    def test_unknown_backend_raises_value_error(self, sample_graph):
        with pytest.raises(ValueError, match="Unknown backend"):
            score_nodes(sample_graph, "auth", backend="foobar")

    def test_openai_import_error_propagates(self, sample_graph):
        with patch("slurp.scorer._embed_openai",
                   side_effect=ImportError("pip install openai")):
            with pytest.raises(ImportError, match="pip install openai"):
                score_nodes(sample_graph, "auth", backend="openai")

    def test_anthropic_import_error_propagates(self, sample_graph):
        with patch("slurp.scorer._embed_anthropic",
                   side_effect=ImportError("pip install anthropic")):
            with pytest.raises(ImportError, match="pip install anthropic"):
                score_nodes(sample_graph, "auth", backend="anthropic")

    def test_embed_called_with_query_plus_all_nodes(self, sample_graph):
        n_nodes = sample_graph.number_of_nodes()
        fake = _fake_embeddings(n_nodes + 1)
        with patch("slurp.scorer._embed_openai", return_value=fake) as mock_fn:
            score_nodes(sample_graph, "my query", backend="openai")
        texts_arg = mock_fn.call_args[0][0]
        assert texts_arg[0] == "my query"         # query is first
        assert len(texts_arg) == n_nodes + 1       # query + all nodes

    def test_most_similar_node_gets_highest_score(self, sample_graph):
        node_ids = list(sample_graph.nodes)
        dim = len(node_ids) + 1
        # query embedding: 1.0 in slot 0
        query_emb = [1.0] + [0.0] * (dim - 1)
        # first node embedding: 1.0 in slot 0 (perfectly matches query)
        node_embs = []
        for i, _ in enumerate(node_ids):
            v = [0.0] * dim
            if i == 0:
                v[0] = 1.0   # perfect match
            else:
                v[i + 1] = 1.0  # orthogonal to query
            node_embs.append(v)
        all_embs = [query_emb] + node_embs

        with patch("slurp.scorer._embed_openai", return_value=all_embs):
            scores = score_nodes(sample_graph, "auth", backend="openai")

        best_id = max(scores, key=scores.__getitem__)
        assert best_id == node_ids[0]

    def test_empty_graph_returns_empty(self):
        G = nx.DiGraph()
        assert score_nodes(G, "auth", backend="openai") == {}

    def test_embedding_backend_returns_correct_node_count(self, sample_graph):
        n_nodes = sample_graph.number_of_nodes()
        fake = _fake_embeddings(n_nodes + 1)
        with patch("slurp.scorer._embed_anthropic", return_value=fake):
            scores = score_nodes(sample_graph, "auth", backend="anthropic")
        assert len(scores) == n_nodes


# ---------------------------------------------------------------------------
# score_nodes_detailed with embedding backends (mocked)
# ---------------------------------------------------------------------------

class TestEmbeddingBackendDetailed:
    def test_structural_zeros_for_embedding_backend(self, sample_graph):
        n_nodes = sample_graph.number_of_nodes()
        fake = _fake_embeddings(n_nodes + 1)
        with patch("slurp.scorer._embed_openai", return_value=fake):
            _, structural, _ = score_nodes_detailed(sample_graph, "auth", backend="openai")
        assert all(v == 0.0 for v in structural.values())

    def test_semantic_equals_final_for_embedding_backend(self, sample_graph):
        n_nodes = sample_graph.number_of_nodes()
        fake = _fake_embeddings(n_nodes + 1)
        with patch("slurp.scorer._embed_openai", return_value=fake):
            final, _, semantic = score_nodes_detailed(sample_graph, "auth", backend="openai")
        for nid in sample_graph.nodes:
            assert abs(final[nid] - semantic[nid]) < 1e-12

    def test_three_dicts_same_keys_for_embedding(self, sample_graph):
        n_nodes = sample_graph.number_of_nodes()
        fake = _fake_embeddings(n_nodes + 1)
        with patch("slurp.scorer._embed_anthropic", return_value=fake):
            final, structural, semantic = score_nodes_detailed(
                sample_graph, "auth", backend="anthropic"
            )
        assert set(final) == set(structural) == set(semantic) == set(sample_graph.nodes)

    def test_empty_graph_returns_three_empty_dicts_embedding(self):
        final, structural, semantic = score_nodes_detailed(
            nx.DiGraph(), "auth", backend="openai"
        )
        assert final == {} and structural == {} and semantic == {}


# ---------------------------------------------------------------------------
# CLI --backend flag (integration via CliRunner)
# ---------------------------------------------------------------------------

from click.testing import CliRunner
from slurp.cli import cli


class TestBackendCLI:
    def test_default_backend_tfidf(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(cli, [
            "auth", "--graph", str(sample_graph_json), "--no-audit"
        ])
        assert result.exit_code == 0

    def test_backend_flag_accepted(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(cli, [
            "auth", "--graph", str(sample_graph_json),
            "--backend", "tfidf", "--no-audit"
        ])
        assert result.exit_code == 0

    def test_openai_backend_calls_embed_openai(
        self, sample_graph_json, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        import networkx as _nx
        G = _nx.DiGraph()
        G.add_node("a", label="auth")
        n = 7  # sample_graph has 7 nodes
        fake = _fake_embeddings(n + 1)
        with patch("slurp.scorer._embed_openai", return_value=fake) as mock_fn:
            result = CliRunner().invoke(cli, [
                "auth", "--graph", str(sample_graph_json),
                "--backend", "openai", "--no-audit", "--min-score", "0.0"
            ])
        assert result.exit_code == 0, result.output
        mock_fn.assert_called()

    def test_anthropic_backend_calls_embed_anthropic(
        self, sample_graph_json, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        n = 7
        fake = _fake_embeddings(n + 1)
        with patch("slurp.scorer._embed_anthropic", return_value=fake) as mock_fn:
            result = CliRunner().invoke(cli, [
                "auth", "--graph", str(sample_graph_json),
                "--backend", "anthropic", "--no-audit", "--min-score", "0.0"
            ])
        assert result.exit_code == 0, result.output
        mock_fn.assert_called()

    def test_invalid_backend_rejected(self, sample_graph_json, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(cli, [
            "auth", "--graph", str(sample_graph_json), "--backend", "gpt4"
        ])
        assert result.exit_code != 0
