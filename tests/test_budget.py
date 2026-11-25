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
    fit_to_budget,
    full_graph_tokens,
    left_out_from_stats,
    select_subgraph,
)
from slurp._graphutils import _left_out
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
        if rng.random() < 0.15:
            attrs["source_file"] = f"tests/test_{rng.choice(words)}_{i}.py"
        elif rng.random() < 0.5:
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
        flags = {"include_tests": rng.random() < 0.3, "include_imports": rng.random() < 0.3}
        for b in (rng.randint(150, 400), rng.randint(400, 3000), rng.randint(3000, 20000)):
            try:
                text, sub, stats = build_context(G, scores, b, min_score=rng.choice([0.0, 0.3]),
                                                 format=fmt, query=query, **flags)
            except BudgetTooSmallError:
                continue
            # The closing tip, its line on tests and imports included, is text too.
            assert count_tokens(text) <= b
            assert count_tokens(text) == stats["tokens_used"]
            assert stats["nodes_selected"] == sub.number_of_nodes()
            assert stats["tokens_budget"] == b
            assert not set(sub.nodes) & _left_out(G, **flags)
            assert (stats["nodes_selected"] + stats["nodes_over_budget"]
                    + stats["nodes_below_min_score"] + stats.get("nodes_excluded_tests", 0)
                    + stats.get("nodes_excluded_imports", 0)) == stats["nodes_total"]
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
                _, sub, _ = build_context(G, scores, b, min_score=0.0)
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


# ---------------------------------------------------------------------------
# One default min_score everywhere
# ---------------------------------------------------------------------------

class TestOneDefaultMinScore:
    def test_every_entry_point_uses_the_same_default(self):
        import inspect

        from slurp import benchmark, budget, cli, evaluator, mcp

        expected = budget.DEFAULT_MIN_SCORE
        assert expected == 0.15

        def click_default(command, name="min_score"):
            return next(p.default for p in command.params if p.name == name)

        assert click_default(cli.run) == expected
        assert click_default(cli.export_cmd) == expected
        assert click_default(cli.benchmark_cmd) == expected
        assert mcp._MIN_SCORE == expected
        for fn in (budget.select_subgraph, budget.build_context, benchmark.run_benchmark,
                   evaluator.run_retrieval_eval, evaluator.build_slurp_context):
            assert inspect.signature(fn).parameters["min_score"].default == expected, fn

    def test_diff_budget_uses_the_default(self, monkeypatch):
        import slurp.cli as cli_mod

        seen = {}
        real = cli_mod.build_context

        def spy(*args, **kwargs):
            seen["min_score"] = kwargs.get("min_score", "default")
            return real(*args, **kwargs)

        monkeypatch.setattr(cli_mod, "build_context", spy)
        import json

        from click.testing import CliRunner

        def graph(path, extra):
            nodes = [{"id": "a", "label": "alpha", "type": "function"}] + extra
            path.write_text(json.dumps({"nodes": nodes, "links": []}))
            return str(path)

        import tempfile

        d = Path(tempfile.mkdtemp())
        old = graph(d / "old.json", [])
        new = graph(d / "new.json", [{"id": "b", "label": "beta", "type": "function"}])
        res = CliRunner().invoke(cli_mod.cli, ["diff", old, new, "--budget", "2000"])
        assert res.exit_code == 0, res.output
        assert seen["min_score"] == "default"  # not overridden: the library default applies


# ---------------------------------------------------------------------------
# Configurable token factor (another tokenizer, estimated)
# ---------------------------------------------------------------------------

