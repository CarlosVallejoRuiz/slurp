"""The optimal-budget study's scripts (docs/design/presupuesto-optimo/). Not part of slurp."""

import runpy
from pathlib import Path

import networkx as nx
import pytest

DESIGN = Path(__file__).resolve().parent.parent / "docs" / "design" / "presupuesto-optimo"


@pytest.fixture(scope="module")
def Q():
    return runpy.run_path(str(DESIGN / "questions.py"))


def _commit(Q, files, churn=None, author="Ann ann@x.org", subject="parser: handle empty input"):
    c = Q["Commit"]("a" * 40, "b" * 40, author, subject)
    c.files.update(files)
    c.churn.update(churn or {p: 10 for p in files})
    return c


def _graph(*nodes):
    G = nx.DiGraph()
    for nid, typ, path, line in nodes:
        G.add_node(nid, type=typ, source_file=path, source_location=f"L{line}", label=nid)
    return G


class TestCleanSubject:
    @pytest.mark.parametrize("subject,question", [
        ("feat(router): support regexp quantifiers (#2209)", "router: support regexp quantifiers"),
        ("searcher: avoid an allocation when reading lines", "searcher: avoid an allocation when reading lines"),
        ("[3.x] Fix bind handling in the URL parser, fixes #123", "Fix bind handling in the URL parser"),
        ("fix: keep ref.current value during lifecycle", "keep ref.current value during lifecycle"),
    ])
    def test_kept_and_cleaned(self, Q, subject, question):
        assert Q["clean_subject"](subject) == (question, "")

    @pytest.mark.parametrize("subject,reason", [
        ("docs: explain the router", "type:docs"),
        ("chore(deps): update everything", "type:chore"),
        ("fix", "vague"),
        ("Fix bug", "vague"),
        ("update some stuff", "vague"),
        ("timing: add test for middleware timing", "keyword"),
        ("fix mypy complain in main", "keyword"),
        ("save coverage database files", "keyword"),
        ("Bump version to 1.2.0", "keyword"),
        ("Revert the parser change from last week", "keyword"),
    ])
    def test_rejected(self, Q, subject, reason):
        assert Q["clean_subject"](subject) == (None, reason)


class TestFiles:
    @pytest.mark.parametrize("path,source", [
        ("src/a.py", True), ("lib/x.ts", True), ("cmd/main.go", True),
        ("noxfile.py", False), ("setup.py", False), ("vite.config.ts", False),
        ("node_modules/pkg/index.js", False), ("README.md", False),
    ])
    def test_source(self, Q, path, source):
        assert Q["is_source"](path) is source

    @pytest.mark.parametrize("path,test", [
        ("tests/test_a.py", True), ("pkg/a_test.go", True), ("src/a.test.ts", True),
        ("src/a.go", False), ("lib/latest.py", False),
    ])
    def test_test_file(self, Q, path, test):
        assert Q["is_test_file"](path) is test


class TestStageA:
    def test_a_good_commit_passes(self, Q):
        c = _commit(Q, {"src/parser.py": "M", "tests/test_parser.py": "M"})
        assert Q["stage_a"](c) == ("parser: handle empty input", "")

    @pytest.mark.parametrize("files,churn,author,reason", [
        ({"src/a.py": "M"}, None, "dependabot[bot] x@y", "bot"),
        ({"tests/test_a.py": "M", "README.md": "M"}, None, None, "no modified source file"),
        ({"src/new.py": "A"}, None, None, "no modified source file"),
        ({f"src/m{i}.py": "M" for i in range(6)}, None, None, "too many source files"),
        ({"src/a.py": "M", "src/b.py": "R100", "src/c.py": "R090", "src/d.py": "R100"},
         None, None, "mass rename"),
        ({"src/a.py": "M"}, {"src/a.py": 400}, None, "too many changed lines"),
        ({f"docs/p{i}.md": "M" for i in range(20)} | {"src/a.py": "M"}, None, None,
         "too many files"),
    ])
    def test_rejections(self, Q, files, churn, author, reason):
        c = _commit(Q, files, churn, author=author or "Ann ann@x.org")
        assert Q["stage_a"](c)[1] == reason

    def test_root_commit(self, Q):
        c = _commit(Q, {"src/a.py": "M"})
        c.parent = ""
        assert Q["stage_a"](c)[1] == "root commit"


