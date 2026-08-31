"""Tests for slurp.indexer — static code indexing without graphify."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from slurp.cli import cli
import slurp.indexer as indexer_mod
from slurp.indexer import (
    _CPP_TS_AVAILABLE,
    _CSHARP_TS_AVAILABLE,
    _C_TS_AVAILABLE,
    _KOTLIN_TS_AVAILABLE,
    _PHP_TS_AVAILABLE,
    _JAVA_TS_AVAILABLE,
    _RUBY_TS_AVAILABLE,
    _RUST_TS_AVAILABLE,
    _SCALA_TS_AVAILABLE,
    _SWIFT_TS_AVAILABLE,
    _TREE_SITTER_AVAILABLE,
    _file_id,
    _index_c_regex,
    _index_csharp_regex,
    _index_java_regex,
    _index_ruby_regex,
    _index_kotlin_regex,
    _index_php_regex,
    _index_rust_regex,
    _index_scala_regex,
    _index_swift_regex,
    _tree_is_broken,
    format_parser_summary,
    index_c,
    index_cpp,
    index_csharp,
    index_elixir,
    index_java,
    index_lua,
    index_powershell,
    index_kotlin,
    index_php,
    index_ruby,
    index_rust,
    index_scala,
    index_swift,
    _index_typescript_regex,
    _parse_ts_tree,
    parser_summary,
    _should_skip,
    index_go,
    index_project,
    index_python,
    index_typescript,
)

# ---------------------------------------------------------------------------
# Sample source fixtures
# ---------------------------------------------------------------------------

PYTHON_MODULE = '''\
"""Auth module."""
import os
from pathlib import Path

def authenticate_user(username: str, password: str) -> dict | None:
    """Validate credentials."""
    user = db.find(username)
    return user

class AuthService:
    def __init__(self) -> None:
        self.db = None

    def login(self, username: str, password: str) -> dict:
        return authenticate_user(username, password)

    async def async_login(self, username: str) -> dict:
        return {}

class TokenValidator:
    def validate(self, token: str) -> bool:
        return bool(token)
'''

TS_MODULE = '''\
import { User } from \'./models\';

interface AuthConfig {
    timeout: number;
}

class AuthService {
    authenticate(username: string): Promise<User> {
        return Promise.resolve({} as User);
    }
}

function validateToken(token: string): boolean {
    return token.length > 0;
}

const hashPassword = (password: string) => password;
export const getUser = async (id: number) => ({} as User);
'''

GO_MODULE = '''\
package auth

import (
    "errors"
    "time"
)

type User struct {
    ID       int64
    Username string
}

type Authenticator interface {
    Authenticate(username string) (*User, error)
}

func NewAuthService() *AuthService {
    return &AuthService{}
}

type AuthService struct {
    secret string
}

func (s *AuthService) Authenticate(username string) (*User, error) {
    if username == "" {
        return nil, errors.New("required")
    }
    return &User{}, nil
}

func ValidateToken(token string) bool {
    return len(token) > 0
}
'''


# ===========================================================================
# _file_id helper
# ===========================================================================

class TestFileId:
    def test_single_file(self):
        assert _file_id(Path("auth.py")) == "auth"

    def test_nested_path(self):
        assert _file_id(Path("src/auth/service.py")) == "src.auth.service"

    def test_dash_replaced_with_underscore(self):
        assert _file_id(Path("my-module.py")) == "my_module"

    def test_deep_nesting(self):
        assert _file_id(Path("a/b/c/d.ts")) == "a.b.c.d"

    def test_no_extension(self):
        # Path with no extension — with_suffix("") is a no-op
        assert _file_id(Path("Makefile")) == "Makefile"

    def test_typescript_extension_stripped(self):
        assert _file_id(Path("src/api.ts")) == "src.api"

    def test_go_extension_stripped(self):
        assert _file_id(Path("cmd/main.go")) == "cmd.main"


# ===========================================================================
# _should_skip helper
# ===========================================================================

class TestShouldSkip:
    def test_skips_git(self):
        assert _should_skip(Path("/project/.git/config"))

    def test_skips_pycache(self):
        assert _should_skip(Path("/project/src/__pycache__/auth.pyc"))

    def test_skips_node_modules(self):
        assert _should_skip(Path("/project/node_modules/lodash/index.js"))

    def test_skips_venv(self):
        assert _should_skip(Path("/project/.venv/lib/python3.12/site.py"))

    def test_skips_egg_info(self):
        assert _should_skip(Path("/project/mypackage.egg-info/PKG-INFO"))

    def test_skips_build_dir(self):
        assert _should_skip(Path("/project/build/output.js"))

    def test_does_not_skip_src(self):
        assert not _should_skip(Path("/project/src/auth.py"))

    def test_does_not_skip_tests(self):
        assert not _should_skip(Path("/project/tests/test_auth.py"))

    def test_does_not_skip_root_file(self):
        assert not _should_skip(Path("auth.py"))


# ===========================================================================
# index_python
# ===========================================================================

class TestIndexPython:
    def test_module_node_created(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text(PYTHON_MODULE)
        nodes, _ = index_python(f, tmp_path)
        module_nodes = [n for n in nodes if n["type"] == "module"]
        assert len(module_nodes) == 1
        assert module_nodes[0]["label"] == "auth"

    def test_function_extracted(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text(PYTHON_MODULE)
        nodes, _ = index_python(f, tmp_path)
        labels = {n["label"] for n in nodes if n["type"] == "function"}
        assert "authenticate_user" in labels

    def test_class_extracted(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text(PYTHON_MODULE)
        nodes, _ = index_python(f, tmp_path)
        labels = {n["label"] for n in nodes if n["type"] == "class"}
        assert "AuthService" in labels
        assert "TokenValidator" in labels

    def test_method_inside_class_has_class_in_id(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text(PYTHON_MODULE)
        nodes, _ = index_python(f, tmp_path)
        method_ids = {n["id"] for n in nodes if n["label"] == "login"}
        assert any("AuthService" in nid for nid in method_ids)

    def test_async_function_type_is_function(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text(PYTHON_MODULE)
        nodes, _ = index_python(f, tmp_path)
        async_nodes = [n for n in nodes if n["label"] == "async_login"]
        assert len(async_nodes) == 1
        assert async_nodes[0]["type"] == "function"

    def test_import_extracted(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text(PYTHON_MODULE)
        nodes, _ = index_python(f, tmp_path)
        import_nodes = [n for n in nodes if n["type"] == "import"]
        labels = {n["label"] for n in import_nodes}
        assert "os" in labels

    def test_from_import_extracted(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text(PYTHON_MODULE)
        nodes, _ = index_python(f, tmp_path)
        import_labels = {n["label"] for n in nodes if n["type"] == "import"}
        assert "pathlib.Path" in import_labels

    def test_import_star_skipped(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("from os import *\n")
        nodes, _ = index_python(f, tmp_path)
        import_nodes = [n for n in nodes if n["type"] == "import"]
        assert not import_nodes

    def test_contains_edges_created(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text(PYTHON_MODULE)
        _, edges = index_python(f, tmp_path)
        relations = {e["relation"] for e in edges}
        assert "contains" in relations

    def test_imports_from_edges_created(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text(PYTHON_MODULE)
        _, edges = index_python(f, tmp_path)
        relations = {e["relation"] for e in edges}
        assert "imports_from" in relations

    def test_node_schema(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text("def foo(): pass\n")
        nodes, _ = index_python(f, tmp_path)
        for n in nodes:
            assert "id" in n
            assert "label" in n
            assert "type" in n
            assert "source_file" in n
            assert "source_location" in n
            assert "file_type" in n

    def test_edge_schema(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text("def foo(): pass\n")
        _, edges = index_python(f, tmp_path)
        for e in edges:
            assert "source" in e
            assert "target" in e
            assert "relation" in e

    def test_file_type_is_code(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text("def foo(): pass\n")
        nodes, _ = index_python(f, tmp_path)
        for n in nodes:
            assert n["file_type"] == "code"

    def test_source_location_has_l_prefix(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text("def foo(): pass\n")
        nodes, _ = index_python(f, tmp_path)
        func = next(n for n in nodes if n["type"] == "function")
        assert func["source_location"].startswith("L")

    def test_syntax_error_returns_empty(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text("def (: broken\n")
        nodes, edges = index_python(f, tmp_path)
        assert nodes == []
        assert edges == []

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.py"
        f.write_text("")
        nodes, edges = index_python(f, tmp_path)
        assert len(nodes) == 1  # just the module node
        assert nodes[0]["type"] == "module"
        assert edges == []

    def test_nested_function_has_parent_in_id(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("def outer():\n    def inner():\n        pass\n")
        nodes, _ = index_python(f, tmp_path)
        inner = next(n for n in nodes if n["label"] == "inner")
        assert "outer" in inner["id"]

    def test_multiple_classes_all_extracted(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text(PYTHON_MODULE)
        nodes, _ = index_python(f, tmp_path)
        class_labels = {n["label"] for n in nodes if n["type"] == "class"}
        assert "AuthService" in class_labels
        assert "TokenValidator" in class_labels

    def test_relative_source_file(self, tmp_path):
        (tmp_path / "src").mkdir()
        f = tmp_path / "src" / "auth.py"
        f.write_text("def foo(): pass\n")
        nodes, _ = index_python(f, tmp_path)
        for n in nodes:
            assert not Path(n["source_file"]).is_absolute()

    def test_node_ids_are_unique(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text(PYTHON_MODULE)
        nodes, _ = index_python(f, tmp_path)
        ids = [n["id"] for n in nodes]
        assert len(ids) == len(set(ids))


# ===========================================================================
# index_typescript (regex fallback — tree-sitter not installed)
# ===========================================================================

class TestIndexTypescript:
    def test_module_node_always_present(self, tmp_path):
        f = tmp_path / "auth.ts"
        f.write_text(TS_MODULE)
        nodes, _ = index_typescript(f, tmp_path)
        module_nodes = [n for n in nodes if n["type"] == "module"]
        assert len(module_nodes) == 1

    def test_function_extracted(self, tmp_path):
        f = tmp_path / "auth.ts"
        f.write_text(TS_MODULE)
        nodes, _ = index_typescript(f, tmp_path)
        labels = {n["label"] for n in nodes if n["type"] == "function"}
        assert "validateToken" in labels

    def test_class_extracted(self, tmp_path):
        f = tmp_path / "auth.ts"
        f.write_text(TS_MODULE)
        nodes, _ = index_typescript(f, tmp_path)
        labels = {n["label"] for n in nodes if n["type"] == "class"}
        assert "AuthService" in labels

    def test_interface_extracted(self, tmp_path):
        f = tmp_path / "auth.ts"
        f.write_text(TS_MODULE)
        nodes, _ = index_typescript(f, tmp_path)
        labels = {n["label"] for n in nodes if n["type"] == "interface"}
        assert "AuthConfig" in labels

    def test_arrow_function_extracted(self, tmp_path):
        f = tmp_path / "auth.ts"
        f.write_text(TS_MODULE)
        nodes, _ = index_typescript(f, tmp_path)
        labels = {n["label"] for n in nodes}
        assert "hashPassword" in labels

    def test_exported_arrow_function_extracted(self, tmp_path):
        f = tmp_path / "auth.ts"
        f.write_text(TS_MODULE)
        nodes, _ = index_typescript(f, tmp_path)
        labels = {n["label"] for n in nodes}
        assert "getUser" in labels

    def test_import_extracted(self, tmp_path):
        f = tmp_path / "auth.ts"
        f.write_text(TS_MODULE)
        nodes, _ = index_typescript(f, tmp_path)
        import_nodes = [n for n in nodes if n["type"] == "import"]
        assert len(import_nodes) >= 1
        assert any("models" in n["label"] for n in import_nodes)

    def test_empty_file_returns_module_node(self, tmp_path):
        f = tmp_path / "empty.ts"
        f.write_text("")
        nodes, edges = index_typescript(f, tmp_path)
        assert len(nodes) == 1
        assert nodes[0]["type"] == "module"
        assert edges == []

    def test_tsx_file_accepted(self, tmp_path):
        f = tmp_path / "Component.tsx"
        f.write_text("export function Button() { return null; }\n")
        nodes, _ = index_typescript(f, tmp_path)
        assert any(n["label"] == "Button" for n in nodes)

    def test_js_file_accepted(self, tmp_path):
        f = tmp_path / "utils.js"
        f.write_text("function greet(name) { return 'Hello ' + name; }\n")
        nodes, _ = index_typescript(f, tmp_path)
        assert any(n["label"] == "greet" for n in nodes)

    def test_node_schema(self, tmp_path):
        f = tmp_path / "auth.ts"
        f.write_text("function foo(): void {}\n")
        nodes, _ = index_typescript(f, tmp_path)
        for n in nodes:
            assert "id" in n
            assert "label" in n
            assert "type" in n
            assert "source_file" in n
            assert "source_location" in n
            assert "file_type" in n

    def test_contains_edges(self, tmp_path):
        f = tmp_path / "auth.ts"
        f.write_text(TS_MODULE)
        _, edges = index_typescript(f, tmp_path)
        assert any(e["relation"] == "contains" for e in edges)

    def test_export_keyword_does_not_duplicate(self, tmp_path):
        f = tmp_path / "auth.ts"
        f.write_text("export function foo() {}\n")
        nodes, _ = index_typescript(f, tmp_path)
        foo_nodes = [n for n in nodes if n["label"] == "foo"]
        assert len(foo_nodes) == 1

    def test_abstract_class(self, tmp_path):
        f = tmp_path / "base.ts"
        f.write_text("abstract class BaseService { abstract init(): void; }\n")
        nodes, _ = index_typescript(f, tmp_path)
        assert any(n["label"] == "BaseService" for n in nodes)

    def test_missing_file_returns_module_only(self, tmp_path):
        f = tmp_path / "nonexistent.ts"
        nodes, edges = index_typescript(f, tmp_path)
        assert len(nodes) == 1
        assert nodes[0]["type"] == "module"
        assert edges == []


# ===========================================================================
# index_go
# ===========================================================================

class TestIndexGo:
    def test_package_node_created(self, tmp_path):
        f = tmp_path / "auth.go"
        f.write_text(GO_MODULE)
        nodes, _ = index_go(f, tmp_path)
        module_nodes = [n for n in nodes if n["type"] == "module"]
        assert len(module_nodes) == 1
        assert module_nodes[0]["label"] == "auth"

    def test_struct_extracted_as_class(self, tmp_path):
        f = tmp_path / "auth.go"
        f.write_text(GO_MODULE)
        nodes, _ = index_go(f, tmp_path)
        class_labels = {n["label"] for n in nodes if n["type"] == "class"}
        assert "User" in class_labels
        assert "AuthService" in class_labels

    def test_interface_extracted(self, tmp_path):
        f = tmp_path / "auth.go"
        f.write_text(GO_MODULE)
        nodes, _ = index_go(f, tmp_path)
        iface_labels = {n["label"] for n in nodes if n["type"] == "interface"}
        assert "Authenticator" in iface_labels

    def test_function_extracted(self, tmp_path):
        f = tmp_path / "auth.go"
        f.write_text(GO_MODULE)
        nodes, _ = index_go(f, tmp_path)
        func_labels = {n["label"] for n in nodes if n["type"] == "function"}
        assert "NewAuthService" in func_labels
        assert "ValidateToken" in func_labels

    def test_method_on_receiver_extracted(self, tmp_path):
        f = tmp_path / "auth.go"
        f.write_text(GO_MODULE)
        nodes, _ = index_go(f, tmp_path)
        func_labels = {n["label"] for n in nodes if n["type"] == "function"}
        assert "Authenticate" in func_labels

    def test_imports_extracted(self, tmp_path):
        f = tmp_path / "auth.go"
        f.write_text(GO_MODULE)
        nodes, _ = index_go(f, tmp_path)
        import_nodes = [n for n in nodes if n["type"] == "import"]
        import_labels = {n["label"] for n in import_nodes}
        assert "errors" in import_labels
        assert "time" in import_labels

    def test_single_import(self, tmp_path):
        f = tmp_path / "main.go"
        f.write_text('package main\nimport "fmt"\nfunc main() {}\n')
        nodes, _ = index_go(f, tmp_path)
        import_nodes = [n for n in nodes if n["type"] == "import"]
        assert any(n["label"] == "fmt" for n in import_nodes)

    def test_empty_file_returns_empty(self, tmp_path):
        f = tmp_path / "empty.go"
        f.write_text("")
        nodes, edges = index_go(f, tmp_path)
        # No package declaration → still returns a module node with stem as name
        assert isinstance(nodes, list)
        assert isinstance(edges, list)

    def test_node_schema(self, tmp_path):
        f = tmp_path / "auth.go"
        f.write_text(GO_MODULE)
        nodes, _ = index_go(f, tmp_path)
        for n in nodes:
            assert "id" in n
            assert "label" in n
            assert "type" in n
            assert "source_file" in n
            assert "source_location" in n
            assert "file_type" in n

    def test_contains_edges(self, tmp_path):
        f = tmp_path / "auth.go"
        f.write_text(GO_MODULE)
        _, edges = index_go(f, tmp_path)
        assert any(e["relation"] == "contains" for e in edges)

    def test_imports_from_edges(self, tmp_path):
        f = tmp_path / "auth.go"
        f.write_text(GO_MODULE)
        _, edges = index_go(f, tmp_path)
        assert any(e["relation"] == "imports_from" for e in edges)


# ===========================================================================
# index_project
# ===========================================================================

def _make_project(tmp_path: Path) -> None:
    """Write a small multi-file project into tmp_path."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text(PYTHON_MODULE)
    (tmp_path / "src" / "utils.ts").write_text(TS_MODULE)
    (tmp_path / "cmd").mkdir()
    (tmp_path / "cmd" / "main.go").write_text(GO_MODULE)


