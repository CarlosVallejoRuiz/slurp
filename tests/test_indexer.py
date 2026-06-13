"""Tests for slurp.indexer — static code indexing without graphify."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from slurp.cli import cli
from slurp.indexer import (
    _file_id,
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
