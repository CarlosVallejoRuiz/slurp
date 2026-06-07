"""Tests for slurp.injector — code block extraction and graph injection."""

from __future__ import annotations

from pathlib import Path

import networkx as nx
import pytest

from slurp.injector import (
    _extract_brace_balanced,
    _extract_python,
    _parse_line,
    extract_code_block,
    inject_code,
)

# ---------------------------------------------------------------------------
# Sample source code strings used across multiple test classes
# ---------------------------------------------------------------------------

PYTHON_CODE = """\
def authenticate_user(username, password):
    \"\"\"Validates credentials.\"\"\"
    if not username:
        return None

    return db.find(username)

def other_function():
    pass
"""

TS_CODE = """\
function authenticate(
    username: string
): Promise<User> {
    const user = await db.find(username);
    if (!user) {
        throw new Error("Not found");
    }
    return user;
}

function other(): null {
    return null;
}
"""

GO_CODE = """\
func Authenticate(username string) (*User, error) {
\tuser, err := db.Find(username)
\tif err != nil {
\t\treturn nil, err
\t}
\treturn user, nil
}

func Other() {}
"""

JS_CODE = """\
function processPayment(amount) {
    const result = stripe.charge(amount);
    return result;
}
"""


# ===========================================================================
# _parse_line
# ===========================================================================

class TestParseLineNumber:
    def test_l_prefix_uppercase(self):
        assert _parse_line("L42") == 42

    def test_l_prefix_lowercase(self):
        assert _parse_line("l10") == 10

    def test_plain_number_string(self):
        assert _parse_line("7") == 7

    def test_integer_input(self):
        assert _parse_line(3) == 3

    def test_whitespace_stripped(self):
        assert _parse_line("  L5 ") == 5

    def test_invalid_string_returns_none(self):
        assert _parse_line("abc") is None

    def test_empty_string_returns_none(self):
        assert _parse_line("") is None

    def test_none_returns_none(self):
        assert _parse_line(None) is None

    def test_zero_int_returns_none(self):
        assert _parse_line(0) is None

    def test_negative_int_returns_none(self):
        assert _parse_line(-5) is None

    def test_l_prefix_only_returns_none(self):
        assert _parse_line("L") is None

    def test_zero_string_returns_none(self):
        assert _parse_line("L0") is None

    def test_large_line_number(self):
        assert _parse_line("L9999") == 9999


# ===========================================================================
# _extract_python
# ===========================================================================

class TestExtractPython:
    def test_extracts_function_header(self):
        lines = PYTHON_CODE.splitlines()
        result = _extract_python(lines, 1, 100)
        assert result is not None
        assert result.startswith("def authenticate_user")

    def test_extracts_function_body(self):
        lines = PYTHON_CODE.splitlines()
        result = _extract_python(lines, 1, 100)
        assert "return db.find(username)" in result

    def test_stops_before_next_function(self):
        lines = PYTHON_CODE.splitlines()
        result = _extract_python(lines, 1, 100)
        assert "def other_function" not in result

    def test_no_trailing_blank_lines(self):
        lines = PYTHON_CODE.splitlines()
        result = _extract_python(lines, 1, 100)
        assert result is not None
        assert result[-1] != "\n"
        assert result.rstrip() == result

    def test_blank_lines_inside_body_preserved(self):
        lines = PYTHON_CODE.splitlines()
        result = _extract_python(lines, 1, 100)
        # There's a blank line between the early-return and the final return
        assert "\n\n" in result

    def test_out_of_range_returns_none(self):
        lines = PYTHON_CODE.splitlines()
        assert _extract_python(lines, 999, 100) is None

    def test_line_zero_returns_none(self):
        lines = PYTHON_CODE.splitlines()
        assert _extract_python(lines, 0, 100) is None

    def test_blank_start_line_returns_none(self):
        lines = ["", "def foo():", "    pass"]
        assert _extract_python(lines, 1, 100) is None

    def test_max_lines_caps_extraction(self):
        long_lines = ["def huge():"] + [f"    x_{i} = {i}" for i in range(200)]
        result = _extract_python(long_lines, 1, 10)
        assert result is not None
        # header (1) + up to 9 body lines = ≤ 10 total lines
        assert result.count("\n") <= 9

    def test_nested_function_included(self):
        code = "def outer():\n    def inner():\n        return 1\n    return inner\n"
        lines = code.splitlines()
        result = _extract_python(lines, 1, 100)
        assert result is not None
        assert "def inner():" in result
        assert "return inner" in result

    def test_single_line_function(self):
        lines = ["def noop(): pass"]
        result = _extract_python(lines, 1, 100)
        assert result == "def noop(): pass"

    def test_method_inside_class(self):
        code = "class C:\n    def method(self):\n        return 42\n\n    def other(self):\n        pass\n"
        lines = code.splitlines()
        # Extract method (starts at line 2, indent=4)
        result = _extract_python(lines, 2, 100)
        assert result is not None
        assert "def method(self):" in result
        assert "return 42" in result
        assert "def other" not in result


