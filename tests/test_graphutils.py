"""Tests for slurp/_graphutils.py — the helpers shared by modules that read a graph."""

import networkx as nx
import pytest

from slurp._graphutils import (
    _is_import,
    _is_test,
    _left_out,
    _left_out_sets,
    _split_callers,
)


def _graph_with(path: str, node_id: str = "node", key: str = "source_file") -> nx.DiGraph:
    G = nx.DiGraph()
    G.add_node(node_id, label=node_id.rsplit(".", 1)[-1], type="function", **{key: path})
    return G


class TestIsTest:
    """Test code is recognised by the conventions test runners collect by."""

    @pytest.mark.parametrize("path", [
        # Test directories, in any language and at any depth.
        "tests/helpers.py",
        "test/fixtures.rb",
        "pkg/tests/util.py",
        "src/__tests__/Button.tsx",
        "spec/models/user.rb",
        "src/test/java/com/acme/Fixtures.java",       # Maven / Gradle
        "Tests/AppTests/Helpers.swift",               # SwiftPM
        "MyApp.Tests/Builders.cs",                    # .NET test project
        "MyApp.UnitTests/Builders.cs",
        # Python: pytest, unittest, Django
        "test_core.py",
        "lib/core_test.py",
        "conftest.py",
        "polls/tests.py",
        # JavaScript / TypeScript: Jest, Vitest, Mocha, Jasmine
        "src/button.test.tsx",
        "src/button.spec.ts",
        "lib/parse.test.js",
        "api/client.spec.mjs",
        # Go
        "server/handler_test.go",
        # Java, Kotlin, Scala: JUnit / Surefire, ScalaTest, specs2
        "UserServiceTest.java",
        "UserServiceTests.java",
        "TestUserService.java",
        "UserServiceTestCase.java",
        "UserRepositoryTest.kt",
        "ParserSpec.scala",
        "ParserSuite.scala",
        # C#, PHP, Swift
        "UserServiceTests.cs",
        "UserTest.php",
        "LoginTests.swift",
        # Ruby, Elixir, Lua
        "user_spec.rb",
        "user_test.rb",
        "my_app_test.exs",
        "parser_spec.lua",
        # C / C++: GoogleTest, Chromium, plain
        "parser_test.cc",
        "parser_unittest.cpp",
        "test_parser.c",
        # PowerShell: Pester
        "Get-Thing.Tests.ps1",
        # Windows separators
        "tests\\test_core.py",
    ])
    def test_test_code(self, path):
        assert _is_test(_graph_with(path), "node")

    @pytest.mark.parametrize("path", [
        "latest_results.py",              # the v1.1.1 bug: "test_" inside "latest_results"
        "scripts/latest_results.py",
        "greatest_hits.py",
        "contests/entry.py",              # "tests/" inside "contests/"
        "lib/respec/parser.py",           # "spec/" inside "respec/"
        "inspector/spec_utils.py",
        "attestation.py",
        "components/testimonials.tsx",
        "src/contest.ts",
        "scripts/test-hardware-upload.ts",  # a script: no runner collects it
        "assets/test-corpus/Root.tsx",
        "Contest.java",
        "Testimonial.java",
        "Latest.kt",
        "server/protest.go",
        "testdata/fixture.go",            # Go fixtures, never compiled as tests
        "lib/core.py",
    ])
    def test_production_code(self, path):
        assert not _is_test(_graph_with(path), "node")

    def test_a_test_named_function_in_production_code_is_production(self):
        # pytest collects test_* functions only from test files.
        assert not _is_test(_graph_with("lib/db.py", "lib.db.test_connection"),
                            "lib.db.test_connection")

    def test_rust_unit_tests_live_in_a_tests_module(self):
        G = _graph_with("src/lib.rs", "src.lib.tests.it_adds")
        assert _is_test(G, "src.lib.tests.it_adds")
        assert not _is_test(_graph_with("src/lib.rs", "src.lib.add"), "src.lib.add")
        assert not _is_test(_graph_with("src/contest.rs", "src.contest.run"), "src.contest.run")

    def test_the_tests_module_rule_is_rust_only(self):
        # A Python module named `tests` inside a package is caught by its path.
        assert not _is_test(_graph_with("lib/core.py", "lib.core.tests_helper"),
                            "lib.core.tests_helper")

    def test_file_path_is_read_when_source_file_is_absent(self):
        assert _is_test(_graph_with("tests/test_core.py", key="file_path"), "node")
        assert not _is_test(_graph_with("lib/core.py", key="file_path"), "node")

    def test_a_node_without_a_file_is_not_a_test(self):
        G = nx.DiGraph()
        G.add_node("test_orphan", label="test_orphan")
        assert not _is_test(G, "test_orphan")
        assert not _is_test(G, "not_in_graph")


