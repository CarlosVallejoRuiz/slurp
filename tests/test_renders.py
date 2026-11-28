"""`renders` edges (v1.5, #87) in everything that reads the graph.

The indexer's side (which JSX becomes an edge, on both branches) lives in
test_indexer.py: TestTSRenderEdges and TestTSComponentWrappers.
"""

import io
import json
import os
import time
from pathlib import Path

import networkx as nx
import pytest
from click.testing import CliRunner

from slurp import indexer as indexer_mod
from slurp import __version__, scorer
from slurp.cli import cli
from slurp.explainer import _risk_level, explain_node, format_explanation
from slurp.formatter import format_subgraph
from slurp.impact import analyze_impact
from slurp.loader import build_graph
from slurp.mcp import GraphState, _handle
from slurp.refresh import META_KEY
from slurp.viz import build_html


def _small() -> nx.DiGraph:
    G = nx.DiGraph()
    for n in ("page", "badge", "fmt"):
        G.add_node(n, label=n, type="function", source_file=f"{n}.tsx")
    G.add_edge("page", "badge", relation="renders")
    G.add_edge("page", "fmt", relation="calls")
    return G


def _stats(G: nx.DiGraph) -> dict:
    n = G.number_of_nodes()
    return {"nodes_selected": n, "nodes_total": n, "tokens_used": 0, "tokens_budget": 1000,
            "coverage_pct": 100.0}


def test_the_personalised_walk_follows_renders_like_calls():
    lists = scorer._walk_lists(_small())
    assert dict(lists["page"]) == {"badge": 0.5, "fmt": 0.5}
    assert dict(lists["badge"]) == {"page": 1.0}       # walked back, like a call


def test_markdown_and_json_print_the_relation():
    G = _small()
    assert "- page → renders → badge" in format_subgraph(G, _stats(G), "markdown")
    assert '"relation": "renders"' in format_subgraph(G, _stats(G), "json")


def test_the_viewer_draws_renders_dashed_and_names_every_relation():
    G = _small()
    html = build_html(G, _stats(G), {n: 0.5 for n in G}, "q")
    assert '"label": "renders"' in html
    assert 'dashes: e.label === "renders"' in html
    assert "title: e.label" in html


# A shadcn/ui-style component, rendered by six pages and one test.
_BUTTON = ('import * as React from "react";\n'
           "export const Button = React.forwardRef((props, ref) => {\n"
           "  return <button ref={ref} {...props} />;\n});\n")


def _write_button_project(root: Path) -> None:
    files = {"components/ui/button.tsx": _BUTTON}
    for i in range(6):
        files[f"app/p{i}/page.tsx"] = (
            'import { Button } from "../../components/ui/button";\n'
            f"export default function Page{i}() {{ return <form><Button /></form>; }}\n")
    files["tests/button.test.tsx"] = (
        'import { Button } from "../components/ui/button";\n'
        "export function renderIt() { return <Button />; }\n")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.fixture(params=[True, False], ids=["treesitter", "regex"])
def button_graph(request, tmp_path, monkeypatch):
    if request.param and not indexer_mod._TREE_SITTER_AVAILABLE:
        pytest.skip("requires the 'ts' extra")
    monkeypatch.setattr(indexer_mod, "_TREE_SITTER_AVAILABLE", request.param)
    _write_button_project(tmp_path)
    return build_graph(indexer_mod.index_project(tmp_path))


def test_explain_counts_the_pages_that_render_a_component(button_graph):
    node = "components.ui.button.Button"
    assert _risk_level(button_graph, node) == "HIGH"
    text = format_explanation(explain_node(button_graph, node))
    assert "6 production callers, 1 in tests" in text


def test_impact_counts_the_pages_that_render_a_component(button_graph):
    result = analyze_impact(button_graph, "components/ui/button.tsx")
    node = "components.ui.button.Button"
    assert result.risk_level == "HIGH"
    assert result.dependents_per_node[node] == 6
    assert len(result.affected_files) == 6
    assert len(result.test_dependents) == 1


