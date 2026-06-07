"""Tests for slurp/loader.py."""

import json
from pathlib import Path

import networkx as nx
import pytest

from slurp.loader import (
    SlurpLoadError,
    _normalize_graphml_attrs,
    convert_graph,
    detect_format,
    load_graph,
    load_graph_neo4j,
)


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


# ---------------------------------------------------------------------------
# Helpers / fixtures for new format tests
# ---------------------------------------------------------------------------

def _write_nodes_csv(path: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    import csv as _csv
    if not rows:
        return
    fields = fieldnames or list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = _csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_rels_csv(path: Path, rows: list[dict]) -> None:
    import csv as _csv
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = _csv.DictWriter(f, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# _normalize_graphml_attrs
# ---------------------------------------------------------------------------

class TestNormalizeGraphmlAttrs:
    def test_existing_label_is_kept(self):
        attrs = {"label": "MyFunc", "type": "function"}
        result = _normalize_graphml_attrs(attrs, "node_1")
        assert result["label"] == "MyFunc"

    def test_name_becomes_label_when_no_label(self):
        attrs = {"name": "MyFunc"}
        result = _normalize_graphml_attrs(attrs, "node_1")
        assert result["label"] == "MyFunc"
        assert "name" not in result

    def test_node_id_fallback_when_no_label_or_name(self):
        result = _normalize_graphml_attrs({}, "node_42")
        assert result["label"] == "node_42"

    def test_kind_becomes_type_when_no_type(self):
        attrs = {"label": "X", "kind": "class"}
        result = _normalize_graphml_attrs(attrs, "n")
        assert result["type"] == "class"
        assert "kind" not in result

    def test_existing_type_is_kept_over_kind(self):
        attrs = {"label": "X", "type": "function", "kind": "class"}
        result = _normalize_graphml_attrs(attrs, "n")
        assert result["type"] == "function"

    def test_desc_becomes_description(self):
        attrs = {"label": "X", "desc": "Does stuff"}
        result = _normalize_graphml_attrs(attrs, "n")
        assert result["description"] == "Does stuff"
        assert "desc" not in result

    def test_unknown_attributes_are_preserved(self):
        attrs = {"label": "X", "importance": 9, "file_path": "src/x.py"}
        result = _normalize_graphml_attrs(attrs, "n")
        assert result["importance"] == 9
        assert result["file_path"] == "src/x.py"


# ---------------------------------------------------------------------------
# GraphML loading
# ---------------------------------------------------------------------------

@pytest.fixture
def graphml_file(tmp_path, sample_graph) -> Path:
    """Write the sample_graph as a GraphML file and return its path."""
    path = tmp_path / "test.graphml"
    # Sanitize None values before writing
    G_clean = nx.DiGraph()
    for nid in sample_graph.nodes:
        attrs = {k: v for k, v in sample_graph.nodes[nid].items() if v is not None}
        G_clean.add_node(nid, **attrs)
    for u, v in sample_graph.edges:
        attrs = {k: v for k, v in sample_graph.edges[u, v].items() if v is not None}
        G_clean.add_edge(u, v, **attrs)
    nx.write_graphml(G_clean, str(path))
    return path


class TestLoadGraphML:
    def test_loads_correct_node_count(self, graphml_file, sample_graph):
        G = load_graph(graphml_file)
        assert G.number_of_nodes() == sample_graph.number_of_nodes()

    def test_loads_correct_edge_count(self, graphml_file, sample_graph):
        G = load_graph(graphml_file)
        assert G.number_of_edges() == sample_graph.number_of_edges()

    def test_is_digraph(self, graphml_file):
        G = load_graph(graphml_file)
        assert isinstance(G, nx.DiGraph)

    def test_all_nodes_have_label(self, graphml_file):
        G = load_graph(graphml_file)
        for nid in G.nodes:
            assert "label" in G.nodes[nid], f"Node {nid!r} missing label"

    def test_known_node_labels_present(self, graphml_file):
        G = load_graph(graphml_file)
        labels = {G.nodes[nid]["label"] for nid in G.nodes}
        assert "authenticate_user" in labels
        assert "JWTMiddleware" in labels

    def test_node_type_preserved(self, graphml_file):
        G = load_graph(graphml_file)
        types = {G.nodes[nid].get("type") for nid in G.nodes}
        assert "function" in types
        assert "class" in types

    def test_name_attribute_normalized_to_label(self, tmp_path):
        G_in = nx.DiGraph()
        G_in.add_node("n1", name="MyNode", kind="class")
        G_in.add_node("n2", name="OtherNode")
        nx.write_graphml(G_in, str(tmp_path / "t.graphml"))
        G = load_graph(tmp_path / "t.graphml")
        assert G.nodes["n1"]["label"] == "MyNode"
        assert G.nodes["n1"]["type"] == "class"

    def test_node_without_name_gets_id_as_label(self, tmp_path):
        G_in = nx.DiGraph()
        G_in.add_node("mynode123")
        nx.write_graphml(G_in, str(tmp_path / "t.graphml"))
        G = load_graph(tmp_path / "t.graphml")
        assert G.nodes["mynode123"]["label"] == "mynode123"

    def test_desc_attribute_normalized_to_description(self, tmp_path):
        G_in = nx.DiGraph()
        G_in.add_node("n1", label="X", desc="Short description")
        nx.write_graphml(G_in, str(tmp_path / "t.graphml"))
        G = load_graph(tmp_path / "t.graphml")
        assert G.nodes["n1"]["description"] == "Short description"
        assert "desc" not in G.nodes["n1"]

    def test_edge_label_becomes_relation(self, tmp_path):
        G_in = nx.DiGraph()
        G_in.add_node("a", label="A")
        G_in.add_node("b", label="B")
        G_in.add_edge("a", "b", label="calls")
        nx.write_graphml(G_in, str(tmp_path / "t.graphml"))
        G = load_graph(tmp_path / "t.graphml")
        assert G.edges["a", "b"].get("relation") == "calls"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(SlurpLoadError, match="not found"):
            load_graph(tmp_path / "missing.graphml")

    def test_invalid_graphml_raises(self, tmp_path):
        bad = tmp_path / "bad.graphml"
        bad.write_text("this is not xml", encoding="utf-8")
        with pytest.raises(SlurpLoadError, match="Cannot read GraphML"):
            load_graph(bad)

    def test_extra_attributes_preserved(self, tmp_path):
        G_in = nx.DiGraph()
        G_in.add_node("n1", label="X", importance=9, custom_attr="hello")
        nx.write_graphml(G_in, str(tmp_path / "t.graphml"))
        G = load_graph(tmp_path / "t.graphml")
        # importance may be loaded as string by GraphML — check it exists
        assert "custom_attr" in G.nodes["n1"]


# ---------------------------------------------------------------------------
# Neo4j CSV — load_graph_neo4j
# ---------------------------------------------------------------------------

class TestLoadGraphNeo4j:
    def test_basic_load_produces_correct_node_count(self, tmp_path):
        rows = [
            {"nodeId": "a", "name": "Alpha", "type": "function"},
            {"nodeId": "b", "name": "Beta",  "type": "class"},
        ]
        _write_nodes_csv(tmp_path / "nodes.csv", rows)
        G = load_graph_neo4j(tmp_path / "nodes.csv")
        assert G.number_of_nodes() == 2

    def test_node_id_preserved(self, tmp_path):
        rows = [{"nodeId": "x123", "name": "X"}]
        _write_nodes_csv(tmp_path / "nodes.csv", rows)
        G = load_graph_neo4j(tmp_path / "nodes.csv")
        assert "x123" in G.nodes

    def test_name_becomes_label(self, tmp_path):
        rows = [{"nodeId": "a", "name": "MyFunc", "type": "function"}]
        _write_nodes_csv(tmp_path / "nodes.csv", rows)
        G = load_graph_neo4j(tmp_path / "nodes.csv")
        assert G.nodes["a"]["label"] == "MyFunc"

    def test_type_column_preserved(self, tmp_path):
        rows = [{"nodeId": "a", "name": "X", "type": "class"}]
        _write_nodes_csv(tmp_path / "nodes.csv", rows)
        G = load_graph_neo4j(tmp_path / "nodes.csv")
        assert G.nodes["a"]["type"] == "class"

    def test_labels_column_used_as_type_fallback(self, tmp_path):
        rows = [{"nodeId": "a", "name": "X", "labels": "Person"}]
        _write_nodes_csv(tmp_path / "nodes.csv", rows)
        G = load_graph_neo4j(tmp_path / "nodes.csv")
        assert G.nodes["a"]["type"] == "Person"

    def test_description_column_loaded(self, tmp_path):
        rows = [{"nodeId": "a", "name": "X", "description": "Does auth"}]
        _write_nodes_csv(tmp_path / "nodes.csv", rows)
        G = load_graph_neo4j(tmp_path / "nodes.csv")
        assert G.nodes["a"]["description"] == "Does auth"

    def test_neo4j_native_id_column(self, tmp_path):
        rows = [
            {":ID": "n1", "name": "NodeOne"},
            {":ID": "n2", "name": "NodeTwo"},
        ]
        _write_nodes_csv(tmp_path / "nodes.csv", rows, fieldnames=[":ID", "name"])
        G = load_graph_neo4j(tmp_path / "nodes.csv")
        assert "n1" in G.nodes
        assert "n2" in G.nodes

    def test_relationships_loaded(self, tmp_path):
        _write_nodes_csv(tmp_path / "nodes.csv", [
            {"nodeId": "a", "name": "A"},
            {"nodeId": "b", "name": "B"},
        ])
        _write_rels_csv(tmp_path / "rels.csv", [
            {"startNodeId": "a", "endNodeId": "b", "type": "CALLS"},
        ])
        G = load_graph_neo4j(tmp_path / "nodes.csv", tmp_path / "rels.csv")
        assert G.has_edge("a", "b")

    def test_relationship_type_stored_as_relation(self, tmp_path):
        _write_nodes_csv(tmp_path / "nodes.csv", [
            {"nodeId": "a", "name": "A"},
            {"nodeId": "b", "name": "B"},
        ])
        _write_rels_csv(tmp_path / "rels.csv", [
            {"startNodeId": "a", "endNodeId": "b", "type": "calls"},
        ])
        G = load_graph_neo4j(tmp_path / "nodes.csv", tmp_path / "rels.csv")
        assert G.edges["a", "b"]["relation"] == "calls"

    def test_neo4j_native_relationship_columns(self, tmp_path):
        _write_nodes_csv(tmp_path / "nodes.csv", [
            {":ID": "x", "name": "X"},
            {":ID": "y", "name": "Y"},
        ], fieldnames=[":ID", "name"])
        _write_rels_csv(tmp_path / "rels.csv", [
            {":START_ID": "x", ":END_ID": "y", ":TYPE": "IMPORTS"},
        ])
        G = load_graph_neo4j(tmp_path / "nodes.csv", tmp_path / "rels.csv")
        assert G.has_edge("x", "y")
        assert G.edges["x", "y"]["relation"] == "IMPORTS"

    def test_edges_to_unknown_nodes_skipped(self, tmp_path):
        _write_nodes_csv(tmp_path / "nodes.csv", [
            {"nodeId": "a", "name": "A"},
        ])
        _write_rels_csv(tmp_path / "rels.csv", [
            {"startNodeId": "a", "endNodeId": "ghost", "type": "calls"},
        ])
        G = load_graph_neo4j(tmp_path / "nodes.csv", tmp_path / "rels.csv")
        assert G.number_of_edges() == 0

    def test_multiple_edges(self, tmp_path):
        _write_nodes_csv(tmp_path / "nodes.csv", [
            {"nodeId": "a", "name": "A"},
            {"nodeId": "b", "name": "B"},
            {"nodeId": "c", "name": "C"},
        ])
        _write_rels_csv(tmp_path / "rels.csv", [
            {"startNodeId": "a", "endNodeId": "b", "type": "calls"},
            {"startNodeId": "b", "endNodeId": "c", "type": "depends_on"},
        ])
        G = load_graph_neo4j(tmp_path / "nodes.csv", tmp_path / "rels.csv")
        assert G.number_of_edges() == 2

    def test_missing_nodes_file_raises(self, tmp_path):
        with pytest.raises(SlurpLoadError, match="not found"):
            load_graph_neo4j(tmp_path / "missing.csv")

    def test_missing_relationships_file_raises(self, tmp_path):
        _write_nodes_csv(tmp_path / "nodes.csv", [{"nodeId": "a", "name": "A"}])
        with pytest.raises(SlurpLoadError, match="not found"):
            load_graph_neo4j(tmp_path / "nodes.csv", tmp_path / "missing_rels.csv")

    def test_no_id_column_raises(self, tmp_path):
        _write_nodes_csv(tmp_path / "nodes.csv", [{"name": "X", "type": "fn"}])
        with pytest.raises(SlurpLoadError, match="no ID column"):
            load_graph_neo4j(tmp_path / "nodes.csv")

    def test_empty_csv_raises(self, tmp_path):
        (tmp_path / "nodes.csv").write_text("nodeId,name\n", encoding="utf-8")
        with pytest.raises(SlurpLoadError, match="no data rows"):
            load_graph_neo4j(tmp_path / "nodes.csv")

    def test_node_id_fallback_used_as_label(self, tmp_path):
        _write_nodes_csv(tmp_path / "nodes.csv", [{"nodeId": "func_auth"}])
        G = load_graph_neo4j(tmp_path / "nodes.csv")
        assert G.nodes["func_auth"]["label"] == "func_auth"


# ---------------------------------------------------------------------------
# Auto-detection by extension
# ---------------------------------------------------------------------------

class TestAutoDetectExtension:
    def test_json_extension_routes_to_json_loader(self, sample_graph_json):
        G = load_graph(sample_graph_json)
        assert G.number_of_nodes() > 0

    def test_graphml_extension_routes_to_graphml_loader(self, tmp_path, sample_graph):
        path = tmp_path / "g.graphml"
        G_clean = nx.DiGraph()
        for nid in sample_graph.nodes:
            attrs = {k: v for k, v in sample_graph.nodes[nid].items() if v is not None}
            G_clean.add_node(nid, **attrs)
        for u, v in sample_graph.edges:
            attrs = {k: v for k, v in sample_graph.edges[u, v].items() if v is not None}
            G_clean.add_edge(u, v, **attrs)
        nx.write_graphml(G_clean, str(path))
        G = load_graph(path)
        assert G.number_of_nodes() == sample_graph.number_of_nodes()

    def test_csv_extension_routes_to_neo4j_loader(self, tmp_path):
        _write_nodes_csv(tmp_path / "g.csv", [
            {"nodeId": "a", "name": "A"},
            {"nodeId": "b", "name": "B"},
        ])
        G = load_graph(tmp_path / "g.csv")
        assert G.number_of_nodes() == 2

    def test_csv_auto_finds_relationships_file(self, tmp_path):
        _write_nodes_csv(tmp_path / "nodes.csv", [
            {"nodeId": "a", "name": "A"},
            {"nodeId": "b", "name": "B"},
        ])
        _write_rels_csv(tmp_path / "relationships.csv", [
            {"startNodeId": "a", "endNodeId": "b", "type": "calls"},
        ])
        G = load_graph(tmp_path / "nodes.csv")
        assert G.has_edge("a", "b")

    def test_csv_works_without_relationships_file(self, tmp_path):
        _write_nodes_csv(tmp_path / "g.csv", [{"nodeId": "a", "name": "A"}])
        G = load_graph(tmp_path / "g.csv")
        assert G.number_of_nodes() == 1
        assert G.number_of_edges() == 0

    def test_unknown_extension_falls_through_to_json(self, tmp_path):
        data = {"nodes": [{"id": "a", "label": "A"}], "edges": []}
        path = tmp_path / "graph.json"
        path.write_text(json.dumps(data))
        G = load_graph(path)
        assert "a" in G.nodes


# ---------------------------------------------------------------------------
# convert_graph
# ---------------------------------------------------------------------------

class TestConvertGraphGraphML:
    def test_creates_graphml_file(self, tmp_path, sample_graph):
        out = tmp_path / "out.graphml"
        paths = convert_graph(sample_graph, out, format="graphml")
        assert len(paths) == 1
        assert paths[0].exists()

    def test_graphml_round_trip_preserves_node_count(self, tmp_path, sample_graph):
        out = tmp_path / "out.graphml"
        convert_graph(sample_graph, out, format="graphml")
        G2 = load_graph(out)
        assert G2.number_of_nodes() == sample_graph.number_of_nodes()

    def test_graphml_round_trip_preserves_edges(self, tmp_path, sample_graph):
        out = tmp_path / "out.graphml"
        convert_graph(sample_graph, out, format="graphml")
        G2 = load_graph(out)
        assert G2.number_of_edges() == sample_graph.number_of_edges()

    def test_graphml_round_trip_preserves_labels(self, tmp_path, sample_graph):
        out = tmp_path / "out.graphml"
        convert_graph(sample_graph, out, format="graphml")
        G2 = load_graph(out)
        orig_labels = {sample_graph.nodes[n]["label"] for n in sample_graph.nodes}
        new_labels  = {G2.nodes[n]["label"] for n in G2.nodes}
        assert orig_labels == new_labels

    def test_none_values_are_dropped(self, tmp_path):
        G = nx.DiGraph()
        G.add_node("a", label="A", description=None, type="function")
        out = tmp_path / "out.graphml"
        convert_graph(G, out, format="graphml")
        G2 = load_graph(out)
        assert "a" in G2.nodes
        assert G2.nodes["a"]["label"] == "A"

    def test_unknown_format_raises(self, tmp_path, sample_graph):
        with pytest.raises(ValueError, match="Unknown export format"):
            convert_graph(sample_graph, tmp_path / "x", format="rdf")


class TestConvertGraphNeo4j:
    def test_creates_two_csv_files(self, tmp_path, sample_graph):
        paths = convert_graph(sample_graph, tmp_path / "export", format="neo4j")
        assert len(paths) == 2
        assert all(p.exists() for p in paths)

    def test_nodes_file_has_correct_row_count(self, tmp_path, sample_graph):
        import csv as _csv
        convert_graph(sample_graph, tmp_path / "export", format="neo4j")
        with (tmp_path / "export_nodes.csv").open() as f:
            rows = list(_csv.DictReader(f))
        assert len(rows) == sample_graph.number_of_nodes()

    def test_relationships_file_has_correct_row_count(self, tmp_path, sample_graph):
        import csv as _csv
        convert_graph(sample_graph, tmp_path / "export", format="neo4j")
        with (tmp_path / "export_relationships.csv").open() as f:
            rows = list(_csv.DictReader(f))
        assert len(rows) == sample_graph.number_of_edges()

    def test_neo4j_round_trip_preserves_node_count(self, tmp_path, sample_graph):
        paths = convert_graph(sample_graph, tmp_path / "export", format="neo4j")
        nodes_path = next(p for p in paths if "nodes" in p.name)
        rels_path  = next(p for p in paths if "relationship" in p.name)
        G2 = load_graph_neo4j(nodes_path, rels_path)
        assert G2.number_of_nodes() == sample_graph.number_of_nodes()

    def test_neo4j_round_trip_preserves_edges(self, tmp_path, sample_graph):
        paths = convert_graph(sample_graph, tmp_path / "export", format="neo4j")
        nodes_path = next(p for p in paths if "nodes" in p.name)
        rels_path  = next(p for p in paths if "relationship" in p.name)
        G2 = load_graph_neo4j(nodes_path, rels_path)
        assert G2.number_of_edges() == sample_graph.number_of_edges()

    def test_neo4j_round_trip_preserves_labels(self, tmp_path, sample_graph):
        paths = convert_graph(sample_graph, tmp_path / "export", format="neo4j")
        nodes_path = next(p for p in paths if "nodes" in p.name)
        G2 = load_graph_neo4j(nodes_path)
        orig_labels = {sample_graph.nodes[n]["label"] for n in sample_graph.nodes}
        new_labels  = {G2.nodes[n]["label"] for n in G2.nodes}
        assert orig_labels == new_labels


# ---------------------------------------------------------------------------
# convert_graph → load_graph round-trip (JSON → GraphML → load)
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_json_to_graphml_to_load(self, tmp_path, sample_graph_json):
        G1 = load_graph(sample_graph_json)
        out = tmp_path / "converted.graphml"
        convert_graph(G1, out, format="graphml")
        G2 = load_graph(out)
        assert G2.number_of_nodes() == G1.number_of_nodes()
        assert G2.number_of_edges() == G1.number_of_edges()

    def test_json_to_neo4j_to_load(self, tmp_path, sample_graph_json):
        G1 = load_graph(sample_graph_json)
        paths = convert_graph(G1, tmp_path / "export", format="neo4j")
        nodes_path = next(p for p in paths if "nodes" in p.name)
        rels_path  = next(p for p in paths if "relationship" in p.name)
        G2 = load_graph_neo4j(nodes_path, rels_path)
        assert G2.number_of_nodes() == G1.number_of_nodes()
        assert G2.number_of_edges() == G1.number_of_edges()
