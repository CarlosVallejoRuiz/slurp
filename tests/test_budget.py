"""Tests for slurp.budget — the budget bounds the text actually sent."""

import random
import subprocess
import sys
from pathlib import Path

import networkx as nx
import pytest

import slurp.budget as budget_mod
from slurp.budget import (
    BudgetTooSmallError,
    build_context,
    clear_node_token_cache,
    count_tokens,
    select_subgraph,
)
from slurp.formatter import format_subgraph, item_text

FORMATS = ("markdown", "json", "yaml")


@pytest.fixture(autouse=True)
def _fresh_costs():
    clear_node_token_cache()
    yield
    clear_node_token_cache()


def _line(n: int, prefix: str = "n") -> nx.DiGraph:
    """n nodes in a calls chain, each with a short description."""
    G = nx.DiGraph()
    for i in range(n):
        G.add_node(f"{prefix}{i}", label=f"func{i}", type="function",
                   description=f"does thing {i}", source_file=f"src/m{i % 3}.py")
    for i in range(n - 1):
        G.add_edge(f"{prefix}{i}", f"{prefix}{i + 1}", relation="calls")
    return G


def _decreasing(G: nx.DiGraph) -> dict[str, float]:
    nodes = sorted(G.nodes)
    return {n: round(1.0 - i / (len(nodes) + 1), 4) for i, n in enumerate(nodes)}


def _render(sub, stats, fmt, scores, query):
    return format_subgraph(sub, stats, fmt, scores=scores, query=query)


# ---------------------------------------------------------------------------
# count_tokens
# ---------------------------------------------------------------------------

class TestCountTokens:
    def test_empty_string_is_zero(self):
        assert count_tokens("") == 0

    def test_text_has_tokens(self):
        assert count_tokens("hello world") > 0

    def test_longer_text_has_more(self):
        assert count_tokens("the quick brown fox jumps") > count_tokens("fox")

    def test_deterministic(self):
        assert count_tokens("auth service") == count_tokens("auth service")

    def test_explicit_encoding(self):
        assert count_tokens("test", model="cl100k_base") > 0


# ---------------------------------------------------------------------------
# Items are costed with the formatter's own renderers
# ---------------------------------------------------------------------------

class TestItemCosts:
    def test_source_file_and_file_path_read_the_same(self):
        G = nx.DiGraph()
        G.add_node("a", label="alpha", source_file="src/auth.py")
        G.add_node("b", label="alpha", file_path="src/auth.py")
        for fmt in FORMATS:
            ta, tb = item_text(G, ("n", "a"), fmt), item_text(G, ("n", "b"), fmt)
            assert "src/auth.py" in ta and "src/auth.py" in tb
            assert count_tokens(ta) == count_tokens(tb.replace('"b"', '"a"').replace("b\n", "a\n"))

    def test_markdown_node_item_matches_the_formatter_block(self):
        G = _line(1)
        stats = {"nodes_selected": 1, "nodes_total": 1, "tokens_used": 0,
                 "tokens_budget": 100, "coverage_pct": 100.0}
        out = format_subgraph(G, stats, "markdown", scores={"n0": 0.1234})
        assert item_text(G, ("n", "n0"), "markdown").rstrip("\n") in out

    def test_edge_item_matches_the_formatter_line(self):
        G = _line(2)
        stats = {"nodes_selected": 2, "nodes_total": 2, "tokens_used": 0,
                 "tokens_budget": 100, "coverage_pct": 100.0}
        out = format_subgraph(G, stats, "markdown")
        assert item_text(G, ("e", "n0", "n1"), "markdown").rstrip("\n") in out

    def test_costs_are_memoised_and_cleared(self, monkeypatch):
        G = _line(5)
        calls = []
        real = budget_mod.item_text
        monkeypatch.setattr(budget_mod, "item_text",
                            lambda *a, **k: calls.append(a[1]) or real(*a, **k))
        scores = _decreasing(G)
        build_context(G, scores, 2000)
        first = len(calls)
        build_context(G, scores, 2000)
        assert len(calls) == first          # second query: all hits
        clear_node_token_cache()
        build_context(G, scores, 2000)
        assert len(calls) == 2 * first