class TestIndexProject:
    def test_indexes_python_files(self, tmp_path):
        (tmp_path / "auth.py").write_text(PYTHON_MODULE)
        graph = index_project(tmp_path)
        labels = {n["label"] for n in graph["nodes"]}
        assert "authenticate_user" in labels

    def test_indexes_typescript_files(self, tmp_path):
        (tmp_path / "utils.ts").write_text(TS_MODULE)
        graph = index_project(tmp_path)
        labels = {n["label"] for n in graph["nodes"]}
        assert "validateToken" in labels

    def test_indexes_go_files(self, tmp_path):
        (tmp_path / "main.go").write_text(GO_MODULE)
        graph = index_project(tmp_path)
        labels = {n["label"] for n in graph["nodes"]}
        assert "ValidateToken" in labels

    def test_indexes_multiple_languages(self, tmp_path):
        _make_project(tmp_path)
        graph = index_project(tmp_path)
        types_found = {n["type"] for n in graph["nodes"]}
        assert "module" in types_found
        assert "function" in types_found
        assert "class" in types_found

    def test_output_has_required_keys(self, tmp_path):
        graph = index_project(tmp_path)
        assert "nodes" in graph
        assert "links" in graph
        assert "built_at_commit" in graph

    def test_built_at_commit_is_none(self, tmp_path):
        graph = index_project(tmp_path)
        assert graph["built_at_commit"] is None

    def test_skips_pycache(self, tmp_path):
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "auth.cpython-312.pyc").write_text("")
        (tmp_path / "auth.py").write_text("def foo(): pass\n")
        graph = index_project(tmp_path)
        for n in graph["nodes"]:
            assert "__pycache__" not in n.get("source_file", "")

    def test_skips_node_modules(self, tmp_path):
        (tmp_path / "node_modules" / "lodash").mkdir(parents=True)
        (tmp_path / "node_modules" / "lodash" / "index.js").write_text(
            "function identity(x) { return x; }\n"
        )
        (tmp_path / "app.ts").write_text("function main() {}\n")
        graph = index_project(tmp_path)
        for n in graph["nodes"]:
            assert "node_modules" not in n.get("source_file", "")

    def test_node_ids_are_unique(self, tmp_path):
        _make_project(tmp_path)
        graph = index_project(tmp_path)
        ids = [n["id"] for n in graph["nodes"]]
        assert len(ids) == len(set(ids))

    def test_all_edge_endpoints_known(self, tmp_path):
        _make_project(tmp_path)
        graph = index_project(tmp_path)
        node_ids = {n["id"] for n in graph["nodes"]}
        for e in graph["links"]:
            assert e["source"] in node_ids, f"unknown source: {e['source']}"
            assert e["target"] in node_ids, f"unknown target: {e['target']}"

    def test_applies_ignore_type_rule(self, tmp_path):
        (tmp_path / "auth.py").write_text(PYTHON_MODULE)
        from slurp.ignore import SlurpIgnore

        ignore = SlurpIgnore(type_rules=["import"])
        graph = index_project(tmp_path, ignore)
        types = {n["type"] for n in graph["nodes"]}
        assert "import" not in types

    def test_applies_ignore_id_rule(self, tmp_path):
        (tmp_path / "auth.py").write_text("def secret_fn(): pass\n")
        from slurp.ignore import SlurpIgnore

        ignore = SlurpIgnore(id_rules=["*.secret_fn"])
        graph = index_project(tmp_path, ignore)
        labels = {n["label"] for n in graph["nodes"]}
        assert "secret_fn" not in labels

    def test_empty_directory(self, tmp_path):
        graph = index_project(tmp_path)
        assert graph["nodes"] == []
        assert graph["links"] == []

    def test_graphify_format_compatible(self, tmp_path):
        (tmp_path / "auth.py").write_text("def foo(): pass\n")
        graph = index_project(tmp_path)
        # The loader.py accepts this format — verify the keys are correct
        assert isinstance(graph["nodes"], list)
        assert isinstance(graph["links"], list)
        for n in graph["nodes"]:
            assert isinstance(n["id"], str)
            assert isinstance(n["label"], str)


# ===========================================================================
# slurp index CLI command
# ===========================================================================

class TestIndexCLI:
    def test_basic_invocation(self, tmp_path):
        (tmp_path / "app.py").write_text("def main(): pass\n")
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["index", str(tmp_path), "--output", str(tmp_path / "graph.json")],
        )
        assert result.exit_code == 0, result.output

    def test_creates_output_file(self, tmp_path):
        (tmp_path / "app.py").write_text("def main(): pass\n")
        out = tmp_path / "out" / "graph.json"
        runner = CliRunner()
        runner.invoke(cli, ["index", str(tmp_path), "--output", str(out)])
        assert out.exists()

    def test_output_is_valid_json(self, tmp_path):
        (tmp_path / "app.py").write_text("def hello(): pass\n")
        out = tmp_path / "graph.json"
        runner = CliRunner()
        runner.invoke(cli, ["index", str(tmp_path), "--output", str(out)])
        data = json.loads(out.read_text())
        assert "nodes" in data
        assert "links" in data

    def test_output_contains_nodes(self, tmp_path):
        (tmp_path / "app.py").write_text("def hello(): pass\n")
        out = tmp_path / "graph.json"
        runner = CliRunner()
        runner.invoke(cli, ["index", str(tmp_path), "--output", str(out)])
        data = json.loads(out.read_text())
        assert len(data["nodes"]) > 0

    def test_next_command_shown(self, tmp_path):
        (tmp_path / "app.py").write_text("def main(): pass\n")
        out = tmp_path / "graph.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["index", str(tmp_path), "--output", str(out)])
        assert "Next:" in result.output

    def test_node_count_shown(self, tmp_path):
        (tmp_path / "app.py").write_text("def main(): pass\n")
        out = tmp_path / "graph.json"
        runner = CliRunner()
        result = runner.invoke(cli, ["index", str(tmp_path), "--output", str(out)])
        assert "nodes" in result.output

    def test_default_output_path(self, tmp_path):
        (tmp_path / "app.py").write_text("def main(): pass\n")
        runner = CliRunner()
        result = runner.invoke(cli, ["index", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "graphify-out" / "graph.json").exists()

    def test_empty_project_exits_successfully(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["index", str(tmp_path), "--output", str(tmp_path / "graph.json")],
        )
        assert result.exit_code == 0

    def test_output_graphify_format(self, tmp_path):
        (tmp_path / "app.py").write_text("def main(): pass\n")
        out = tmp_path / "graph.json"
        runner = CliRunner()
        runner.invoke(cli, ["index", str(tmp_path), "--output", str(out)])
        data = json.loads(out.read_text())
        assert "built_at_commit" in data
        assert data["built_at_commit"] is None


# ---------------------------------------------------------------------------
# Fixtures for parser tests
# ---------------------------------------------------------------------------

TS_RICH_CLASS = '''\
export class UserService {
  private cache = new Map<string, User>();
  constructor(private db: Database) {}
  async findUser(id: string): Promise<User | null> {
    const localHelper = () => { return null; };
    return this.db.get(id);
  }
  static create(): UserService { return new UserService(null); }
  get size(): number { return this.cache.size; }
  set limit(v: number) { this._limit = v; }
  private handle = (e: Event) => { console.log(e); };
}
export function formatName(u: User): string { return u.first; }
'''


def _regex_nodes(source: str, name: str = "svc.ts"):
    """Run the regex fallback directly and return (nodes, edges)."""
    rel = Path(name)
    return _index_typescript_regex(source, rel, _file_id(rel))


def _ids(nodes) -> set[str]:
    return {n["id"] for n in nodes}


# ---------------------------------------------------------------------------
# Fix 1 + 2 — tree-sitter availability
# ---------------------------------------------------------------------------


class TestTreeSitterAvailable:
    def test_flag_is_a_bool(self):
        assert isinstance(_TREE_SITTER_AVAILABLE, bool)

    def test_flag_true_when_package_importable(self):
        try:
            import tree_sitter  # noqa: F401
            import tree_sitter_typescript  # noqa: F401
        except ImportError:
            pytest.skip("tree-sitter not installed in this environment")
        assert _TREE_SITTER_AVAILABLE is True

    @pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="requires the 'ts' extra")
    def test_tree_sitter_path_extracts_constructor(self, tmp_path):
        path = tmp_path / "svc.ts"
        path.write_text(TS_RICH_CLASS, encoding="utf-8")
        nodes, _ = index_typescript(path, tmp_path)
        assert "svc.UserService.constructor" in _ids(nodes)

    @pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="requires the 'ts' extra")
    def test_tree_sitter_nests_local_function_under_method(self, tmp_path):
        """The distinguishing capability: regex cannot reach this depth."""
        path = tmp_path / "svc.ts"
        path.write_text(TS_RICH_CLASS, encoding="utf-8")
        nodes, _ = index_typescript(path, tmp_path)
        assert "svc.UserService.findUser.localHelper" in _ids(nodes)

    @pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="requires the 'ts' extra")
    def test_parse_ts_tree_returns_a_tree(self):
        tree = _parse_ts_tree(b"class A { foo() {} }")
        assert tree.root_node.type == "program"

    def test_parse_ts_tree_raises_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(indexer_mod, "_TREE_SITTER_AVAILABLE", False)
        with pytest.raises(RuntimeError, match="not installed"):
            _parse_ts_tree(b"class A {}")


# ---------------------------------------------------------------------------
# Fix 3 — regex fallback captures class members
# ---------------------------------------------------------------------------


class TestTSRegexFallback:
    def test_constructor_extracted(self):
        nodes, _ = _regex_nodes(TS_RICH_CLASS)
        assert "svc.UserService.constructor" in _ids(nodes)

    def test_async_method_extracted(self):
        nodes, _ = _regex_nodes(TS_RICH_CLASS)
        assert "svc.UserService.findUser" in _ids(nodes)

    def test_static_method_extracted(self):
        nodes, _ = _regex_nodes(TS_RICH_CLASS)
        assert "svc.UserService.create" in _ids(nodes)

    def test_getter_extracted(self):
        nodes, _ = _regex_nodes(TS_RICH_CLASS)
        assert "svc.UserService.size" in _ids(nodes)

    def test_setter_extracted(self):
        nodes, _ = _regex_nodes(TS_RICH_CLASS)
        assert "svc.UserService.limit" in _ids(nodes)

    def test_arrow_property_extracted(self):
        nodes, _ = _regex_nodes(TS_RICH_CLASS)
        assert "svc.UserService.handle" in _ids(nodes)

    def test_generator_method_extracted(self):
        nodes, _ = _regex_nodes(
            "class S {\n  protected async *stream() { yield 1; }\n}\n"
        )
        assert "svc.S.stream" in _ids(nodes)

    def test_members_are_functions(self):
        nodes, _ = _regex_nodes(TS_RICH_CLASS)
        member = next(n for n in nodes if n["id"] == "svc.UserService.findUser")
        assert member["type"] == "function"

    def test_contains_edge_links_class_to_method(self):
        _, edges = _regex_nodes(TS_RICH_CLASS)
        assert {"source": "svc.UserService", "target": "svc.UserService.findUser",
                "relation": "contains"} in edges

    def test_no_file_to_method_edge(self):
        """The method must hang off the class, never off the module."""
        _, edges = _regex_nodes(TS_RICH_CLASS)
        assert not any(
            e["source"] == "svc" and e["target"] == "svc.UserService.findUser"
            for e in edges
        )

    def test_local_function_not_a_top_level_symbol(self):
        nodes, _ = _regex_nodes(TS_RICH_CLASS)
        assert "svc.localHelper" not in _ids(nodes)

    def test_nested_ids_use_module_class_method(self):
        nodes, _ = _regex_nodes(TS_RICH_CLASS)
        for nid in ("svc.UserService.constructor", "svc.UserService.create"):
            assert nid.count(".") == 2
            assert nid.startswith("svc.UserService.")

    def test_top_level_function_still_extracted(self):
        nodes, _ = _regex_nodes(TS_RICH_CLASS)
        assert "svc.formatName" in _ids(nodes)

    def test_top_level_arrow_const_still_extracted(self):
        nodes, _ = _regex_nodes("export const run = () => 1;\n")
        assert "svc.run" in _ids(nodes)

    def test_control_flow_is_not_a_member(self):
        source = (
            "class S {\n"
            "  go() {\n"
            "    if (x) { return 1; }\n"
            "    for (const a of b) { while (c) {} }\n"
            "  }\n"
            "}\n"
        )
        ids = _ids(_regex_nodes(source)[0])
        for keyword in ("if", "for", "while", "return"):
            assert f"svc.S.{keyword}" not in ids

    def test_nested_callback_is_not_a_member(self):
        source = (
            "class S {\n"
            "  run() {\n"
            "    items.forEach(function inner() { return 1; });\n"
            "  }\n"
            "}\n"
        )
        assert "svc.S.inner" not in _ids(_regex_nodes(source)[0])

    def test_comment_lines_are_ignored(self):
        source = (
            "class S {\n"
            "  // fake() is only mentioned in a comment\n"
            "  /* other() too */\n"
            "  real() {}\n"
            "}\n"
        )
        ids = _ids(_regex_nodes(source)[0])
        assert "svc.S.real" in ids
        assert "svc.S.fake" not in ids

    def test_two_classes_keep_their_own_members(self):
        source = (
            "class A {\n  alpha() {}\n}\n"
            "class B {\n  beta() {}\n}\n"
        )
        ids = _ids(_regex_nodes(source)[0])
        assert "svc.A.alpha" in ids
        assert "svc.B.beta" in ids
        assert "svc.A.beta" not in ids

    def test_empty_class_yields_only_the_class(self):
        ids = _ids(_regex_nodes("class Empty {}\n")[0])
        assert ids == {"svc.Empty"}

    def test_unterminated_class_does_not_raise(self):
        nodes, _ = _regex_nodes("class Broken {\n  method() {\n")
        assert "svc.Broken" in _ids(nodes)


# ---------------------------------------------------------------------------
# Fix 4 — parser summary
# ---------------------------------------------------------------------------


class TestParserSummary:
    def test_python_only(self):
        assert parser_summary({".py"}) == [("Python", "ast", False)]

    def test_empty_set_yields_nothing(self):
        assert parser_summary(set()) == []

    def test_unknown_extension_ignored(self):
        assert parser_summary({".md", ".txt"}) == []

    @pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="requires the 'ts' extra")
    def test_python_and_ts_with_tree_sitter(self):
        assert parser_summary({".py", ".ts"}) == [
            ("Python", "ast", False),
            ("TypeScript", "tree-sitter", False),
        ]

    def test_python_and_ts_without_tree_sitter(self, monkeypatch):
        monkeypatch.setattr(indexer_mod, "_TREE_SITTER_AVAILABLE", False)
        assert parser_summary({".py", ".ts"}) == [
            ("Python", "ast", False),
            ("TypeScript", "regex fallback", True),
        ]

    def test_python_and_go(self):
        assert parser_summary({".py", ".go"}) == [
            ("Python", "ast", False),
            ("Go", "regex", False),
        ]

    def test_jsx_counts_as_typescript(self, monkeypatch):
        monkeypatch.setattr(indexer_mod, "_TREE_SITTER_AVAILABLE", True)
        assert parser_summary({".jsx"}) == [("TypeScript", "tree-sitter", False)]

    def test_order_is_python_typescript_go(self, monkeypatch):
        monkeypatch.setattr(indexer_mod, "_TREE_SITTER_AVAILABLE", True)
        langs = [lang for lang, _, _ in parser_summary({".go", ".ts", ".py"})]
        assert langs == ["Python", "TypeScript", "Go"]


class TestParserSummaryCLI:
    @staticmethod
    def _project(tmp_path, python=True, ts=False, go=False):
        if python:
            (tmp_path / "a.py").write_text("def hello():\n    pass\n", encoding="utf-8")
        if ts:
            (tmp_path / "b.ts").write_text(TS_RICH_CLASS, encoding="utf-8")
        if go:
            (tmp_path / "c.go").write_text(
                "package main\n\nfunc Handle() error { return nil }\n", encoding="utf-8")
        return tmp_path

    def _run(self, tmp_path, **kw):
        root = self._project(tmp_path, **kw)
        return CliRunner().invoke(
            cli, ["index", str(root), "--output", str(tmp_path / "g.json")]
        )

    def test_python_only_output(self, tmp_path):
        result = self._run(tmp_path)
        assert result.exit_code == 0, result.output
        assert "Parsers: Python (ast)" in result.output

    @pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="requires the 'ts' extra")
    def test_python_and_ts_output(self, tmp_path):
        result = self._run(tmp_path, ts=True)
        assert "Python (ast)" in result.output
        assert "TypeScript (tree-sitter)" in result.output

    def test_python_and_go_output(self, tmp_path):
        result = self._run(tmp_path, go=True)
        assert "Python (ast)" in result.output
        assert "Go (regex)" in result.output

    def test_fallback_shown_without_tree_sitter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(indexer_mod, "_TREE_SITTER_AVAILABLE", False)
        result = self._run(tmp_path, ts=True)
        assert "TypeScript (regex fallback)" in result.output

    def test_install_hint_shown_on_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setattr(indexer_mod, "_TREE_SITTER_AVAILABLE", False)
        result = self._run(tmp_path, ts=True)
        assert "uv sync --extra ts" in result.output

    @pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="requires the 'ts' extra")
    def test_no_install_hint_when_tree_sitter_present(self, tmp_path):
        result = self._run(tmp_path, ts=True)
        assert "uv sync --extra ts" not in result.output

    def test_no_hint_for_a_python_only_project(self, tmp_path):
        result = self._run(tmp_path)
        assert "uv sync --extra ts" not in result.output

    def test_parsers_line_precedes_saved_line(self, tmp_path):
        result = self._run(tmp_path)
        assert result.output.index("Parsers:") < result.output.index("Saved:")