DIFF = """\
diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -7 +7 @@ class A:
-        self.x = x
+        self.x = x or 0
@@ -12,0 +13,2 @@ class A:
+    def more(self):
+        return 2
diff --git a/src/new.py b/src/new.py
--- /dev/null
+++ b/src/new.py
@@ -0,0 +1 @@
+x = 1
"""


class TestDiffToLines:
    def test_changed_lines_and_insertions(self, Q):
        assert Q["parse_diff"](DIFF) == {"src/a.py": {7, (12, 13)}}


PY = """\
class A:
    def __init__(
        self,
        x,
    ):
        self.y = 1
        self.x = x

    def other(self):
        return 1

    pass_through = True
"""

TS = """\
export const serve = (
  options: {
    root: string
  }
) => {
  return options.root
}

export function other() {
  return 1
}
"""

GO = """\
package shop

func Checkout(cart map[string]interface{}) string {
\tx := 1
\treturn "a"
}

func Other() {}
"""


class TestDefinitionsTouched:
    def _relevant(self, Q, G, path, text, touched):
        return Q["relevant_from"](G, {path: touched}, lambda p: text.splitlines())

    def test_black_signature_does_not_end_the_method(self, Q):
        G = _graph(("A", "class", "a.py", 1), ("A.__init__", "method", "a.py", 2),
                   ("A.other", "method", "a.py", 9))
        assert self._relevant(Q, G, "a.py", PY, {7}) == ["A.__init__"]

    def test_an_insertion_between_methods_belongs_to_the_class(self, Q):
        G = _graph(("A", "class", "a.py", 1), ("A.__init__", "method", "a.py", 2),
                   ("A.other", "method", "a.py", 9))
        assert self._relevant(Q, G, "a.py", PY, {(7, 8)}) == ["A"]

    def test_an_insertion_inside_a_body_belongs_to_it(self, Q):
        G = _graph(("A", "class", "a.py", 1), ("A.__init__", "method", "a.py", 2))
        assert self._relevant(Q, G, "a.py", PY, {(6, 7)}) == ["A.__init__"]

    def test_an_insertion_after_the_last_definition_belongs_to_none(self, Q):
        G = _graph(("f", "function", "b.py", 1))
        text = "def f():\n    return 1\n\nX = 2\n"
        assert self._relevant(Q, G, "b.py", text, {(2, 3)}) == []

    def test_module_level_lines_are_not_relevant(self, Q):
        G = _graph(("f", "function", "b.py", 1))
        assert self._relevant(Q, G, "b.py", "def f():\n    return 1\n\nX = 2\n", {4}) == []

    def test_a_typescript_parameter_typed_with_braces(self, Q):
        pytest.importorskip("tree_sitter_typescript")
        G = _graph(("serve", "function", "s.ts", 1), ("other", "function", "s.ts", 9))
        assert self._relevant(Q, G, "s.ts", TS, {6}) == ["serve"]

    def test_a_go_parameter_with_interface_braces(self, Q):
        G = _graph(("shop.Checkout", "function", "s.go", 3), ("shop.Other", "function", "s.go", 8))
        assert self._relevant(Q, G, "s.go", GO, {5}) == ["shop.Checkout"]

    def test_test_nodes_are_never_relevant(self, Q):
        G = _graph(("t.test_f", "function", "tests/test_b.py", 1))
        assert self._relevant(Q, G, "tests/test_b.py", "def test_f():\n    pass\n", {2}) == []


class TestCommentOnly:
    def test_python_docstrings_and_comments(self, Q):
        old = 'def f():\n    """Old."""\n    return 1  # one\n'
        new = 'def f():\n    """New words."""\n    # a comment\n    return 1\n'
        assert Q["code_signature"]("a.py", old) == Q["code_signature"]("a.py", new)

    def test_python_code_change(self, Q):
        assert Q["code_signature"]("a.py", "x = 1\n") != Q["code_signature"]("a.py", "x = 2\n")

    def test_typescript_comments(self, Q):
        pytest.importorskip("tree_sitter_typescript")
        old = "/** Old. */\nexport function f() { return 1 }\n"
        new = "/** New docs. */\nexport function f() {\n  // why\n  return 1\n}\n"
        assert Q["code_signature"]("a.ts", old) == Q["code_signature"]("a.ts", new)

    def test_go_comments(self, Q):
        assert Q["code_signature"]("a.go", "// a\nfunc F() {}\n") == \
            Q["code_signature"]("a.go", "/* b */\nfunc F() {}\n")


