"""The v1.4 ranking study's experimental scorer (docs/design/orden-v1.4/). Not part of slurp."""

import runpy
from pathlib import Path

import networkx as nx
import pytest

from slurp.scorer import score_nodes

DESIGN = Path(__file__).resolve().parent.parent / "docs" / "design" / "orden-v1.4"
FIXTURE = Path(__file__).parent / "fixtures" / "graphify_project" / "graphify-out" / "graph.json"


@pytest.fixture(scope="module")
def V():
    return runpy.run_path(str(DESIGN / "variants.py"))


def _code_graph():
    """A small slurp-index-like graph: modules, functions, a class, calls and contains."""
    G = nx.DiGraph()
    nodes = [
        ("src.router.trie.Node", "Node", "class", "src/router/trie.ts", 3),
        ("src.router.trie.Node.insert", "insert", "function", "src/router/trie.ts", 10),
        ("src.router.trie.Node.search", "search", "function", "src/router/trie.ts", 30),
        ("src.router.trie", "trie.ts", "module", "src/router/trie.ts", 1),
        ("src.adapter.lambda.handle", "handle", "function", "src/adapter/lambda.ts", 5),
        ("src.adapter.lambda.createRequest", "createRequest", "function",
         "src/adapter/lambda.ts", 40),
        ("src.adapter.lambda.headers", "parseHeaders", "function", "src/adapter/lambda.ts", 60),
        ("src.adapter.lambda", "lambda.ts", "module", "src/adapter/lambda.ts", 1),
    ]
    for nid, label, typ, path, line in nodes:
        G.add_node(nid, label=label, type=typ, description="", source_file=path,
                   source_location=f"L{line}", file_type="code")
    for src, dst, rel in [("src.router.trie", "src.router.trie.Node", "contains"),
                          ("src.router.trie.Node", "src.router.trie.Node.insert", "contains"),
                          ("src.router.trie.Node", "src.router.trie.Node.search", "contains"),
                          ("src.adapter.lambda", "src.adapter.lambda.handle", "contains"),
                          ("src.adapter.lambda", "src.adapter.lambda.createRequest", "contains"),
                          ("src.adapter.lambda", "src.adapter.lambda.headers", "contains"),
                          ("src.adapter.lambda.handle", "src.adapter.lambda.createRequest",
                           "calls"),
                          ("src.adapter.lambda.createRequest", "src.adapter.lambda.headers",
                           "calls")]:
        G.add_edge(src, dst, relation=rel)
    return G


class TestBaselineIsV13:
    @pytest.mark.parametrize("query", ["insert into the trie", "handle multi value headers",
                                       "lambda request", "nothing matches here"])
    def test_identical_to_score_nodes(self, V, query):
        G = _code_graph()
        assert V["score"](G, query, V["BASE"], V["Index"](G)) == score_nodes(G, query)

    def test_identical_on_a_real_graph(self, V, real_graphify_json):
        from slurp.loader import load_graph
        G = load_graph(Path(real_graphify_json))
        for query in ("authentication flow", "parse the configuration file"):
            assert V["score"](G, query, V["BASE"], V["Index"](G)) == score_nodes(G, query)


