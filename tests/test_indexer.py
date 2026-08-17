"""Tests for slurp.indexer — static code indexing without graphify."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from slurp.cli import cli
import slurp.indexer as indexer_mod
from slurp.indexer import (
    _TREE_SITTER_AVAILABLE,
    _file_id,
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