class TestQuestionMetadata:
    def test_names_a_symbol(self, Q):
        assert Q["names_a_symbol"]("Fix update_query_pairs with repeated key",
                                   ["update_query_pairs"])
        assert not Q["names_a_symbol"]("jsx: improve form attribute types",
                                       ["FormHTMLAttributes"])

    def test_the_order_is_seeded_per_project(self, Q):
        items = list(range(50))
        a = Q["seeded_order"](items, "hono", 12)
        assert a == Q["seeded_order"](items, "hono", 12)
        assert a != Q["seeded_order"](items, "tokio", 12)
        assert sorted(a) == items


# --- measure.py: the harness -----------------------------------------------------

FIXTURE = Path(__file__).parent / "fixtures" / "graphify_project" / "graphify-out" / "graph.json"


@pytest.fixture(scope="module")
def M():
    return runpy.run_path(str(DESIGN / "measure.py"))


class TestThresholds:
    def _scores(self):
        # A floor of 0.30 and a few nodes that share terms with the query.
        return [0.70, 0.66, 0.62, 0.50, 0.48, 0.47, 0.40, 0.31] + [0.30] * 20

    def test_relative_to_the_best(self, M):
        t = M["thresholds"](self._scores())
        assert t["C1:0.5"] == pytest.approx(0.35)
        assert t["C1:0.8"] == pytest.approx(0.56)

    def test_above_the_floor(self, M):
        t = M["thresholds"](self._scores())
        assert t["C2:0.1"] == pytest.approx(0.30 + 0.1 * 0.40)
        assert t["C2:0.4"] == pytest.approx(0.30 + 0.4 * 0.40)

    def test_sharp_drop_keeps_at_least_five(self, M):
        t = M["thresholds"](self._scores())
        # Drops: 0.04 0.04 0.12 0.02 0.01 0.07 0.09 0.01 ... over a range of 0.40.
        # The 0.12 drop is at i=3, before the first five: the first from i=5 on that
        # reaches 0.15 * 0.40 = 0.06 is after s_6 = 0.47.
        assert t["C3:0.15"] == pytest.approx(0.47)
        assert t["C3:0.3"] is None            # no drop of 0.12 from the fifth on

    def test_no_signal_no_cut_from_a_drop(self, M):
        t = M["thresholds"]([0.3] * 10)
        assert t["C3:0.05"] is None
        assert t["C1:0.5"] == pytest.approx(0.15)

    def test_every_combination_of_the_design(self, M):
        t = M["thresholds"](self._scores())
        assert len(t) == 7 + 6 + 5


class TestHarness:
    def _question(self, G):
        nodes = sorted(G.nodes)
        return {"id": "q1", "question": "authenticate user token", "relevant_nodes": nodes[:2],
                "names_symbol": False}

    def test_identical_to_run_retrieval_eval(self, M, sample_graph_json):
        from slurp.loader import load_graph
        G = load_graph(sample_graph_json)
        q = self._question(G)
        r = M["measure_question"](G, q, grid=[200, 400, 2000], control=None, cuts=False)
        assert M["verify"](G, q, r, budgets=[200, 400, 2000]) == []

    def test_identical_on_a_real_graph(self, M):
        from slurp.loader import load_graph
        G = load_graph(FIXTURE)
        q = {"id": "q2", "question": "parse the configuration file",
             "relevant_nodes": sorted(G.nodes)[5:8], "names_symbol": False}
        r = M["measure_question"](G, q, grid=[1000, 4000], control=None, cuts=False)
        assert M["verify"](G, q, r, budgets=[1000, 4000]) == []

    def test_a_cut_stays_under_its_ceiling_and_no_cut_is_the_base(self, M):
        from slurp.loader import load_graph
        G = load_graph(FIXTURE)
        q = {"id": "q3", "question": "parse the configuration file",
             "relevant_nodes": sorted(G.nodes)[5:8], "names_symbol": False}
        grid = [1000, 2000, 4000]
        r = M["measure_question"](G, q, grid=grid, control=None, cuts=True)
        for key, by_fmt in r["cut"].items():
            for fmt, row in by_fmt.items():
                for b, (_recall, tokens, _nodes) in row.items():
                    assert tokens <= int(b), (key, fmt, b)
        # C3 with no drop large enough does not cut: the base, exactly.
        nocut = [k for k, v in M["thresholds"]([0.3] * 5).items() if v is None]
        assert nocut and all(k.startswith("C3") for k in nocut)

    def test_the_record(self, M, sample_graph_json):
        from slurp.loader import load_graph
        G = load_graph(sample_graph_json)
        r = M["measure_question"](G, self._question(G), grid=[400], control=800, cuts=True)
        assert set(r["base"]) == {"markdown", "json"}
        assert set(r["base"]["markdown"]) == {"400", "800"}
        recall, tokens, nodes = r["base"]["markdown"]["400"]
        assert 0 <= recall <= 1 and 0 < tokens <= 400 and nodes >= 0
        assert len(r["cut"]) == 18 and set(r["cut"]["C1:0.5"]["json"]) == {"400"}
        assert r["present"] == 2 and r["graph_nodes"] == G.number_of_nodes()