class TestVariants:
    def _lex(self, V, G, query, **cfg):
        c = {**V["BASE"], **cfg}
        return V["lexical"](G, query, c, V["Index"](G))

    def test_path_terms_reach_a_node_whose_name_shares_nothing(self, V):
        G = _code_graph()
        assert self._lex(V, G, "router quantifiers")["src.router.trie.Node.insert"] == 0
        assert self._lex(V, G, "router quantifiers", w_path=0.5)[
            "src.router.trie.Node.insert"] > 0

    def test_stemming(self, V):
        G = _code_graph()
        assert self._lex(V, G, "header lookup")["src.adapter.lambda.headers"] == 0
        assert self._lex(V, G, "header lookup", stem=True)["src.adapter.lambda.headers"] > 0

    def test_bm25_is_normalised_and_reads_docs(self, V):
        G = _code_graph()
        G.nodes["src.adapter.lambda.handle"]["doc"] = "Adapts an AWS Lambda event into a request"
        lex = self._lex(V, G, "aws event", lex="bm25", k1=1.2, b=0.75, w_name=2, doc=True)
        assert max(lex.values()) == pytest.approx(1.0)
        assert lex["src.adapter.lambda.handle"] == pytest.approx(1.0)
        assert self._lex(V, G, "aws event", lex="bm25", k1=1.2, b=0.75, w_name=2,
                         doc=False)["src.adapter.lambda.handle"] == 0

    def test_stop_words_are_generic_english_only(self, V):
        stop = V["STOP_EN"]
        assert {"the", "of", "when", "with"} <= stop
        assert not ({"add", "fix", "support", "handle", "allow", "use", "make"} & stop)
        assert V["query_terms"]("handle the headers", {**V["BASE"], "stop": True}) == [
            "handle", "headers"]

    def test_cohesion_lifts_file_siblings(self, V):
        G = _code_graph()
        base = self._lex(V, G, "create request")
        lifted = self._lex(V, G, "create request", cohesion=0.5)
        assert base["src.adapter.lambda.headers"] == 0
        assert lifted["src.adapter.lambda.headers"] == pytest.approx(
            0.5 * base["src.adapter.lambda.createRequest"])

    def test_ppr_follows_calls_from_the_matches(self, V):
        G = _code_graph()
        cfg = {**V["BASE"], "struct": "ppr", "restart": 0.3, "r": 0.5, "k": 10}
        s = V["structural"](G, V["lexical"](G, "handle", cfg, V["Index"](G)), cfg, V["Index"](G))
        assert s["src.adapter.lambda.createRequest"] > s["src.router.trie.Node.insert"]

    def test_ppr_without_a_match_is_the_global_pagerank(self, V):
        G = _code_graph()
        cfg = {**V["BASE"], "struct": "ppr", "restart": 0.3, "r": 0.5, "k": 10}
        idx = V["Index"](G)
        lex = V["lexical"](G, "zzz", cfg, idx)
        assert V["structural"](G, lex, cfg, idx) == V["structural"](G, lex, V["BASE"], idx)


class TestDocstrings:
    def test_block_go_rust_and_python(self, V):
        above = V["doc_comment_above"]
        ts = ["/**", " * Parses headers.", " * @param e the event", " */", "@Deco()",
              "export function f() {}"]
        assert above(ts, 6, "block") == "Parses headers.\n@param e the event"
        go = ["// Checkout charges the cart.", "//go:noinline", "func Checkout() {}"]
        assert above(go, 3, "go") == "Checkout charges the cart."
        rs = ["/// Runs the engine.", "#[inline]", "pub fn run() {}"]
        assert above(rs, 3, "rust") == "Runs the engine."
        assert above(["// plain", "", "func F() {}"], 3, "go") == ""
        assert V["python_docstrings"]('def f():\n    """Doc of f."""\n    return 1\n') == {
            1: "Doc of f."}

    def test_attach_and_print_the_first_line_only_when_asked(self, V):
        G = nx.DiGraph()
        G.add_node("m.f", label="f", type="function", description="", source_file="m.py",
                   source_location="L1")
        src = {"m.py": 'def f():\n    """Line one.\n\n    More."""\n'}
        assert V["attach_docs"](G, src.get) == 1
        assert G.nodes["m.f"]["doc"] == "Line one.\n\nMore."
        assert G.nodes["m.f"]["description"] == ""
        V["print_docs"](G, True)
        assert G.nodes["m.f"]["description"] == "Line one."
        V["print_docs"](G, False)
        assert G.nodes["m.f"]["description"] == ""


# --- select_v14.py: the decisions fixed in the design ------------------------------

@pytest.fixture(scope="module")
def S():
    return runpy.run_path(str(DESIGN / "select_v14.py"))


def _means(**by_cfg):
    """{cfg: {project: mean recall}} from keyword lists over projects p0..p6."""
    return {cfg: {f"p{i}": v for i, v in enumerate(vals)} for cfg, vals in by_cfg.items()}


