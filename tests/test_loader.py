"""Tests for slurp/loader.py."""

import json
from pathlib import Path

import networkx as nx
import pytest

from slurp.loader import SlurpLoadError, detect_format, load_graph


class TestDetectFormat:
    def test_graphify_format_detected_by_type_field(self):
        data = {"nodes": [{"id": "a", "label": "A", "type": "function"}], "edges": []}
        assert detect_format(data) == "graphify"

    def test_graphify_format_detected_by_description(self):
        data = {"nodes": [{"id": "a", "label": "A", "description": "does stuff"}], "edges": []}
        assert detect_format(data) == "graphify"

    def test_graphify_format_detected_by_importance(self):
        data = {"nodes": [{"id": "a", "label": "A", "importance": 5}], "edges": []}
        assert detect_format(data) == "graphify"

    def test_graphify_format_detected_by_file_path(self):
        data = {"nodes": [{"id": "a", "label": "A", "file_path": "src/a.py"}], "edges": []}
        assert detect_format(data) == "graphify"

    def test_generic_format_only_id_and_label(self):
        data = {"nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}], "edges": []}
        assert detect_format(data) == "generic"

    def test_empty_nodes_returns_generic(self):
        assert detect_format({"nodes": []}) == "generic"

    def test_missing_nodes_key_returns_generic(self):
        assert detect_format({}) == "generic"

    def test_sample_graph_is_graphify(self, sample_graph_json):
        data = json.loads(sample_graph_json.read_text())
        assert detect_format(data) == "graphify"


class TestLoadGraph:
    def test_loads_sample_graph(self, sample_graph_json):
        G = load_graph(sample_graph_json)
        assert isinstance(G, nx.DiGraph)

    def test_node_count(self, sample_graph_json):
        G = load_graph(sample_graph_json)
        assert G.number_of_nodes() == 7

    def test_edge_count(self, sample_graph_json):
        G = load_graph(sample_graph_json)
        assert G.number_of_edges() == 6

    def test_node_attributes_preserved(self, sample_graph_json):
        G = load_graph(sample_graph_json)
        node = G.nodes["authenticate_user"]
        assert node["label"] == "authenticate_user"
        assert node["type"] == "function"
        assert node["importance"] == 9
        assert node["file_path"] == "src/auth/service.py"

    def test_edge_attributes_preserved(self, sample_graph_json):
        G = load_graph(sample_graph_json)
        edge = G.edges["authenticate_user", "hash_password"]
        assert edge["relation"] == "calls"
        assert edge["weight"] == 3

    def test_graph_is_directed(self, sample_graph_json):
        G = load_graph(sample_graph_json)
        assert G.is_directed()

    def test_edge_direction_preserved(self, sample_graph_json):
        G = load_graph(sample_graph_json)
        assert G.has_edge("JWTMiddleware", "authenticate_user")
        assert not G.has_edge("authenticate_user", "JWTMiddleware")

    def test_loads_generic_format(self, generic_graph_json):
        G = load_graph(generic_graph_json)
        assert G.number_of_nodes() == 3
        assert G.number_of_edges() == 2

    def test_generic_node_labels_preserved(self, generic_graph_json):
        G = load_graph(generic_graph_json)
        assert G.nodes["a"]["label"] == "NodeA"


class TestLoadGraphErrors:
    def test_file_not_found(self, tmp_path):
        missing = tmp_path / "nonexistent.json"
        with pytest.raises(SlurpLoadError, match="not found"):
            load_graph(missing)

    def test_invalid_json(self, tmp_path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{ not valid json }")
        with pytest.raises(SlurpLoadError, match="Invalid JSON"):
            load_graph(bad_file)

    def test_empty_nodes_list(self, tmp_path):
        graph_file = tmp_path / "empty.json"
        graph_file.write_text(json.dumps({"nodes": [], "edges": []}))
        with pytest.raises(SlurpLoadError, match="no nodes"):
            load_graph(graph_file)

    def test_missing_nodes_key(self, tmp_path):
        graph_file = tmp_path / "no_nodes.json"
        graph_file.write_text(json.dumps({"edges": []}))
        with pytest.raises(SlurpLoadError, match="no nodes"):
            load_graph(graph_file)

    def test_non_object_json(self, tmp_path):
        graph_file = tmp_path / "array.json"
        graph_file.write_text(json.dumps([1, 2, 3]))
        with pytest.raises(SlurpLoadError):
            load_graph(graph_file)

    def test_node_missing_id(self, tmp_path):
        graph_file = tmp_path / "no_id.json"
        data = {"nodes": [{"label": "NoId"}], "edges": []}
        graph_file.write_text(json.dumps(data))
        with pytest.raises(SlurpLoadError, match="'id'"):
            load_graph(graph_file)

    def test_node_missing_label(self, tmp_path):
        graph_file = tmp_path / "no_label.json"
        data = {"nodes": [{"id": "x"}], "edges": []}
        graph_file.write_text(json.dumps(data))
        with pytest.raises(SlurpLoadError, match="'label'"):
            load_graph(graph_file)

    def test_graph_with_no_edges_loads_fine(self, tmp_path):
        graph_file = tmp_path / "no_edges.json"
        data = {"nodes": [{"id": "a", "label": "A"}]}
        graph_file.write_text(json.dumps(data))
        G = load_graph(graph_file)
        assert G.number_of_nodes() == 1
        assert G.number_of_edges() == 0


# ---------------------------------------------------------------------------
# Real graphify format (links key, file_type, source_file, confidence)
# ---------------------------------------------------------------------------


class TestDetectFormatRealGraphify:
    def test_links_key_detected_as_graphify(self):
        data = {
            "directed": True,
            "multigraph": False,
            "nodes": [{"id": "a", "label": "A"}],
            "links": [],
        }
        assert detect_format(data) == "graphify"

    def test_links_key_takes_priority_over_node_fields(self):
        # Even with no graphify-specific node fields, links → graphify
        data = {"nodes": [{"id": "a", "label": "A"}], "links": []}
        assert detect_format(data) == "graphify"

    def test_file_type_field_detected_as_graphify(self):
        data = {"nodes": [{"id": "a", "label": "A", "file_type": "function"}]}
        assert detect_format(data) == "graphify"

    def test_source_file_field_detected_as_graphify(self):
        data = {"nodes": [{"id": "a", "label": "A", "source_file": "src/a.py"}]}
        assert detect_format(data) == "graphify"

    def test_real_graphify_fixture_detected_as_graphify(self, real_graphify_json):
        data = json.loads(real_graphify_json.read_text())
        assert detect_format(data) == "graphify"


class TestLoadRealGraphifyFormat:
    def test_loads_real_graphify_json(self, real_graphify_json):
        G = load_graph(real_graphify_json)
        assert isinstance(G, nx.DiGraph)

    def test_node_count(self, real_graphify_json):
        G = load_graph(real_graphify_json)
        assert G.number_of_nodes() == 4

    def test_edge_count(self, real_graphify_json):
        G = load_graph(real_graphify_json)
        assert G.number_of_edges() == 3

    def test_graph_is_directed(self, real_graphify_json):
        G = load_graph(real_graphify_json)
        assert G.is_directed()

    def test_node_id_is_full_qualified_path(self, real_graphify_json):
        G = load_graph(real_graphify_json)
        assert "src/auth/service.py::authenticate_user" in G.nodes

    def test_node_label_preserved(self, real_graphify_json):
        G = load_graph(real_graphify_json)
        node = G.nodes["src/auth/service.py::authenticate_user"]
        assert node["label"] == "authenticate_user"

    def test_node_file_type_preserved(self, real_graphify_json):
        G = load_graph(real_graphify_json)
        node = G.nodes["src/auth/service.py::authenticate_user"]
        assert node["file_type"] == "function"

    def test_node_source_file_preserved(self, real_graphify_json):
        G = load_graph(real_graphify_json)
        node = G.nodes["src/auth/service.py::authenticate_user"]
        assert node["source_file"] == "src/auth/service.py"

    def test_node_importance_preserved(self, real_graphify_json):
        G = load_graph(real_graphify_json)
        node = G.nodes["src/auth/service.py::authenticate_user"]
        assert node["importance"] == 9

    def test_edge_relation_preserved(self, real_graphify_json):
        G = load_graph(real_graphify_json)
        src = "src/middleware/jwt.py::JWTMiddleware"
        tgt = "src/auth/service.py::authenticate_user"
        assert G.edges[src, tgt]["relation"] == "calls"

    def test_edge_weight_preserved(self, real_graphify_json):
        G = load_graph(real_graphify_json)
        src = "src/middleware/jwt.py::JWTMiddleware"
        tgt = "src/auth/service.py::authenticate_user"
        assert G.edges[src, tgt]["weight"] == 3

    def test_edge_confidence_preserved(self, real_graphify_json):
        G = load_graph(real_graphify_json)
        src = "src/middleware/jwt.py::JWTMiddleware"
        tgt = "src/auth/service.py::authenticate_user"
        assert G.edges[src, tgt]["confidence"] == 0.95

    def test_edge_direction_preserved(self, real_graphify_json):
        G = load_graph(real_graphify_json)
        src = "src/middleware/jwt.py::JWTMiddleware"
        tgt = "src/auth/service.py::authenticate_user"
        assert G.has_edge(src, tgt)
        assert not G.has_edge(tgt, src)

    def test_top_level_metadata_not_added_as_node(self, real_graphify_json):
        G = load_graph(real_graphify_json)
        # 'directed', 'multigraph', 'built_at_commit' must not become nodes
        for key in ("directed", "multigraph", "built_at_commit", "graph"):
            assert key not in G.nodes

    def test_links_key_empty_means_no_edges(self, tmp_path):
        data = {
            "directed": True,
            "multigraph": False,
            "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            "links": [],
        }
        graph_file = tmp_path / "empty_links.json"
        graph_file.write_text(json.dumps(data))
        G = load_graph(graph_file)
        assert G.number_of_edges() == 0

    def test_links_preferred_over_edges_when_both_present(self, tmp_path):
        # If a file has both 'links' and 'edges', 'links' wins
        data = {
            "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}, {"id": "c", "label": "C"}],
            "links": [{"source": "a", "target": "b"}],
            "edges": [{"source": "a", "target": "c"}],
        }
        graph_file = tmp_path / "both.json"
        graph_file.write_text(json.dumps(data))
        G = load_graph(graph_file)
        assert G.has_edge("a", "b")
        assert not G.has_edge("a", "c")

    def test_integration_with_scorer(self, real_graphify_json):
        from slurp.scorer import score_nodes
        G = load_graph(real_graphify_json)
        scores = score_nodes(G, "authentication jwt")
        assert len(scores) == G.number_of_nodes()
        assert all(0.0 <= s <= 1.0 for s in scores.values())

    def test_integration_with_budget(self, real_graphify_json):
        from slurp.budget import select_subgraph
        from slurp.scorer import score_nodes
        G = load_graph(real_graphify_json)
        scores = score_nodes(G, "authentication")
        subG, stats = select_subgraph(G, scores, budget=10_000)
        assert stats["nodes_total"] == 4
        assert stats["tokens_used"] <= 10_000