# ---------------------------------------------------------------------------
# The guarantee, checked on the final text
# ---------------------------------------------------------------------------

def _random_graph(rng: random.Random, n: int) -> nx.DiGraph:
    words = ["auth", "user", "token", "cache", "db", "pool", "player", "stats", "x" * 30]
    rels = ["calls", "calls", "contains", "imports_from", "references"]
    G = nx.DiGraph()
    for i in range(n):
        desc = " ".join(rng.choice(words) for _ in range(rng.randint(0, 25)))
        attrs = {"label": f"{rng.choice(words)}_{i}", "description": desc}
        if rng.random() < 0.5:
            attrs["source_file"] = f"lib/{rng.choice(words)}/{i}.ts"
        elif rng.random() < 0.5:
            attrs["file_path"] = f"src/{i}.py"
        if rng.random() < 0.7:
            attrs["type"] = rng.choice(["function", "class", "module", "import"])
        G.add_node(f"pkg.mod{i % 7}.sym{i}", **attrs)
    nodes = list(G.nodes)
    for _ in range(n * 2):
        if n > 1:
            u, v = rng.sample(nodes, 2)
            G.add_edge(u, v, relation=rng.choice(rels))
    return G


class TestBudgetGuarantee:
    """Property: the text returned measures <= budget, and the stats tell the truth."""

    @pytest.mark.parametrize("seed", range(40))
    def test_random_graphs_budgets_and_formats(self, seed):
        rng = random.Random(seed)
        G = _random_graph(rng, rng.randint(0, 60))
        query = " ".join(rng.sample(["auth", "user", "player stats", "db pool", "zzz"], 2))
        scores = {n: rng.random() for n in G.nodes}
        fmt = FORMATS[seed % 3]
        for b in (rng.randint(150, 400), rng.randint(400, 3000), rng.randint(3000, 20000)):
            try:
                text, sub, stats = build_context(G, scores, b, min_score=rng.choice([0.0, 0.3]),
                                                 format=fmt, query=query)
            except BudgetTooSmallError:
                continue
            assert count_tokens(text) <= b
            assert count_tokens(text) == stats["tokens_used"]
            assert stats["nodes_selected"] == sub.number_of_nodes()
            assert stats["tokens_budget"] == b
            # select_subgraph + format_subgraph reproduce the same text.
            sub2, stats2 = select_subgraph(G, scores, b, min_score=0.0, format=fmt, query=query)
            assert count_tokens(_render(sub2, stats2, fmt, scores, query)) <= b

    @pytest.mark.parametrize("fmt", FORMATS)
    def test_every_selected_edge_joins_selected_nodes(self, fmt):
        G = _random_graph(random.Random(7), 50)
        scores = {n: 0.5 for n in G.nodes}
        _, sub, _ = build_context(G, scores, 3000, format=fmt, query="auth")
        assert all(u in sub and v in sub for u, v in sub.edges)

    def test_relationships_count_against_the_budget(self):
        # A dense graph: v1.0.7 counted nodes only and overshot several times over.
        G = nx.complete_graph(30, create_using=nx.DiGraph)
        G = nx.relabel_nodes(G, {i: f"module.symbol_{i}" for i in G.nodes})
        for n in G.nodes:
            G.nodes[n].update(label=n.split(".")[-1], type="function")
        nx.set_edge_attributes(G, "calls", "relation")
        text, sub, _ = build_context(G, {n: 0.9 for n in G.nodes}, 1500)
        assert count_tokens(text) <= 1500
        assert sub.number_of_edges() > 0

    def test_header_reports_what_is_sent(self):
        G = _line(40)
        text, sub, stats = build_context(G, _decreasing(G), 900, query="func")
        assert f"Selected {sub.number_of_nodes()}/40 nodes" in text
        assert f"{count_tokens(text):,}/900 tokens used" in text


