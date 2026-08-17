"""Tests for slurp.federation — querying several project graphs as one."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from slurp.cli import cli
from slurp.federation import (
    PROJECT_ATTR,
    PROJECT_SEPARATOR,
    FederationStats,
    derive_labels,
    load_and_merge,
    load_graphs,
    merge_graphs,
    merge_graphs_with_stats,
)
from slurp.loader import SlurpLoadError, build_graph, load_graph


def _graph(nodes: list[tuple[str, str]], edges: list[tuple[str, str]] | None = None) -> dict:
    """Build a graph.json dict from (id, label) pairs and (source, target) edges."""
    return {
        "nodes": [
            {"id": nid, "label": label, "type": "function",
             "description": f"{label} does things", "source_file": f"{nid}.py"}
            for nid, label in nodes
        ],
        "links": [
            {"source": s, "target": t, "relation": "calls"}
            for s, t in (edges or [])
        ],
        "built_at_commit": None,
    }


@pytest.fixture
def auth_graph() -> dict:
    return _graph(
        [("login", "login"), ("verify_token", "verify_token")],
        [("login", "verify_token")],
    )


@pytest.fixture
def api_graph() -> dict:
    return _graph(
        [("handler", "handler"), ("router", "router")],
        [("router", "handler")],
    )


def _ids(graph: dict) -> set[str]:
    return {n["id"] for n in graph["nodes"]}


class TestMergeWithoutCollisions:
    def test_all_nodes_present(self, auth_graph, api_graph):
        merged = merge_graphs([auth_graph, api_graph], ["auth", "api"])
        assert len(merged["nodes"]) == 4

    def test_all_edges_present(self, auth_graph, api_graph):
        merged = merge_graphs([auth_graph, api_graph], ["auth", "api"])
        assert len(merged["links"]) == 2

    def test_output_is_standard_graph_json(self, auth_graph, api_graph):
        merged = merge_graphs([auth_graph, api_graph], ["auth", "api"])
        assert set(merged) >= {"nodes", "links"}
        assert all("id" in n and "label" in n for n in merged["nodes"])

    def test_node_attributes_are_preserved(self, auth_graph, api_graph):
        merged = merge_graphs([auth_graph, api_graph], ["auth", "api"])
        node = next(n for n in merged["nodes"] if n["id"].endswith("login"))
        assert node["type"] == "function"
        assert node["description"] == "login does things"

    def test_empty_input(self):
        merged, stats = merge_graphs_with_stats([])
        assert merged["nodes"] == []
        assert stats == FederationStats()

    def test_accepts_edges_key(self):
        a = {"nodes": [{"id": "x", "label": "x"}], "edges": [{"source": "x", "target": "x"}]}
        b = {"nodes": [{"id": "y", "label": "y"}], "edges": []}
        merged = merge_graphs([a, b], ["a", "b"])
        assert merged["links"][0] == {"source": "a::x", "target": "a::x"}


class TestIdPrefixing:
    def test_ids_are_namespaced(self, auth_graph, api_graph):
        merged = merge_graphs([auth_graph, api_graph], ["auth", "api"])
        assert _ids(merged) == {
            "auth::login", "auth::verify_token", "api::handler", "api::router",
        }

    def test_separator_constant(self):
        assert PROJECT_SEPARATOR == "::"

    def test_project_attribute_added(self, auth_graph, api_graph):
        merged = merge_graphs([auth_graph, api_graph], ["auth", "api"])
        by_id = {n["id"]: n for n in merged["nodes"]}
        assert by_id["auth::login"][PROJECT_ATTR] == "auth"
        assert by_id["api::handler"][PROJECT_ATTR] == "api"

    def test_labels_default_to_positional_names(self, auth_graph, api_graph):
        merged = merge_graphs([auth_graph, api_graph])
        assert "graph1::login" in _ids(merged)
        assert "graph2::handler" in _ids(merged)

    def test_nodes_without_id_are_skipped(self):
        a = {"nodes": [{"label": "orphan"}, {"id": "ok", "label": "ok"}], "links": []}
        merged = merge_graphs([a, _graph([("z", "z")])], ["a", "b"])
        assert _ids(merged) == {"a::ok", "b::z"}


class TestEdgeRewriting:
    def test_endpoints_get_the_same_prefix(self, auth_graph, api_graph):
        merged = merge_graphs([auth_graph, api_graph], ["auth", "api"])
        pairs = {(e["source"], e["target"]) for e in merged["links"]}
        assert pairs == {
            ("auth::login", "auth::verify_token"),
            ("api::router", "api::handler"),
        }

    def test_edge_attributes_preserved(self, auth_graph, api_graph):
        merged = merge_graphs([auth_graph, api_graph], ["auth", "api"])
        assert all(e["relation"] == "calls" for e in merged["links"])

    def test_no_dangling_edges(self, api_graph):
        broken = _graph([("a", "a")], [("a", "does_not_exist")])
        merged = merge_graphs([broken, api_graph], ["x", "api"])
        ids = _ids(merged)
        for e in merged["links"]:
            assert e["source"] in ids and e["target"] in ids

    def test_edges_never_cross_projects_implicitly(self, auth_graph):
        """Two projects defining the same id must not become connected."""
        other = _graph([("login", "login"), ("extra", "extra")], [("login", "extra")])
        merged = merge_graphs([auth_graph, other], ["auth", "other"])
        crossing = [
            e for e in merged["links"]
            if e["source"].split(PROJECT_SEPARATOR)[0]
            != e["target"].split(PROJECT_SEPARATOR)[0]
        ]
        assert crossing == []


class TestCollisions:
    def test_shared_id_is_counted(self, auth_graph):
        other = _graph([("login", "login"), ("other", "other")])
        _, stats = merge_graphs_with_stats([auth_graph, other], ["auth", "other"])
        assert stats.collision_count == 1

    def test_colliding_nodes_both_survive(self, auth_graph):
        other = _graph([("login", "login")])
        merged = merge_graphs([auth_graph, other], ["auth", "other"])
        assert "auth::login" in _ids(merged)
        assert "other::login" in _ids(merged)

    def test_no_collisions_counts_zero(self, auth_graph, api_graph):
        _, stats = merge_graphs_with_stats([auth_graph, api_graph], ["auth", "api"])
        assert stats.collision_count == 0

    def test_duplicate_within_one_graph_is_not_a_collision(self):
        dupes = {"nodes": [{"id": "x", "label": "x"}, {"id": "x", "label": "x2"}],
                 "links": []}
        _, stats = merge_graphs_with_stats([dupes, _graph([("y", "y")])], ["a", "b"])
        assert stats.collision_count == 0
        assert stats.total_nodes == 2

    def test_id_in_three_graphs_counts_once(self):
        graphs = [_graph([("same", "same")]) for _ in range(3)]
        _, stats = merge_graphs_with_stats(graphs, ["a", "b", "c"])
        assert stats.collision_count == 1
        assert stats.total_nodes == 3


class TestCustomLabels:
    def test_labels_are_used_as_prefixes(self, auth_graph, api_graph):
        merged = merge_graphs([auth_graph, api_graph], ["frontend", "backend"])
        assert "frontend::login" in _ids(merged)
        assert "backend::router" in _ids(merged)

    def test_wrong_label_count_raises(self, auth_graph, api_graph):
        with pytest.raises(ValueError, match="1 labels for 2 graphs"):
            merge_graphs([auth_graph, api_graph], ["only-one"])

    def test_duplicate_labels_raise(self, auth_graph, api_graph):
        with pytest.raises(ValueError, match="Duplicate graph labels"):
            merge_graphs([auth_graph, api_graph], ["same", "same"])

    def test_derive_labels_uses_stem(self):
        paths = [Path("a/auth.json"), Path("b/api.json")]
        assert derive_labels(paths) == ["auth", "api"]

    def test_derive_labels_disambiguates_identical_stems(self):
        """Every project keeps its graph at graphify-out/graph.json."""
        paths = [
            Path("auth-service/graphify-out/graph.json"),
            Path("api-gateway/graphify-out/graph.json"),
        ]
        assert derive_labels(paths) == ["auth-service", "api-gateway"]

    def test_derive_labels_always_distinct(self):
        paths = [Path("graph.json"), Path("graph.json"), Path("graph.json")]
        assert len(set(derive_labels(paths))) == 3


class TestSingleGraph:
    def test_ids_unchanged(self, auth_graph):
        merged = merge_graphs([auth_graph])
        assert _ids(merged) == {"login", "verify_token"}

    def test_no_project_attribute(self, auth_graph):
        merged = merge_graphs([auth_graph])
        assert all(PROJECT_ATTR not in n for n in merged["nodes"])

    def test_edges_unchanged(self, auth_graph):
        merged = merge_graphs([auth_graph])
        assert merged["links"] == [
            {"source": "login", "target": "verify_token", "relation": "calls"}
        ]

    def test_label_is_ignored_for_ids(self, auth_graph):
        merged = merge_graphs([auth_graph], ["whatever"])
        assert _ids(merged) == {"login", "verify_token"}

    def test_stats_still_reported(self, auth_graph):
        _, stats = merge_graphs_with_stats([auth_graph], ["solo"])
        assert stats.graphs_merged == 1
        assert stats.nodes_per_graph == {"solo": 2}
        assert stats.collision_count == 0


class TestLoaderCompatibility:
    def test_merged_graph_builds_a_digraph(self, auth_graph, api_graph):
        merged = merge_graphs([auth_graph, api_graph], ["auth", "api"])
        G = build_graph(merged)
        assert G.number_of_nodes() == 4
        assert G.number_of_edges() == 2

    def test_project_attr_survives_into_the_graph(self, auth_graph, api_graph):
        G = build_graph(merge_graphs([auth_graph, api_graph], ["auth", "api"]))
        assert G.nodes["auth::login"][PROJECT_ATTR] == "auth"

    def test_merged_graph_round_trips_through_load_graph(
        self, tmp_path, auth_graph, api_graph
    ):
        merged = merge_graphs([auth_graph, api_graph], ["auth", "api"])
        path = tmp_path / "merged.json"
        path.write_text(json.dumps(merged), encoding="utf-8")
        G = load_graph(path)
        assert G.number_of_nodes() == 4
        assert "api::router" in G.nodes

    def test_merged_graph_is_queryable(self, auth_graph, api_graph):
        from slurp.budget import select_subgraph
        from slurp.scorer import score_nodes

        G = build_graph(merge_graphs([auth_graph, api_graph], ["auth", "api"]))
        scores = score_nodes(G, "login")
        subG, stats = select_subgraph(G, scores, budget=4000)
        assert stats["nodes_total"] == 4
        assert subG.number_of_nodes() > 0


class TestFederationStats:
    def test_fields(self, auth_graph, api_graph):
        _, stats = merge_graphs_with_stats([auth_graph, api_graph], ["auth", "api"])
        assert stats.graphs_merged == 2
        assert stats.total_nodes == 4
        assert stats.total_edges == 2
        assert stats.nodes_per_graph == {"auth": 2, "api": 2}

    def test_nodes_per_graph_reflects_sizes(self):
        small = _graph([("a", "a")])
        big = _graph([("b", "b"), ("c", "c"), ("d", "d")])
        _, stats = merge_graphs_with_stats([small, big], ["small", "big"])
        assert stats.nodes_per_graph == {"small": 1, "big": 3}
        assert stats.total_nodes == 4

    def test_totals_match_the_merged_graph(self, auth_graph, api_graph):
        merged, stats = merge_graphs_with_stats([auth_graph, api_graph], ["auth", "api"])
        assert stats.total_nodes == len(merged["nodes"])
        assert stats.total_edges == len(merged["links"])


class TestLoadFromDisk:
    def _write(self, tmp_path: Path, name: str, graph: dict) -> Path:
        path = tmp_path / name
        path.write_text(json.dumps(graph), encoding="utf-8")
        return path

    def test_load_and_merge(self, tmp_path, auth_graph, api_graph):
        paths = [self._write(tmp_path, "auth.json", auth_graph),
                 self._write(tmp_path, "api.json", api_graph)]
        merged, stats = load_and_merge(paths)
        assert stats.graphs_merged == 2
        assert "auth::login" in _ids(merged)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(SlurpLoadError, match="not found"):
            load_graphs([tmp_path / "nope.json"])

    def test_invalid_json_raises(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with pytest.raises(SlurpLoadError, match="Could not read graph"):
            load_graphs([bad])

    def test_empty_graph_raises(self, tmp_path):
        empty = tmp_path / "empty.json"
        empty.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
        with pytest.raises(SlurpLoadError, match="no nodes"):
            load_graphs([empty])


class TestCliFederation:
    def _write(self, tmp_path: Path, name: str, graph: dict) -> Path:
        path = tmp_path / name
        path.write_text(json.dumps(graph), encoding="utf-8")
        return path

    def test_single_graph_output_unchanged(self, tmp_path, auth_graph):
        path = self._write(tmp_path, "auth.json", auth_graph)
        res = CliRunner().invoke(cli, ["login", "--graph", str(path), "--no-audit"])
        assert res.exit_code == 0, res.output
        assert "Federated:" not in res.output

    def test_multiple_graphs_show_federation_header(self, tmp_path, auth_graph, api_graph):
        a = self._write(tmp_path, "auth.json", auth_graph)
        b = self._write(tmp_path, "api.json", api_graph)
        res = CliRunner().invoke(
            cli, ["login", "--graph", str(a), "--graph", str(b), "--no-audit"]
        )
        assert res.exit_code == 0, res.output
        assert "Federated: 2 graphs · 4 nodes total" in res.output

    def test_graph_label_flag(self, tmp_path, auth_graph, api_graph):
        a = self._write(tmp_path, "auth.json", auth_graph)
        b = self._write(tmp_path, "api.json", api_graph)
        res = CliRunner().invoke(cli, [
            "login", "--graph", str(a), "--graph", str(b),
            "--graph-label", "frontend", "--graph-label", "backend",
            "--no-audit", "--explain",
        ])
        assert res.exit_code == 0, res.output
        assert "Federated: 2 graphs" in res.output

    def test_label_count_mismatch_errors(self, tmp_path, auth_graph, api_graph):
        a = self._write(tmp_path, "auth.json", auth_graph)
        b = self._write(tmp_path, "api.json", api_graph)
        res = CliRunner().invoke(cli, [
            "login", "--graph", str(a), "--graph", str(b),
            "--graph-label", "only-one", "--no-audit",
        ])
        assert res.exit_code != 0
        assert "one label per graph" in res.output

    def test_collisions_are_reported(self, tmp_path, auth_graph):
        a = self._write(tmp_path, "one.json", auth_graph)
        b = self._write(tmp_path, "two.json", auth_graph)
        res = CliRunner().invoke(
            cli, ["login", "--graph", str(a), "--graph", str(b), "--no-audit"]
        )
        assert "appeared in more than one graph" in res.output

    def test_missing_graph_errors_cleanly(self, tmp_path, auth_graph):
        a = self._write(tmp_path, "auth.json", auth_graph)
        res = CliRunner().invoke(cli, [
            "login", "--graph", str(a), "--graph", str(tmp_path / "gone.json"),
            "--no-audit",
        ])
        assert res.exit_code != 0
        assert "not found" in res.output

    def test_help_documents_both_flags(self):
        res = CliRunner().invoke(cli, ["run", "--help"])
        assert "--graph-label" in res.output
        assert "federated" in res.output


class TestMcpFederation:
    def _write(self, tmp_path: Path, name: str, graph: dict) -> Path:
        path = tmp_path / name
        path.write_text(json.dumps(graph), encoding="utf-8")
        return path

    def test_federated_state_merges(self, tmp_path, auth_graph, api_graph):
        from slurp.mcp import FederatedGraphState

        paths = [self._write(tmp_path, "auth.json", auth_graph),
                 self._write(tmp_path, "api.json", api_graph)]
        state = FederatedGraphState(paths)
        assert state.graphs_count == 2
        assert state.graph.number_of_nodes() == 4

    def test_single_graph_state_reports_one(self, tmp_path, auth_graph):
        from slurp.mcp import GraphState

        path = self._write(tmp_path, "auth.json", auth_graph)
        assert GraphState(path, load_graph(path)).graphs_count == 1

    def test_reload_on_any_member_change(self, tmp_path, auth_graph, api_graph):
        import os

        from slurp.mcp import FederatedGraphState

        paths = [self._write(tmp_path, "auth.json", auth_graph),
                 self._write(tmp_path, "api.json", api_graph)]
        state = FederatedGraphState(paths)
        assert state.check_reload() is False

        bigger = _graph([("handler", "h"), ("router", "r"), ("extra", "e")])
        paths[1].write_text(json.dumps(bigger), encoding="utf-8")
        os.utime(paths[1], (state.mtimes[1] + 10, state.mtimes[1] + 10))

        assert state.check_reload() is True
        assert state.graph.number_of_nodes() == 5

    def test_query_response_carries_graphs_count(self, tmp_path, auth_graph, api_graph):
        from io import StringIO

        from slurp.mcp import FederatedGraphState, _handle

        paths = [self._write(tmp_path, "auth.json", auth_graph),
                 self._write(tmp_path, "api.json", api_graph)]
        state = FederatedGraphState(paths)
        out = StringIO()
        _handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "slurp_query",
                        "arguments": {"query": "login", "budget": 2000}}},
            None, out, session_log=False, state=state,
        )
        assert json.loads(out.getvalue())["result"]["graphs_count"] == 2

    def test_single_graph_response_has_no_graphs_count(self, tmp_path, auth_graph):
        from io import StringIO

        from slurp.mcp import GraphState, _handle

        path = self._write(tmp_path, "auth.json", auth_graph)
        state = GraphState(path, load_graph(path))
        out = StringIO()
        _handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "slurp_query",
                        "arguments": {"query": "login", "budget": 2000}}},
            None, out, session_log=False, state=state,
        )
        assert "graphs_count" not in json.loads(out.getvalue())["result"]


class TestVizFederation:
    def _fed_html(self, auth_graph: dict, api_graph: dict) -> str:
        from slurp.budget import select_subgraph
        from slurp.scorer import score_nodes
        from slurp.viz import build_html

        G = build_graph(merge_graphs([auth_graph, api_graph], ["auth", "api"]))
        scores = score_nodes(G, "login")
        subG, stats = select_subgraph(G, scores, budget=8000, min_score=0.0)
        return build_html(subG, stats, scores, "login")

    def test_projects_are_listed(self, auth_graph, api_graph):
        html = self._fed_html(auth_graph, api_graph)
        assert 'var PROJECTS = ["api", "auth"]' in html

    def test_project_field_rendered_in_panel(self, auth_graph, api_graph):
        html = self._fed_html(auth_graph, api_graph)
        assert 'fld("Project"' in html

    def test_projects_section_only_when_several(self, auth_graph):
        from slurp.budget import select_subgraph
        from slurp.scorer import score_nodes
        from slurp.viz import build_html

        G = build_graph(merge_graphs([auth_graph]))
        scores = score_nodes(G, "login")
        subG, stats = select_subgraph(G, scores, budget=4000, min_score=0.0)
        html = build_html(subG, stats, scores, "login")
        assert "var PROJECTS = []" in html

    def test_nodes_carry_project_value(self, auth_graph, api_graph):
        html = self._fed_html(auth_graph, api_graph)
        assert '"project": "auth"' in html