class TestTokenFactor:
    def test_default_factor_changes_nothing(self):
        G = _random_graph(random.Random(2), 40)
        scores = {n: 0.5 for n in G.nodes}
        a = build_context(G, scores, 2000, query="auth")
        b = build_context(G, scores, 2000, query="auth", token_factor=1.0)
        assert a[0] == b[0]

    @pytest.mark.parametrize("factor", [1.56, 2.0, 0.8])
    def test_scaled_count_fits_and_is_reported(self, factor):
        import math

        G = _random_graph(random.Random(4), 60)
        scores = {n: 0.5 for n in G.nodes}
        text, sub, stats = build_context(G, scores, 3000, query="auth", token_factor=factor)
        scaled = math.ceil(count_tokens(text) * factor)
        assert scaled <= 3000
        assert stats["tokens_used"] == scaled
        assert stats["token_factor"] == factor

    def test_bigger_factor_selects_less(self):
        G = _random_graph(random.Random(4), 60)
        scores = {n: 0.5 for n in G.nodes}
        one = build_context(G, scores, 3000, query="auth")[1].number_of_nodes()
        more = build_context(G, scores, 3000, query="auth", token_factor=1.56)[1].number_of_nodes()
        assert more <= one

    @pytest.mark.parametrize("bad", [0, -1.0, 50])
    def test_invalid_factor_is_rejected(self, bad):
        G = _line(3)
        with pytest.raises(ValueError, match="token factor"):
            build_context(G, _decreasing(G), 2000, token_factor=bad)

    def test_env_var_is_the_default_and_the_flag_wins(self, tmp_path, monkeypatch):
        import math

        from click.testing import CliRunner

        from slurp.cli import cli

        graph = str(_graph_file(tmp_path))
        base = ["auth", "--graph", graph, "--budget", "1500", "--no-audit"]
        monkeypatch.setenv("SLURP_TOKEN_FACTOR", "1.56")
        env_run = CliRunner().invoke(cli, base)
        assert env_run.exit_code == 0, env_run.output
        assert math.ceil(count_tokens(env_run.stdout.rstrip("\n")) * 1.56) <= 1500
        flag_run = CliRunner().invoke(cli, [*base, "--token-factor", "1.0"])
        assert count_tokens(flag_run.stdout.rstrip("\n")) > count_tokens(env_run.stdout.rstrip("\n"))

    def test_bad_env_value_is_a_clear_error(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        from slurp.cli import cli

        monkeypatch.setenv("SLURP_TOKEN_FACTOR", "fast")
        res = CliRunner().invoke(cli, ["auth", "--graph", str(_graph_file(tmp_path)), "--no-audit"])
        assert res.exit_code != 0 and "SLURP_TOKEN_FACTOR" in res.output

    def test_mcp_reads_the_env_var(self, tmp_path, monkeypatch):
        import io
        import json
        import math

        from slurp.loader import load_graph
        from slurp.mcp import _handle

        monkeypatch.setenv("SLURP_TOKEN_FACTOR", "2.0")
        out = io.StringIO()
        _handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "slurp_query", "arguments": {"query": "auth", "budget": 1500}}},
                load_graph(_graph_file(tmp_path)), out, session_log=False)
        text = json.loads(out.getvalue())["result"]["content"][0]["text"]
        assert math.ceil(count_tokens(text) * 2.0) <= 1500


# ---------------------------------------------------------------------------
# Why nodes are left out: the budget, or the minimum score
# ---------------------------------------------------------------------------

