"""Tests for slurp/ignore.py."""

import networkx as nx
import pytest

from slurp.ignore import SlurpIgnore, apply_ignore, load_ignore


# ---------------------------------------------------------------------------
# SlurpIgnore.should_ignore — type rules
# ---------------------------------------------------------------------------


class TestTypeRules:
    def test_matches_type_attribute(self):
        ig = SlurpIgnore(type_rules=["document"])
        assert ig.should_ignore({"type": "document"}, "n")

    def test_matches_file_type_attribute(self):
        ig = SlurpIgnore(type_rules=["config"])
        assert ig.should_ignore({"file_type": "config"}, "n")

    def test_type_takes_priority_over_file_type(self):
        # When both "type" and "file_type" exist, "type" wins.
        ig = SlurpIgnore(type_rules=["document"])
        assert ig.should_ignore({"type": "document", "file_type": "code"}, "n")

    def test_no_match_different_type(self):
        ig = SlurpIgnore(type_rules=["document"])
        assert not ig.should_ignore({"type": "code"}, "n")

    def test_no_match_empty_type(self):
        ig = SlurpIgnore(type_rules=["document"])
        assert not ig.should_ignore({}, "n")

    def test_multiple_type_rules(self):
        ig = SlurpIgnore(type_rules=["document", "config"])
        assert ig.should_ignore({"type": "document"}, "n1")
        assert ig.should_ignore({"type": "config"}, "n2")
        assert not ig.should_ignore({"type": "code"}, "n3")


# ---------------------------------------------------------------------------
# SlurpIgnore.should_ignore — file rules
# ---------------------------------------------------------------------------


class TestFileRules:
    def test_matches_bare_extension_glob(self):
        ig = SlurpIgnore(file_rules=["*.md"])
        assert ig.should_ignore({"file_path": "README.md"}, "n")

    def test_matches_filename_within_nested_path(self):
        # "*.md" should match the filename even inside a subdirectory.
        ig = SlurpIgnore(file_rules=["*.md"])
        assert ig.should_ignore({"file_path": "docs/CHANGELOG.md"}, "n")

    def test_matches_directory_prefix_glob(self):
        ig = SlurpIgnore(file_rules=["node_modules/*"])
        assert ig.should_ignore({"file_path": "node_modules/lodash/index.js"}, "n")

    def test_no_match_different_extension(self):
        ig = SlurpIgnore(file_rules=["*.md"])
        assert not ig.should_ignore({"file_path": "src/auth.py"}, "n")

    def test_uses_source_file_attribute_as_fallback(self):
        ig = SlurpIgnore(file_rules=["*.md"])
        assert ig.should_ignore({"source_file": "README.md"}, "n")

    def test_no_file_path_attribute_skips_rule(self):
        ig = SlurpIgnore(file_rules=["*.md"])
        assert not ig.should_ignore({"type": "code"}, "n")

    def test_multiple_file_rules(self):
        ig = SlurpIgnore(file_rules=["*.md", "*.txt"])
        assert ig.should_ignore({"file_path": "notes.txt"}, "n")
        assert ig.should_ignore({"file_path": "README.md"}, "n")
        assert not ig.should_ignore({"file_path": "main.py"}, "n")


# ---------------------------------------------------------------------------
# SlurpIgnore.should_ignore — id rules
# ---------------------------------------------------------------------------


class TestIdRules:
    def test_matches_exact_id(self):
        ig = SlurpIgnore(id_rules=["deprecated_func"])
        assert ig.should_ignore({}, "deprecated_func")

    def test_matches_glob_prefix(self):
        ig = SlurpIgnore(id_rules=["tmp_*"])
        assert ig.should_ignore({}, "tmp_cache")
        assert ig.should_ignore({}, "tmp_helper")
        assert not ig.should_ignore({}, "main_func")

    def test_matches_glob_suffix(self):
        ig = SlurpIgnore(id_rules=["*_test"])
        assert ig.should_ignore({}, "auth_test")
        assert not ig.should_ignore({}, "auth_service")

    def test_empty_node_id_no_match(self):
        ig = SlurpIgnore(id_rules=["tmp_*"])
        assert not ig.should_ignore({}, "")


# ---------------------------------------------------------------------------
# SlurpIgnore.should_ignore — combined / edge cases
# ---------------------------------------------------------------------------


class TestCombinedRules:
    def test_empty_ignore_never_matches(self):
        ig = SlurpIgnore()
        assert not ig.should_ignore({"type": "code", "file_path": "src/main.py"}, "n")

    def test_any_matching_rule_is_sufficient(self):
        # Only file rule matches, not type — should still return True.
        ig = SlurpIgnore(type_rules=["document"], file_rules=["*.md"])
        assert ig.should_ignore({"type": "code", "file_path": "README.md"}, "n")

    def test_all_rules_checked_independently(self):
        ig = SlurpIgnore(type_rules=["config"], file_rules=["*.yaml"], id_rules=["skip_*"])
        assert ig.should_ignore({"type": "config"}, "safe_node")   # type matches
        assert ig.should_ignore({"file_path": "config.yaml"}, "x")  # file matches
        assert ig.should_ignore({}, "skip_me")                       # id matches
        assert not ig.should_ignore({"type": "code", "file_path": "main.py"}, "ok")