class TestStudyRun:
    def test_the_groups_are_the_approved_ones(self):
        R = runpy.run_path(str(DESIGN / "run_study.py"))
        assert R["GROUPS"] == {
            "derivation": ["ky", "prismastats", "hono", "slurp", "jsoup", "caddy", "tokio",
                           "sqlalchemy"],
            "validation": ["gin", "click", "ripgrep", "excalidraw", "commons-lang",
                           "prometheus"]}
        assert sorted(R["ORDER"]) == sorted(R["GROUPS"]["derivation"] + R["GROUPS"]["validation"])
        assert R["WORKERS"] == 4 and R["N_QUESTIONS"] == 120


# --- analyze.py: the criterion ---------------------------------------------------

@pytest.fixture(scope="module")
def A():
    return runpy.run_path(str(DESIGN / "analyze.py"))


GRID = [1000, 1500, 2000, 3000, 4000, 6000, 8000, 12000, 16000, 24000, 32000]


def _rec(recalls: dict, tokens=None, cut=None, **extra):
    """A question record with markdown recall per budget (JSON the same)."""
    row = {str(b): [recalls.get(b, recalls[max(k for k in recalls if k <= b)]),
                    (tokens or {}).get(b, int(b * 0.98)), 10] for b in GRID + [64000]}
    rec = {"present": 1, "graph_nodes": 1000, "candidates": 400, "test_nodes": 300,
           "names_symbol": False, "base": {"markdown": row, "json": dict(row)}, "cut": cut or {}}
    rec.update(extra)
    return rec


class TestOptimalBudget:
    def test_smallest_budget_reaching_95_percent_of_the_maximum(self, A):
        recs = [_rec({1000: 0.0, 4000: 1.0})] * 19 + [_rec({1000: 0.0, 16000: 1.0})]
        # Mean recall: 0 below 4000, 0.95 from 4000, 1.0 from 16000.
        star = A["optimal"](A["curve"](recs, "markdown"))
        assert star["b_star"] == 4000 and star["r_max"] == 1.0 and not star["censored"]

    def test_censored_when_recall_still_grows_past_32000(self, A):
        recs = [_rec({1000: 0.5, 64000: 1.0})] * 10
        assert A["optimal"](A["curve"](recs, "markdown"))["censored"]

    def test_unscorable_questions_are_left_out(self, A):
        recs = [_rec({1000: 1.0}), {**_rec({1000: 0.0}), "present": 0}]
        assert A["curve"](recs, "markdown")[1000]["recall"] == 1.0

    def test_bootstrap_is_seeded(self, A):
        recs = [_rec({1000: 0.0, 2000: 1.0})] * 5 + [_rec({1000: 0.0, 8000: 1.0})] * 5
        a = A["bootstrap"](recs, "markdown", n=200)
        assert a == A["bootstrap"](recs, "markdown", n=200)
        assert a["p10"] <= a["median"] <= a["p90"]

    def test_round_up_to_the_grid(self, A):
        assert A["round_up"](3100) == 4000 and A["round_up"](4000) == 4000
        assert A["round_up"](10) == 1000 and A["round_up"](10 ** 6) == 32000

    def test_grid_steps(self, A):
        assert A["steps"](4000, 16000) == 4 and A["steps"](3000, 3000) == 0