class TestLeftOutBreakdown:
    """stats split the nodes left out by cause, and the tip reports the split."""

    @staticmethod
    def _graph_and_scores() -> tuple[nx.DiGraph, dict[str, float]]:
        G = _line(12)
        # n0..n7 clear the 0.15 threshold, n8..n11 do not.
        scores = {f"n{i}": (0.9 - i * 0.05 if i < 8 else 0.1) for i in range(12)}
        return G, scores

    @pytest.mark.parametrize("fmt", FORMATS)
    def test_counts_add_up_and_name_each_cause(self, fmt):
        G, scores = self._graph_and_scores()
        _text, sub, stats = build_context(G, scores, budget=250, format=fmt, query="q")
        assert stats["min_score"] == 0.15
        assert stats["nodes_below_min_score"] == 4
        assert stats["nodes_over_budget"] == 8 - sub.number_of_nodes() > 0
        assert (stats["nodes_selected"] + stats["nodes_over_budget"]
                + stats["nodes_below_min_score"]) == stats["nodes_total"]

    def test_a_generous_budget_leaves_only_the_min_score_cause(self):
        G, scores = self._graph_and_scores()
        text, _sub, stats = build_context(G, scores, budget=50_000, query="q")
        assert stats["nodes_over_budget"] == 0 and stats["nodes_below_min_score"] == 4
        tip = text.split("\n---\n")[-1]
        assert "--budget" not in tip and "4 nodes scored below" in tip

    def test_the_printed_tip_matches_the_stats_after_trimming(self, monkeypatch):
        G, scores = self._graph_and_scores()
        # Force the final measurement to trim: selection aims past the budget.
        monkeypatch.setattr(budget_mod, "_SELECTION_MARGIN", -0.5)
        text, sub, stats = build_context(G, scores, budget=300, query="q")
        assert count_tokens(text) <= 300
        assert stats["nodes_over_budget"] == 8 - sub.number_of_nodes()
        assert f"{stats['nodes_over_budget']} more node" in text

    def test_zero_min_score_counts_nothing_below_it(self):
        G, scores = self._graph_and_scores()
        _text, _sub, stats = build_context(G, scores, budget=250, min_score=0.0)
        assert stats["nodes_below_min_score"] == 0
        assert stats["nodes_over_budget"] == 12 - stats["nodes_selected"]


# ---------------------------------------------------------------------------
# Tests and import nodes do not compete for a place by default
# ---------------------------------------------------------------------------

def _with_tests_and_imports() -> tuple[nx.DiGraph, dict[str, float]]:
    """Production code, a test that names it, and an import of it, all scored high."""
    G = nx.DiGraph()
    nodes = {
        "lib.budget.select": ("select", "function", "lib/budget.py", 0.80),
        "lib.budget.helper": ("helper", "function", "lib/budget.py", 0.40),
        "lib.other.unrelated": ("unrelated", "function", "lib/other.py", 0.50),
        "lib.other.reached": ("reached", "function", "lib/other.py", 0.30),
        "tests.test_budget.test_select": ("test_select", "function",
                                          "tests/test_budget.py", 0.95),
        "lib.cli.import_select": ("lib.budget.select", "import", "lib/cli.py", 0.90),
        "tests.test_budget.import_select": ("lib.budget.select", "import",
                                            "tests/test_budget.py", 0.90),
    }
    for nid, (label, ntype, path, _) in nodes.items():
        G.add_node(nid, label=label, type=ntype, source_file=path)
    G.add_edge("lib.budget.select", "lib.budget.helper", relation="calls")
    G.add_edge("tests.test_budget.test_select", "lib.budget.select", relation="calls")
    G.add_edge("tests.test_budget.test_select", "lib.other.reached", relation="calls")
    G.add_edge("lib.cli.import_select", "lib.budget.select", relation="calls")
    return G, {nid: v[3] for nid, v in nodes.items()}