# ---------------------------------------------------------------------------
# Fix 2 — warning only when tree-sitter is installed but fails
# ---------------------------------------------------------------------------


class TestStaleWarning:
    @pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="requires the 'ts' extra")
    def test_parse_failure_warns_on_stderr(self, tmp_path, capsys):
        path = tmp_path / "svc.ts"
        path.write_text(TS_RICH_CLASS, encoding="utf-8")

        def boom(*_a, **_kw):
            raise ValueError("simulated parser explosion")

        original = indexer_mod._parse_ts_tree
        indexer_mod._parse_ts_tree = boom
        try:
            index_typescript(path, tmp_path)
        finally:
            indexer_mod._parse_ts_tree = original

        assert "tree-sitter failed" in capsys.readouterr().err

    @pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="requires the 'ts' extra")
    def test_warning_names_the_file_and_the_error(self, tmp_path, capsys):
        path = tmp_path / "svc.ts"
        path.write_text(TS_RICH_CLASS, encoding="utf-8")

        def boom(*_a, **_kw):
            raise ValueError("simulated parser explosion")

        original = indexer_mod._parse_ts_tree
        indexer_mod._parse_ts_tree = boom
        try:
            index_typescript(path, tmp_path)
        finally:
            indexer_mod._parse_ts_tree = original

        err = capsys.readouterr().err
        assert "svc.ts" in err
        assert "simulated parser explosion" in err
        assert "falling back to regex" in err

    @pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="requires the 'ts' extra")
    def test_parse_failure_still_returns_regex_nodes(self, tmp_path):
        path = tmp_path / "svc.ts"
        path.write_text(TS_RICH_CLASS, encoding="utf-8")

        def boom(*_a, **_kw):
            raise ValueError("simulated parser explosion")

        original = indexer_mod._parse_ts_tree
        indexer_mod._parse_ts_tree = boom
        try:
            nodes, _ = index_typescript(path, tmp_path)
        finally:
            indexer_mod._parse_ts_tree = original

        assert "svc.UserService.findUser" in _ids(nodes)

    def test_missing_tree_sitter_is_silent(self, tmp_path, monkeypatch, capsys):
        """An absent optional extra must never print anything."""
        monkeypatch.setattr(indexer_mod, "_TREE_SITTER_AVAILABLE", False)
        path = tmp_path / "svc.ts"
        path.write_text(TS_RICH_CLASS, encoding="utf-8")
        index_typescript(path, tmp_path)
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""

    def test_missing_tree_sitter_still_indexes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(indexer_mod, "_TREE_SITTER_AVAILABLE", False)
        path = tmp_path / "svc.ts"
        path.write_text(TS_RICH_CLASS, encoding="utf-8")
        nodes, _ = index_typescript(path, tmp_path)
        assert "svc.UserService.constructor" in _ids(nodes)


# ---------------------------------------------------------------------------
# Java / Rust / C# / Ruby — sample sources
# ---------------------------------------------------------------------------

JAVA_SOURCE = '''\
package com.example.app;
import java.util.List;

@Service
public class UserService extends BaseService implements Repo, Closeable {
    @Autowired
    private final UserRepo repo;
    public UserService(UserRepo r) { this.repo = r; }
    @Override
    public <T extends Comparable<T>> List<T> findAll(int page) { return null; }
    protected void helper() { if (true) { return; } }
}
'''

RUST_SOURCE = '''\
use std::collections::HashMap;
pub mod config {
    pub struct Settings { pub name: String }
}
pub trait Storage { fn get(&self, k: &str) -> Option<String>; }
pub struct Db { conn: String }
impl Storage for Db {
    fn get(&self, k: &str) -> Option<String> { None }
}
impl Db { pub fn new() -> Self { Db { conn: String::new() } } }
pub enum Mode { Fast, Slow }
macro_rules! my_macro { () => {}; }
pub fn helper<'a, T: Clone>(x: &'a T) -> &'a T { x }
fn private_fn() {}
'''

CSHARP_SOURCE = '''\
using System.Threading.Tasks;
namespace MyApp.Controllers {
    public interface IRepo { void Save(); }
    [Authorize]
    public class HomeController : ControllerBase, IRepo {
        [HttpGet]
        public async Task<IActionResult> Index(int id) { return null; }
        public string Name { get; set; }
        public HomeController() {}
        public void Save() {}
    }
}
'''

RUBY_SOURCE = '''\
module Auth
  CONFIG = 5
  HANDLER = Proc.new { |x| x }
  class UserService < Base
    include Comparable
    extend Forwardable
    prepend Loggable
    attr_accessor :name, :email
    attr_reader :id
    def initialize(id); @id = id; end
    def self.create; new(1); end
    def method_missing(n); super; end
  end
end
'''


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def _node(nodes, node_id: str) -> dict:
    return next(n for n in nodes if n["id"] == node_id)


def _relations(edges, relation: str) -> set[tuple[str, str]]:
    return {(e["source"], e["target"]) for e in edges if e["relation"] == relation}


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------


class TestIndexJava:
    @pytest.mark.skipif(not _JAVA_TS_AVAILABLE, reason="requires the 'java' extra")
    def test_class_and_methods(self, tmp_path):
        nodes, _ = index_java(_write(tmp_path, "UserService.java", JAVA_SOURCE), tmp_path)
        ids = _ids(nodes)
        assert "UserService.UserService" in ids
        assert "UserService.UserService.findAll" in ids

    @pytest.mark.skipif(not _JAVA_TS_AVAILABLE, reason="requires the 'java' extra")
    def test_package_extracted(self, tmp_path):
        nodes, _ = index_java(_write(tmp_path, "UserService.java", JAVA_SOURCE), tmp_path)
        assert any(n["type"] == "package" and n["label"] == "com.example.app" for n in nodes)

    @pytest.mark.skipif(not _JAVA_TS_AVAILABLE, reason="requires the 'java' extra")
    def test_constructor_typed_as_constructor(self, tmp_path):
        nodes, _ = index_java(_write(tmp_path, "UserService.java", JAVA_SOURCE), tmp_path)
        assert _node(nodes, "UserService.UserService.UserService")["type"] == "constructor"

    @pytest.mark.skipif(not _JAVA_TS_AVAILABLE, reason="requires the 'java' extra")
    def test_field_extracted(self, tmp_path):
        nodes, _ = index_java(_write(tmp_path, "UserService.java", JAVA_SOURCE), tmp_path)
        assert _node(nodes, "UserService.UserService.repo")["type"] == "field"

    @pytest.mark.skipif(not _JAVA_TS_AVAILABLE, reason="requires the 'java' extra")
    def test_class_annotation_recorded(self, tmp_path):
        nodes, _ = index_java(_write(tmp_path, "UserService.java", JAVA_SOURCE), tmp_path)
        assert "@Service" in _node(nodes, "UserService.UserService")["annotations"]

    @pytest.mark.skipif(not _JAVA_TS_AVAILABLE, reason="requires the 'java' extra")
    def test_field_annotation_recorded(self, tmp_path):
        nodes, _ = index_java(_write(tmp_path, "UserService.java", JAVA_SOURCE), tmp_path)
        assert "@Autowired" in _node(nodes, "UserService.UserService.repo")["annotations"]

    @pytest.mark.skipif(not _JAVA_TS_AVAILABLE, reason="requires the 'java' extra")
    def test_method_annotation_recorded(self, tmp_path):
        nodes, _ = index_java(_write(tmp_path, "UserService.java", JAVA_SOURCE), tmp_path)
        assert "@Override" in _node(nodes, "UserService.UserService.findAll")["annotations"]

    @pytest.mark.skipif(not _JAVA_TS_AVAILABLE, reason="requires the 'java' extra")
    def test_visibility_recorded(self, tmp_path):
        nodes, _ = index_java(_write(tmp_path, "UserService.java", JAVA_SOURCE), tmp_path)
        assert _node(nodes, "UserService.UserService.repo")["visibility"] == "private"
        assert _node(nodes, "UserService.UserService.helper")["visibility"] == "protected"

    @pytest.mark.skipif(not _JAVA_TS_AVAILABLE, reason="requires the 'java' extra")
    def test_generics_in_signature(self, tmp_path):
        nodes, _ = index_java(_write(tmp_path, "UserService.java", JAVA_SOURCE), tmp_path)
        assert "<T extends Comparable<T>>" in _node(
            nodes, "UserService.UserService.findAll")["signature"]

    @pytest.mark.skipif(not _JAVA_TS_AVAILABLE, reason="requires the 'java' extra")
    def test_extends_edge(self, tmp_path):
        _, edges = index_java(_write(tmp_path, "UserService.java", JAVA_SOURCE), tmp_path)
        assert ("UserService.UserService", "BaseService") in _relations(edges, "extends")

    @pytest.mark.skipif(not _JAVA_TS_AVAILABLE, reason="requires the 'java' extra")
    def test_implements_edges(self, tmp_path):
        _, edges = index_java(_write(tmp_path, "UserService.java", JAVA_SOURCE), tmp_path)
        implements = _relations(edges, "implements")
        assert ("UserService.UserService", "Repo") in implements
        assert ("UserService.UserService", "Closeable") in implements

    @pytest.mark.skipif(not _JAVA_TS_AVAILABLE, reason="requires the 'java' extra")
    def test_inner_class_is_nested_in_the_id(self, tmp_path):
        source = "public class Outer {\n  static class Inner {\n    void ping() {}\n  }\n}\n"
        nodes, _ = index_java(_write(tmp_path, "Outer.java", source), tmp_path)
        assert "Outer.Outer.Inner.ping" in _ids(nodes)

    def test_regex_fallback_extracts_class_and_methods(self):
        nodes, _ = _index_java_regex(JAVA_SOURCE, Path("UserService.java"), "UserService")
        ids = _ids(nodes)
        assert "UserService.UserService" in ids
        assert "UserService.UserService.findAll" in ids

    def test_regex_fallback_records_annotations(self):
        nodes, _ = _index_java_regex(JAVA_SOURCE, Path("UserService.java"), "UserService")
        assert "@Service" in _node(nodes, "UserService.UserService")["annotations"]

    def test_regex_fallback_records_visibility(self):
        nodes, _ = _index_java_regex(JAVA_SOURCE, Path("UserService.java"), "UserService")
        assert _node(nodes, "UserService.UserService.helper")["visibility"] == "protected"

    def test_regex_fallback_extends_and_implements(self):
        _, edges = _index_java_regex(JAVA_SOURCE, Path("UserService.java"), "UserService")
        assert ("UserService.UserService", "BaseService") in _relations(edges, "extends")
        assert ("UserService.UserService", "Repo") in _relations(edges, "implements")

    def test_regex_fallback_skips_control_flow(self):
        nodes, _ = _index_java_regex(JAVA_SOURCE, Path("UserService.java"), "UserService")
        assert "UserService.UserService.if" not in _ids(nodes)


# ---------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------


class TestIndexRust:
    @pytest.mark.skipif(not _RUST_TS_AVAILABLE, reason="requires the 'rust' extra")
    def test_struct_trait_enum_extracted(self, tmp_path):
        nodes, _ = index_rust(_write(tmp_path, "db.rs", RUST_SOURCE), tmp_path)
        ids = _ids(nodes)
        assert {"db.Db", "db.Storage", "db.Mode"} <= ids

    @pytest.mark.skipif(not _RUST_TS_AVAILABLE, reason="requires the 'rust' extra")
    def test_node_types(self, tmp_path):
        nodes, _ = index_rust(_write(tmp_path, "db.rs", RUST_SOURCE), tmp_path)
        assert _node(nodes, "db.Db")["type"] == "struct"
        assert _node(nodes, "db.Storage")["type"] == "trait"
        assert _node(nodes, "db.Mode")["type"] == "enum"
        assert _node(nodes, "db.config")["type"] == "module"

    @pytest.mark.skipif(not _RUST_TS_AVAILABLE, reason="requires the 'rust' extra")
    def test_macro_typed_as_macro(self, tmp_path):
        nodes, _ = index_rust(_write(tmp_path, "db.rs", RUST_SOURCE), tmp_path)
        assert _node(nodes, "db.my_macro")["type"] == "macro"

    @pytest.mark.skipif(not _RUST_TS_AVAILABLE, reason="requires the 'rust' extra")
    def test_visibility_pub_vs_private(self, tmp_path):
        nodes, _ = index_rust(_write(tmp_path, "db.rs", RUST_SOURCE), tmp_path)
        assert _node(nodes, "db.helper")["visibility"] == "pub"
        assert _node(nodes, "db.private_fn")["visibility"] == "private"

    @pytest.mark.skipif(not _RUST_TS_AVAILABLE, reason="requires the 'rust' extra")
    def test_lifetimes_in_signature(self, tmp_path):
        nodes, _ = index_rust(_write(tmp_path, "db.rs", RUST_SOURCE), tmp_path)
        helper = _node(nodes, "db.helper")
        assert helper["lifetimes"] == ["a"]
        assert "'a" in helper["signature"]

    @pytest.mark.skipif(not _RUST_TS_AVAILABLE, reason="requires the 'rust' extra")
    def test_impl_trait_creates_implements_edge(self, tmp_path):
        _, edges = index_rust(_write(tmp_path, "db.rs", RUST_SOURCE), tmp_path)
        assert ("db.Db", "Storage") in _relations(edges, "implements")

    @pytest.mark.skipif(not _RUST_TS_AVAILABLE, reason="requires the 'rust' extra")
    def test_inherent_impl_has_no_implements_edge(self, tmp_path):
        source = "pub struct A;\nimpl A { pub fn go() {} }\n"
        _, edges = index_rust(_write(tmp_path, "a.rs", source), tmp_path)
        assert _relations(edges, "implements") == set()

    @pytest.mark.skipif(not _RUST_TS_AVAILABLE, reason="requires the 'rust' extra")
    def test_impl_methods_attach_to_the_type(self, tmp_path):
        nodes, _ = index_rust(_write(tmp_path, "db.rs", RUST_SOURCE), tmp_path)
        assert "db.Db.new" in _ids(nodes)

    @pytest.mark.skipif(not _RUST_TS_AVAILABLE, reason="requires the 'rust' extra")
    def test_use_creates_imports_from_edge(self, tmp_path):
        _, edges = index_rust(_write(tmp_path, "db.rs", RUST_SOURCE), tmp_path)
        assert any(e["relation"] == "imports_from" for e in edges)

    def test_regex_fallback_extracts_items(self):
        nodes, _ = _index_rust_regex(RUST_SOURCE, Path("db.rs"), "db")
        ids = _ids(nodes)
        assert {"db.Db", "db.Storage", "db.Mode", "db.my_macro"} <= ids

    def test_regex_fallback_visibility(self):
        nodes, _ = _index_rust_regex(RUST_SOURCE, Path("db.rs"), "db")
        assert _node(nodes, "db.helper")["visibility"] == "pub"
        assert _node(nodes, "db.private_fn")["visibility"] == "private"

    def test_regex_fallback_lifetimes(self):
        nodes, _ = _index_rust_regex(RUST_SOURCE, Path("db.rs"), "db")
        assert _node(nodes, "db.helper")["lifetimes"] == ["a"]

    def test_regex_fallback_implements_edge(self):
        _, edges = _index_rust_regex(RUST_SOURCE, Path("db.rs"), "db")
        assert ("db.Db", "Storage") in _relations(edges, "implements")


# ---------------------------------------------------------------------------
# C#
# ---------------------------------------------------------------------------