# ---------------------------------------------------------------------------
# Budgets too small to hold a node
# ---------------------------------------------------------------------------

class TestTooSmall:
    def test_below_the_header_is_an_error_naming_the_minimum(self):
        G = _line(3)
        with pytest.raises(BudgetTooSmallError, match=r"Use --budget \d+"):
            build_context(G, _decreasing(G), 20)

    def test_zero_or_negative_budget_is_an_error(self):
        G = _line(3)
        for b in (0, -5):
            with pytest.raises(BudgetTooSmallError):
                build_context(G, _decreasing(G), b)

    def test_top_node_too_big_returns_header_and_warning(self):
        G = nx.DiGraph()
        G.add_node("big", label="huge", description="word " * 600)
        scores = {"big": 1.0}
        floor = None
        for b in range(60, 400, 10):     # the first budget past the floor
            try:
                text, sub, stats = build_context(G, scores, b)
                floor = b
                break
            except BudgetTooSmallError:
                continue
        assert floor is not None
        assert sub.number_of_nodes() == 0
        assert "too small" in text and "--budget" in text
        assert count_tokens(text) <= floor
        needed = stats["min_budget"]
        text2, sub2, _ = build_context(G, scores, needed)
        assert sub2.number_of_nodes() == 1 and count_tokens(text2) <= needed

    @pytest.mark.parametrize("fmt", ["json", "yaml"])
    def test_warning_reaches_structured_formats(self, fmt):
        G = nx.DiGraph()
        G.add_node("big", label="huge", description="word " * 600)
        for b in range(80, 600, 10):
            try:
                text, sub, stats = build_context(G, {"big": 1.0}, b, format=fmt)
                break
            except BudgetTooSmallError:
                continue
        assert sub.number_of_nodes() == 0
        assert "warning" in stats and "too small" in text


# ---------------------------------------------------------------------------
# What enters first
# ---------------------------------------------------------------------------