class TestRule:
    def _cells(self, A, exponent=0.5):
        # B* = 40 * N^0.5, N from 400 to 90000: the power law the fit must find.
        out = {}
        for i, n in enumerate([400, 1600, 3600, 6400, 10000, 22500, 40000, 90000]):
            out[f"p{i}"] = {"features": {"N": n, "C": n / 2, "T": n * 60, "tau": 0.4,
                                         "lang": ["Python", "Go"][i % 2]},
                            "b_star": {"markdown": A["round_up"](40 * n ** exponent),
                                       "json": A["round_up"](80 * n ** exponent)},
                            "censored": {"markdown": False, "json": False}}
        return out

    def test_a_power_law_in_nodes_is_found(self, A):
        fit = A["fit_rule"](self._cells(A))
        assert fit["family"] in ("R1:N", "R1:C", "R1:T")
        rule = fit["rule"]
        assert rule({"N": 10000, "C": 5000, "T": 600000, "tau": 0.4, "lang": "Go"},
                    "markdown") in (4000, 6000)

    def test_a_flat_optimum_gives_a_constant(self, A):
        cells = self._cells(A)
        for c in cells.values():
            c["b_star"] = {"markdown": 4000, "json": 6000}
        fit = A["fit_rule"](cells)
        assert fit["family"] == "R0"
        assert fit["rule"]({"N": 1, "C": 1, "T": 1, "tau": 0, "lang": "Go"}, "markdown") == 4000

    def test_acceptance(self, A):
        # One validation cell: B* = 4000 with R = 0.95; the rule gives 3000 with R = 0.93.
        cell = {"curve": {3000: {"recall": 0.93}, 4000: {"recall": 0.95},
                          6000: {"recall": 0.96}}, "b_star": 4000}
        ok = A["accept"]([{**cell, "b_rule": 3000}])
        assert ok["passed"] is False            # mean loss 0.02 > 0.01
        ok = A["accept"]([{**cell, "b_rule": 4000}, {**cell, "b_rule": 6000}])
        assert ok["passed"] and ok["max_loss"] == 0 and ok["geo_cost"] == pytest.approx(
            (1.0 * 1.5) ** 0.5)

    def test_the_cost_limits(self, A):
        cell = {"curve": {4000: {"recall": 0.9}, 32000: {"recall": 0.9}}, "b_star": 4000}
        assert not A["accept"]([{**cell, "b_rule": 32000}])["passed"]     # 8x > 4x


class TestMethodGate:
    def test_stops_only_if_all_four_cells_are_more_than_two_steps_apart(self, A):
        far = {"prismastats": {"markdown": (3000, 16000), "json": (6000, 32000)},
               "slurp": {"markdown": (2000, 12000), "json": (3000, 16000)}}
        assert A["method_gate"](far)["stop"]
        near = {**far, "slurp": {"markdown": (8000, 12000), "json": (3000, 16000)}}
        assert not A["method_gate"](near)["stop"]


class TestCut:
    def test_the_eligible_cut_saving_the_most_is_chosen(self, A):
        # Two cells; ceiling 8000 gives 1000 tokens. "a" loses nothing and saves 50%,
        # "b" saves 80% but loses 0.05 in one cell.
        def cell(r_a, t_a, r_b, t_b):
            return {"b_star_recall": 0.9, "ceiling": 8000, "ref_tokens": 1000,
                    "cut": {"a": {"recall": r_a, "tokens": t_a},
                            "b": {"recall": r_b, "tokens": t_b}}}
        cells = [cell(0.9, 500, 0.85, 200), cell(0.9, 500, 0.9, 200)]
        pick = A["choose_cut"](cells)
        assert pick["key"] == "a" and pick["savings"] == pytest.approx(0.5)
        res = A["accept_cut"](cells, "a")
        assert res["passed"] and res["mean_savings"] == pytest.approx(0.5)
        assert not A["accept_cut"](cells, "b")["passed"]


class TestLlmCheck:
    @pytest.fixture(scope="class")
    def L(self):
        return runpy.run_path(str(DESIGN / "llm_check.py"))

    def test_never_calls_claude_without_launch(self, L):
        with pytest.raises(SystemExit, match="--launch"):
            L["run"](False)

    def test_budget_multiples(self, L):
        assert [L["_round_up"](4000 * m) for m in (0.5, 1, 2, 4)] == [2000, 4000, 8000, 16000]
        assert L["_round_up"](16000 * 4) == 64000 and L["_round_up"](300) == 1000


class TestFitSet:
    def test_projects_under_40_questions_do_not_enter_the_fit(self, A):
        counts = {"ky": 120, "prismastats": 68, "hono": 120, "slurp": 38, "jsoup": 120,
                  "caddy": 120, "tokio": 120, "sqlalchemy": 120}
        assert A["fit_set"](counts) == ["ky", "prismastats", "hono", "jsoup", "caddy", "tokio",
                                        "sqlalchemy"]