class TestIndexCSharp:
    @pytest.mark.skipif(not _CSHARP_TS_AVAILABLE, reason="requires the 'csharp' extra")
    def test_namespace_nested_in_id(self, tmp_path):
        nodes, _ = index_csharp(_write(tmp_path, "Home.cs", CSHARP_SOURCE), tmp_path)
        assert "Home.MyApp.Controllers.HomeController.Index" in _ids(nodes)

    @pytest.mark.skipif(not _CSHARP_TS_AVAILABLE, reason="requires the 'csharp' extra")
    def test_class_attribute_recorded(self, tmp_path):
        nodes, _ = index_csharp(_write(tmp_path, "Home.cs", CSHARP_SOURCE), tmp_path)
        node = _node(nodes, "Home.MyApp.Controllers.HomeController")
        assert "[Authorize]" in node["attributes"]

    @pytest.mark.skipif(not _CSHARP_TS_AVAILABLE, reason="requires the 'csharp' extra")
    def test_method_attribute_recorded(self, tmp_path):
        nodes, _ = index_csharp(_write(tmp_path, "Home.cs", CSHARP_SOURCE), tmp_path)
        node = _node(nodes, "Home.MyApp.Controllers.HomeController.Index")
        assert "[HttpGet]" in node["attributes"]

    @pytest.mark.skipif(not _CSHARP_TS_AVAILABLE, reason="requires the 'csharp' extra")
    def test_async_method_flagged(self, tmp_path):
        nodes, _ = index_csharp(_write(tmp_path, "Home.cs", CSHARP_SOURCE), tmp_path)
        assert _node(nodes, "Home.MyApp.Controllers.HomeController.Index")["is_async"] is True

    @pytest.mark.skipif(not _CSHARP_TS_AVAILABLE, reason="requires the 'csharp' extra")
    def test_sync_method_not_flagged_async(self, tmp_path):
        nodes, _ = index_csharp(_write(tmp_path, "Home.cs", CSHARP_SOURCE), tmp_path)
        assert "is_async" not in _node(nodes, "Home.MyApp.Controllers.HomeController.Save")

    @pytest.mark.skipif(not _CSHARP_TS_AVAILABLE, reason="requires the 'csharp' extra")
    def test_property_typed_as_property(self, tmp_path):
        nodes, _ = index_csharp(_write(tmp_path, "Home.cs", CSHARP_SOURCE), tmp_path)
        node = _node(nodes, "Home.MyApp.Controllers.HomeController.Name")
        assert node["type"] == "property"
        assert node["label_qualified"] == "HomeController.Name"

    @pytest.mark.skipif(not _CSHARP_TS_AVAILABLE, reason="requires the 'csharp' extra")
    def test_interface_prefix_detected(self, tmp_path):
        nodes, _ = index_csharp(_write(tmp_path, "Home.cs", CSHARP_SOURCE), tmp_path)
        assert _node(nodes, "Home.MyApp.Controllers.IRepo")["is_interface"] is True

    @pytest.mark.skipif(not _CSHARP_TS_AVAILABLE, reason="requires the 'csharp' extra")
    def test_class_is_not_flagged_as_interface(self, tmp_path):
        nodes, _ = index_csharp(_write(tmp_path, "Home.cs", CSHARP_SOURCE), tmp_path)
        node = _node(nodes, "Home.MyApp.Controllers.HomeController")
        assert node.get("is_interface", False) is False

    @pytest.mark.skipif(not _CSHARP_TS_AVAILABLE, reason="requires the 'csharp' extra")
    def test_base_list_splits_extends_and_implements(self, tmp_path):
        _, edges = index_csharp(_write(tmp_path, "Home.cs", CSHARP_SOURCE), tmp_path)
        cls = "Home.MyApp.Controllers.HomeController"
        assert (cls, "ControllerBase") in _relations(edges, "extends")
        assert (cls, "IRepo") in _relations(edges, "implements")

    @pytest.mark.skipif(not _CSHARP_TS_AVAILABLE, reason="requires the 'csharp' extra")
    def test_constructor_typed_as_constructor(self, tmp_path):
        nodes, _ = index_csharp(_write(tmp_path, "Home.cs", CSHARP_SOURCE), tmp_path)
        node = _node(nodes, "Home.MyApp.Controllers.HomeController.HomeController")
        assert node["type"] == "constructor"

    def test_regex_fallback_extracts_types_and_members(self):
        nodes, _ = _index_csharp_regex(CSHARP_SOURCE, Path("Home.cs"), "Home")
        ids = _ids(nodes)
        assert "Home.MyApp.Controllers.HomeController" in ids
        assert "Home.MyApp.Controllers.HomeController.Index" in ids

    def test_regex_fallback_attributes(self):
        nodes, _ = _index_csharp_regex(CSHARP_SOURCE, Path("Home.cs"), "Home")
        node = _node(nodes, "Home.MyApp.Controllers.HomeController")
        assert node["attributes"] == ["[Authorize]"]

    def test_regex_fallback_async_flag(self):
        nodes, _ = _index_csharp_regex(CSHARP_SOURCE, Path("Home.cs"), "Home")
        assert _node(nodes, "Home.MyApp.Controllers.HomeController.Index")["is_async"] is True

    def test_regex_fallback_property(self):
        nodes, _ = _index_csharp_regex(CSHARP_SOURCE, Path("Home.cs"), "Home")
        assert _node(nodes, "Home.MyApp.Controllers.HomeController.Name")["type"] == "property"

    def test_regex_fallback_edges(self):
        _, edges = _index_csharp_regex(CSHARP_SOURCE, Path("Home.cs"), "Home")
        cls = "Home.MyApp.Controllers.HomeController"
        assert (cls, "ControllerBase") in _relations(edges, "extends")
        assert (cls, "IRepo") in _relations(edges, "implements")


# ---------------------------------------------------------------------------
# Ruby
# ---------------------------------------------------------------------------


class TestIndexRuby:
    @pytest.mark.skipif(not _RUBY_TS_AVAILABLE, reason="requires the 'ruby' extra")
    def test_module_and_class_nested(self, tmp_path):
        nodes, _ = index_ruby(_write(tmp_path, "user.rb", RUBY_SOURCE), tmp_path)
        ids = _ids(nodes)
        assert "user.Auth" in ids
        assert "user.Auth.UserService" in ids

    @pytest.mark.skipif(not _RUBY_TS_AVAILABLE, reason="requires the 'ruby' extra")
    def test_instance_method(self, tmp_path):
        nodes, _ = index_ruby(_write(tmp_path, "user.rb", RUBY_SOURCE), tmp_path)
        node = _node(nodes, "user.Auth.UserService.initialize")
        assert node["type"] == "method"
        assert node["is_class_method"] is False

    @pytest.mark.skipif(not _RUBY_TS_AVAILABLE, reason="requires the 'ruby' extra")
    def test_class_method_flagged(self, tmp_path):
        nodes, _ = index_ruby(_write(tmp_path, "user.rb", RUBY_SOURCE), tmp_path)
        assert _node(nodes, "user.Auth.UserService.create")["is_class_method"] is True

    @pytest.mark.skipif(not _RUBY_TS_AVAILABLE, reason="requires the 'ruby' extra")
    def test_method_missing_is_dynamic(self, tmp_path):
        nodes, _ = index_ruby(_write(tmp_path, "user.rb", RUBY_SOURCE), tmp_path)
        assert _node(nodes, "user.Auth.UserService.method_missing")["type"] == "dynamic"

    @pytest.mark.skipif(not _RUBY_TS_AVAILABLE, reason="requires the 'ruby' extra")
    def test_attr_accessor_creates_accessor_nodes(self, tmp_path):
        nodes, _ = index_ruby(_write(tmp_path, "user.rb", RUBY_SOURCE), tmp_path)
        name = _node(nodes, "user.Auth.UserService.name")
        assert name["type"] == "accessor"
        assert name["accessor_kind"] == "attr_accessor"

    @pytest.mark.skipif(not _RUBY_TS_AVAILABLE, reason="requires the 'ruby' extra")
    def test_attr_reader_kind_recorded(self, tmp_path):
        nodes, _ = index_ruby(_write(tmp_path, "user.rb", RUBY_SOURCE), tmp_path)
        assert _node(nodes, "user.Auth.UserService.id")["accessor_kind"] == "attr_reader"

    @pytest.mark.skipif(not _RUBY_TS_AVAILABLE, reason="requires the 'ruby' extra")
    def test_constant_and_proc(self, tmp_path):
        nodes, _ = index_ruby(_write(tmp_path, "user.rb", RUBY_SOURCE), tmp_path)
        assert _node(nodes, "user.Auth.CONFIG")["type"] == "constant"
        assert _node(nodes, "user.Auth.HANDLER")["type"] == "proc"

    @pytest.mark.skipif(not _RUBY_TS_AVAILABLE, reason="requires the 'ruby' extra")
    def test_mixin_edges(self, tmp_path):
        _, edges = index_ruby(_write(tmp_path, "user.rb", RUBY_SOURCE), tmp_path)
        mixins = _relations(edges, "mixin")
        cls = "user.Auth.UserService"
        assert (cls, "Comparable") in mixins
        assert (cls, "Forwardable") in mixins
        assert (cls, "Loggable") in mixins

    @pytest.mark.skipif(not _RUBY_TS_AVAILABLE, reason="requires the 'ruby' extra")
    def test_superclass_edge(self, tmp_path):
        _, edges = index_ruby(_write(tmp_path, "user.rb", RUBY_SOURCE), tmp_path)
        assert ("user.Auth.UserService", "Base") in _relations(edges, "extends")

    def test_regex_fallback_nesting(self):
        nodes, _ = _index_ruby_regex(RUBY_SOURCE, Path("user.rb"), "user")
        assert "user.Auth.UserService.initialize" in _ids(nodes)

    def test_regex_fallback_class_method_flag(self):
        nodes, _ = _index_ruby_regex(RUBY_SOURCE, Path("user.rb"), "user")
        assert _node(nodes, "user.Auth.UserService.create")["is_class_method"] is True

    def test_regex_fallback_method_missing(self):
        nodes, _ = _index_ruby_regex(RUBY_SOURCE, Path("user.rb"), "user")
        assert _node(nodes, "user.Auth.UserService.method_missing")["type"] == "dynamic"

    def test_regex_fallback_accessors(self):
        nodes, _ = _index_ruby_regex(RUBY_SOURCE, Path("user.rb"), "user")
        assert _node(nodes, "user.Auth.UserService.email")["accessor_kind"] == "attr_accessor"

    def test_regex_fallback_proc_vs_constant(self):
        nodes, _ = _index_ruby_regex(RUBY_SOURCE, Path("user.rb"), "user")
        assert _node(nodes, "user.Auth.HANDLER")["type"] == "proc"
        assert _node(nodes, "user.Auth.CONFIG")["type"] == "constant"

    def test_regex_fallback_mixin_edges(self):
        _, edges = _index_ruby_regex(RUBY_SOURCE, Path("user.rb"), "user")
        assert ("user.Auth.UserService", "Comparable") in _relations(edges, "mixin")


# ---------------------------------------------------------------------------
# Wiring: extensions, project indexing, parser summary
# ---------------------------------------------------------------------------


class TestNewLanguageWiring:
    @pytest.mark.parametrize("ext", [".java", ".rs", ".cs", ".rb"])
    def test_extension_registered(self, ext):
        from slurp.indexer import _INDEXED_EXTENSIONS
        assert ext in _INDEXED_EXTENSIONS

    def test_index_project_picks_up_all_four(self, tmp_path):
        _write(tmp_path, "A.java", JAVA_SOURCE)
        _write(tmp_path, "b.rs", RUST_SOURCE)
        _write(tmp_path, "C.cs", CSHARP_SOURCE)
        _write(tmp_path, "d.rb", RUBY_SOURCE)
        graph = index_project(tmp_path)
        files = {n["source_file"] for n in graph["nodes"] if n.get("source_file")}
        assert {"A.java", "b.rs", "C.cs", "d.rb"} <= files

    @pytest.mark.parametrize("ext,language", [
        (".java", "Java"), (".rs", "Rust"), (".cs", "C#"), (".rb", "Ruby"),
    ])
    def test_parser_summary_lists_language(self, ext, language):
        assert any(lang == language for lang, _, _ in parser_summary({ext}))

    @pytest.mark.parametrize("ext,flag", [
        (".java", "_JAVA_TS_AVAILABLE"), (".rs", "_RUST_TS_AVAILABLE"),
        (".cs", "_CSHARP_TS_AVAILABLE"), (".rb", "_RUBY_TS_AVAILABLE"),
    ])
    def test_parser_summary_reports_fallback(self, ext, flag, monkeypatch):
        monkeypatch.setattr(indexer_mod, flag, False)
        _, parser, is_fallback = parser_summary({ext})[0]
        assert parser == "regex fallback"
        assert is_fallback is True

    def test_go_is_not_marked_as_fallback(self):
        assert parser_summary({".go"}) == [("Go", "regex", False)]

    def test_cli_shows_the_new_languages(self, tmp_path):
        _write(tmp_path, "A.java", JAVA_SOURCE)
        _write(tmp_path, "b.rs", RUST_SOURCE)
        result = CliRunner().invoke(
            cli, ["index", str(tmp_path), "--output", str(tmp_path / "g.json")])
        assert result.exit_code == 0, result.output
        assert "Java" in result.output
        assert "Rust" in result.output


