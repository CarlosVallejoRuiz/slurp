"""Tests for slurp/_graphutils.py — the helpers shared by modules that read a graph."""

import networkx as nx
import pytest

from slurp._graphutils import _is_test, _split_callers


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