class TestTestsAndImportsLeftOut:
    TESTS = {"tests.test_budget.test_select", "tests.test_budget.import_select"}
    IMPORTS = {"lib.cli.import_select", "tests.test_budget.import_select"}

    def test_they_never_take_a_place_by_default(self):
        G, scores = _with_tests_and_imports()
        _text, sub, _stats = build_context(G, scores, 50_000, min_score=0.0)
        assert set(sub.nodes) == {"lib.budget.select", "lib.budget.helper",
                                  "lib.other.unrelated", "lib.other.reached"}

    def test_each_flag_lets_its_kind_back_in(self):
        G, scores = _with_tests_and_imports()
        _t, with_tests, _s = build_context(G, scores, 50_000, min_score=0.0,
                                           include_tests=True)
        assert set(with_tests.nodes) & (self.TESTS | self.IMPORTS) == {
            "tests.test_budget.test_select"}
        _t, with_imports, _s = build_context(G, scores, 50_000, min_score=0.0,
                                             include_imports=True)
        assert set(with_imports.nodes) & (self.TESTS | self.IMPORTS) == {
            "lib.cli.import_select"}
        _t, both, _s = build_context(G, scores, 50_000, min_score=0.0,
                                     include_tests=True, include_imports=True)
        assert set(both.nodes) == set(G.nodes)

    def test_a_left_out_node_passes_no_boost_on(self):
        # The test calls `reached` (0.30); selected, it would lift it to
        # 0.95 * 0.7 above `unrelated` (0.50). Left out, it lifts nothing.
        G, scores = _with_tests_and_imports()

        def entry_order(**flags) -> list[str]:
            room = build_context(G, scores, 50_000, min_score=0.0, **flags)[2]["tokens_used"]
            order: list[str] = []
            for budget in range(50, room * 2, 5):
                try:
                    _t, sub, _s = build_context(G, scores, budget, min_score=0.0, **flags)
                except BudgetTooSmallError:
                    continue
                order.extend(n for n in sorted(sub.nodes) if n not in order)
            return order

        default = entry_order()
        assert default.index("lib.other.unrelated") < default.index("lib.other.reached")
        let_in = entry_order(include_tests=True)
        assert let_in.index("lib.other.reached") < let_in.index("lib.other.unrelated")

    @pytest.mark.parametrize("fmt", FORMATS)
    def test_stats_count_them_apart_and_the_counts_add_up(self, fmt):
        G, scores = _with_tests_and_imports()
        _t, _sub, stats = build_context(G, scores, 50_000, min_score=0.0, format=fmt)
        # An import in a test file counts once, as a test.
        assert stats["nodes_excluded_tests"] == 2
        assert stats["nodes_excluded_imports"] == 1
        assert (stats["nodes_selected"] + stats["nodes_over_budget"]
                + stats["nodes_below_min_score"] + 3) == stats["nodes_total"] == 7

    def test_the_keys_are_absent_when_nothing_is_left_out(self):
        _t, _sub, stats = build_context(_line(5), _decreasing(_line(5)), 50_000)
        assert "nodes_excluded_tests" not in stats
        assert "nodes_excluded_imports" not in stats

    def test_the_tip_names_them_and_fits_the_budget(self):
        G, scores = _with_tests_and_imports()
        text, _sub, stats = build_context(G, scores, 50_000, min_score=0.0, query="select")
        assert ("💡 2 test nodes and 1 import node are left out by default — "
                "--include-tests / --include-imports to let them compete") in text
        tight = stats["tokens_used"] - 1   # one node fewer: the tip must still fit
        text, _sub, stats = build_context(G, scores, tight, min_score=0.0, query="select")
        assert count_tokens(text) <= tight and "left out by default" in text

    def test_the_whole_graph_baseline_holds_every_node(self):
        G, scores = _with_tests_and_imports()
        everything = build_context(G, scores, 10_000_000, min_score=0.0, include_tests=True,
                                   include_imports=True)[2]["tokens_used"]
        assert full_graph_tokens(G, scores) == everything

    def test_fit_to_budget_rebuilds_the_breakdown_from_stats(self):
        # The CLI's --inject-code trims again after build_context: the stats
        # it prints must keep every cause, the left-out kinds included.
        G, scores = _with_tests_and_imports()
        _t, sub, stats = build_context(G, scores, 50_000, min_score=0.0, query="q")
        left_out = left_out_from_stats(stats)
        assert left_out == (4, 0.0, 2, 1)

        def render(s, st):
            return format_subgraph(s, st, "markdown", scores=scores, query="q")

        values = {("n", n): scores[n] for n in sub.nodes}
        _t2, _sub2, again = fit_to_budget(sub, 50_000, render, values, 7, left_out=left_out)
        for key in ("nodes_over_budget", "nodes_below_min_score", "nodes_excluded_tests",
                    "nodes_excluded_imports"):
            assert again[key] == stats[key]

    def test_hand_built_stats_have_no_breakdown_to_rebuild(self):
        assert left_out_from_stats({"nodes_selected": 1, "nodes_total": 2}) is None