# ===========================================================================
# _extract_brace_balanced
# ===========================================================================

class TestExtractBraceBalanced:
    def test_extracts_ts_function_header(self):
        lines = TS_CODE.splitlines()
        result = _extract_brace_balanced(lines, 1, 100)
        assert result is not None
        assert "function authenticate(" in result

    def test_extracts_ts_function_body(self):
        lines = TS_CODE.splitlines()
        result = _extract_brace_balanced(lines, 1, 100)
        assert "return user;" in result

    def test_stops_at_function_boundary(self):
        lines = TS_CODE.splitlines()
        result = _extract_brace_balanced(lines, 1, 100)
        assert "function other()" not in result

    def test_nested_braces_handled(self):
        lines = TS_CODE.splitlines()
        result = _extract_brace_balanced(lines, 1, 100)
        assert "if (!user)" in result

    def test_extracts_go_function(self):
        lines = GO_CODE.splitlines()
        result = _extract_brace_balanced(lines, 1, 100)
        assert "func Authenticate" in result
        assert "return user, nil" in result

    def test_go_function_does_not_include_second_func(self):
        lines = GO_CODE.splitlines()
        result = _extract_brace_balanced(lines, 1, 100)
        assert "func Other" not in result

    def test_one_line_empty_function(self):
        lines = ["func Empty() {}"]
        result = _extract_brace_balanced(lines, 1, 100)
        assert result == "func Empty() {}"

    def test_max_lines_cuts_off(self):
        lines = TS_CODE.splitlines()
        result = _extract_brace_balanced(lines, 1, 3)
        assert result is not None
        assert result.count("\n") <= 2

    def test_out_of_range_returns_none(self):
        lines = GO_CODE.splitlines()
        assert _extract_brace_balanced(lines, 999, 100) is None

    def test_line_zero_returns_none(self):
        lines = GO_CODE.splitlines()
        assert _extract_brace_balanced(lines, 0, 100) is None

    def test_destructuring_param_no_early_stop(self):
        # A destructured parameter has { } within the signature line —
        # the check at end-of-line means we don't stop there.
        code = "function fn({ name }: Config) {\n    return name;\n}\n"
        lines = code.splitlines()
        result = _extract_brace_balanced(lines, 1, 100)
        assert result is not None
        assert "return name;" in result


# ===========================================================================
# extract_code_block — dispatch by extension + edge cases
# ===========================================================================