class TestSelection:
    def test_macro(self, S):
        m = _means(base=[0.5] * 7)
        assert S["macro"](m["base"], [f"p{i}" for i in range(7)]) == pytest.approx(0.5)

    def test_a_variant_enters_with_one_point_and_no_project_losing_three(self, S):
        projects = [f"p{i}" for i in range(7)]
        m = _means(cur=[0.5] * 7, good=[0.52] * 7, small=[0.505] * 7,
                   hurts=[0.6] * 6 + [0.46])
        pick = S["best_addition"](m, "cur", ["good", "small", "hurts"], projects)
        assert pick == "good"
        assert S["best_addition"](m, "cur", ["small", "hurts"], projects) is None

    def test_gate(self, S):
        projects = [f"p{i}" for i in range(7)]
        m = _means(base=[0.5] * 7, cand=[0.56] * 7, low=[0.54] * 7,
                   loses=[0.62] * 6 + [0.46])
        assert S["gate"](m, "cand", projects)["passed"]
        assert not S["gate"](m, "low", projects)["passed"]
        assert not S["gate"](m, "loses", projects)["passed"]

    def test_v6_stays_out_if_any_labelled_question_drops(self, S):
        without = {"q1": 1.0, "q2": 0.5, "q3": 0.0}
        assert S["v6_allowed"](without, {"q1": 1.0, "q2": 0.5, "q3": 1.0})
        assert not S["v6_allowed"](without, {"q1": 1.0, "q2": 0.0, "q3": 1.0})

    def test_docstring_rule(self, S):
        assert S["print_docstring"](r_printed=0.600, r_scoring_only=0.609)       # 0.009 < 0.01
        assert not S["print_docstring"](r_printed=0.600, r_scoring_only=0.610)
        assert S["print_docstring"](r_printed=0.620, r_scoring_only=0.600)

    def test_validation_acceptance(self, S):
        projects = [f"p{i}" for i in range(6)]
        m = {"base": {p: 0.5 for p in projects}, "cand": {p: 0.56 for p in projects}}
        refs_ok = {"prismastats": (0.967, 0.933), "slurp": (0.875, 0.8125)}
        res = S["accept"](m, "cand", projects, refs_ok, latency_ok=True)
        assert res["passed"]
        refs_bad = {"prismastats": (0.967, 0.89), "slurp": (0.875, 0.875)}
        assert not S["accept"](m, "cand", projects, refs_bad, latency_ok=True)["passed"]
        assert not S["accept"](m, "cand", projects, refs_ok, latency_ok=False)["passed"]


class TestGrids:
    @pytest.fixture(scope="class")
    def R(self):
        return runpy.run_path(str(DESIGN / "run_v14.py"))

    def test_the_approved_grids(self, R):
        F = R["FAMILIES"]
        assert {k: len(v) for k, v in F.items()} == {"V1": 12, "V2": 12, "V3": 3, "V4": 3,
                                                     "V5": 1, "V6": 1, "V1-0": 6, "PRINT": 1}
        assert {(c["k1"], c["b"], c["w_name"]) for c in F["V1"]} == {
            (k1, b, wn) for k1 in (0.9, 1.2, 1.5) for b in (0.5, 0.75) for wn in (2.0, 3.0)}
        assert {(c["restart"], c["r"], c["k"]) for c in F["V2"]} == {
            (a, r, k) for a in (0.15, 0.3, 0.5) for r in (0.5, 1.0) for k in (10, 30)}
        assert [c["w_path"] for c in F["V3"]] == [0.25, 0.5, 1.0]
        assert [c["cohesion"] for c in F["V4"]] == [0.3, 0.5, 0.7]

    def test_tokens_compose(self, R, V):
        cfg = R["cfg_of"](["V1#0", "V3#1", "V6#0"], V["BASE"])
        assert (cfg["lex"], cfg["doc"], cfg["k1"], cfg["w_path"], cfg["stop"]) == (
            "bm25", True, 0.9, 0.5, True)
        assert R["cfg_of"]([], V["BASE"]) == V["BASE"]


class TestPushOrder:
    def test_local_push_is_deterministic_and_sums_to_one(self, V):
        G = _code_graph()
        cfg = {**V["BASE"], "struct": "ppr", "restart": 0.15, "r": 1.0, "k": 10}
        idx = V["Index"](G)
        lex = V["lexical"](G, "handle create request", cfg, idx)
        a = V["structural"](G, lex, cfg, idx)
        b = V["structural"](G, lex, cfg, V["Index"](G))
        assert a == b and max(a.values()) == pytest.approx(1.0, abs=1e-5)
