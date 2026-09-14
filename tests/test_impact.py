"""Tests for slurp/impact.py."""

import json

import networkx as nx
from click.testing import CliRunner

from slurp.cli import cli
from slurp.impact import (
    ImpactResult,
    affected_subgraph,
    analyze_impact,
    format_impact,
    impact_to_dict,
    nodes_in_file,
)

TARGET = "lib/core.py"


def _add(G, nid, label, source, ntype="function"):
    G.add_node(nid, label=label, type=ntype, source_file=source, description="")


def _graph(dependents: int = 3, *, chain: bool = False) -> nx.DiGraph:
    """A file with two definitions, one of them depended on."""
    G = nx.DiGraph()
    _add(G, "file", "core.py", TARGET, ntype="module")
    _add(G, "hot", "createClient", TARGET)
    _add(G, "cold", "formatDate", TARGET)
    G.add_edge("file", "hot", relation="contains")
    G.add_edge("file", "cold", relation="contains")
    for i in range(dependents):
        _add(G, f"dep{i}", f"consumer{i}", f"app/page{i}.tsx")
        G.add_edge(f"dep{i}", "hot", relation="calls")
    if chain:
        _add(G, "far", "downstream", "app/far.tsx")
        G.add_edge("far", "dep0", relation="calls")
        _add(G, "further", "furtherStill", "app/further.tsx")
        G.add_edge("further", "far", relation="calls")
    return G


class TestNodesInFile:
    def test_finds_every_node_defined_in_the_file(self):
        found = nodes_in_file(_graph(), TARGET)
        assert set(found) == {"file", "hot", "cold"}

    def test_matches_on_a_path_suffix(self):
        """The caller should not have to know how the graph spelled paths."""
        assert set(nodes_in_file(_graph(), "core.py")) == {"file", "hot", "cold"}

    def test_exact_match_wins_over_suffix(self):
        G = _graph()
        _add(G, "other", "helper", "vendor/lib/core.py")
        assert "other" not in nodes_in_file(G, TARGET)

    def test_unknown_file_returns_nothing(self):
        assert nodes_in_file(_graph(), "does/not/exist.py") == []

    def test_empty_path_returns_nothing(self):
        assert nodes_in_file(_graph(), "") == []


class TestAnalyzeImpact:
    def test_collects_the_nodes_in_the_file(self):
        result = analyze_impact(_graph(), TARGET)
        assert set(result.nodes_in_file) == {"file", "hot", "cold"}

    def test_direct_dependents_are_correct(self):
        result = analyze_impact(_graph(dependents=3), TARGET)
        assert set(result.direct_dependents) == {"dep0", "dep1", "dep2"}

    def test_dependents_inside_the_file_are_not_blast_radius(self):
        """A caller in the same file is not something the edit could surprise."""
        G = _graph(dependents=0)
        G.add_edge("cold", "hot", relation="calls")
        result = analyze_impact(G, TARGET)
        assert result.direct_dependents == []

    def test_transitive_dependents_respect_the_hop_limit(self):
        G = _graph(dependents=1, chain=True)
        assert analyze_impact(G, TARGET, hops=1).transitive_dependents == []
        assert analyze_impact(G, TARGET, hops=2).transitive_dependents == ["far"]
        assert set(analyze_impact(G, TARGET, hops=3).transitive_dependents) == {
            "far", "further"}

    def test_highest_risk_node_is_the_most_depended_on(self):
        result = analyze_impact(_graph(dependents=4), TARGET)
        assert result.highest_risk_node == "hot"

    def test_highest_risk_node_is_never_the_file_itself(self):
        """The file node carries the import count and would win every time."""
        G = _graph(dependents=1)
        for i in range(9):
            _add(G, f"imp{i}", f"importer{i}", f"app/i{i}.tsx")
            G.add_edge(f"imp{i}", "file", relation="imports_from")
        assert analyze_impact(G, TARGET).highest_risk_node == "hot"

    def test_high_risk_above_five_direct_dependents(self):
        assert analyze_impact(_graph(dependents=6), TARGET).risk_level == "HIGH"

    def test_medium_risk_between_two_and_five(self):
        assert analyze_impact(_graph(dependents=3), TARGET).risk_level == "MEDIUM"

    def test_low_risk_when_nothing_depends_on_it(self):
        assert analyze_impact(_graph(dependents=0), TARGET).risk_level == "LOW"

    def test_affected_files_are_unique_and_exclude_the_target(self):
        result = analyze_impact(_graph(dependents=3), TARGET)
        assert result.affected_files == [
            "app/page0.tsx", "app/page1.tsx", "app/page2.tsx"]
        assert TARGET not in result.affected_files

    def test_safe_nodes_have_no_dependants_at_all(self):
        result = analyze_impact(_graph(dependents=3), TARGET)
        assert "cold" in result.safe_nodes
        assert "hot" not in result.safe_nodes

    def test_a_node_called_only_from_this_file_is_not_safe(self):
        """Contained is not the same as safe."""
        G = _graph(dependents=0)
        G.add_edge("cold", "hot", relation="calls")
        assert "hot" not in analyze_impact(G, TARGET).safe_nodes

    def test_impact_score_is_a_fraction(self):
        result = analyze_impact(_graph(dependents=3), TARGET)
        assert 0.0 <= result.impact_score <= 1.0

    def test_unknown_file_returns_an_empty_result_without_raising(self):
        result = analyze_impact(_graph(), "nowhere/at/all.py")
        assert isinstance(result, ImpactResult)
        assert result.nodes_in_file == []
        assert result.risk_level == "LOW"
        assert result.direct_dependents == []

    def test_empty_graph_is_handled(self):
        assert analyze_impact(nx.DiGraph(), TARGET).nodes_in_file == []


