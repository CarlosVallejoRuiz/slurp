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