def test_readme_lists_renders_among_the_relations():
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    assert "`renders`" in readme
    assert "a React component rendered as\n> JSX (`<Badge />`) gets no `calls` edge" not in readme


def test_tool_descriptions_name_the_render_relation():
    from slurp.mcp import _EXPLAIN_TOOL_DEFINITION, _IMPACT_TOOL_DEFINITION, _QUERY_HEAD
    assert "render" in _QUERY_HEAD
    assert "renders it" in _EXPLAIN_TOOL_DEFINITION["description"]
    assert "render it" in _IMPACT_TOOL_DEFINITION["description"]


# ---------------------------------------------------------------------------
# Upgrading: a graph v1.4 wrote has no renders edge and no memo/forwardRef node
# ---------------------------------------------------------------------------

_BUTTON_ID = "components.ui.button.Button"


def _age(root: Path, seconds: float = 60) -> None:
    past = time.time() - seconds
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + dirnames:
            os.utime(Path(dirpath) / name, (past, past), follow_symlinks=False)
        os.utime(dirpath, (past, past))


@pytest.fixture
def v14_graph(tmp_path) -> Path:
    """graphify-out/graph.json as `slurp index` 1.4.0 wrote it, read by this slurp."""
    root = tmp_path / "proj"
    _write_button_project(root)
    res = CliRunner().invoke(cli, ["index", str(root), "--ignore-file",
                                   str(root / ".slurpignore")])
    assert res.exit_code == 0, res.output
    graph = root / "graphify-out" / "graph.json"
    data = json.loads(graph.read_text(encoding="utf-8"))
    data["nodes"] = [n for n in data["nodes"] if n["id"] != _BUTTON_ID]
    data["links"] = [e for e in data["links"] if e["relation"] != "renders"
                     and _BUTTON_ID not in (e["source"], e["target"])]
    data["slurp"]["slurp_version"] = "1.4.0"
    graph.write_text(json.dumps(data), encoding="utf-8")
    _age(root)
    return graph


def _on_disk(graph: Path) -> dict:
    return json.loads(graph.read_text(encoding="utf-8"))


def _rebuilt_by_this_slurp(graph: Path) -> None:
    data = _on_disk(graph)
    assert data["slurp"]["slurp_version"] == __version__
    assert _BUTTON_ID in {n["id"] for n in data["nodes"]}
    assert sum(1 for e in data["links"] if e["relation"] == "renders") == 7


def test_a_query_rebuilds_a_v14_graph_before_answering(v14_graph):
    res = CliRunner().invoke(cli, ["button", "--graph", str(v14_graph), "--no-audit"])
    assert res.exit_code == 0, res.output
    assert "⚠" not in res.stderr
    assert "renders" in res.stdout
    _rebuilt_by_this_slurp(v14_graph)


def test_the_mcp_server_rebuilds_a_v14_graph_before_answering(v14_graph):
    state = GraphState(v14_graph)
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "slurp_explain", "arguments": {"node_name": "Button"}}}
    out = io.StringIO()
    _handle(msg, state.graph, out, session_log=False, state=state)
    result = json.loads(out.getvalue())["result"]
    assert result["_meta"][META_KEY]["status"] == "refreshed"
    assert "6 production callers, 1 in tests" in result["content"][0]["text"]
    _rebuilt_by_this_slurp(v14_graph)


def test_a_graphify_graph_of_a_react_project_is_never_rebuilt(v14_graph):
    data = _on_disk(v14_graph)
    del data["slurp"], data["built_at_commit"]
    data.update({"directed": True, "multigraph": False, "graph": {}})
    v14_graph.write_text(json.dumps(data), encoding="utf-8")
    before = v14_graph.read_bytes()
    res = CliRunner().invoke(cli, ["button", "--graph", str(v14_graph), "--no-audit"])
    assert res.exit_code == 0, res.output
    assert v14_graph.read_bytes() == before