class TestSplitCallers:
    def test_latest_results_counts_as_production(self):
        G = nx.DiGraph()
        G.add_node("target", source_file="lib/core.py")
        G.add_node("report", source_file="scripts/latest_results.py")
        G.add_node("t", source_file="tests/test_core.py")
        G.add_edge("report", "target", relation="calls")
        G.add_edge("t", "target", relation="calls")
        assert _split_callers(G, "target") == (["report"], ["t"])

    def test_an_import_node_is_never_a_caller(self):
        # slurp index writes one `import` node per import statement; it calls
        # nothing, and where a graph says otherwise it is not a dependant.
        G = nx.DiGraph()
        G.add_node("target", type="function", source_file="lib/core.py")
        G.add_node("app.import_target", type="import", source_file="app.py")
        G.add_node("app.main", type="function", source_file="app.py")
        G.add_edge("app.import_target", "target", relation="calls")
        G.add_edge("app.main", "target", relation="calls")
        assert _split_callers(G, "target") == (["app.main"], [])

    def test_an_import_node_has_no_dependants(self):
        # The module that declares an import owns it, as it owns its functions.
        G = nx.DiGraph()
        G.add_node("app", type="module", source_file="app.py")
        G.add_node("app.import_os", type="import", label="os", source_file="app.py")
        G.add_edge("app", "app.import_os", relation="imports_from")
        assert _split_callers(G, "app.import_os") == ([], [])

    def test_graphify_import_edges_between_real_nodes_still_count(self):
        G = nx.DiGraph()
        G.add_node("a", source_file="a.ts")
        G.add_node("b", source_file="b.ts")
        G.add_edge("a", "b", relation="imports_from")
        assert _split_callers(G, "b") == (["a"], [])


def _mixed_graph() -> nx.DiGraph:
    G = nx.DiGraph()
    G.add_node("lib.core.run", type="function", source_file="lib/core.py")
    G.add_node("lib.core.import_os", type="import", source_file="lib/core.py")
    G.add_node("tests.test_core.test_run", type="function", source_file="tests/test_core.py")
    G.add_node("tests.test_core.import_run", type="import", source_file="tests/test_core.py")
    G.add_node("concept", file_type="concept")          # graphify: no type at all
    return G


class TestIsImport:
    def test_only_the_import_type(self):
        G = _mixed_graph()
        assert _is_import(G, "lib.core.import_os")
        assert not _is_import(G, "lib.core.run")
        assert not _is_import(G, "concept")
        assert not _is_import(G, "missing")


class TestLeftOut:
    """The nodes that do not compete for a place in a subgraph by default."""

    def test_tests_and_imports_by_default(self):
        G = _mixed_graph()
        assert _left_out(G) == {"lib.core.import_os", "tests.test_core.test_run",
                                "tests.test_core.import_run"}

    def test_each_flag_lets_its_kind_back_in(self):
        G = _mixed_graph()
        # An import in a test file is both: it stays out until both are let in.
        assert _left_out(G, include_tests=True) == {"lib.core.import_os",
                                                    "tests.test_core.import_run"}
        assert _left_out(G, include_imports=True) == {"tests.test_core.test_run",
                                                      "tests.test_core.import_run"}
        assert _left_out(G, include_tests=True, include_imports=True) == set()

    def test_the_kinds_are_computed_once_per_graph(self):
        G = _mixed_graph()
        first = _left_out_sets(G)
        assert _left_out_sets(G) is first
        tests, imports = first
        assert tests == {"tests.test_core.test_run", "tests.test_core.import_run"}
        assert imports == {"lib.core.import_os", "tests.test_core.import_run"}

    def test_a_new_graph_is_never_served_a_dead_graphs_kinds(self):
        tests, _ = _left_out_sets(_mixed_graph())
        G = nx.DiGraph()
        G.add_node("lib.core.run", type="function", source_file="lib/core.py")
        assert _left_out_sets(G) == (frozenset(), frozenset())
        assert tests  # the first graph did have tests

    def test_an_empty_graph(self):
        assert _left_out(nx.DiGraph()) == set()