# ---------------------------------------------------------------------------
# A node passes its boost to at most _BOOST_FANOUT neighbours (v1.2, #64/#65)
# ---------------------------------------------------------------------------

def _hub_and_relevant(spokes: int = 30) -> tuple[nx.DiGraph, dict[str, float]]:
    """A hub scored high with many weak spokes, and an unrelated relevant node.

    The shape measured on PrismaStats: createAdminClient (0.839, 97 callers)
    lent 0.587 to every caller, above useChatContext (0.510), and the callers
    filled the budget before it.
    """
    G = nx.DiGraph()
    G.add_node("hub", label="hub", type="function", source_file="lib/hub.py")
    G.add_node("relevant", label="relevant", type="function", source_file="lib/r.py")
    scores = {"hub": 0.84, "relevant": 0.51}
    for i in range(spokes):
        G.add_node(f"s{i:02d}", label=f"spoke{i}", type="function", source_file=f"app/s{i}.py")
        G.add_edge(f"s{i:02d}", "hub", relation="calls")
        scores[f"s{i:02d}"] = round(0.30 + i * 0.001, 3)   # s29 is the most relevant
    return G, scores


def _entry_order(G: nx.DiGraph, scores: dict[str, float]) -> list[str]:
    """Nodes in the order a growing budget lets them in."""
    order: list[str] = []
    for budget in range(100, 6000, 10):
        try:
            _t, sub, _s = build_context(G, scores, budget, min_score=0.0)
        except BudgetTooSmallError:
            continue
        order.extend(n for n in sorted(sub.nodes, key=lambda n: (-scores[n], n))
                     if n not in order)
        if len(order) == G.number_of_nodes():
            break
    return order


class TestBoostFanout:
    def test_the_fanout_is_ten(self):
        assert budget_mod._BOOST_FANOUT == 10

    def test_a_hub_lends_its_boost_to_ten_neighbours_only(self):
        G, scores = _hub_and_relevant(spokes=30)
        order = _entry_order(G, scores)
        # The hub, then the ten spokes it boosts to 0.84 * 0.7 = 0.588, then
        # the relevant node at 0.51 — not after all thirty spokes.
        assert order[0] == "hub"
        assert order.index("relevant") == 11

    def test_the_heirs_are_the_neighbours_with_the_highest_own_score(self):
        G, scores = _hub_and_relevant(spokes=30)
        order = _entry_order(G, scores)
        assert set(order[1:11]) == {f"s{i:02d}" for i in range(20, 30)}

    def test_ties_on_own_score_go_by_id(self):
        G, scores = _hub_and_relevant(spokes=12)
        for i in range(12):
            scores[f"s{i:02d}"] = 0.3
        order = _entry_order(G, scores)
        assert set(order[1:11]) == {f"s{i:02d}" for i in range(10)}

    def test_a_node_with_few_neighbours_boosts_them_all(self):
        G, scores = _hub_and_relevant(spokes=8)
        order = _entry_order(G, scores)
        assert order.index("relevant") == 9

    def test_left_out_neighbours_take_no_place_among_the_heirs(self):
        # Ten tests around the hub must not use up its ten boosts.
        G, scores = _hub_and_relevant(spokes=12)
        for i in range(10):
            G.add_node(f"t{i}", label=f"test_hub_{i}", type="function",
                       source_file="tests/test_hub.py")
            G.add_edge(f"t{i}", "hub", relation="calls")
            scores[f"t{i}"] = 0.9
        order = _entry_order(G, scores)
        assert set(order[1:11]) == {f"s{i:02d}" for i in range(2, 12)}