class TestExtractCodeBlock:
    def test_python_file_dispatches_indentation(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text(PYTHON_CODE)
        result = extract_code_block(f, "L1")
        assert result is not None
        assert "def authenticate_user" in result

    def test_typescript_file(self, tmp_path):
        f = tmp_path / "auth.ts"
        f.write_text(TS_CODE)
        result = extract_code_block(f, "L1")
        assert result is not None
        assert "function authenticate(" in result

    def test_tsx_file(self, tmp_path):
        f = tmp_path / "component.tsx"
        f.write_text(TS_CODE)
        result = extract_code_block(f, "L1")
        assert result is not None

    def test_javascript_file(self, tmp_path):
        f = tmp_path / "payment.js"
        f.write_text(JS_CODE)
        result = extract_code_block(f, "L1")
        assert result is not None
        assert "processPayment" in result

    def test_jsx_file(self, tmp_path):
        f = tmp_path / "Component.jsx"
        f.write_text(JS_CODE)
        result = extract_code_block(f, "L1")
        assert result is not None

    def test_go_file(self, tmp_path):
        f = tmp_path / "auth.go"
        f.write_text(GO_CODE)
        result = extract_code_block(f, "L1")
        assert result is not None
        assert "func Authenticate" in result

    def test_unknown_extension_uses_indentation(self, tmp_path):
        f = tmp_path / "script.rb"
        f.write_text("def hello\n  puts 'hi'\nend\n")
        result = extract_code_block(f, "L1")
        assert result is not None
        assert "def hello" in result

    def test_file_not_found_returns_none(self, tmp_path):
        result = extract_code_block(tmp_path / "missing.py", "L1")
        assert result is None

    def test_none_source_location_returns_none(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text(PYTHON_CODE)
        result = extract_code_block(f, None)
        assert result is None

    def test_invalid_source_location_returns_none(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text(PYTHON_CODE)
        result = extract_code_block(f, "notanumber")
        assert result is None

    def test_line_out_of_range_returns_none(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text(PYTHON_CODE)
        result = extract_code_block(f, "L9999")
        assert result is None

    def test_integer_source_location(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text(PYTHON_CODE)
        result = extract_code_block(f, 1)
        assert result is not None
        assert "def authenticate_user" in result

    def test_label_ignored_no_error(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text(PYTHON_CODE)
        result = extract_code_block(f, "L1", label="anything")
        assert result is not None

    def test_max_lines_respected(self, tmp_path):
        f = tmp_path / "big.py"
        f.write_text("def big():\n" + "    pass\n" * 200)
        result = extract_code_block(f, "L1", max_lines=5)
        assert result is not None
        assert result.count("\n") <= 4


# ===========================================================================
# inject_code
# ===========================================================================

def _make_graph(tmp_path: Path) -> tuple[nx.DiGraph, Path]:
    """Build a small test graph with a real Python source file."""
    src = tmp_path / "src"
    src.mkdir()
    py = src / "auth.py"
    py.write_text(PYTHON_CODE)

    G = nx.DiGraph()
    G.add_node("n1", label="authenticate_user", type="function",
               source_file="src/auth.py", source_location="L1")
    G.add_node("n2", label="other_function", type="function",
               source_file="src/auth.py", source_location="L8")
    G.add_node("n3", label="NoSource", type="concept")
    return G, tmp_path


class TestInjectCode:
    def test_adds_code_block_to_node(self, tmp_path):
        G, root = _make_graph(tmp_path)
        result = inject_code(G, root)
        assert "code_block" in result.nodes["n1"]
        assert "def authenticate_user" in result.nodes["n1"]["code_block"]

    def test_code_block_content_correct(self, tmp_path):
        G, root = _make_graph(tmp_path)
        result = inject_code(G, root)
        cb = result.nodes["n1"]["code_block"]
        assert "return db.find(username)" in cb

    def test_node_without_source_file_skipped(self, tmp_path):
        G, root = _make_graph(tmp_path)
        result = inject_code(G, root)
        assert "code_block" not in result.nodes["n3"]

    def test_too_many_nodes_returns_graph_unchanged(self, tmp_path):
        G, root = _make_graph(tmp_path)
        for i in range(30):
            G.add_node(f"extra_{i}", label=f"X{i}")
        # 33 nodes > 30 — inject_code must be a no-op
        result = inject_code(G, root, max_nodes=30)
        assert "code_block" not in result.nodes["n1"]

    def test_exactly_at_max_nodes_runs(self, tmp_path):
        G, root = _make_graph(tmp_path)
        # 3 nodes, max_nodes=3 → should execute normally
        result = inject_code(G, root, max_nodes=3)
        assert "code_block" in result.nodes["n1"]

    def test_returns_copy_original_unmodified(self, tmp_path):
        G, root = _make_graph(tmp_path)
        result = inject_code(G, root)
        # Original graph must NOT be mutated
        assert "code_block" not in G.nodes["n1"]
        assert "code_block" in result.nodes["n1"]

    def test_file_not_found_gracefully_skipped(self, tmp_path):
        G = nx.DiGraph()
        G.add_node("n1", label="foo", source_file="nonexistent.py", source_location="L1")
        result = inject_code(G, tmp_path)
        assert "code_block" not in result.nodes["n1"]

    def test_file_path_attr_fallback(self, tmp_path):
        """inject_code falls back to 'file_path' attribute when 'source_file' absent."""
        src = tmp_path / "auth.py"
        src.write_text(PYTHON_CODE)
        G = nx.DiGraph()
        G.add_node("n1", label="authenticate_user", file_path="auth.py", source_location="L1")
        result = inject_code(G, tmp_path)
        assert "code_block" in result.nodes["n1"]

    def test_edges_preserved_after_injection(self, tmp_path):
        G, root = _make_graph(tmp_path)
        G.add_edge("n1", "n2", relation="calls")
        result = inject_code(G, root)
        assert result.has_edge("n1", "n2")
        assert result.edges["n1", "n2"]["relation"] == "calls"

    def test_empty_graph_returns_empty(self):
        G = nx.DiGraph()
        result = inject_code(G, Path("."))
        assert result.number_of_nodes() == 0

    def test_node_without_source_location_skipped(self, tmp_path):
        src = tmp_path / "auth.py"
        src.write_text(PYTHON_CODE)
        G = nx.DiGraph()
        G.add_node("n1", label="foo", source_file="auth.py")  # no source_location
        result = inject_code(G, tmp_path)
        assert "code_block" not in result.nodes["n1"]

    def test_custom_max_nodes(self, tmp_path):
        G, root = _make_graph(tmp_path)
        # max_nodes=2 → 3 nodes exceeds limit, should not run
        result = inject_code(G, root, max_nodes=2)
        assert "code_block" not in result.nodes["n1"]