# ---------------------------------------------------------------------------
# load_ignore
# ---------------------------------------------------------------------------


class TestLoadIgnore:
    def test_missing_file_returns_empty_ignore(self, tmp_path):
        ig = load_ignore(tmp_path / ".slurpignore")
        assert ig.type_rules == []
        assert ig.file_rules == []
        assert ig.id_rules == []

    def test_parses_type_rules(self, tmp_path):
        f = tmp_path / ".slurpignore"
        f.write_text("type:document\ntype:config\n")
        ig = load_ignore(f)
        assert ig.type_rules == ["document", "config"]

    def test_parses_file_rules(self, tmp_path):
        f = tmp_path / ".slurpignore"
        f.write_text("file:*.md\nfile:node_modules/*\n")
        ig = load_ignore(f)
        assert ig.file_rules == ["*.md", "node_modules/*"]

    def test_parses_id_rules(self, tmp_path):
        f = tmp_path / ".slurpignore"
        f.write_text("id:tmp_*\nid:deprecated_*\n")
        ig = load_ignore(f)
        assert ig.id_rules == ["tmp_*", "deprecated_*"]

    def test_skips_comment_lines(self, tmp_path):
        f = tmp_path / ".slurpignore"
        f.write_text("# ignore docs\ntype:document\n# and configs\ntype:config\n")
        ig = load_ignore(f)
        assert ig.type_rules == ["document", "config"]
        assert not any(r.startswith("#") for r in ig.type_rules)

    def test_skips_blank_lines(self, tmp_path):
        f = tmp_path / ".slurpignore"
        f.write_text("\ntype:document\n\nfile:*.md\n\n")
        ig = load_ignore(f)
        assert ig.type_rules == ["document"]
        assert ig.file_rules == ["*.md"]

    def test_unknown_prefix_silently_ignored(self, tmp_path):
        f = tmp_path / ".slurpignore"
        f.write_text("type:document\nfoo:bar\n")
        ig = load_ignore(f)
        assert ig.type_rules == ["document"]
        assert ig.file_rules == []
        assert ig.id_rules == []

    def test_mixed_rules(self, tmp_path):
        f = tmp_path / ".slurpignore"
        f.write_text(
            "# slurpignore example\n"
            "type:document\n"
            "type:config\n"
            "file:*.md\n"
            "file:node_modules/*\n"
            "id:tmp_*\n"
        )
        ig = load_ignore(f)
        assert ig.type_rules == ["document", "config"]
        assert ig.file_rules == ["*.md", "node_modules/*"]
        assert ig.id_rules == ["tmp_*"]


# ---------------------------------------------------------------------------
# apply_ignore
# ---------------------------------------------------------------------------


class TestApplyIgnore:
    def test_removes_matching_nodes(self):
        G = nx.DiGraph()
        G.add_node("a", type="document", label="Doc")
        G.add_node("b", type="code", label="Code")
        ig = SlurpIgnore(type_rules=["document"])
        result = apply_ignore(G, ig)
        assert "a" not in result.nodes
        assert "b" in result.nodes

    def test_removes_incident_edges(self):
        G = nx.DiGraph()
        G.add_node("a", type="document")
        G.add_node("b", type="code")
        G.add_node("c", type="code")
        G.add_edge("a", "b")
        G.add_edge("b", "c")
        ig = SlurpIgnore(type_rules=["document"])
        result = apply_ignore(G, ig)
        assert ("a", "b") not in result.edges
        assert ("b", "c") in result.edges

    def test_empty_ignore_returns_full_graph(self, sample_graph):
        ig = SlurpIgnore()
        result = apply_ignore(sample_graph, ig)
        assert set(result.nodes) == set(sample_graph.nodes)
        assert set(result.edges) == set(sample_graph.edges)

    def test_result_is_a_copy(self, sample_graph):
        ig = SlurpIgnore()
        result = apply_ignore(sample_graph, ig)
        result.add_node("new_node")
        assert "new_node" not in sample_graph.nodes

    def test_apply_to_sample_graph_filters_class_nodes(self, sample_graph):
        ig = SlurpIgnore(type_rules=["class"])
        result = apply_ignore(sample_graph, ig)
        for nid in result.nodes:
            assert result.nodes[nid].get("type") != "class"

    def test_apply_file_rule_removes_matching_nodes(self):
        G = nx.DiGraph()
        G.add_node("doc", label="README", file_path="README.md")
        G.add_node("code", label="Auth", file_path="src/auth.py")
        ig = SlurpIgnore(file_rules=["*.md"])
        result = apply_ignore(G, ig)
        assert "doc" not in result.nodes
        assert "code" in result.nodes

    def test_apply_id_rule_removes_matching_nodes(self):
        G = nx.DiGraph()
        G.add_node("tmp_cache", label="Cache")
        G.add_node("auth_service", label="Auth")
        ig = SlurpIgnore(id_rules=["tmp_*"])
        result = apply_ignore(G, ig)
        assert "tmp_cache" not in result.nodes
        assert "auth_service" in result.nodes