class TestNewLanguageFallbackSilence:
    """An absent grammar must degrade quietly, exactly like TypeScript's."""

    @pytest.mark.parametrize("flag,fn,name,source", [
        ("_JAVA_TS_AVAILABLE", "index_java", "A.java", JAVA_SOURCE),
        ("_RUST_TS_AVAILABLE", "index_rust", "b.rs", RUST_SOURCE),
        ("_CSHARP_TS_AVAILABLE", "index_csharp", "C.cs", CSHARP_SOURCE),
        ("_RUBY_TS_AVAILABLE", "index_ruby", "d.rb", RUBY_SOURCE),
    ])
    def test_missing_grammar_is_silent(self, flag, fn, name, source,
                                       tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(indexer_mod, flag, False)
        path = _write(tmp_path, name, source)
        nodes, _ = getattr(indexer_mod, fn)(path, tmp_path)
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""
        assert len(nodes) > 1  # regex still produced something


# ---------------------------------------------------------------------------
# PHP / Kotlin / Scala / Swift — sample sources
# ---------------------------------------------------------------------------

PHP_SOURCE = '''\
<?php
namespace App\\Controllers;
use App\\Traits\\Loggable;
#[Route('/home')]
class HomeController extends Base implements Jsonable {
    use Loggable;
    private string $name;
    public function __construct(string $n) { $this->name = $n; }
    public function __get($k) { return null; }
    protected function index(int $id): string { return "x"; }
}
function helper(): void {}
'''

KOTLIN_SOURCE = '''\
package com.example
data class User(val id: Int, val name: String)
sealed class Result {
}
object Singleton { fun ping() {} }
class Repo {
    companion object { fun create() = Repo() }
    suspend fun fetch(id: Int): User? = null
}
fun String.slugify(): String = this
'''

SCALA_SOURCE = '''\
package com.example
trait Storage { def get(k: String): Option[String] }
case class User(id: Int, name: String)
class Repo(db: String) extends Base with Storage {
  val cache = Map.empty[String, String]
  var counter = 0
  def find(id: Int): Option[User] = None
  implicit def toStr(u: User): String = u.name
}
object Repo { def apply() = new Repo("") }
'''

SWIFT_SOURCE = '''\
import Foundation
protocol Drawable { func draw() }
struct Point: Drawable {
    var x: Int
    func draw() {}
}
class ViewController: UIViewController, Drawable {
    @IBOutlet var label: UILabel!
    var area: Int { return 2 }
    func draw() {}
    func load() async throws {}
}
extension Point {
    func scaled() -> Point { return self }
}
enum State { case on }
'''


# ---------------------------------------------------------------------------
# PHP
# ---------------------------------------------------------------------------


class TestIndexPhp:
    @pytest.mark.skipif(not _PHP_TS_AVAILABLE, reason="requires the 'php' extra")
    def test_class_and_methods(self, tmp_path):
        nodes, _ = index_php(_write(tmp_path, "Home.php", PHP_SOURCE), tmp_path)
        ids = _ids(nodes)
        assert "Home.HomeController" in ids
        assert "Home.HomeController.index" in ids

    @pytest.mark.skipif(not _PHP_TS_AVAILABLE, reason="requires the 'php' extra")
    def test_namespace_extracted(self, tmp_path):
        nodes, _ = index_php(_write(tmp_path, "Home.php", PHP_SOURCE), tmp_path)
        assert any(n["type"] == "namespace" for n in nodes)

    @pytest.mark.skipif(not _PHP_TS_AVAILABLE, reason="requires the 'php' extra")
    def test_constructor_typed(self, tmp_path):
        nodes, _ = index_php(_write(tmp_path, "Home.php", PHP_SOURCE), tmp_path)
        assert _node(nodes, "Home.HomeController.__construct")["type"] == "constructor"

    @pytest.mark.skipif(not _PHP_TS_AVAILABLE, reason="requires the 'php' extra")
    def test_magic_method_typed(self, tmp_path):
        nodes, _ = index_php(_write(tmp_path, "Home.php", PHP_SOURCE), tmp_path)
        assert _node(nodes, "Home.HomeController.__get")["type"] == "magic"

    @pytest.mark.skipif(not _PHP_TS_AVAILABLE, reason="requires the 'php' extra")
    def test_attribute_recorded(self, tmp_path):
        nodes, _ = index_php(_write(tmp_path, "Home.php", PHP_SOURCE), tmp_path)
        assert "#[Route]" in _node(nodes, "Home.HomeController")["attributes"]

    @pytest.mark.skipif(not _PHP_TS_AVAILABLE, reason="requires the 'php' extra")
    def test_visibility_recorded(self, tmp_path):
        nodes, _ = index_php(_write(tmp_path, "Home.php", PHP_SOURCE), tmp_path)
        assert _node(nodes, "Home.HomeController.index")["visibility"] == "protected"

    @pytest.mark.skipif(not _PHP_TS_AVAILABLE, reason="requires the 'php' extra")
    def test_trait_use_is_a_mixin_edge(self, tmp_path):
        _, edges = index_php(_write(tmp_path, "Home.php", PHP_SOURCE), tmp_path)
        assert ("Home.HomeController", "Loggable") in _relations(edges, "mixin")

    @pytest.mark.skipif(not _PHP_TS_AVAILABLE, reason="requires the 'php' extra")
    def test_extends_and_implements(self, tmp_path):
        _, edges = index_php(_write(tmp_path, "Home.php", PHP_SOURCE), tmp_path)
        assert ("Home.HomeController", "Base") in _relations(edges, "extends")
        assert ("Home.HomeController", "Jsonable") in _relations(edges, "implements")

    @pytest.mark.skipif(not _PHP_TS_AVAILABLE, reason="requires the 'php' extra")
    def test_trait_declaration_typed(self, tmp_path):
        nodes, _ = index_php(_write(tmp_path, "T.php", "<?php\ntrait T { public function g() {} }\n"), tmp_path)
        assert _node(nodes, "T.T")["type"] == "trait"

    def test_regex_fallback_class_and_members(self):
        nodes, _ = _index_php_regex(PHP_SOURCE, Path("Home.php"), "Home")
        ids = _ids(nodes)
        assert "Home.App\\Controllers.HomeController" in ids or "Home.HomeController" in ids
        assert any(i.endswith(".index") for i in ids)

    def test_regex_fallback_magic_method(self):
        nodes, _ = _index_php_regex(PHP_SOURCE, Path("Home.php"), "Home")
        magic = [n for n in nodes if n["type"] == "magic"]
        assert any(n["label"] == "__get" for n in magic)

    def test_regex_fallback_constructor(self):
        nodes, _ = _index_php_regex(PHP_SOURCE, Path("Home.php"), "Home")
        assert any(n["type"] == "constructor" and n["label"] == "__construct" for n in nodes)

    def test_regex_fallback_mixin_edge(self):
        _, edges = _index_php_regex(PHP_SOURCE, Path("Home.php"), "Home")
        assert any(e["relation"] == "mixin" and e["target"] == "Loggable" for e in edges)

    def test_regex_fallback_visibility(self):
        nodes, _ = _index_php_regex(PHP_SOURCE, Path("Home.php"), "Home")
        index = next(n for n in nodes if n["label"] == "index")
        assert index["visibility"] == "protected"


# ---------------------------------------------------------------------------
# Kotlin
# ---------------------------------------------------------------------------


class TestIndexKotlin:
    def test_data_class_flagged(self, tmp_path):
        nodes, _ = index_kotlin(_write(tmp_path, "Repo.kt", KOTLIN_SOURCE), tmp_path)
        assert _node(nodes, "Repo.User")["is_data_class"] is True

    def test_sealed_class_flagged(self, tmp_path):
        nodes, _ = index_kotlin(_write(tmp_path, "Repo.kt", KOTLIN_SOURCE), tmp_path)
        assert _node(nodes, "Repo.Result")["is_sealed"] is True

    def test_object_typed_as_object(self, tmp_path):
        nodes, _ = index_kotlin(_write(tmp_path, "Repo.kt", KOTLIN_SOURCE), tmp_path)
        assert _node(nodes, "Repo.Singleton")["type"] == "object"

    def test_companion_nested_under_class(self, tmp_path):
        nodes, _ = index_kotlin(_write(tmp_path, "Repo.kt", KOTLIN_SOURCE), tmp_path)
        companion = _node(nodes, "Repo.Repo.Companion")
        assert companion["type"] == "object"
        assert companion["is_companion"] is True

    def test_suspend_function_flagged(self, tmp_path):
        nodes, _ = index_kotlin(_write(tmp_path, "Repo.kt", KOTLIN_SOURCE), tmp_path)
        assert _node(nodes, "Repo.Repo.fetch")["is_suspend"] is True

    def test_extension_function_records_receiver(self, tmp_path):
        nodes, _ = index_kotlin(_write(tmp_path, "Repo.kt", KOTLIN_SOURCE), tmp_path)
        assert _node(nodes, "Repo.slugify")["extends_type"] == "String"

    def test_package_extracted(self, tmp_path):
        nodes, _ = index_kotlin(_write(tmp_path, "Repo.kt", KOTLIN_SOURCE), tmp_path)
        assert any(n["type"] == "package" for n in nodes)

    def test_constructor_params_are_not_supertypes(self, tmp_path):
        """`data class User(val id: Int)` must not yield `User extends Int`."""
        _, edges = index_kotlin(_write(tmp_path, "Repo.kt", KOTLIN_SOURCE), tmp_path)
        assert ("Repo.User", "Int") not in _relations(edges, "extends")

    def test_regex_fallback_covers_everything(self):
        nodes, _ = _index_kotlin_regex(KOTLIN_SOURCE, Path("Repo.kt"), "Repo")
        ids = _ids(nodes)
        assert {"Repo.User", "Repo.Singleton", "Repo.Repo",
                "Repo.Repo.Companion", "Repo.slugify"} <= ids

    def test_regex_fallback_suspend(self):
        nodes, _ = _index_kotlin_regex(KOTLIN_SOURCE, Path("Repo.kt"), "Repo")
        assert _node(nodes, "Repo.Repo.fetch")["is_suspend"] is True


# ---------------------------------------------------------------------------
# Scala
# ---------------------------------------------------------------------------


class TestIndexScala:
    @pytest.mark.skipif(not _SCALA_TS_AVAILABLE, reason="requires the 'scala' extra")
    def test_case_class_flagged(self, tmp_path):
        nodes, _ = index_scala(_write(tmp_path, "Repo.scala", SCALA_SOURCE), tmp_path)
        assert _node(nodes, "Repo.User")["is_case"] is True

    @pytest.mark.skipif(not _SCALA_TS_AVAILABLE, reason="requires the 'scala' extra")
    def test_trait_typed(self, tmp_path):
        nodes, _ = index_scala(_write(tmp_path, "Repo.scala", SCALA_SOURCE), tmp_path)
        assert _node(nodes, "Repo.Storage")["type"] == "trait"

    @pytest.mark.skipif(not _SCALA_TS_AVAILABLE, reason="requires the 'scala' extra")
    def test_def_val_var_preserved_as_types(self, tmp_path):
        nodes, _ = index_scala(_write(tmp_path, "Repo.scala", SCALA_SOURCE), tmp_path)
        assert _node(nodes, "Repo.Repo.find")["type"] == "def"
        assert _node(nodes, "Repo.Repo.cache")["type"] == "val"
        assert _node(nodes, "Repo.Repo.counter")["type"] == "var"

    @pytest.mark.skipif(not _SCALA_TS_AVAILABLE, reason="requires the 'scala' extra")
    def test_implicit_flagged(self, tmp_path):
        nodes, _ = index_scala(_write(tmp_path, "Repo.scala", SCALA_SOURCE), tmp_path)
        assert _node(nodes, "Repo.Repo.toStr")["is_implicit"] is True

    @pytest.mark.skipif(not _SCALA_TS_AVAILABLE, reason="requires the 'scala' extra")
    def test_non_implicit_not_flagged(self, tmp_path):
        nodes, _ = index_scala(_write(tmp_path, "Repo.scala", SCALA_SOURCE), tmp_path)
        assert "is_implicit" not in _node(nodes, "Repo.Repo.find")

    @pytest.mark.skipif(not _SCALA_TS_AVAILABLE, reason="requires the 'scala' extra")
    def test_extends_and_with_edges(self, tmp_path):
        _, edges = index_scala(_write(tmp_path, "Repo.scala", SCALA_SOURCE), tmp_path)
        assert ("Repo.Repo", "Base") in _relations(edges, "extends")
        assert ("Repo.Repo", "Storage") in _relations(edges, "with")

    @pytest.mark.skipif(not _SCALA_TS_AVAILABLE, reason="requires the 'scala' extra")
    def test_companion_object_does_not_collide_with_class(self, tmp_path):
        nodes, _ = index_scala(_write(tmp_path, "Repo.scala", SCALA_SOURCE), tmp_path)
        ids = [n["id"] for n in nodes]
        assert len(ids) == len(set(ids))
        assert _node(nodes, "Repo.Companion")["is_companion"] is True

    @pytest.mark.skipif(not _SCALA_TS_AVAILABLE, reason="requires the 'scala' extra")
    def test_pattern_matching_is_not_a_node(self, tmp_path):
        source = "object A { def f(x: Int) = x match { case 1 => 1; case _ => 0 } }\n"
        nodes, _ = index_scala(_write(tmp_path, "A.scala", source), tmp_path)
        assert not any(n["label"] == "match" for n in nodes)

    def test_regex_fallback_case_class(self):
        nodes, _ = _index_scala_regex(SCALA_SOURCE, Path("Repo.scala"), "Repo")
        assert _node(nodes, "Repo.User")["is_case"] is True

    def test_regex_fallback_def_val_var(self):
        nodes, _ = _index_scala_regex(SCALA_SOURCE, Path("Repo.scala"), "Repo")
        assert _node(nodes, "Repo.Repo.cache")["type"] == "val"
        assert _node(nodes, "Repo.Repo.counter")["type"] == "var"

    def test_regex_fallback_implicit(self):
        nodes, _ = _index_scala_regex(SCALA_SOURCE, Path("Repo.scala"), "Repo")
        assert _node(nodes, "Repo.Repo.toStr")["is_implicit"] is True

    def test_regex_fallback_with_edge(self):
        _, edges = _index_scala_regex(SCALA_SOURCE, Path("Repo.scala"), "Repo")
        assert ("Repo.Repo", "Storage") in _relations(edges, "with")


# ---------------------------------------------------------------------------
# Swift
# ---------------------------------------------------------------------------


class TestIndexSwift:
    @pytest.mark.skipif(not _SWIFT_TS_AVAILABLE, reason="requires the 'swift' extra")
    def test_protocol_typed(self, tmp_path):
        nodes, _ = index_swift(_write(tmp_path, "View.swift", SWIFT_SOURCE), tmp_path)
        assert _node(nodes, "View.Drawable")["type"] == "protocol"

    @pytest.mark.skipif(not _SWIFT_TS_AVAILABLE, reason="requires the 'swift' extra")
    def test_struct_and_class_distinguished(self, tmp_path):
        nodes, _ = index_swift(_write(tmp_path, "View.swift", SWIFT_SOURCE), tmp_path)
        assert _node(nodes, "View.Point")["type"] == "struct"
        assert _node(nodes, "View.ViewController")["type"] == "class"

    @pytest.mark.skipif(not _SWIFT_TS_AVAILABLE, reason="requires the 'swift' extra")
    def test_enum_distinguished(self, tmp_path):
        nodes, _ = index_swift(_write(tmp_path, "View.swift", SWIFT_SOURCE), tmp_path)
        assert _node(nodes, "View.State")["type"] == "enum"

    @pytest.mark.skipif(not _SWIFT_TS_AVAILABLE, reason="requires the 'swift' extra")
    def test_conforms_to_edges(self, tmp_path):
        _, edges = index_swift(_write(tmp_path, "View.swift", SWIFT_SOURCE), tmp_path)
        conforms = _relations(edges, "conforms_to")
        assert ("View.Point", "Drawable") in conforms
        assert ("View.ViewController", "Drawable") in conforms

    @pytest.mark.skipif(not _SWIFT_TS_AVAILABLE, reason="requires the 'swift' extra")
    def test_extension_typed_and_linked(self, tmp_path):
        nodes, edges = index_swift(_write(tmp_path, "View.swift", SWIFT_SOURCE), tmp_path)
        assert any(n["type"] == "extension" for n in nodes)
        assert any(e["relation"] == "extends" and e["target"] == "Point" for e in edges)

    @pytest.mark.skipif(not _SWIFT_TS_AVAILABLE, reason="requires the 'swift' extra")
    def test_attribute_recorded(self, tmp_path):
        nodes, _ = index_swift(_write(tmp_path, "View.swift", SWIFT_SOURCE), tmp_path)
        assert "@IBOutlet" in _node(nodes, "View.ViewController.label")["attributes"]

    @pytest.mark.skipif(not _SWIFT_TS_AVAILABLE, reason="requires the 'swift' extra")
    def test_computed_property_flagged(self, tmp_path):
        nodes, _ = index_swift(_write(tmp_path, "View.swift", SWIFT_SOURCE), tmp_path)
        assert _node(nodes, "View.ViewController.area")["is_computed"] is True

    @pytest.mark.skipif(not _SWIFT_TS_AVAILABLE, reason="requires the 'swift' extra")
    def test_stored_property_not_computed(self, tmp_path):
        nodes, _ = index_swift(_write(tmp_path, "View.swift", SWIFT_SOURCE), tmp_path)
        assert "is_computed" not in _node(nodes, "View.Point.x")

    @pytest.mark.skipif(not _SWIFT_TS_AVAILABLE, reason="requires the 'swift' extra")
    def test_async_function_flagged(self, tmp_path):
        nodes, _ = index_swift(_write(tmp_path, "View.swift", SWIFT_SOURCE), tmp_path)
        assert _node(nodes, "View.ViewController.load")["is_async"] is True

    @pytest.mark.skipif(not _SWIFT_TS_AVAILABLE, reason="requires the 'swift' extra")
    def test_guard_and_defer_are_not_nodes(self, tmp_path):
        source = "class A {\n  func go() {\n    guard let x = y else { return }\n    defer { cleanup() }\n  }\n}\n"
        nodes, _ = index_swift(_write(tmp_path, "A.swift", source), tmp_path)
        labels = {n["label"] for n in nodes}
        assert "guard" not in labels
        assert "defer" not in labels

    def test_regex_fallback_types(self):
        nodes, _ = _index_swift_regex(SWIFT_SOURCE, Path("View.swift"), "View")
        assert _node(nodes, "View.Drawable")["type"] == "protocol"
        assert _node(nodes, "View.Point")["type"] == "struct"

    def test_regex_fallback_computed_property(self):
        nodes, _ = _index_swift_regex(SWIFT_SOURCE, Path("View.swift"), "View")
        assert _node(nodes, "View.ViewController.area")["is_computed"] is True

    def test_regex_fallback_async(self):
        nodes, _ = _index_swift_regex(SWIFT_SOURCE, Path("View.swift"), "View")
        assert _node(nodes, "View.ViewController.load")["is_async"] is True

    def test_regex_fallback_conforms_to(self):
        _, edges = _index_swift_regex(SWIFT_SOURCE, Path("View.swift"), "View")
        assert ("View.Point", "Drawable") in _relations(edges, "conforms_to")


# ---------------------------------------------------------------------------
# Broken-grammar guard + wiring for the second language batch
# ---------------------------------------------------------------------------


class TestBrokenTreeGuard:
    def test_clean_tree_is_not_broken(self):
        class _Root:
            has_error = False
        class _Tree:
            root_node = _Root()
        assert _tree_is_broken(_Tree()) is False

    def test_error_tree_is_broken(self):
        class _Root:
            has_error = True
        class _Tree:
            root_node = _Root()
        assert _tree_is_broken(_Tree()) is True

    def test_missing_root_is_not_broken(self):
        class _Tree:
            root_node = None
        assert _tree_is_broken(_Tree()) is False

    @pytest.mark.skipif(not _KOTLIN_TS_AVAILABLE, reason="requires the 'kotlin' extra")
    def test_kotlin_falls_back_and_warns(self, tmp_path, capsys):
        """tree-sitter-kotlin 1.1.0 mis-parses class-then-object; the guard
        must catch it rather than silently losing most of the file."""
        nodes, _ = index_kotlin(_write(tmp_path, "Repo.kt", KOTLIN_SOURCE), tmp_path)
        assert "ERROR nodes" in capsys.readouterr().err
        assert "Repo.Singleton" in _ids(nodes)


class TestSecondBatchWiring:
    @pytest.mark.parametrize("ext", [".php", ".kt", ".kts", ".scala", ".swift"])
    def test_extension_registered(self, ext):
        from slurp.indexer import _INDEXED_EXTENSIONS
        assert ext in _INDEXED_EXTENSIONS

    @pytest.mark.parametrize("ext,language", [
        (".php", "PHP"), (".kt", "Kotlin"), (".scala", "Scala"), (".swift", "Swift"),
    ])
    def test_parser_summary_lists_language(self, ext, language):
        assert any(lang == language for lang, _, _ in parser_summary({ext}))

    @pytest.mark.parametrize("ext,flag", [
        (".php", "_PHP_TS_AVAILABLE"), (".kt", "_KOTLIN_TS_AVAILABLE"),
        (".scala", "_SCALA_TS_AVAILABLE"), (".swift", "_SWIFT_TS_AVAILABLE"),
    ])
    def test_parser_summary_reports_fallback(self, ext, flag, monkeypatch):
        monkeypatch.setattr(indexer_mod, flag, False)
        _, parser, is_fallback = parser_summary({ext})[0]
        assert parser == "regex fallback"
        assert is_fallback is True

    def test_index_project_picks_up_all_four(self, tmp_path):
        _write(tmp_path, "a.php", PHP_SOURCE)
        _write(tmp_path, "b.kt", KOTLIN_SOURCE)
        _write(tmp_path, "c.scala", SCALA_SOURCE)
        _write(tmp_path, "d.swift", SWIFT_SOURCE)
        graph = index_project(tmp_path)
        files = {n["source_file"] for n in graph["nodes"] if n.get("source_file")}
        assert {"a.php", "b.kt", "c.scala", "d.swift"} <= files

    @pytest.mark.parametrize("flag,fn,name,source", [
        ("_PHP_TS_AVAILABLE", "index_php", "a.php", PHP_SOURCE),
        ("_KOTLIN_TS_AVAILABLE", "index_kotlin", "b.kt", KOTLIN_SOURCE),
        ("_SCALA_TS_AVAILABLE", "index_scala", "c.scala", SCALA_SOURCE),
        ("_SWIFT_TS_AVAILABLE", "index_swift", "d.swift", SWIFT_SOURCE),
    ])
    def test_missing_grammar_is_silent(self, flag, fn, name, source,
                                       tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(indexer_mod, flag, False)
        nodes, _ = getattr(indexer_mod, fn)(_write(tmp_path, name, source), tmp_path)
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""
        assert len(nodes) > 1


# ---------------------------------------------------------------------------
# format_parser_summary — line wrapping
# ---------------------------------------------------------------------------


class TestFormatParserSummary:
    @staticmethod
    def _summary(n: int) -> list[tuple[str, str, bool]]:
        names = ["Python", "TypeScript", "Java", "Rust", "C#",
                 "Ruby", "PHP", "Kotlin", "Scala", "Swift"]
        return [(names[i], "tree-sitter", False) for i in range(n)]

    def test_empty_summary_yields_no_lines(self):
        assert format_parser_summary([]) == []

    @pytest.mark.parametrize("n", [1, 2, 3, 4])
    def test_four_or_fewer_stay_on_one_line(self, n):
        assert len(format_parser_summary(self._summary(n))) == 1

    @pytest.mark.parametrize("n,expected", [(5, 2), (6, 2), (7, 3), (9, 3), (10, 4)])
    def test_more_than_four_wrap_at_three_per_line(self, n, expected):
        assert len(format_parser_summary(self._summary(n))) == expected

    def test_first_line_starts_with_the_prefix(self):
        lines = format_parser_summary(self._summary(10))
        assert lines[0].startswith("  Parsers: ")

    def test_continuation_lines_indent_eleven_spaces(self):
        lines = format_parser_summary(self._summary(10))
        for line in lines[1:]:
            assert line.startswith(" " * 11)
            assert not line.startswith(" " * 12)

    def test_continuation_aligns_under_the_first_language(self):
        lines = format_parser_summary(self._summary(10))
        first_language_col = lines[0].index("Python")
        for line in lines[1:]:
            assert len(line) - len(line.lstrip()) == first_language_col

    def test_no_line_holds_more_than_three_languages(self):
        for line in format_parser_summary(self._summary(10)):
            assert line.count("·") <= 2

    def test_every_language_appears_exactly_once(self):
        joined = " ".join(format_parser_summary(self._summary(10)))
        for lang in ("Python", "TypeScript", "Java", "Rust", "C#",
                     "Ruby", "PHP", "Kotlin", "Scala", "Swift"):
            assert joined.count(f"{lang} (") == 1

    def test_single_line_output_matches_expected_shape(self):
        summary = [("Python", "ast", False), ("Go", "regex", False)]
        assert format_parser_summary(summary) == ["  Parsers: Python (ast) · Go (regex)"]

    def test_fallback_is_wrapped_in_orange_markup(self):
        summary = [("Kotlin", "regex fallback", True)]
        assert "[orange1]Kotlin (regex fallback)[/]" in format_parser_summary(summary)[0]

    def test_non_fallback_has_no_markup(self):
        summary = [("Python", "ast", False)]
        assert "orange1" not in format_parser_summary(summary)[0]

    def test_fallback_markup_survives_wrapping(self):
        summary = self._summary(9) + [("Kotlin", "regex fallback", True)]
        joined = " ".join(format_parser_summary(summary))
        assert "[orange1]Kotlin (regex fallback)[/]" in joined

    def test_wrapped_output_has_no_overlong_line(self):
        """The whole point: no display line should blow past a terminal width."""
        for line in format_parser_summary(self._summary(10)):
            assert len(re.sub(r"\[/?[\w]*\]", "", line)) <= 100


class TestParserSummaryWrappingCLI:
    def test_cli_wraps_with_many_languages(self, tmp_path):
        _write(tmp_path, "a.py", "def f(): pass\n")
        _write(tmp_path, "b.ts", "export function g() {}\n")
        _write(tmp_path, "c.java", JAVA_SOURCE)
        _write(tmp_path, "d.rs", RUST_SOURCE)
        _write(tmp_path, "e.cs", CSHARP_SOURCE)
        _write(tmp_path, "f.rb", RUBY_SOURCE)
        result = CliRunner().invoke(
            cli, ["index", str(tmp_path), "--output", str(tmp_path / "g.json")])
        assert result.exit_code == 0, result.output
        parser_lines = [ln for ln in result.output.splitlines()
                        if "Parsers:" in ln or ln.startswith(" " * 11)]
        assert len(parser_lines) >= 2

    def test_cli_single_line_for_few_languages(self, tmp_path):
        _write(tmp_path, "a.py", "def f(): pass\n")
        result = CliRunner().invoke(
            cli, ["index", str(tmp_path), "--output", str(tmp_path / "g.json")])
        assert "  Parsers: Python (ast)" in result.output


# ---------------------------------------------------------------------------
# C / C++ / Lua / Elixir / PowerShell — sample sources
# ---------------------------------------------------------------------------

C_HEADER_SOURCE = '''\
#include <stdio.h>
#define SQUARE(x) ((x)*(x))
typedef struct { int x; } Point;
enum Color { RED, GREEN };
int helper(int a);
'''

C_IMPL_SOURCE = '''\
#include "point.h"
static int helper(int a) { return a; }
int main(void) { return 0; }
'''

CPP_SOURCE = '''\
#include <vector>
namespace app {
template<typename T>
class Repo {
public:
    Repo();
    T get(int id);
};
struct Point { int x; };
}
extern "C" { void c_api(void); }
'''

LUA_SOURCE = '''\
local M = {}
local function helper(x) return x end
function M.create(a) return a end
function M:method(b) return b end
function globalFn(c) return c end
'''

ELIXIR_SOURCE = '''\
defmodule App.Web.Router do
  use Phoenix.Router
  import Plug.Conn
  alias App.Repo
  defstruct [:id, :name]
  def index(conn), do: conn
  defp helper(x), do: x
  defmacro route(p), do: p
end
'''

POWERSHELL_SOURCE = '''\
#requires -Version 5.1
class Config {
    [string] GetPath() { return "x" }
}
[CmdletBinding()]
function Get-User {
    param([string]$Name)
    return $Name
}
function Set-Config { return 1 }
'''


class TestIndexC:
    @pytest.mark.skipif(not _C_TS_AVAILABLE, reason="requires the 'cpp' extra")
    def test_function_prototype_in_header(self, tmp_path):
        nodes, _ = index_c(_write(tmp_path, "point.h", C_HEADER_SOURCE), tmp_path)
        assert "point.helper" in _ids(nodes)

    @pytest.mark.skipif(not _C_TS_AVAILABLE, reason="requires the 'cpp' extra")
    def test_header_flagged(self, tmp_path):
        nodes, _ = index_c(_write(tmp_path, "point.h", C_HEADER_SOURCE), tmp_path)
        assert _node(nodes, "point.helper")["is_header"] is True

    @pytest.mark.skipif(not _C_TS_AVAILABLE, reason="requires the 'cpp' extra")
    def test_implementation_not_flagged_as_header(self, tmp_path):
        nodes, _ = index_c(_write(tmp_path, "point.c", C_IMPL_SOURCE), tmp_path)
        assert "is_header" not in _node(nodes, "point.main")

    @pytest.mark.skipif(not _C_TS_AVAILABLE, reason="requires the 'cpp' extra")
    def test_macro_with_params(self, tmp_path):
        nodes, _ = index_c(_write(tmp_path, "point.h", C_HEADER_SOURCE), tmp_path)
        assert _node(nodes, "point.SQUARE")["type"] == "macro"

    @pytest.mark.skipif(not _C_TS_AVAILABLE, reason="requires the 'cpp' extra")
    def test_typedef_and_enum(self, tmp_path):
        nodes, _ = index_c(_write(tmp_path, "point.h", C_HEADER_SOURCE), tmp_path)
        assert _node(nodes, "point.Point")["type"] == "typedef"
        assert _node(nodes, "point.Color")["type"] == "enum"

    @pytest.mark.skipif(not _C_TS_AVAILABLE, reason="requires the 'cpp' extra")
    def test_include_creates_import_edge(self, tmp_path):
        _, edges = index_c(_write(tmp_path, "point.h", C_HEADER_SOURCE), tmp_path)
        assert any(e["relation"] == "imports_from" for e in edges)

    def test_regex_fallback_extracts_symbols(self):
        nodes, _ = _index_c_regex(C_HEADER_SOURCE, Path("point.h"), "point")
        ids = _ids(nodes)
        assert "point.SQUARE" in ids
        assert "point.Color" in ids

    def test_regex_fallback_marks_header(self):
        nodes, _ = _index_c_regex(C_HEADER_SOURCE, Path("point.h"), "point")
        assert _node(nodes, "point.SQUARE")["is_header"] is True

    def test_regex_fallback_impl_not_header(self):
        nodes, _ = _index_c_regex(C_IMPL_SOURCE, Path("point.c"), "point")
        assert _node(nodes, "point.main")["is_header"] is False

    def test_regex_fallback_skips_control_flow(self):
        nodes, _ = _index_c_regex("int f(void) { if (x) { return 1; } }\n",
                                  Path("a.c"), "a")
        assert "a.if" not in _ids(nodes)


class TestIndexCpp:
    @pytest.mark.skipif(not _CPP_TS_AVAILABLE, reason="requires the 'cpp' extra")
    def test_namespace_nested_in_id(self, tmp_path):
        nodes, _ = index_cpp(_write(tmp_path, "repo.cpp", CPP_SOURCE), tmp_path)
        assert "repo.app.Repo" in _ids(nodes)

    @pytest.mark.skipif(not _CPP_TS_AVAILABLE, reason="requires the 'cpp' extra")
    def test_template_flagged_with_params(self, tmp_path):
        nodes, _ = index_cpp(_write(tmp_path, "repo.cpp", CPP_SOURCE), tmp_path)
        node = _node(nodes, "repo.app.Repo")
        assert node["is_template"] is True
        assert node["template_params"] == "<typename T>"

    @pytest.mark.skipif(not _CPP_TS_AVAILABLE, reason="requires the 'cpp' extra")
    def test_method_and_constructor(self, tmp_path):
        nodes, _ = index_cpp(_write(tmp_path, "repo.cpp", CPP_SOURCE), tmp_path)
        assert _node(nodes, "repo.app.Repo.get")["type"] == "method"
        assert _node(nodes, "repo.app.Repo.Repo")["type"] == "method"

    @pytest.mark.skipif(not _CPP_TS_AVAILABLE, reason="requires the 'cpp' extra")
    def test_extern_c_flagged(self, tmp_path):
        nodes, _ = index_cpp(_write(tmp_path, "repo.cpp", CPP_SOURCE), tmp_path)
        assert _node(nodes, "repo.c_api")["is_extern_c"] is True

    @pytest.mark.skipif(not _CPP_TS_AVAILABLE, reason="requires the 'cpp' extra")
    def test_non_extern_not_flagged(self, tmp_path):
        nodes, _ = index_cpp(_write(tmp_path, "repo.cpp", CPP_SOURCE), tmp_path)
        assert "is_extern_c" not in _node(nodes, "repo.app.Point")

    @pytest.mark.skipif(not _CPP_TS_AVAILABLE, reason="requires the 'cpp' extra")
    def test_hpp_is_a_header(self, tmp_path):
        nodes, _ = index_cpp(_write(tmp_path, "r.hpp", "struct S { int f(); };\n"), tmp_path)
        assert _node(nodes, "r.S")["is_header"] is True

    def test_regex_fallback_template(self):
        nodes, _ = _index_c_regex(CPP_SOURCE, Path("repo.cpp"), "repo")
        assert _node(nodes, "repo.app.Repo")["is_template"] is True

    def test_regex_fallback_namespace(self):
        nodes, _ = _index_c_regex(CPP_SOURCE, Path("repo.cpp"), "repo")
        assert any(n["type"] == "namespace" for n in nodes)

    def test_regex_fallback_extern_c(self):
        nodes, _ = _index_c_regex(CPP_SOURCE, Path("repo.cpp"), "repo")
        assert _node(nodes, "repo.app.c_api")["is_extern_c"] is True


class TestIndexLua:
    def test_local_function_flagged(self, tmp_path):
        nodes, _ = index_lua(_write(tmp_path, "mod.lua", LUA_SOURCE), tmp_path)
        assert _node(nodes, "mod.helper")["is_local"] is True

    def test_global_function_not_local(self, tmp_path):
        nodes, _ = index_lua(_write(tmp_path, "mod.lua", LUA_SOURCE), tmp_path)
        assert _node(nodes, "mod.globalFn")["is_local"] is False

    def test_table_indexed_as_module(self, tmp_path):
        nodes, _ = index_lua(_write(tmp_path, "mod.lua", LUA_SOURCE), tmp_path)
        assert _node(nodes, "mod.M")["type"] == "module"

    def test_table_function_nested_under_table(self, tmp_path):
        nodes, _ = index_lua(_write(tmp_path, "mod.lua", LUA_SOURCE), tmp_path)
        assert "mod.M.create" in _ids(nodes)

    def test_colon_method_nested_and_typed(self, tmp_path):
        nodes, _ = index_lua(_write(tmp_path, "mod.lua", LUA_SOURCE), tmp_path)
        assert _node(nodes, "mod.M.method")["type"] == "method"

    def test_contains_edge_table_to_method(self, tmp_path):
        _, edges = index_lua(_write(tmp_path, "mod.lua", LUA_SOURCE), tmp_path)
        assert ("mod.M", "mod.M.method") in _relations(edges, "contains")

    def test_colon_method_on_unknown_table_creates_it(self, tmp_path):
        nodes, _ = index_lua(_write(tmp_path, "c.lua",
                                    "function Klass:go() end\n"), tmp_path)
        assert "c.Klass" in _ids(nodes)
        assert "c.Klass.go" in _ids(nodes)

    def test_no_tree_sitter_warning(self, tmp_path, capsys):
        index_lua(_write(tmp_path, "mod.lua", LUA_SOURCE), tmp_path)
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""


class TestIndexElixir:
    def test_nested_module_id(self, tmp_path):
        nodes, _ = index_elixir(_write(tmp_path, "router.ex", ELIXIR_SOURCE), tmp_path)
        assert "router.App.Web.Router" in _ids(nodes)

    def test_def_is_public(self, tmp_path):
        nodes, _ = index_elixir(_write(tmp_path, "router.ex", ELIXIR_SOURCE), tmp_path)
        assert _node(nodes, "router.App.Web.Router.index")["is_private"] is False

    def test_defp_is_private(self, tmp_path):
        nodes, _ = index_elixir(_write(tmp_path, "router.ex", ELIXIR_SOURCE), tmp_path)
        assert _node(nodes, "router.App.Web.Router.helper")["is_private"] is True

    def test_defmacro_typed_as_macro(self, tmp_path):
        nodes, _ = index_elixir(_write(tmp_path, "router.ex", ELIXIR_SOURCE), tmp_path)
        assert _node(nodes, "router.App.Web.Router.route")["type"] == "macro"

    def test_defstruct_records_fields(self, tmp_path):
        nodes, _ = index_elixir(_write(tmp_path, "router.ex", ELIXIR_SOURCE), tmp_path)
        node = _node(nodes, "router.App.Web.Router.__struct__")
        assert node["type"] == "struct"
        assert node["fields"] == ["id", "name"]

    @pytest.mark.parametrize("relation,target", [
        ("use", "Phoenix.Router"), ("import", "Plug.Conn"), ("alias", "App.Repo"),
    ])
    def test_directive_edges(self, relation, target, tmp_path):
        _, edges = index_elixir(_write(tmp_path, "router.ex", ELIXIR_SOURCE), tmp_path)
        assert ("router.App.Web.Router", target) in _relations(edges, relation)

    def test_pipelines_are_not_nodes(self, tmp_path):
        source = ("defmodule A do\n  def go(x) do\n    x |> String.trim() |> ok()\n"
                  "  end\nend\n")
        nodes, _ = index_elixir(_write(tmp_path, "a.ex", source), tmp_path)
        labels = {n["label"] for n in nodes}
        assert "trim" not in labels
        assert "ok" not in labels

    def test_no_tree_sitter_warning(self, tmp_path, capsys):
        index_elixir(_write(tmp_path, "router.ex", ELIXIR_SOURCE), tmp_path)
        assert capsys.readouterr().err == ""


class TestIndexPowerShell:
    def test_function_extracted(self, tmp_path):
        nodes, _ = index_powershell(_write(tmp_path, "Mod.ps1", POWERSHELL_SOURCE), tmp_path)
        assert "Mod.Get-User" in _ids(nodes)

    def test_verb_and_noun_split(self, tmp_path):
        nodes, _ = index_powershell(_write(tmp_path, "Mod.ps1", POWERSHELL_SOURCE), tmp_path)
        node = _node(nodes, "Mod.Get-User")
        assert node["verb"] == "Get"
        assert node["noun"] == "User"

    def test_second_verb_noun_pair(self, tmp_path):
        nodes, _ = index_powershell(_write(tmp_path, "Mod.ps1", POWERSHELL_SOURCE), tmp_path)
        assert _node(nodes, "Mod.Set-Config")["verb"] == "Set"

    def test_attribute_recorded(self, tmp_path):
        nodes, _ = index_powershell(_write(tmp_path, "Mod.ps1", POWERSHELL_SOURCE), tmp_path)
        assert "[CmdletBinding]" in _node(nodes, "Mod.Get-User")["attributes"]

    def test_has_params_flag(self, tmp_path):
        nodes, _ = index_powershell(_write(tmp_path, "Mod.ps1", POWERSHELL_SOURCE), tmp_path)
        assert _node(nodes, "Mod.Get-User")["has_params"] is True

    def test_function_without_params_not_flagged(self, tmp_path):
        nodes, _ = index_powershell(_write(tmp_path, "Mod.ps1", POWERSHELL_SOURCE), tmp_path)
        assert "has_params" not in _node(nodes, "Mod.Set-Config")

    def test_class_and_method(self, tmp_path):
        nodes, _ = index_powershell(_write(tmp_path, "Mod.ps1", POWERSHELL_SOURCE), tmp_path)
        assert _node(nodes, "Mod.Config")["type"] == "class"
        assert _node(nodes, "Mod.Config.GetPath")["type"] == "method"

    def test_requires_edge(self, tmp_path):
        _, edges = index_powershell(_write(tmp_path, "Mod.ps1", POWERSHELL_SOURCE), tmp_path)
        assert any(e["relation"] == "requires" for e in edges)

    def test_non_verb_noun_function_has_no_verb(self, tmp_path):
        nodes, _ = index_powershell(_write(tmp_path, "P.ps1", "function Helper { 1 }\n"), tmp_path)
        assert "verb" not in _node(nodes, "P.Helper")

    def test_no_tree_sitter_warning(self, tmp_path, capsys):
        index_powershell(_write(tmp_path, "Mod.ps1", POWERSHELL_SOURCE), tmp_path)
        assert capsys.readouterr().err == ""


class TestThirdBatchWiring:
    @pytest.mark.parametrize("ext", [
        ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx",
        ".lua", ".ex", ".exs", ".ps1", ".psm1",
    ])
    def test_extension_registered(self, ext):
        from slurp.indexer import _INDEXED_EXTENSIONS
        assert ext in _INDEXED_EXTENSIONS

    @pytest.mark.parametrize("ext,language", [
        (".c", "C"), (".cpp", "C++"), (".lua", "Lua"),
        (".ex", "Elixir"), (".ps1", "PowerShell"),
    ])
    def test_parser_summary_lists_language(self, ext, language):
        assert any(lang == language for lang, _, _ in parser_summary({ext}))

    @pytest.mark.parametrize("ext", [".lua", ".ex", ".ps1"])
    def test_regex_only_languages_are_not_fallbacks(self, ext):
        """Regex is the design for these, so they must never show as degraded."""
        _, parser, is_fallback = parser_summary({ext})[0]
        assert parser == "regex"
        assert is_fallback is False

    @pytest.mark.parametrize("ext,flag", [
        (".c", "_C_TS_AVAILABLE"), (".cpp", "_CPP_TS_AVAILABLE"),
    ])
    def test_c_family_reports_fallback(self, ext, flag, monkeypatch):
        monkeypatch.setattr(indexer_mod, flag, False)
        _, parser, is_fallback = parser_summary({ext})[0]
        assert parser == "regex fallback"
        assert is_fallback is True

    def test_index_project_picks_up_all_five(self, tmp_path):
        _write(tmp_path, "a.c", C_IMPL_SOURCE)
        _write(tmp_path, "b.cpp", CPP_SOURCE)
        _write(tmp_path, "c.lua", LUA_SOURCE)
        _write(tmp_path, "d.ex", ELIXIR_SOURCE)
        _write(tmp_path, "e.ps1", POWERSHELL_SOURCE)
        graph = index_project(tmp_path)
        files = {n["source_file"] for n in graph["nodes"] if n.get("source_file")}
        assert {"a.c", "b.cpp", "c.lua", "d.ex", "e.ps1"} <= files

    @pytest.mark.parametrize("flag,fn,name,source", [
        ("_C_TS_AVAILABLE", "index_c", "a.c", C_IMPL_SOURCE),
        ("_CPP_TS_AVAILABLE", "index_cpp", "b.cpp", CPP_SOURCE),
    ])
    def test_missing_grammar_is_silent(self, flag, fn, name, source,
                                       tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(indexer_mod, flag, False)
        nodes, _ = getattr(indexer_mod, fn)(_write(tmp_path, name, source), tmp_path)
        captured = capsys.readouterr()
        assert captured.err == ""
        assert len(nodes) > 1


class TestPythonCallEdges:
    """`calls` edges extracted from Python source by _PythonCallVisitor."""

    def _calls(self, tmp_path: Path, source: str) -> set[tuple[str, str]]:
        """Index *source* as mod.py and return its `calls` edges as pairs."""
        f = tmp_path / "mod.py"
        f.write_text(source, encoding="utf-8")
        _, edges = index_python(f, tmp_path)
        return {
            (e["source"], e["target"])
            for e in edges
            if e["relation"] == "calls"
        }

    def _edges(self, tmp_path: Path, source: str) -> list[dict]:
        f = tmp_path / "mod.py"
        f.write_text(source, encoding="utf-8")
        _, edges = index_python(f, tmp_path)
        return edges

    def test_top_level_function_calls_another(self, tmp_path):
        calls = self._calls(tmp_path, "def helper():\n    pass\n\n\ndef main():\n    return helper()\n")
        assert ("mod.main", "mod.helper") in calls

    def test_self_method_call(self, tmp_path):
        source = (
            "class Service:\n"
            "    def run(self):\n"
            "        return self.step()\n\n"
            "    def step(self):\n"
            "        pass\n"
        )
        calls = self._calls(tmp_path, source)
        assert ("mod.Service.run", "mod.Service.step") in calls

    def test_cls_method_call(self, tmp_path):
        source = (
            "class Service:\n"
            "    @classmethod\n"
            "    def build(cls):\n"
            "        return cls.make()\n\n"
            "    @classmethod\n"
            "    def make(cls):\n"
            "        pass\n"
        )
        assert ("mod.Service.build", "mod.Service.make") in self._calls(tmp_path, source)

    def test_constructor_call(self, tmp_path):
        source = "class Gateway:\n    pass\n\n\ndef build():\n    return Gateway()\n"
        assert ("mod.build", "mod.Gateway") in self._calls(tmp_path, source)

    def test_method_calls_module_level_function(self, tmp_path):
        source = (
            "def validate(x):\n"
            "    return True\n\n\n"
            "class Gateway:\n"
            "    def charge(self, card):\n"
            "        return validate(card)\n"
        )
        assert ("mod.Gateway.charge", "mod.validate") in self._calls(tmp_path, source)

    def test_stdlib_call_resolves_to_nothing_at_project_level(self, tmp_path):
        """A stdlib call leaves a pending edge that project indexing drops."""
        source = "import os\n\n\ndef where():\n    return os.getcwd()\n"
        (tmp_path / "mod.py").write_text(source, encoding="utf-8")
        graph = index_project(tmp_path)
        assert [e for e in graph["links"] if e["relation"] == "calls"] == []

    def test_stdlib_call_produces_only_a_pending_edge(self, tmp_path):
        """index_python alone cannot know `os` is external — index_project does."""
        source = "import os\n\n\ndef where():\n    return os.getcwd()\n"
        f = tmp_path / "mod.py"
        f.write_text(source, encoding="utf-8")
        _, edges = index_python(f, tmp_path)
        calls = [e for e in edges if e["relation"] == "calls"]
        assert len(calls) == 1
        assert calls[0]["target"] == "mod.import_os"
        assert calls[0][indexer_mod._PENDING_SYMBOL] == "os.getcwd"

    def test_builtin_call_produces_no_edge(self, tmp_path):
        source = "def size(items):\n    return len(items)\n"
        assert self._calls(tmp_path, source) == set()

    def test_external_object_method_produces_no_edge(self, tmp_path):
        """A method on an object of unknown type must never resolve."""
        source = (
            "def process(client):\n"
            "    return client.fetch()\n"
        )
        assert self._calls(tmp_path, source) == set()

    def test_recursive_function_gets_self_edge(self, tmp_path):
        source = "def walk(n):\n    if n:\n        return walk(n - 1)\n    return 0\n"
        assert ("mod.walk", "mod.walk") in self._calls(tmp_path, source)

    def test_call_inside_comprehension_is_detected(self, tmp_path):
        source = (
            "def transform(x):\n"
            "    return x\n\n\n"
            "def run(items):\n"
            "    return [transform(i) for i in items]\n"
        )
        assert ("mod.run", "mod.transform") in self._calls(tmp_path, source)

    def test_module_without_calls_emits_no_call_edges(self, tmp_path):
        source = "def a():\n    pass\n\n\ndef b():\n    pass\n"
        assert self._calls(tmp_path, source) == set()

    def test_contains_edges_are_unaffected(self, tmp_path):
        source = (
            "def helper():\n"
            "    pass\n\n\n"
            "class Service:\n"
            "    def run(self):\n"
            "        return helper()\n"
        )
        edges = self._edges(tmp_path, source)
        contains = {(e["source"], e["target"]) for e in edges if e["relation"] == "contains"}
        assert contains == {
            ("mod", "mod.helper"),
            ("mod", "mod.Service"),
            ("mod.Service", "mod.Service.run"),
        }

    def test_imports_from_edges_are_unaffected(self, tmp_path):
        source = "import os\nfrom pathlib import Path\n\n\ndef f():\n    pass\n"
        edges = self._edges(tmp_path, source)
        imports = [e for e in edges if e["relation"] == "imports_from"]
        assert len(imports) == 2

    def test_call_edges_only_reference_existing_nodes(self, tmp_path):
        source = (
            "import json\n\n\n"
            "def helper():\n"
            "    pass\n\n\n"
            "class Service:\n"
            "    def run(self):\n"
            "        helper()\n"
            "        return json.dumps(self.data())\n\n"
            "    def data(self):\n"
            "        return {}\n"
        )
        f = tmp_path / "mod.py"
        f.write_text(source, encoding="utf-8")
        nodes, edges = index_python(f, tmp_path)
        ids = {n["id"] for n in nodes}
        for e in edges:
            if e["relation"] == "calls":
                assert e["source"] in ids
                assert e["target"] in ids

    def test_local_variable_type_resolves_method_call(self, tmp_path):
        source = (
            "class Gateway:\n"
            "    def charge(self):\n"
            "        pass\n\n\n"
            "def checkout():\n"
            "    gw = Gateway()\n"
            "    return gw.charge()\n"
        )
        calls = self._calls(tmp_path, source)
        assert ("mod.checkout", "mod.Gateway") in calls
        assert ("mod.checkout", "mod.Gateway.charge") in calls

    def test_variable_bound_to_function_result_is_not_a_type(self, tmp_path):
        """`x = helper()` must not make `x.charge()` resolve to a class method."""
        source = (
            "class Gateway:\n"
            "    def charge(self):\n"
            "        pass\n\n\n"
            "def helper():\n"
            "    pass\n\n\n"
            "def run():\n"
            "    x = helper()\n"
            "    return x.charge()\n"
        )
        calls = self._calls(tmp_path, source)
        assert ("mod.run", "mod.helper") in calls
        assert ("mod.run", "mod.Gateway.charge") not in calls

    def test_rebinding_clears_a_stale_local_type(self, tmp_path):
        source = (
            "class Gateway:\n"
            "    def charge(self):\n"
            "        pass\n\n\n"
            "def run(other):\n"
            "    gw = Gateway()\n"
            "    gw = other\n"
            "    return gw.charge()\n"
        )
        calls = self._calls(tmp_path, source)
        assert ("mod.run", "mod.Gateway") in calls
        assert ("mod.run", "mod.Gateway.charge") not in calls

    def test_inherited_method_resolves_to_defining_class(self, tmp_path):
        source = (
            "class Base:\n"
            "    def emit(self):\n"
            "        pass\n\n\n"
            "class Child(Base):\n"
            "    def run(self):\n"
            "        return self.emit()\n"
        )
        assert ("mod.Child.run", "mod.Base.emit") in self._calls(tmp_path, source)

    def test_override_wins_over_base(self, tmp_path):
        source = (
            "class Base:\n"
            "    def emit(self):\n"
            "        pass\n\n\n"
            "class Child(Base):\n"
            "    def emit(self):\n"
            "        pass\n\n"
            "    def run(self):\n"
            "        return self.emit()\n"
        )
        calls = self._calls(tmp_path, source)
        assert ("mod.Child.run", "mod.Child.emit") in calls
        assert ("mod.Child.run", "mod.Base.emit") not in calls

    def test_bare_name_does_not_resolve_to_a_class_attribute(self, tmp_path):
        """A method never shadows a module function for a bare-name call."""
        source = (
            "def process(x):\n"
            "    return x\n\n\n"
            "class Service:\n"
            "    def process(self, x):\n"
            "        pass\n\n"
            "    def run(self):\n"
            "        return process(1)\n"
        )
        calls = self._calls(tmp_path, source)
        assert ("mod.Service.run", "mod.process") in calls
        assert ("mod.Service.run", "mod.Service.process") not in calls

    def test_nested_function_call_resolves(self, tmp_path):
        source = (
            "def outer():\n"
            "    def inner():\n"
            "        pass\n"
            "    return inner()\n"
        )
        assert ("mod.outer", "mod.outer.inner") in self._calls(tmp_path, source)

    def test_async_function_calls_are_detected(self, tmp_path):
        source = (
            "def helper():\n"
            "    pass\n\n\n"
            "async def run():\n"
            "    return helper()\n"
        )
        assert ("mod.run", "mod.helper") in self._calls(tmp_path, source)

    def test_decorator_call_attributed_to_enclosing_scope(self, tmp_path):
        """A decorator runs where the function is defined, not inside it."""
        source = (
            "def wrap(*a):\n"
            "    return lambda f: f\n\n\n"
            "@wrap()\n"
            "def target():\n"
            "    pass\n"
        )
        calls = self._calls(tmp_path, source)
        assert ("mod", "mod.wrap") in calls
        assert ("mod.target", "mod.wrap") not in calls

    def test_duplicate_calls_emit_one_edge(self, tmp_path):
        source = (
            "def helper():\n"
            "    pass\n\n\n"
            "def run():\n"
            "    helper()\n"
            "    helper()\n"
            "    return helper()\n"
        )
        f = tmp_path / "mod.py"
        f.write_text(source, encoding="utf-8")
        _, edges = index_python(f, tmp_path)
        pairs = [
            (e["source"], e["target"]) for e in edges if e["relation"] == "calls"
        ]
        assert pairs.count(("mod.run", "mod.helper")) == 1

    def test_call_in_nested_argument_is_detected(self, tmp_path):
        source = (
            "def inner():\n"
            "    pass\n\n\n"
            "def outer(x):\n"
            "    pass\n\n\n"
            "def run():\n"
            "    return outer(inner())\n"
        )
        calls = self._calls(tmp_path, source)
        assert ("mod.run", "mod.outer") in calls
        assert ("mod.run", "mod.inner") in calls

    def test_syntax_error_still_returns_empty(self, tmp_path):
        f = tmp_path / "broken.py"
        f.write_text("def (:\n", encoding="utf-8")
        assert index_python(f, tmp_path) == ([], [])


TS_CALL_SAMPLE = """\
function hashToken(raw: string): string { return raw; }
function validateCard(number: string): boolean { return true; }

class PaymentGateway {
  charge(card: string, amount: number) {
    if (!validateCard(card)) return null;
    const token = hashToken(card);
    return this._submit(token, amount);
  }
  private _submit(token: string, amount: number) {}
}

function checkout(cart: any) {
  const gateway = new PaymentGateway();
  return gateway.charge(cart.card, cart.total);
}
"""


class TestTSCallEdges:
    """`calls` edges for TypeScript/JavaScript, both parser branches."""

    def _index(self, tmp_path: Path, source: str, *, tree_sitter: bool,
               monkeypatch, name: str = "payments.ts"):
        monkeypatch.setattr(indexer_mod, "_TREE_SITTER_AVAILABLE", tree_sitter)
        f = tmp_path / name
        f.write_text(source, encoding="utf-8")
        return indexer_mod.index_typescript(f, tmp_path)

    def _calls(self, tmp_path: Path, source: str, *, tree_sitter: bool,
               monkeypatch, name: str = "payments.ts") -> set[tuple[str, str]]:
        _, edges = self._index(
            tmp_path, source, tree_sitter=tree_sitter, monkeypatch=monkeypatch, name=name
        )
        return {
            (e["source"], e["target"]) for e in edges if e["relation"] == "calls"
        }

    # -- the reference sample, on both branches ---------------------------

    @pytest.mark.parametrize("tree_sitter", [True, False], ids=["treesitter", "regex"])
    def test_sample_produces_exactly_the_expected_edges(
        self, tmp_path, monkeypatch, tree_sitter
    ):
        if tree_sitter and not _TREE_SITTER_AVAILABLE:
            pytest.skip("requires the 'ts' extra")
        calls = self._calls(
            tmp_path, TS_CALL_SAMPLE, tree_sitter=tree_sitter, monkeypatch=monkeypatch
        )
        assert calls == {
            ("payments.PaymentGateway.charge", "payments.validateCard"),
            ("payments.PaymentGateway.charge", "payments.hashToken"),
            ("payments.PaymentGateway.charge", "payments.PaymentGateway._submit"),
            ("payments.checkout", "payments.PaymentGateway"),
            ("payments.checkout", "payments.PaymentGateway.charge"),
        }

    # -- per-behaviour, parametrised over both branches -------------------

    @pytest.mark.parametrize("tree_sitter", [True, False], ids=["treesitter", "regex"])
    def test_top_level_function_calls_another(self, tmp_path, monkeypatch, tree_sitter):
        if tree_sitter and not _TREE_SITTER_AVAILABLE:
            pytest.skip("requires the 'ts' extra")
        source = "function helper() { return 1; }\nfunction main() { return helper(); }\n"
        calls = self._calls(tmp_path, source, tree_sitter=tree_sitter, monkeypatch=monkeypatch)
        assert ("payments.main", "payments.helper") in calls

    @pytest.mark.parametrize("tree_sitter", [True, False], ids=["treesitter", "regex"])
    def test_this_method_call(self, tmp_path, monkeypatch, tree_sitter):
        if tree_sitter and not _TREE_SITTER_AVAILABLE:
            pytest.skip("requires the 'ts' extra")
        source = "class S {\n  run() { return this.step(); }\n  step() {}\n}\n"
        calls = self._calls(tmp_path, source, tree_sitter=tree_sitter, monkeypatch=monkeypatch)
        assert ("payments.S.run", "payments.S.step") in calls

    @pytest.mark.parametrize("tree_sitter", [True, False], ids=["treesitter", "regex"])
    def test_new_class_is_a_constructor_call(self, tmp_path, monkeypatch, tree_sitter):
        if tree_sitter and not _TREE_SITTER_AVAILABLE:
            pytest.skip("requires the 'ts' extra")
        source = "class Gw {}\nfunction build() { return new Gw(); }\n"
        calls = self._calls(tmp_path, source, tree_sitter=tree_sitter, monkeypatch=monkeypatch)
        assert ("payments.build", "payments.Gw") in calls

    @pytest.mark.parametrize("tree_sitter", [True, False], ids=["treesitter", "regex"])
    def test_external_api_produces_no_edge(self, tmp_path, monkeypatch, tree_sitter):
        if tree_sitter and not _TREE_SITTER_AVAILABLE:
            pytest.skip("requires the 'ts' extra")
        source = "function noisy() {\n  console.log('x');\n  return Math.random();\n}\n"
        calls = self._calls(tmp_path, source, tree_sitter=tree_sitter, monkeypatch=monkeypatch)
        assert calls == set()

    @pytest.mark.parametrize("tree_sitter", [True, False], ids=["treesitter", "regex"])
    def test_method_on_unknown_object_produces_no_edge(
        self, tmp_path, monkeypatch, tree_sitter
    ):
        if tree_sitter and not _TREE_SITTER_AVAILABLE:
            pytest.skip("requires the 'ts' extra")
        source = (
            "class Gw {\n  charge() {}\n}\n"
            "function report(repo: any) { return repo.charge(); }\n"
        )
        calls = self._calls(tmp_path, source, tree_sitter=tree_sitter, monkeypatch=monkeypatch)
        assert ("payments.report", "payments.Gw.charge") not in calls

    @pytest.mark.parametrize("tree_sitter", [True, False], ids=["treesitter", "regex"])
    def test_module_without_calls_emits_none(self, tmp_path, monkeypatch, tree_sitter):
        if tree_sitter and not _TREE_SITTER_AVAILABLE:
            pytest.skip("requires the 'ts' extra")
        source = "function a() {}\nfunction b() {}\n"
        calls = self._calls(tmp_path, source, tree_sitter=tree_sitter, monkeypatch=monkeypatch)
        assert calls == set()

    @pytest.mark.parametrize("tree_sitter", [True, False], ids=["treesitter", "regex"])
    def test_contains_edges_are_unaffected(self, tmp_path, monkeypatch, tree_sitter):
        if tree_sitter and not _TREE_SITTER_AVAILABLE:
            pytest.skip("requires the 'ts' extra")
        _, edges = self._index(
            tmp_path, TS_CALL_SAMPLE, tree_sitter=tree_sitter, monkeypatch=monkeypatch
        )
        contains = {(e["source"], e["target"]) for e in edges if e["relation"] == "contains"}
        assert contains == {
            ("payments", "payments.hashToken"),
            ("payments", "payments.validateCard"),
            ("payments", "payments.PaymentGateway"),
            ("payments.PaymentGateway", "payments.PaymentGateway.charge"),
            ("payments.PaymentGateway", "payments.PaymentGateway._submit"),
            ("payments", "payments.checkout"),
        }

    @pytest.mark.parametrize("tree_sitter", [True, False], ids=["treesitter", "regex"])
    def test_imports_from_edges_are_unaffected(self, tmp_path, monkeypatch, tree_sitter):
        if tree_sitter and not _TREE_SITTER_AVAILABLE:
            pytest.skip("requires the 'ts' extra")
        source = "import { createClient } from '@supabase/supabase-js';\nfunction f() {}\n"
        _, edges = self._index(
            tmp_path, source, tree_sitter=tree_sitter, monkeypatch=monkeypatch
        )
        assert sum(1 for e in edges if e["relation"] == "imports_from") == 1

    @pytest.mark.parametrize("tree_sitter", [True, False], ids=["treesitter", "regex"])
    def test_call_edges_only_reference_existing_nodes(
        self, tmp_path, monkeypatch, tree_sitter
    ):
        if tree_sitter and not _TREE_SITTER_AVAILABLE:
            pytest.skip("requires the 'ts' extra")
        nodes, edges = self._index(
            tmp_path, TS_CALL_SAMPLE, tree_sitter=tree_sitter, monkeypatch=monkeypatch
        )
        ids = {n["id"] for n in nodes}
        for e in edges:
            if e["relation"] == "calls":
                assert e["source"] in ids and e["target"] in ids

    @pytest.mark.parametrize("tree_sitter", [True, False], ids=["treesitter", "regex"])
    def test_string_and_comment_contents_do_not_produce_edges(
        self, tmp_path, monkeypatch, tree_sitter
    ):
        if tree_sitter and not _TREE_SITTER_AVAILABLE:
            pytest.skip("requires the 'ts' extra")
        source = (
            "function helper() {}\n"
            "function main() {\n"
            "  // helper()\n"
            "  const s = 'helper()';\n"
            "  return s;\n"
            "}\n"
        )
        calls = self._calls(tmp_path, source, tree_sitter=tree_sitter, monkeypatch=monkeypatch)
        assert ("payments.main", "payments.helper") not in calls

    @pytest.mark.parametrize("tree_sitter", [True, False], ids=["treesitter", "regex"])
    def test_duplicate_calls_emit_one_edge(self, tmp_path, monkeypatch, tree_sitter):
        if tree_sitter and not _TREE_SITTER_AVAILABLE:
            pytest.skip("requires the 'ts' extra")
        source = (
            "function helper() {}\n"
            "function main() { helper(); helper(); return helper(); }\n"
        )
        _, edges = self._index(
            tmp_path, source, tree_sitter=tree_sitter, monkeypatch=monkeypatch
        )
        pairs = [(e["source"], e["target"]) for e in edges if e["relation"] == "calls"]
        assert pairs.count(("payments.main", "payments.helper")) == 1

    @pytest.mark.parametrize("tree_sitter", [True, False], ids=["treesitter", "regex"])
    def test_local_variable_from_new_resolves_method(
        self, tmp_path, monkeypatch, tree_sitter
    ):
        if tree_sitter and not _TREE_SITTER_AVAILABLE:
            pytest.skip("requires the 'ts' extra")
        source = (
            "class Gw {\n  charge() {}\n}\n"
            "function run() {\n  const gw = new Gw();\n  return gw.charge();\n}\n"
        )
        calls = self._calls(tmp_path, source, tree_sitter=tree_sitter, monkeypatch=monkeypatch)
        assert ("payments.run", "payments.Gw.charge") in calls

    @pytest.mark.parametrize("tree_sitter", [True, False], ids=["treesitter", "regex"])
    def test_jsx_file_is_handled(self, tmp_path, monkeypatch, tree_sitter):
        if tree_sitter and not _TREE_SITTER_AVAILABLE:
            pytest.skip("requires the 'ts' extra")
        source = "function helper() {}\nfunction main() { return helper(); }\n"
        calls = self._calls(
            tmp_path, source, tree_sitter=tree_sitter, monkeypatch=monkeypatch, name="app.jsx"
        )
        assert ("app.main", "app.helper") in calls

    # -- tree-sitter only -------------------------------------------------

    @pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="requires the 'ts' extra")
    def test_inherited_method_resolves_to_base_class(self, tmp_path, monkeypatch):
        source = (
            "class Base {\n  log(m: string) {}\n}\n"
            "class Child extends Base {\n  run() { return this.log('x'); }\n}\n"
        )
        calls = self._calls(tmp_path, source, tree_sitter=True, monkeypatch=monkeypatch)
        assert ("payments.Child.run", "payments.Base.log") in calls

    @pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="requires the 'ts' extra")
    def test_arrow_const_body_is_its_own_caller(self, tmp_path, monkeypatch):
        source = "class Gw {}\nconst build = () => new Gw();\n"
        calls = self._calls(tmp_path, source, tree_sitter=True, monkeypatch=monkeypatch)
        assert ("payments.build", "payments.Gw") in calls

    @pytest.mark.skipif(not _TREE_SITTER_AVAILABLE, reason="requires the 'ts' extra")
    def test_abstract_signature_does_not_capture_next_body(self, tmp_path, monkeypatch):
        """An abstract member must not be credited with the following method's calls."""
        source = (
            "function helper() {}\n"
            "abstract class Base {\n  abstract load(id: string): unknown;\n}\n"
            "class Impl extends Base {\n  load(id: string) { return helper(); }\n}\n"
        )
        calls = self._calls(tmp_path, source, tree_sitter=True, monkeypatch=monkeypatch)
        assert ("payments.Impl.load", "payments.helper") in calls
        assert ("payments.Base.load", "payments.helper") not in calls


class TestCrossFileCallResolution:
    """`calls` edges resolved across modules by index_project."""

    def _project(self, tmp_path: Path, files: dict[str, str]) -> dict:
        for name, source in files.items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        return index_project(tmp_path)

    def _calls(self, graph: dict) -> set[tuple[str, str]]:
        return {
            (e["source"], e["target"])
            for e in graph["links"]
            if e["relation"] == "calls"
        }

    def test_cross_file_call_resolves(self, tmp_path):
        graph = self._project(tmp_path, {
            "helpers.py": "def helper():\n    return 1\n",
            "app.py": "from helpers import helper\n\n\ndef run():\n    return helper()\n",
        })
        assert ("app.run", "helpers.helper") in self._calls(graph)

    def test_import_alias_resolves(self, tmp_path):
        graph = self._project(tmp_path, {
            "helpers.py": "def helper():\n    return 1\n",
            "app.py": "from helpers import helper as h\n\n\ndef run():\n    return h()\n",
        })
        assert ("app.run", "helpers.helper") in self._calls(graph)

    def test_module_alias_attribute_call_resolves(self, tmp_path):
        graph = self._project(tmp_path, {
            "helpers.py": "def helper():\n    return 1\n",
            "app.py": "import helpers as hp\n\n\ndef run():\n    return hp.helper()\n",
        })
        assert ("app.run", "helpers.helper") in self._calls(graph)

    def test_class_import_resolves(self, tmp_path):
        graph = self._project(tmp_path, {
            "models.py": "class Gateway:\n    pass\n",
            "app.py": "from models import Gateway\n\n\ndef build():\n    return Gateway()\n",
        })
        assert ("app.build", "models.Gateway") in self._calls(graph)

    def test_lazy_import_inside_function_resolves(self, tmp_path):
        """The import node nests under the function, not the module."""
        graph = self._project(tmp_path, {
            "helpers.py": "def helper():\n    return 1\n",
            "app.py": "def run():\n    from helpers import helper\n    return helper()\n",
        })
        assert ("app.run", "helpers.helper") in self._calls(graph)

    def test_short_name_collision_does_not_resolve_wrongly(self, tmp_path):
        """Two modules define `run`; each import must land on its own module."""
        graph = self._project(tmp_path, {
            "alpha.py": "def run():\n    return 'a'\n",
            "beta.py": "def run():\n    return 'b'\n",
            "app.py": (
                "from alpha import run as run_a\n"
                "from beta import run as run_b\n\n\n"
                "def main():\n    return run_a() + run_b()\n"
            ),
        })
        calls = self._calls(graph)
        assert ("app.main", "alpha.run") in calls
        assert ("app.main", "beta.run") in calls

    def test_external_import_produces_no_edge(self, tmp_path):
        graph = self._project(tmp_path, {
            "app.py": "import os\n\n\ndef where():\n    return os.getcwd()\n",
        })
        assert self._calls(graph) == set()

    def test_stdlib_name_shadowing_a_project_class_is_not_resolved(self, tmp_path):
        """`from pathlib import Path` must not bind to a local class named Path."""
        graph = self._project(tmp_path, {
            "models.py": "class Path:\n    pass\n",
            "app.py": "from pathlib import Path\n\n\ndef run():\n    return Path('.')\n",
        })
        assert ("app.run", "models.Path") not in self._calls(graph)

    def test_unknown_symbol_does_not_raise_and_emits_nothing(self, tmp_path):
        graph = self._project(tmp_path, {
            "app.py": (
                "from nowhere import missing\n\n\n"
                "def run():\n    return missing()\n"
            ),
        })
        assert self._calls(graph) == set()

    def test_no_duplicate_when_direct_edge_already_exists(self, tmp_path):
        """A local definition wins; the import must not add a second edge."""
        graph = self._project(tmp_path, {
            "helpers.py": "def helper():\n    return 1\n",
            "app.py": (
                "from helpers import helper\n\n\n"
                "def helper():\n    return 2\n\n\n"
                "def run():\n    return helper()\n"
            ),
        })
        pairs = [
            (e["source"], e["target"])
            for e in graph["links"]
            if e["relation"] == "calls"
        ]
        assert pairs.count(("app.run", "app.helper")) == 1
        assert ("app.run", "helpers.helper") not in pairs

    def test_repeated_cross_file_call_emits_one_edge(self, tmp_path):
        graph = self._project(tmp_path, {
            "helpers.py": "def helper():\n    return 1\n",
            "app.py": (
                "from helpers import helper\n\n\n"
                "def run():\n    helper()\n    helper()\n    return helper()\n"
            ),
        })
        pairs = [
            (e["source"], e["target"])
            for e in graph["links"]
            if e["relation"] == "calls"
        ]
        assert pairs.count(("app.run", "helpers.helper")) == 1

    def test_imports_from_edges_are_unchanged(self, tmp_path):
        graph = self._project(tmp_path, {
            "helpers.py": "def helper():\n    return 1\n",
            "app.py": "from helpers import helper\n\n\ndef run():\n    return helper()\n",
        })
        imports = [e for e in graph["links"] if e["relation"] == "imports_from"]
        assert len(imports) == 1
        assert imports[0]["source"] == "app"
        assert imports[0]["target"] == "app.import_helpers_helper"

    def test_contains_edges_are_unchanged(self, tmp_path):
        graph = self._project(tmp_path, {
            "helpers.py": "def helper():\n    return 1\n",
            "app.py": "from helpers import helper\n\n\ndef run():\n    return helper()\n",
        })
        contains = {
            (e["source"], e["target"])
            for e in graph["links"]
            if e["relation"] == "contains"
        }
        assert ("helpers", "helpers.helper") in contains
        assert ("app", "app.run") in contains

    def test_no_calls_edge_points_at_an_import_node(self, tmp_path):
        """Every pending edge is either resolved or dropped."""
        graph = self._project(tmp_path, {
            "helpers.py": "def helper():\n    return 1\n",
            "app.py": (
                "import os\n"
                "from helpers import helper\n\n\n"
                "def run():\n    os.getcwd()\n    return helper()\n"
            ),
        })
        import_ids = {n["id"] for n in graph["nodes"] if n["type"] == "import"}
        for e in graph["links"]:
            if e["relation"] == "calls":
                assert e["target"] not in import_ids

    def test_private_pending_key_never_survives(self, tmp_path):
        graph = self._project(tmp_path, {
            "helpers.py": "def helper():\n    return 1\n",
            "app.py": "from helpers import helper\n\n\ndef run():\n    return helper()\n",
        })
        for e in graph["links"]:
            assert not any(k.startswith("_") for k in e)

    def test_all_call_endpoints_exist(self, tmp_path):
        graph = self._project(tmp_path, {
            "helpers.py": "def helper():\n    return 1\n",
            "app.py": (
                "import json\n"
                "from helpers import helper\n\n\n"
                "def run():\n    return json.dumps(helper())\n"
            ),
        })
        ids = {n["id"] for n in graph["nodes"]}
        for e in graph["links"]:
            assert e["source"] in ids and e["target"] in ids

    def test_cross_file_calls_increase_on_the_slurp_project(self):
        """The real measure: cross-file edges exist where none did before."""
        graph = index_project(Path("slurp"))
        source_file = {n["id"]: n.get("source_file") for n in graph["nodes"]}
        cross = [
            e for e in graph["links"]
            if e["relation"] == "calls"
            and source_file.get(e["source"]) != source_file.get(e["target"])
        ]
        assert len(cross) > 50

    def test_select_subgraph_has_cross_module_callers(self):
        graph = index_project(Path("slurp"))
        callers = {
            e["source"] for e in graph["links"]
            if e["relation"] == "calls" and e["target"] == "budget.select_subgraph"
        }
        assert "cli.run" in callers
        assert "mcp._handle" in callers


class TestGlobalSymbolIndex:
    """_build_global_symbol_index / _resolve_imported_symbol."""

    def test_indexes_every_suffix(self):
        nodes = [{"id": "pkg.mod.func", "type": "function"}]
        index = indexer_mod._build_global_symbol_index(nodes)
        assert index["func"] == "pkg.mod.func"
        assert index["mod.func"] == "pkg.mod.func"
        assert index["pkg.mod.func"] == "pkg.mod.func"

    def test_ambiguous_short_name_is_dropped(self):
        nodes = [
            {"id": "a.run", "type": "function"},
            {"id": "b.run", "type": "function"},
        ]
        index = indexer_mod._build_global_symbol_index(nodes)
        assert "run" not in index
        assert index["a.run"] == "a.run"
        assert index["b.run"] == "b.run"

    def test_type_filter_disambiguates(self):
        """`audit` names both a module and a CLI command; modules alone are unique."""
        nodes = [
            {"id": "audit", "type": "module"},
            {"id": "cli.audit", "type": "function"},
        ]
        assert "audit" not in indexer_mod._build_global_symbol_index(nodes)
        assert indexer_mod._build_module_index(nodes)["audit"] == "audit"

    def test_resolve_requires_the_module_to_exist(self):
        nodes = [{"id": "models.Path", "type": "class"}]
        kinds = {n["id"]: n["type"] for n in nodes}
        index = indexer_mod._build_module_index(nodes)
        assert indexer_mod._resolve_imported_symbol("pathlib.Path", index, kinds) is None

    def test_resolve_finds_the_symbol_in_its_module(self):
        nodes = [
            {"id": "budget", "type": "module"},
            {"id": "budget.select_subgraph", "type": "function"},
        ]
        kinds = {n["id"]: n["type"] for n in nodes}
        index = indexer_mod._build_module_index(nodes)
        assert indexer_mod._resolve_imported_symbol(
            "slurp.budget.select_subgraph", index, kinds
        ) == "budget.select_subgraph"

    def test_bare_symbol_without_module_does_not_resolve(self):
        nodes = [{"id": "budget", "type": "module"}]
        kinds = {n["id"]: n["type"] for n in nodes}
        index = indexer_mod._build_module_index(nodes)
        assert indexer_mod._resolve_imported_symbol("helper", index, kinds) is None