class TestSelectionOrder:
    def test_highest_score_enters_first(self):
        G = _line(20)
        scores = _decreasing(G)
        _, sub, _ = build_context(G, scores, 400)
        top = max(scores, key=scores.get)
        assert top in sub
        assert all(scores[n] >= min(scores[m] for m in sub) for n in sub)

    def test_min_score_excludes_candidates(self):
        G = _line(10)
        scores = {n: (0.9 if n == "n0" else 0.05) for n in G.nodes}
        _, sub, _ = build_context(G, scores, 5000, min_score=0.15)
        assert set(sub.nodes) == {"n0"}

    def test_neighbour_boost_beats_an_unrelated_node(self):
        G = nx.DiGraph()
        for n in ("hub", "nbr", "other"):
            G.add_node(n, label=n, type="function", description="words " * 40)
        G.add_edge("hub", "nbr", relation="calls")
        scores = {"hub": 1.0, "nbr": 0.1, "other": 0.5}  # 1.0 * 0.7 > 0.5
        # With room for exactly two nodes, the boosted neighbour takes the second.
        for b in range(120, 800, 4):
            try:
                _, sub, _ = build_context(G, scores, b)
            except BudgetTooSmallError:
                continue
            if sub.number_of_nodes() == 2:
                assert set(sub.nodes) == {"hub", "nbr"}
                break
        else:
            pytest.fail("no budget selected exactly two nodes")

    def test_calls_outrank_structural_edges(self):
        G = nx.DiGraph()
        for n in ("a", "b", "c"):
            G.add_node(n, label=n, type="function")
        G.add_edge("a", "b", relation="contains")
        G.add_edge("a", "c", relation="calls")
        scores = {"a": 0.9, "b": 0.9, "c": 0.9}
        G_full = build_context(G, scores, 5000)[1]
        assert set(G_full.edges) == {("a", "b"), ("a", "c")}
        # With room for the nodes and one edge, the call wins.
        for b in range(150, 400, 2):
            try:
                _, sub, _ = build_context(G, scores, b)
            except BudgetTooSmallError:
                continue
            if sub.number_of_nodes() == 3 and sub.number_of_edges() == 1:
                assert set(sub.edges) == {("a", "c")}
                break

    def test_deterministic(self):
        G = _random_graph(random.Random(3), 40)
        scores = {n: 0.5 for n in G.nodes}
        a = build_context(G, scores, 2500, query="auth")[0]
        assert build_context(G, scores, 2500, query="auth")[0] == a

    def test_output_order_does_not_depend_on_the_hash_seed(self, tmp_path):
        script = tmp_path / "run.py"
        script.write_text(
            "import sys, random\n"
            f"sys.path.insert(0, {str(Path(__file__).parent.parent)!r})\n"
            f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
            "from test_budget import _random_graph\n"
            "from slurp.budget import build_context\n"
            "G = _random_graph(random.Random(11), 300)\n"
            "print(build_context(G, {n: 0.4 for n in G.nodes}, 4000, query='auth')[0])\n")
        runs = {subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                               env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"}).stdout
                for seed in ("1", "2", "3")}
        assert len(runs) == 1 and next(iter(runs))


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_graph_returns_the_header(self):
        text, sub, stats = build_context(nx.DiGraph(), {}, 1000)
        assert sub.number_of_nodes() == 0 and stats["nodes_total"] == 0
        assert count_tokens(text) <= 1000

    def test_nothing_above_min_score_is_not_a_budget_warning(self):
        G = _line(4)
        _, sub, stats = build_context(G, {n: 0.01 for n in G.nodes}, 2000, min_score=0.5)
        assert sub.number_of_nodes() == 0 and "warning" not in stats

    def test_large_budget_takes_everything(self):
        G = _line(8)
        _, sub, _ = build_context(G, _decreasing(G), 50_000)
        assert set(sub.nodes) == set(G.nodes)
        assert set(sub.edges) == set(G.edges)

    def test_custom_renderer_is_what_gets_measured(self):
        G = _line(30)
        wrap = "PREFIX " * 50

        def render(sub, stats):
            return wrap + format_subgraph(sub, stats, "markdown")

        text, _, stats = build_context(G, _decreasing(G), 800, render=render)
        assert text.startswith(wrap) and count_tokens(text) <= 800


# ---------------------------------------------------------------------------
# End to end: what each entry point actually prints fits the budget
# ---------------------------------------------------------------------------

def _graph_file(tmp_path: Path) -> Path:
    import json

    G = _random_graph(random.Random(5), 80)
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(nx.node_link_data(G, edges="links")))
    return path