class TestAffectedSubgraph:
    def test_contains_the_file_and_its_dependents(self):
        G = _graph(dependents=2, chain=True)
        sub = affected_subgraph(G, analyze_impact(G, TARGET, hops=2))
        assert {"hot", "cold", "dep0", "dep1", "far"} <= set(sub.nodes)

    def test_excludes_unrelated_nodes(self):
        G = _graph(dependents=1)
        _add(G, "island", "unrelated", "app/island.tsx")
        sub = affected_subgraph(G, analyze_impact(G, TARGET))
        assert "island" not in sub.nodes


class TestFormatImpact:
    def test_reports_the_risk_level(self):
        G = _graph(dependents=7)
        assert "RISK: HIGH" in format_impact(analyze_impact(G, TARGET), G)

    def test_names_the_highest_risk_node(self):
        G = _graph(dependents=7)
        out = format_impact(analyze_impact(G, TARGET), G)
        assert "HIGHEST RISK NODE" in out and "createClient" in out

    def test_lists_safe_nodes(self):
        G = _graph(dependents=3)
        out = format_impact(analyze_impact(G, TARGET), G)
        assert "SAFE TO EDIT" in out and "formatDate" in out

    def test_high_risk_recommends_tests(self):
        G = _graph(dependents=8)
        assert "Add integration tests" in format_impact(analyze_impact(G, TARGET), G)

    def test_low_risk_says_it_is_safe(self):
        G = _graph(dependents=0)
        assert "Safe to edit" in format_impact(analyze_impact(G, TARGET), G)

    def test_unknown_file_explains_itself(self):
        G = _graph()
        out = format_impact(analyze_impact(G, "nope.py"), G)
        assert "No nodes found" in out

    def test_affected_files_are_listed(self):
        G = _graph(dependents=3)
        out = format_impact(analyze_impact(G, TARGET), G)
        assert "AFFECTED FILES" in out and "app/page0.tsx" in out


class TestImpactToDict:
    def test_round_trips_through_json(self):
        G = _graph(dependents=3)
        payload = impact_to_dict(analyze_impact(G, TARGET))
        assert json.loads(json.dumps(payload))["risk_level"] == "MEDIUM"

    def test_carries_every_field(self):
        payload = impact_to_dict(analyze_impact(_graph(), TARGET))
        for key in ("file_path", "risk_level", "impact_score", "nodes_in_file",
                    "direct_dependents", "transitive_dependents",
                    "highest_risk_node", "affected_files", "safe_nodes"):
            assert key in payload


class TestImpactCommand:
    def _graph_file(self, tmp_path):
        G = _graph(dependents=7)
        payload = {
            "nodes": [{"id": n, **G.nodes[n]} for n in G.nodes],
            "links": [{"source": u, "target": v, **G.edges[u, v]} for u, v in G.edges],
        }
        path = tmp_path / "graph.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_text_output(self, tmp_path):
        res = CliRunner().invoke(
            cli, ["impact", TARGET, "--graph", str(self._graph_file(tmp_path))])
        assert res.exit_code == 0
        assert "Impact Analysis" in res.output

    def test_json_output_is_parseable(self, tmp_path):
        res = CliRunner().invoke(
            cli, ["impact", TARGET, "--graph", str(self._graph_file(tmp_path)),
                  "--output", "json"])
        assert res.exit_code == 0
        assert json.loads(res.output)["risk_level"] == "HIGH"

    def test_hops_below_one_is_rejected(self, tmp_path):
        res = CliRunner().invoke(
            cli, ["impact", TARGET, "--graph", str(self._graph_file(tmp_path)),
                  "--hops", "0"])
        assert res.exit_code != 0

    def test_viz_writes_html_with_the_affected_nodes(self, tmp_path):
        out = tmp_path / "impact.html"
        res = CliRunner().invoke(
            cli, ["impact", TARGET, "--graph", str(self._graph_file(tmp_path)),
                  "--viz-output", str(out)])
        assert res.exit_code == 0
        html = out.read_text(encoding="utf-8")
        assert "vis-network" in html
        assert "createClient" in html

    def test_viz_on_an_unknown_file_fails_clearly(self, tmp_path):
        res = CliRunner().invoke(
            cli, ["impact", "nope.py", "--graph", str(self._graph_file(tmp_path)),
                  "--viz-output", str(tmp_path / "x.html")])
        assert res.exit_code != 0
        assert "Nothing to visualise" in res.output

    def test_unknown_file_still_exits_zero_in_text_mode(self, tmp_path):
        res = CliRunner().invoke(
            cli, ["impact", "nope.py", "--graph", str(self._graph_file(tmp_path))])
        assert res.exit_code == 0
        assert "No nodes found" in res.output