class TestEntryPointsFitTheBudget:
    @pytest.mark.parametrize("fmt", FORMATS)
    @pytest.mark.parametrize("budget", [300, 1200, 5000])
    def test_cli_query_output(self, tmp_path, fmt, budget):
        from click.testing import CliRunner

        from slurp.cli import cli

        res = CliRunner().invoke(cli, ["auth user", "--graph", str(_graph_file(tmp_path)),
                                       "--budget", str(budget), "--format", fmt, "--no-audit"])
        assert res.exit_code == 0, res.output
        assert count_tokens(res.stdout.rstrip("\n")) <= budget

    @pytest.mark.parametrize("export_fmt", ["claude", "chatgpt", "claudemd"])
    def test_cli_export_output(self, tmp_path, export_fmt):
        from click.testing import CliRunner

        from slurp.cli import cli

        res = CliRunner().invoke(cli, ["export", "auth", "--graph", str(_graph_file(tmp_path)),
                                       "--budget", "900", "--format", export_fmt])
        assert res.exit_code == 0, res.output
        assert count_tokens(res.stdout.rstrip("\n")) <= 900

    def test_inject_code_is_trimmed_back_under_the_budget(self, tmp_path):
        import json

        from click.testing import CliRunner

        from slurp.cli import cli

        (tmp_path / "src").mkdir()
        body = "".join(f"    step_{i} = compute(step_{i - 1})\n" for i in range(1, 60))
        nodes = []
        for i in range(6):
            (tmp_path / "src" / f"m{i}.py").write_text(f"def auth_{i}():\n{body}")
            nodes.append({"id": f"m{i}.auth_{i}", "label": f"auth_{i}", "type": "function",
                          "source_file": f"src/m{i}.py", "source_location": "L1"})
        graph = tmp_path / "graph.json"
        graph.write_text(json.dumps({"nodes": nodes, "links": []}))
        res = CliRunner().invoke(cli, ["auth", "--graph", str(graph), "--budget", "1000",
                                       "--inject-code", "--no-audit", "--min-score", "0"])
        assert res.exit_code == 0, res.output
        assert "```python" in res.stdout
        assert count_tokens(res.stdout.rstrip("\n")) <= 1000

    def test_cli_budget_too_small_is_a_clean_error(self, tmp_path):
        from click.testing import CliRunner

        from slurp.cli import cli

        res = CliRunner().invoke(cli, ["auth", "--graph", str(_graph_file(tmp_path)),
                                       "--budget", "20", "--no-audit"])
        assert res.exit_code != 0
        assert "--budget" in res.output and "Traceback" not in res.output

    def test_mcp_query_output(self, tmp_path):
        import io
        import json

        from slurp.loader import load_graph
        from slurp.mcp import _handle

        G = load_graph(_graph_file(tmp_path))
        for budget in (300, 2000):
            out = io.StringIO()
            _handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "slurp_query",
                                "arguments": {"query": "auth", "budget": budget}}}, G, out,
                    session_log=False)
            text = json.loads(out.getvalue())["result"]["content"][0]["text"]
            assert count_tokens(text) <= budget

    def test_mcp_budget_too_small_is_invalid_params(self, tmp_path):
        import io
        import json

        from slurp.loader import load_graph
        from slurp.mcp import _handle

        out = io.StringIO()
        _handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "slurp_query", "arguments": {"query": "auth", "budget": 10}}},
                load_graph(_graph_file(tmp_path)), out, session_log=False)
        resp = json.loads(out.getvalue())
        assert resp["error"]["code"] == -32602
        assert "--budget" in resp["error"]["message"]


class TestTrimmedToEmptyIsNeverSilent:
    def test_fit_to_budget_names_the_budget_the_top_node_needs(self):
        from slurp.budget import fit_to_budget

        G = nx.DiGraph()
        G.add_node("big", label="big", description="word " * 400)
        G.add_node("small", label="small", description="tiny")

        def render(sub, stats):
            return format_subgraph(sub, stats, "markdown", scores={"big": 1.0, "small": 0.2})

        text, sub, stats = fit_to_budget(G, 150, render, {("n", "big"): 1.0, ("n", "small"): 0.2}, 2)
        assert count_tokens(text) <= 150
        if sub.number_of_nodes() == 0:
            assert "too small" in text and stats["min_budget"] > 150
        else:
            assert set(sub.nodes) == {"small"}

    def test_injected_code_too_large_for_any_node_warns(self, tmp_path):
        import json

        from click.testing import CliRunner

        from slurp.cli import cli

        body = "".join(f"    s{i} = f(s{i - 1})\n" for i in range(1, 200))
        (tmp_path / "a.py").write_text(f"def auth():\n{body}")
        graph = tmp_path / "graph.json"
        graph.write_text(json.dumps({"nodes": [{"id": "a.auth", "label": "auth", "type": "function",
                                                "source_file": "a.py", "source_location": "L1"}],
                                     "links": []}))
        res = CliRunner().invoke(cli, ["auth", "--graph", str(graph), "--budget", "400",
                                       "--inject-code", "--no-audit", "--min-score", "0"])
        assert res.exit_code == 0, res.output
        assert "too small" in res.stdout and "--budget" in res.stdout
        assert count_tokens(res.stdout.rstrip("\n")) <= 400
