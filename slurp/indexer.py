"""Static code indexer — generates graph.json without graphify or LLMs.

Supports Python (stdlib ast), TypeScript/JS (tree-sitter or regex fallback),
and Go (regex). Produces graphify-compatible graph.json format.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import TYPE_CHECKING

# DECISION: resolved once at import, not per file. tree-sitter is an optional
# extra (`uv sync --extra ts`), so its absence is a supported configuration and
# must stay silent — but a parse failure while it IS installed is a real problem
# and gets reported. The two cases never share an except block.
try:
    from tree_sitter import Language as _TSLanguage
    from tree_sitter import Parser as _TSParser

    _TS_CORE_AVAILABLE = True
except ImportError:
    _TSLanguage = _TSParser = None  # type: ignore[assignment]
    _TS_CORE_AVAILABLE = False


def _load_grammar(module_name: str):
    """Import an optional tree-sitter grammar module, or return None.

    Args:
        module_name: e.g. "tree_sitter_java".

    Returns:
        The imported module, or None when the core or the grammar is missing.
    """
    if not _TS_CORE_AVAILABLE:
        return None
    try:
        import importlib  # noqa: PLC0415

        return importlib.import_module(module_name)
    except ImportError:
        return None


_tsts = _load_grammar("tree_sitter_typescript")
_tsjava = _load_grammar("tree_sitter_java")
_tsrust = _load_grammar("tree_sitter_rust")
_tscsharp = _load_grammar("tree_sitter_c_sharp")
_tsruby = _load_grammar("tree_sitter_ruby")
_tsphp = _load_grammar("tree_sitter_php")
_tskotlin = _load_grammar("tree_sitter_kotlin")
_tsscala = _load_grammar("tree_sitter_scala")
_tsswift = _load_grammar("tree_sitter_swift")
_tsc = _load_grammar("tree_sitter_c")
_tscpp = _load_grammar("tree_sitter_cpp")

_TREE_SITTER_AVAILABLE = _tsts is not None
_JAVA_TS_AVAILABLE = _tsjava is not None
_RUST_TS_AVAILABLE = _tsrust is not None
_CSHARP_TS_AVAILABLE = _tscsharp is not None
_RUBY_TS_AVAILABLE = _tsruby is not None
_PHP_TS_AVAILABLE = _tsphp is not None
_KOTLIN_TS_AVAILABLE = _tskotlin is not None
_SCALA_TS_AVAILABLE = _tsscala is not None
_SWIFT_TS_AVAILABLE = _tsswift is not None
_C_TS_AVAILABLE = _tsc is not None
_CPP_TS_AVAILABLE = _tscpp is not None

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from slurp.ignore import SlurpIgnore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _file_id(rel_path: Path) -> str:
    """Convert a relative file path to a stable dot-separated node ID.

    Examples:
        src/auth/service.py  →  src.auth.service
        auth.py              →  auth
    """
    stem = rel_path.with_suffix("")
    parts = [p.replace("-", "_").replace(" ", "_") for p in stem.parts]
    return ".".join(parts)


def _rel(path: Path, root: Path) -> Path:
    """Return path relative to root, or path itself if not a child of root."""
    try:
        return path.relative_to(root)
    except ValueError:
        return path


def _lineno(source: str, pos: int) -> int:
    """Return the 1-based line number for character offset pos."""
    return source[:pos].count("\n") + 1


_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    "coverage", ".tox", ".eggs", "vendor",
})


def _should_skip(path: Path) -> bool:
    """Return True if path is inside a directory that should be skipped."""
    return any(
        part in _SKIP_DIRS or part.endswith(".egg-info")
        for part in path.parts
    )


# ---------------------------------------------------------------------------
# Python indexer (stdlib ast)
# ---------------------------------------------------------------------------

class _PythonVisitor(ast.NodeVisitor):
    """Traverses a Python AST and emits graphify-compatible node/edge dicts."""

    def __init__(self, rel_path: Path) -> None:
        self.rel_path = rel_path
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self._scope: list[str] = []

        fid = _file_id(rel_path)
        self.nodes.append({
            "id": fid,
            "label": rel_path.stem,
            "type": "module",
            "description": f"Python module {rel_path.name}",
            "source_file": str(rel_path),
            "source_location": "L1",
            "file_type": "code",
        })
        self._scope.append(fid)

    @property
    def _parent(self) -> str:
        return self._scope[-1]

    def _emit(self, nid: str, label: str, ntype: str, lineno: int) -> None:
        self.nodes.append({
            "id": nid,
            "label": label,
            "type": ntype,
            "description": "",
            "source_file": str(self.rel_path),
            "source_location": f"L{lineno}",
            "file_type": "code",
        })
        self.edges.append({"source": self._parent, "target": nid, "relation": "contains"})

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        nid = f"{self._parent}.{node.name}"
        self._emit(nid, node.name, "class", node.lineno)
        self._scope.append(nid)
        self.generic_visit(node)
        self._scope.pop()

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        nid = f"{self._parent}.{node.name}"
        self._emit(nid, node.name, "function", node.lineno)
        self._scope.append(nid)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_func(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            raw = alias.asname or alias.name
            safe = raw.replace(".", "_").replace("-", "_")
            nid = f"{self._parent}.import_{safe}"
            self.nodes.append({
                "id": nid,
                "label": alias.name,
                "type": "import",
                "description": "",
                "source_file": str(self.rel_path),
                "source_location": f"L{node.lineno}",
                "file_type": "code",
            })
            self.edges.append({"source": self._parent, "target": nid, "relation": "imports_from"})

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                continue
            raw = alias.asname or alias.name
            safe = f"{module}_{raw}".replace(".", "_").replace("-", "_")
            nid = f"{self._parent}.import_{safe}"
            label = f"{module}.{alias.name}" if module else alias.name
            self.nodes.append({
                "id": nid,
                "label": label,
                "type": "import",
                "description": "",
                "source_file": str(self.rel_path),
                "source_location": f"L{node.lineno}",
                "file_type": "code",
            })
            self.edges.append({"source": self._parent, "target": nid, "relation": "imports_from"})


def index_python(path: Path, root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Index a Python source file using stdlib ast.

    Args:
        path: Path to the .py file.
        root: Project root for computing relative IDs. Defaults to path.parent.

    Returns:
        (nodes, edges) in graphify-compatible format.
    """
    root = root or path.parent
    rel = _rel(path, root)
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, OSError):
        return [], []

    visitor = _PythonVisitor(rel)
    visitor.visit(tree)
    return visitor.nodes, visitor.edges


# ---------------------------------------------------------------------------
# TypeScript / JavaScript indexer
# ---------------------------------------------------------------------------

_TS_FUNC_RE = re.compile(
    r"(?m)^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s+(\w+)\s*[(<]"
)
_TS_CLASS_RE = re.compile(
    r"(?m)^[ \t]*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)(?=[\s{<])"
)
_TS_INTERFACE_RE = re.compile(
    r"(?m)^[ \t]*(?:export\s+)?interface\s+(\w+)(?=[\s{<])"
)
_TS_ARROW_RE = re.compile(
    r"(?m)^[ \t]*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:\([^\n]*?\)|[\w]+)\s*(?::[^\n]*?)?\s*=>"
)
# Class members, matched only at the top level of a class body (see
# _iter_class_members). Modifiers are optional and may appear in any order.
_TS_MODIFIERS = r"(?:(?:public|private|protected|readonly|static|abstract|override|async|declare)\s+)*"
_TS_METHOD_RE = re.compile(
    rf"^{_TS_MODIFIERS}(?:\*\s*)?(\w+)\s*(?:<[^>()]*>)?\s*\("
)
_TS_ACCESSOR_RE = re.compile(
    rf"^{_TS_MODIFIERS}(get|set)\s+(\w+)\s*\("
)
_TS_PROP_ARROW_RE = re.compile(
    rf"^{_TS_MODIFIERS}(\w+)\s*(?:\?)?\s*(?::[^=\n]*?)?=\s*"
    r"(?:async\s+)?(?:\([^\n]*?\)|\w+)\s*(?::[^\n]*?)?=>"
)
# Words that look like a call but are control flow, not a member declaration.
_TS_NOT_MEMBERS = frozenset({
    "if", "for", "while", "switch", "catch", "do", "return", "typeof", "new",
    "super", "this", "function", "await", "yield", "throw", "else", "case",
    "delete", "void", "in", "of", "import", "export",
})

_TS_IMPORT_RE = re.compile(
    r'import\s+(?:type\s+)?(?:\{[^}]+\}|[\w*][^"\']*)\s+from\s+[\'"]([^\'"]+)[\'"]'
)


def _class_body_span(
    source: str, class_start: int, limit: int | None = None
) -> tuple[int, int] | None:
    """Locate the brace-delimited body of the class beginning at *class_start*.

    Args:
        source: Full file text.
        class_start: Offset where the declaration begins.
        limit: Offset of the next declaration. A body brace found at or after
            this point belongs to that declaration, not this one — bodyless
            declarations (`case class User(id: Int)`) would otherwise swallow
            the following type's members.

    Returns:
        (first_index_inside, index_of_closing_brace), or None if there is no
        body or the braces are unbalanced.
    """
    open_idx = source.find("{", class_start)
    if open_idx == -1 or (limit is not None and open_idx >= limit):
        return None
    depth = 0
    for i in range(open_idx, len(source)):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return open_idx + 1, i
    return None


def _iter_class_members(body: str):
    """Yield (name, type, line_offset) for members declared directly in a class body.

    DECISION: a brace-depth counter keeps this to the class's own members —
    without it, every local function and callback inside a method body would be
    indexed as a method of the class. Braces inside strings can skew the depth;
    that is an accepted limit of a regex fallback.
    """
    depth = 0
    for offset, raw in enumerate(body.splitlines()):
        line = raw.strip()
        if depth == 0 and line and not line.startswith(("//", "/*", "*")):
            if (m := _TS_ACCESSOR_RE.match(line)) is not None:
                yield m.group(2), "function", offset
            elif (m := _TS_PROP_ARROW_RE.match(line)) is not None:
                yield m.group(1), "function", offset
            elif (m := _TS_METHOD_RE.match(line)) is not None:
                name = m.group(1)
                if name not in _TS_NOT_MEMBERS:
                    yield name, "function", offset
        depth += raw.count("{") - raw.count("}")
        if depth < 0:  # defensive: malformed source
            break


def _index_typescript_regex(
    source: str, rel_path: Path, file_id: str
) -> tuple[list[dict], list[dict]]:
    """Regex-based TypeScript/JavaScript parser (fallback when tree-sitter is unavailable).

    Extracts top-level functions, classes, interfaces and arrow consts, plus the
    members declared inside each class body. Class members are nested under the
    class exactly as the tree-sitter visitor nests them, so both parsers produce
    the same node ids and the same class -> member "contains" edges.
    """
    nodes: list[dict] = []
    edges: list[dict] = []

    def _add(name: str, ntype: str, lineno: int, parent: str = file_id) -> str:
        nid = f"{parent}.{name}"
        nodes.append({
            "id": nid,
            "label": name,
            "type": ntype,
            "description": "",
            "source_file": str(rel_path),
            "source_location": f"L{lineno}",
            "file_type": "code",
        })
        edges.append({"source": parent, "target": nid, "relation": "contains"})
        return nid

    for m in _TS_FUNC_RE.finditer(source):
        _add(m.group(1), "function", _lineno(source, m.start()))

    class_spans: list[tuple[int, int]] = []
    for m in _TS_CLASS_RE.finditer(source):
        class_line = _lineno(source, m.start())
        class_nid = _add(m.group(1), "class", class_line)

        span = _class_body_span(source, m.start())
        if span is None:
            continue
        class_spans.append(span)
        body_start, body_end = span
        body = source[body_start:body_end]
        body_line = _lineno(source, body_start)
        for name, ntype, offset in _iter_class_members(body):
            _add(name, ntype, body_line + offset, parent=class_nid)

    for m in _TS_INTERFACE_RE.finditer(source):
        _add(m.group(1), "interface", _lineno(source, m.start()))

    # DECISION: an arrow const declared inside a class body belongs to a method,
    # not to the file. Attaching it at file level would invent a top-level symbol
    # that does not exist; tree-sitter nests it under the enclosing method.
    for m in _TS_ARROW_RE.finditer(source):
        if any(start <= m.start() < end for start, end in class_spans):
            continue
        _add(m.group(1), "function", _lineno(source, m.start()))

    for m in _TS_IMPORT_RE.finditer(source):
        module_path = m.group(1)
        safe = module_path.rsplit("/", 1)[-1].replace("-", "_").replace(".", "_")
        nid = f"{file_id}.import_{safe}"
        nodes.append({
            "id": nid,
            "label": module_path,
            "type": "import",
            "description": "",
            "source_file": str(rel_path),
            "source_location": f"L{_lineno(source, m.start())}",
            "file_type": "code",
        })
        edges.append({"source": file_id, "target": nid, "relation": "imports_from"})

    return nodes, edges


class _BaseVisitor:
    """Shared tree-sitter traversal: scope stack, node emission, dispatch.

    Subclasses add visit_<node_type> methods; unknown types are recursed into.
    """

    _NAME_FIELDS = ("identifier", "type_identifier", "property_identifier", "constant")

    def __init__(self, rel_path: Path, file_id: str) -> None:
        self.rel_path = rel_path
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self._scope: list[str] = [file_id]

    @property
    def _parent(self) -> str:
        return self._scope[-1]

    @classmethod
    def _ts_name(cls, node) -> str:
        name_node = node.child_by_field_name("name")
        if name_node:
            return name_node.text.decode("utf-8")
        for child in node.children:
            if child.type in cls._NAME_FIELDS:
                return child.text.decode("utf-8")
        return ""

    @staticmethod
    def _text(node) -> str:
        return node.text.decode("utf-8", "replace") if node is not None else ""

    def _emit(self, name: str, ntype: str, lineno: int, **extra) -> str:
        """Append a node (plus its 'contains' edge) and return its id.

        Args:
            name: Symbol name, appended to the current scope to form the id.
            ntype: Node type recorded on the graph.
            lineno: 1-based source line.
            **extra: Language-specific attributes (annotations, visibility, ...).
                Keys whose value is None or an empty list/str are dropped so the
                graph stays free of empty fields.
        """
        nid = f"{self._parent}.{name}"
        node = {
            "id": nid,
            "label": name,
            "type": ntype,
            "description": "",
            "source_file": str(self.rel_path),
            "source_location": f"L{lineno}",
            "file_type": "code",
        }
        node.update({k: v for k, v in extra.items() if v not in (None, "", [])})
        self.nodes.append(node)
        self.edges.append({"source": self._parent, "target": nid, "relation": "contains"})
        return nid

    def _edge(self, source: str, target: str, relation: str) -> None:
        self.edges.append({"source": source, "target": target, "relation": relation})

    def visit(self, node) -> None:
        handler = getattr(self, f"visit_{node.type}", None)
        if handler is not None:
            handler(node)
        else:
            self._recurse(node)

    def _recurse(self, node) -> None:
        for child in node.children:
            self.visit(child)

    def _descend(self, node, nid: str) -> None:
        """Recurse into *node* with *nid* pushed as the enclosing scope."""
        self._scope.append(nid)
        self._recurse(node)
        self._scope.pop()


class _TSVisitor(_BaseVisitor):
    """tree-sitter–based TypeScript/JavaScript visitor."""

    _DECL_TYPES: dict[str, str] = {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "class_declaration": "class",
        "abstract_class_declaration": "class",
        "interface_declaration": "interface",
        "method_definition": "function",
        "function_signature": "function",
        "abstract_method_signature": "function",
    }

    def _visit_decl(self, node) -> None:
        ntype = self._DECL_TYPES.get(node.type, "function")
        name = self._ts_name(node)
        if not name:
            self._recurse(node)
            return
        lineno = node.start_point[0] + 1
        nid = self._emit(name, ntype, lineno)
        self._descend(node, nid)

    visit_function_declaration = _visit_decl
    visit_generator_function_declaration = _visit_decl
    visit_class_declaration = _visit_decl
    visit_abstract_class_declaration = _visit_decl
    visit_interface_declaration = _visit_decl
    visit_method_definition = _visit_decl
    visit_function_signature = _visit_decl
    visit_abstract_method_signature = _visit_decl

    def visit_export_statement(self, node) -> None:
        self._recurse(node)

    def visit_lexical_declaration(self, node) -> None:
        for child in node.children:
            if child.type == "variable_declarator":
                name_node = child.child_by_field_name("name")
                val_node = child.child_by_field_name("value")
                if name_node and val_node and val_node.type in ("arrow_function", "function"):
                    name = name_node.text.decode("utf-8")
                    self._emit(name, "function", child.start_point[0] + 1)

    def visit_import_statement(self, node) -> None:
        source_child = node.child_by_field_name("source")
        module = source_child.text.decode("utf-8").strip("'\"") if source_child else ""
        lineno = node.start_point[0] + 1
        safe = module.rsplit("/", 1)[-1].replace("-", "_").replace(".", "_")
        nid = f"{self._parent}.import_{safe}"
        self.nodes.append({
            "id": nid,
            "label": module,
            "type": "import",
            "description": "",
            "source_file": str(self.rel_path),
            "source_location": f"L{lineno}",
            "file_type": "code",
        })
        self.edges.append({"source": self._parent, "target": nid, "relation": "imports_from"})


def _parse_ts_tree(source_bytes: bytes, is_tsx: bool = False):
    """Parse TypeScript source with tree-sitter.

    Raises:
        RuntimeError: If tree-sitter is not installed. Callers should check
            _TREE_SITTER_AVAILABLE first rather than relying on this.
    """
    if not _TREE_SITTER_AVAILABLE:
        raise RuntimeError("tree-sitter is not installed (install the 'ts' extra)")
    lang = _TSLanguage(_tsts.language_tsx() if is_tsx else _tsts.language_typescript())
    return _TSParser(lang).parse(source_bytes)


def index_typescript(path: Path, root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Index a TypeScript/JavaScript file.

    Uses tree-sitter if available; otherwise falls back to regex parsing.

    Args:
        path: Path to the .ts/.tsx/.js/.jsx file.
        root: Project root for relative IDs. Defaults to path.parent.

    Returns:
        (nodes, edges) in graphify-compatible format.
    """
    root = root or path.parent
    rel = _rel(path, root)
    is_tsx = path.suffix.lower() in (".tsx", ".jsx")
    fid = _file_id(rel)

    module_node: dict = {
        "id": fid,
        "label": path.name,
        "type": "module",
        "description": f"TypeScript module {path.name}",
        "source_file": str(rel),
        "source_location": "L1",
        "file_type": "code",
    }

    try:
        source_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [module_node], []

    if _TREE_SITTER_AVAILABLE:
        try:
            tree = _parse_ts_tree(source_text.encode("utf-8"), is_tsx=is_tsx)
            if _tree_is_broken(tree):
                raise ValueError("grammar produced a tree containing ERROR nodes")
            visitor = _TSVisitor(rel, fid)
            visitor.visit(tree.root_node)
            return [module_node] + visitor.nodes, visitor.edges
        except Exception as exc:
            # Installed but broken on this file — the user needs to know, because
            # the regex fallback below extracts strictly less.
            import click  # noqa: PLC0415

            click.echo(
                f"  Warning: tree-sitter failed on {path}, "
                f"falling back to regex: {exc}",
                err=True,
            )

    nodes, edges = _index_typescript_regex(source_text, rel, fid)
    return [module_node] + nodes, edges


# ---------------------------------------------------------------------------
# Go indexer (regex)
# ---------------------------------------------------------------------------

_GO_PACKAGE_RE = re.compile(r"^package\s+(\w+)", re.MULTILINE)
_GO_FUNC_RE = re.compile(
    r"^func\s+(?:\(\s*\w+\s+\*?[\w.]+\s*\)\s+)?(\w+)\s*\(",
    re.MULTILINE,
)
_GO_STRUCT_RE = re.compile(r"^type\s+(\w+)\s+struct\b", re.MULTILINE)
_GO_INTERFACE_RE = re.compile(r"^type\s+(\w+)\s+interface\b", re.MULTILINE)
_GO_IMPORT_BLOCK_RE = re.compile(r"import\s*\((.*?)\)", re.DOTALL)
_GO_IMPORT_SINGLE_RE = re.compile(r'^import\s+"([^"]+)"', re.MULTILINE)
_GO_IMPORT_PATH_RE = re.compile(r'"([^"]+)"')


def index_go(path: Path, root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Index a Go source file using regex.

    Args:
        path: Path to the .go file.
        root: Project root for relative IDs. Defaults to path.parent.

    Returns:
        (nodes, edges) in graphify-compatible format.
    """
    root = root or path.parent
    rel = _rel(path, root)
    fid = _file_id(rel)

    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], []

    nodes: list[dict] = []
    edges: list[dict] = []

    pkg_match = _GO_PACKAGE_RE.search(source)
    pkg_name = pkg_match.group(1) if pkg_match else rel.stem
    nodes.append({
        "id": fid,
        "label": pkg_name,
        "type": "module",
        "description": f"Go package {pkg_name}",
        "source_file": str(rel),
        "source_location": "L1",
        "file_type": "code",
    })

    def _add(name: str, ntype: str, lineno: int) -> None:
        nid = f"{fid}.{name}"
        nodes.append({
            "id": nid,
            "label": name,
            "type": ntype,
            "description": "",
            "source_file": str(rel),
            "source_location": f"L{lineno}",
            "file_type": "code",
        })
        edges.append({"source": fid, "target": nid, "relation": "contains"})

    for m in _GO_STRUCT_RE.finditer(source):
        _add(m.group(1), "class", _lineno(source, m.start()))

    for m in _GO_INTERFACE_RE.finditer(source):
        _add(m.group(1), "interface", _lineno(source, m.start()))

    for m in _GO_FUNC_RE.finditer(source):
        _add(m.group(1), "function", _lineno(source, m.start()))

    seen_imports: set[str] = set()

    for block_m in _GO_IMPORT_BLOCK_RE.finditer(source):
        block_lineno = _lineno(source, block_m.start())
        for path_m in _GO_IMPORT_PATH_RE.finditer(block_m.group(1)):
            imp_path = path_m.group(1)
            safe = imp_path.rsplit("/", 1)[-1].replace("-", "_").replace(".", "_")
            nid = f"{fid}.import_{safe}"
            if nid not in seen_imports:
                seen_imports.add(nid)
                nodes.append({
                    "id": nid,
                    "label": imp_path,
                    "type": "import",
                    "description": "",
                    "source_file": str(rel),
                    "source_location": f"L{block_lineno}",
                    "file_type": "code",
                })
                edges.append({"source": fid, "target": nid, "relation": "imports_from"})

    for m in _GO_IMPORT_SINGLE_RE.finditer(source):
        imp_path = m.group(1)
        safe = imp_path.rsplit("/", 1)[-1].replace("-", "_").replace(".", "_")
        nid = f"{fid}.import_{safe}"
        if nid not in seen_imports:
            seen_imports.add(nid)
            nodes.append({
                "id": nid,
                "label": imp_path,
                "type": "import",
                "description": "",
                "source_file": str(rel),
                "source_location": f"L{_lineno(source, m.start())}",
                "file_type": "code",
            })
            edges.append({"source": fid, "target": nid, "relation": "imports_from"})

    return nodes, edges


# ---------------------------------------------------------------------------
# Project orchestrator
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------

_JAVA_VISIBILITY = ("public", "private", "protected")


def _java_modifiers(node) -> tuple[list[str], str]:
    """Extract (annotations, visibility) from a declaration's `modifiers` child."""
    annotations: list[str] = []
    visibility = ""
    for child in node.children:
        if child.type != "modifiers":
            continue
        for mod in child.children:
            if mod.type in ("marker_annotation", "annotation"):
                annotations.append(mod.text.decode("utf-8", "replace").split("(")[0])
            elif mod.type in _JAVA_VISIBILITY:
                visibility = mod.type
            elif mod.text.decode("utf-8", "replace") in _JAVA_VISIBILITY:
                visibility = mod.text.decode("utf-8", "replace")
    return annotations, visibility


class _JavaVisitor(_BaseVisitor):
    """tree-sitter Java visitor.

    Beyond declarations it records the things that make a Java codebase
    navigable: annotations (Spring/JPA architecture lives in them), access
    modifiers, generic signatures, and the extends/implements graph.
    """

    _DECL_TYPES = {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "record_declaration": "class",
        "annotation_type_declaration": "interface",
    }

    def visit_package_declaration(self, node) -> None:
        name = self._text(node.child(1)).rstrip(";").strip()
        if name:
            self._emit(name, "package", node.start_point[0] + 1)

    def visit_import_declaration(self, node) -> None:
        module = self._text(node).removeprefix("import").strip().rstrip(";").strip()
        if not module:
            return
        safe = module.rsplit(".", 1)[-1]
        nid = f"{self._parent}.import_{safe}"
        self.nodes.append({
            "id": nid, "label": module, "type": "import", "description": "",
            "source_file": str(self.rel_path),
            "source_location": f"L{node.start_point[0] + 1}",
            "file_type": "code",
        })
        self._edge(self._parent, nid, "imports_from")

    def _visit_type(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            self._recurse(node)
            return
        annotations, visibility = _java_modifiers(node)
        nid = self._emit(
            name, self._DECL_TYPES.get(node.type, "class"),
            node.start_point[0] + 1,
            annotations=annotations, visibility=visibility,
        )
        # extends / implements
        for child in node.children:
            if child.type in ("superclass", "extends_interfaces"):
                for tname in self._type_names(child):
                    self._edge(nid, tname, "extends")
            elif child.type == "super_interfaces":
                for tname in self._type_names(child):
                    self._edge(nid, tname, "implements")
        self._descend(node, nid)

    visit_class_declaration = _visit_type
    visit_interface_declaration = _visit_type
    visit_enum_declaration = _visit_type
    visit_record_declaration = _visit_type
    visit_annotation_type_declaration = _visit_type

    @staticmethod
    def _type_names(node) -> list[str]:
        """Collect bare type names under a superclass/super_interfaces node."""
        found: list[str] = []
        stack = list(node.children)
        while stack:
            child = stack.pop(0)
            if child.type in ("type_identifier", "scoped_type_identifier"):
                found.append(child.text.decode("utf-8", "replace"))
            else:
                stack.extend(child.children)
        return found

    def visit_method_declaration(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            return
        annotations, visibility = _java_modifiers(node)
        params = self._text(node.child_by_field_name("parameters"))
        type_params = ""
        for child in node.children:
            if child.type == "type_parameters":
                type_params = self._text(child)
        label = f"{type_params}{name}{params}" if type_params else f"{name}{params}"
        self._emit(
            name, "method", node.start_point[0] + 1,
            annotations=annotations, visibility=visibility, signature=label,
        )

    def visit_constructor_declaration(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            return
        annotations, visibility = _java_modifiers(node)
        self._emit(
            name, "constructor", node.start_point[0] + 1,
            annotations=annotations, visibility=visibility,
            signature=f"{name}{self._text(node.child_by_field_name('parameters'))}",
        )

    def visit_field_declaration(self, node) -> None:
        annotations, visibility = _java_modifiers(node)
        for child in node.children:
            if child.type != "variable_declarator":
                continue
            name = self._text(child.child_by_field_name("name"))
            if name:
                self._emit(
                    name, "field", node.start_point[0] + 1,
                    annotations=annotations, visibility=visibility,
                )


_JAVA_PACKAGE_RE = re.compile(r"(?m)^\s*package\s+([\w.]+)\s*;")
_JAVA_IMPORT_RE = re.compile(r"(?m)^\s*import\s+(?:static\s+)?([\w.*]+)\s*;")
_JAVA_TYPE_RE = re.compile(
    r"(?m)^[ \t]*(?:(?:public|private|protected|static|final|abstract|sealed)\s+)*"
    r"(class|interface|enum|record)\s+(\w+)"
)
_JAVA_MEMBER_RE = re.compile(
    r"^(?:(?:public|private|protected|static|final|abstract|synchronized|native|default|transient|volatile)\s+)*"
    r"(?:<[^>]+>\s*)?(?:[\w.<>\[\],?\s]+\s+)?(\w+)\s*\("
)
# Keeps the leading @ so regex output matches the tree-sitter path exactly.
_JAVA_ANNOTATION_RE = re.compile(r"(@\w+)")
_JAVA_NOT_MEMBERS = frozenset({
    "if", "for", "while", "switch", "catch", "return", "new", "super", "this",
    "do", "else", "try", "synchronized", "throw", "assert",
})


def _index_java_regex(source: str, rel_path: Path, file_id: str) -> tuple[list[dict], list[dict]]:
    """Regex fallback for Java. Captures packages, imports, types and members.

    Annotations and visibility are recovered from the declaration's own line and
    the lines directly above it, which is where Java conventionally puts them.
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    lines = source.splitlines()

    def _add(name: str, ntype: str, lineno: int, parent: str, **extra) -> str:
        nid = f"{parent}.{name}"
        node = {
            "id": nid, "label": name, "type": ntype, "description": "",
            "source_file": str(rel_path), "source_location": f"L{lineno}",
            "file_type": "code",
        }
        node.update({k: v for k, v in extra.items() if v not in (None, "", [])})
        nodes.append(node)
        edges.append({"source": parent, "target": nid, "relation": "contains"})
        return nid

    def _context(line_idx: int) -> tuple[list[str], str]:
        """Annotations on the preceding lines plus visibility on this one."""
        annotations = _JAVA_ANNOTATION_RE.findall(lines[line_idx]) if line_idx < len(lines) else []
        i = line_idx - 1
        while i >= 0 and (stripped := lines[i].strip()).startswith("@"):
            annotations = _JAVA_ANNOTATION_RE.findall(stripped) + annotations
            i -= 1
        visibility = ""
        if line_idx < len(lines):
            for kw in _JAVA_VISIBILITY:
                if re.search(rf"\b{kw}\b", lines[line_idx]):
                    visibility = kw
                    break
        return annotations, visibility

    for m in _JAVA_PACKAGE_RE.finditer(source):
        _add(m.group(1), "package", _lineno(source, m.start()), file_id)

    for m in _JAVA_IMPORT_RE.finditer(source):
        module = m.group(1)
        nid = f"{file_id}.import_{module.rsplit('.', 1)[-1]}"
        nodes.append({
            "id": nid, "label": module, "type": "import", "description": "",
            "source_file": str(rel_path),
            "source_location": f"L{_lineno(source, m.start())}",
            "file_type": "code",
        })
        edges.append({"source": file_id, "target": nid, "relation": "imports_from"})

    for m in _JAVA_TYPE_RE.finditer(source):
        kind, name = m.group(1), m.group(2)
        line_idx = _lineno(source, m.start()) - 1
        annotations, visibility = _context(line_idx)
        ntype = {"class": "class", "interface": "interface",
                 "enum": "enum", "record": "class"}[kind]
        type_nid = _add(name, ntype, line_idx + 1, file_id,
                        annotations=annotations, visibility=visibility)

        header = source[m.start():source.find("{", m.start()) + 1]
        if (ext := re.search(r"\bextends\s+([\w.<>,\s]+?)(?:\bimplements\b|\{)", header)):
            for tname in re.findall(r"\b([A-Z]\w*)", ext.group(1)):
                edges.append({"source": type_nid, "target": tname, "relation": "extends"})
        if (impl := re.search(r"\bimplements\s+([\w.<>,\s]+?)\{", header)):
            for tname in re.findall(r"\b([A-Z]\w*)", impl.group(1)):
                edges.append({"source": type_nid, "target": tname, "relation": "implements"})

        span = _class_body_span(source, m.start())
        if span is None:
            continue
        body_start, body_end = span
        body_line = _lineno(source, body_start)
        depth = 0
        for offset, raw in enumerate(source[body_start:body_end].splitlines()):
            line = raw.strip()
            if depth == 0 and line and not line.startswith(("//", "/*", "*", "@")):
                if (mm := _JAVA_MEMBER_RE.match(line)) is not None:
                    member = mm.group(1)
                    if member not in _JAVA_NOT_MEMBERS:
                        abs_idx = body_line + offset - 1
                        ann, vis = _context(abs_idx)
                        ntype = "constructor" if member == name else "method"
                        _add(member, ntype, abs_idx + 1, type_nid,
                             annotations=ann, visibility=vis)
            depth += raw.count("{") - raw.count("}")
            if depth < 0:
                break

    return nodes, edges


def index_java(path: Path, root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Index a Java file.

    Uses tree-sitter when the optional 'java' extra is installed; otherwise a
    regex fallback that extracts strictly less (no nested-class scoping).
    """
    rel = _rel(path, root) if root else path
    fid = _file_id(rel)
    module_node = {
        "id": fid, "label": rel.stem, "type": "module",
        "description": f"Java file {rel.name}", "source_file": str(rel),
        "source_location": "L1", "file_type": "code",
    }
    try:
        source_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [module_node], []

    if _JAVA_TS_AVAILABLE:
        try:
            tree = _TSParser(_TSLanguage(_tsjava.language())).parse(
                source_text.encode("utf-8"))
            if _tree_is_broken(tree):
                raise ValueError("grammar produced a tree containing ERROR nodes")
            visitor = _JavaVisitor(rel, fid)
            visitor.visit(tree.root_node)
            return [module_node] + visitor.nodes, visitor.edges
        except Exception as exc:
            import click  # noqa: PLC0415

            click.echo(f"  Warning: tree-sitter failed on {path}, "
                       f"falling back to regex: {exc}", err=True)

    nodes, edges = _index_java_regex(source_text, rel, fid)
    return [module_node] + nodes, edges


# ---------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------


class _RustVisitor(_BaseVisitor):
    """tree-sitter Rust visitor.

    Records pub/private visibility, lifetimes in signatures, macro definitions,
    and the trait-implementation graph: `impl Trait for Type` becomes an
    `implements` edge from the type to the trait, which is how Rust expresses
    the relationship that `extends`/`implements` expresses elsewhere.
    """

    @staticmethod
    def _visibility(node) -> str:
        for child in node.children:
            if child.type == "visibility_modifier":
                return child.text.decode("utf-8", "replace")
        return "private"

    def _generics(self, node) -> str:
        for child in node.children:
            if child.type == "type_parameters":
                return self._text(child)
        return ""

    def _visit_container(self, node, ntype: str) -> None:
        name = self._ts_name(node)
        if not name:
            self._recurse(node)
            return
        generics = self._generics(node)
        nid = self._emit(
            name, ntype, node.start_point[0] + 1,
            visibility=self._visibility(node),
            signature=f"{name}{generics}" if generics else "",
        )
        self._descend(node, nid)

    def visit_mod_item(self, node) -> None:
        self._visit_container(node, "module")

    def visit_struct_item(self, node) -> None:
        self._visit_container(node, "struct")

    def visit_enum_item(self, node) -> None:
        self._visit_container(node, "enum")

    def visit_trait_item(self, node) -> None:
        self._visit_container(node, "trait")

    def visit_union_item(self, node) -> None:
        self._visit_container(node, "struct")

    def visit_impl_item(self, node) -> None:
        """`impl Type` or `impl Trait for Type`.

        The block itself is not a symbol users search for, so its functions are
        attached to the type; when a trait is named, an `implements` edge links
        the type to it.
        """
        type_node = node.child_by_field_name("type")
        trait_node = node.child_by_field_name("trait")
        type_name = self._text(type_node)
        if not type_name:
            self._recurse(node)
            return

        target_nid = f"{self._parent}.{type_name}"
        if trait_node is not None:
            self._edge(target_nid, self._text(trait_node), "implements")
        self._descend(node, target_nid)

    def _visit_fn(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            return
        params = self._text(node.child_by_field_name("parameters"))
        generics = self._generics(node)
        signature = f"{name}{generics}{params}"
        self._emit(
            name, "function", node.start_point[0] + 1,
            visibility=self._visibility(node),
            signature=signature,
            # Lifetimes carry ownership semantics a reader needs at a glance.
            # dict.fromkeys de-duplicates while preserving declaration order.
            lifetimes=list(dict.fromkeys(re.findall(r"'(\w+)", signature))),
        )

    visit_function_item = _visit_fn
    visit_function_signature_item = _visit_fn

    def visit_macro_definition(self, node) -> None:
        name = self._ts_name(node)
        if name:
            self._emit(name, "macro", node.start_point[0] + 1)

    def visit_use_declaration(self, node) -> None:
        module = self._text(node).removeprefix("use").strip().rstrip(";").strip()
        if not module:
            return
        safe = module.replace("::", "_").replace("{", "").replace("}", "").split(",")[0].strip()
        nid = f"{self._parent}.import_{safe}"
        self.nodes.append({
            "id": nid, "label": module, "type": "import", "description": "",
            "source_file": str(self.rel_path),
            "source_location": f"L{node.start_point[0] + 1}",
            "file_type": "code",
        })
        self._edge(self._parent, nid, "imports_from")


_RUST_USE_RE = re.compile(r"(?m)^\s*use\s+([^;]+);")
_RUST_ITEM_RE = re.compile(
    r"(?m)^[ \t]*(pub(?:\([^)]*\))?\s+)?(mod|struct|enum|trait|union)\s+(\w+)"
)
_RUST_FN_RE = re.compile(
    r"(?m)^[ \t]*(pub(?:\([^)]*\))?\s+)?(?:async\s+|const\s+|unsafe\s+|extern\s+\"[^\"]*\"\s+)*"
    r"fn\s+(\w+)(<[^>{]*>)?\s*(\([^\n]*?\))?"
)
_RUST_MACRO_RE = re.compile(r"(?m)^[ \t]*macro_rules!\s*(\w+)")
_RUST_IMPL_RE = re.compile(r"(?m)^[ \t]*impl(?:<[^>]*>)?\s+([\w:]+)(?:<[^>]*>)?(?:\s+for\s+([\w:]+))?")


def _index_rust_regex(source: str, rel_path: Path, file_id: str) -> tuple[list[dict], list[dict]]:
    """Regex fallback for Rust."""
    nodes: list[dict] = []
    edges: list[dict] = []

    def _add(name: str, ntype: str, lineno: int, parent: str = file_id, **extra) -> str:
        nid = f"{parent}.{name}"
        node = {
            "id": nid, "label": name, "type": ntype, "description": "",
            "source_file": str(rel_path), "source_location": f"L{lineno}",
            "file_type": "code",
        }
        node.update({k: v for k, v in extra.items() if v not in (None, "", [])})
        nodes.append(node)
        edges.append({"source": parent, "target": nid, "relation": "contains"})
        return nid

    for m in _RUST_USE_RE.finditer(source):
        module = m.group(1).strip()
        safe = module.replace("::", "_").replace("{", "").replace("}", "").split(",")[0].strip()
        nid = f"{file_id}.import_{safe}"
        nodes.append({
            "id": nid, "label": module, "type": "import", "description": "",
            "source_file": str(rel_path),
            "source_location": f"L{_lineno(source, m.start())}",
            "file_type": "code",
        })
        edges.append({"source": file_id, "target": nid, "relation": "imports_from"})

    kinds = {"mod": "module", "struct": "struct", "enum": "enum",
             "trait": "trait", "union": "struct"}
    for m in _RUST_ITEM_RE.finditer(source):
        vis = "pub" if m.group(1) else "private"
        _add(m.group(3), kinds[m.group(2)], _lineno(source, m.start()), visibility=vis)

    for m in _RUST_MACRO_RE.finditer(source):
        _add(m.group(1), "macro", _lineno(source, m.start()))

    # `impl Trait for Type` -> Type implements Trait
    for m in _RUST_IMPL_RE.finditer(source):
        first, second = m.group(1), m.group(2)
        if second:
            edges.append({"source": f"{file_id}.{second}", "target": first,
                          "relation": "implements"})

    for m in _RUST_FN_RE.finditer(source):
        vis = "pub" if m.group(1) else "private"
        name, generics, params = m.group(2), m.group(3) or "", m.group(4) or ""
        signature = f"{name}{generics}{params}"
        _add(name, "function", _lineno(source, m.start()), visibility=vis,
             signature=signature,
             lifetimes=list(dict.fromkeys(re.findall(r"'(\w+)", signature))))

    return nodes, edges


# ---------------------------------------------------------------------------
# C#
# ---------------------------------------------------------------------------


def _csharp_attributes(node) -> list[str]:
    """Collect [Attribute] names declared on a C# member."""
    found: list[str] = []
    for child in node.children:
        if child.type == "attribute_list":
            for attr in child.children:
                if attr.type == "attribute":
                    found.append(f"[{_BaseVisitor._ts_name(attr)}]")
    return found


class _CSharpVisitor(_BaseVisitor):
    """tree-sitter C# visitor.

    Mirrors the Java visitor's annotation handling for `[Attribute]`, tracks
    async methods, records properties separately from methods, and reflects
    nested namespaces in the node id.
    """

    _DECL_TYPES = {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "struct_declaration": "struct",
        "record_declaration": "class",
    }

    @staticmethod
    def _modifiers(node) -> tuple[str, bool]:
        visibility, is_async = "", False
        for child in node.children:
            if child.type == "modifier":
                text = child.text.decode("utf-8", "replace")
                if text in ("public", "private", "protected", "internal"):
                    visibility = text
                elif text == "async":
                    is_async = True
        return visibility, is_async

    def visit_namespace_declaration(self, node) -> None:
        name = self._text(node.child_by_field_name("name"))
        if not name:
            self._recurse(node)
            return
        nid = self._emit(name, "namespace", node.start_point[0] + 1)
        self._descend(node, nid)

    visit_file_scoped_namespace_declaration = visit_namespace_declaration

    def _visit_type(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            self._recurse(node)
            return
        visibility, _ = self._modifiers(node)
        ntype = self._DECL_TYPES.get(node.type, "class")
        nid = self._emit(
            name, ntype, node.start_point[0] + 1,
            attributes=_csharp_attributes(node), visibility=visibility,
            # The I-prefix convention is load-bearing in C# codebases.
            is_interface=(ntype == "interface" or bool(re.match(r"^I[A-Z]", name))),
        )
        for child in node.children:
            if child.type == "base_list":
                for base in child.children:
                    if base.type in ("identifier", "qualified_name", "generic_name"):
                        base_name = self._text(base)
                        relation = "implements" if re.match(r"^I[A-Z]", base_name) else "extends"
                        self._edge(nid, base_name, relation)
        self._descend(node, nid)

    visit_class_declaration = _visit_type
    visit_interface_declaration = _visit_type
    visit_enum_declaration = _visit_type
    visit_struct_declaration = _visit_type
    visit_record_declaration = _visit_type

    def visit_method_declaration(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            return
        visibility, is_async = self._modifiers(node)
        self._emit(
            name, "method", node.start_point[0] + 1,
            attributes=_csharp_attributes(node), visibility=visibility,
            is_async=is_async or None,
            signature=f"{name}{self._text(node.child_by_field_name('parameters'))}",
        )

    def visit_constructor_declaration(self, node) -> None:
        name = self._ts_name(node)
        if name:
            visibility, _ = self._modifiers(node)
            self._emit(name, "constructor", node.start_point[0] + 1,
                       attributes=_csharp_attributes(node), visibility=visibility)

    def visit_property_declaration(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            return
        visibility, _ = self._modifiers(node)
        # Label carries the owning type, matching how C# properties are referred to.
        owner = self._parent.rsplit(".", 1)[-1]
        self._emit(name, "property", node.start_point[0] + 1,
                   attributes=_csharp_attributes(node), visibility=visibility,
                   label_qualified=f"{owner}.{name}")

    def visit_using_directive(self, node) -> None:
        module = self._text(node).removeprefix("using").strip().rstrip(";").strip()
        if not module:
            return
        safe = module.rsplit(".", 1)[-1]
        nid = f"{self._parent}.import_{safe}"
        self.nodes.append({
            "id": nid, "label": module, "type": "import", "description": "",
            "source_file": str(self.rel_path),
            "source_location": f"L{node.start_point[0] + 1}",
            "file_type": "code",
        })
        self._edge(self._parent, nid, "imports_from")


_CS_NAMESPACE_RE = re.compile(r"(?m)^\s*namespace\s+([\w.]+)")
_CS_USING_RE = re.compile(r"(?m)^\s*using\s+(?:static\s+)?([\w.]+)\s*;")
_CS_TYPE_RE = re.compile(
    r"(?m)^[ \t]*(?:\[[^\]]*\]\s*)*"
    r"(?:(?:public|private|protected|internal|static|sealed|abstract|partial|readonly)\s+)*"
    r"(class|interface|enum|struct|record)\s+(\w+)"
)
_CS_PROPERTY_RE = re.compile(
    r"^(?:(?:public|private|protected|internal|static|virtual|override|abstract|readonly)\s+)*"
    r"[\w<>\[\],?\s]+\s+(\w+)\s*\{\s*get\b"
)
_CS_METHOD_RE = re.compile(
    r"^(?:(?:public|private|protected|internal|static|virtual|override|abstract|sealed|partial)\s+)*"
    r"(async\s+)?(?:[\w<>\[\],?\s\.]+\s+)?(\w+)\s*\("
)
_CS_ATTR_RE = re.compile(r"\[(\w+)")
_CS_NOT_MEMBERS = frozenset({
    "if", "for", "foreach", "while", "switch", "catch", "return", "new", "using",
    "lock", "do", "else", "try", "throw", "base", "this", "get", "set",
})


def _index_csharp_regex(source: str, rel_path: Path, file_id: str) -> tuple[list[dict], list[dict]]:
    """Regex fallback for C#."""
    nodes: list[dict] = []
    edges: list[dict] = []
    lines = source.splitlines()

    def _add(name: str, ntype: str, lineno: int, parent: str, **extra) -> str:
        nid = f"{parent}.{name}"
        node = {
            "id": nid, "label": name, "type": ntype, "description": "",
            "source_file": str(rel_path), "source_location": f"L{lineno}",
            "file_type": "code",
        }
        node.update({k: v for k, v in extra.items() if v not in (None, "", [], False)})
        nodes.append(node)
        edges.append({"source": parent, "target": nid, "relation": "contains"})
        return nid

    def _attrs_above(idx: int) -> list[str]:
        found: list[str] = []
        i = idx - 1
        while i >= 0 and (stripped := lines[i].strip()).startswith("["):
            found = [f"[{a}]" for a in _CS_ATTR_RE.findall(stripped)] + found
            i -= 1
        return found

    namespace_nid = file_id
    if (ns := _CS_NAMESPACE_RE.search(source)) is not None:
        namespace_nid = _add(ns.group(1), "namespace",
                             _lineno(source, ns.start()), file_id)

    for m in _CS_USING_RE.finditer(source):
        module = m.group(1)
        nid = f"{file_id}.import_{module.rsplit('.', 1)[-1]}"
        nodes.append({
            "id": nid, "label": module, "type": "import", "description": "",
            "source_file": str(rel_path),
            "source_location": f"L{_lineno(source, m.start())}",
            "file_type": "code",
        })
        edges.append({"source": file_id, "target": nid, "relation": "imports_from"})

    for m in _CS_TYPE_RE.finditer(source):
        kind, name = m.group(1), m.group(2)
        # Line of the `class`/`interface` keyword, not of any attribute block the
        # pattern consumed above it — otherwise both the line number and the
        # visibility lookup land on the wrong line.
        idx = _lineno(source, m.start(1)) - 1
        vis = next((k for k in ("public", "private", "protected", "internal")
                    if re.search(rf"\b{k}\b", lines[idx])), "") if idx < len(lines) else ""
        ntype = {"class": "class", "interface": "interface", "enum": "enum",
                 "struct": "struct", "record": "class"}[kind]
        # Attributes may sit on the declaration's own prefix or on the lines above.
        inline_attrs = [f"[{a}]" for a in _CS_ATTR_RE.findall(source[m.start():m.start(1)])]
        # The same attribute can be seen inline and on the line above; keep one.
        attributes = list(dict.fromkeys(inline_attrs + _attrs_above(idx)))
        type_nid = _add(name, ntype, idx + 1, namespace_nid,
                        attributes=attributes, visibility=vis,
                        is_interface=(ntype == "interface" or bool(re.match(r"^I[A-Z]", name))))

        header = source[m.start():source.find("{", m.start()) + 1]
        if (bases := re.search(r":\s*([\w.,<>\s]+?)\{", header)) is not None:
            for base in (b.strip() for b in bases.group(1).split(",")):
                if base:
                    relation = "implements" if re.match(r"^I[A-Z]", base) else "extends"
                    edges.append({"source": type_nid, "target": base, "relation": relation})

        span = _class_body_span(source, m.start())
        if span is None:
            continue
        body_start, body_end = span
        body_line = _lineno(source, body_start)
        depth = 0
        for offset, raw in enumerate(source[body_start:body_end].splitlines()):
            line = raw.strip()
            if depth == 0 and line and not line.startswith(("//", "/*", "*", "[")):
                abs_idx = body_line + offset - 1
                if (pm := _CS_PROPERTY_RE.match(line)) is not None:
                    _add(pm.group(1), "property", abs_idx + 1, type_nid,
                         attributes=_attrs_above(abs_idx),
                         label_qualified=f"{name}.{pm.group(1)}")
                elif (mm := _CS_METHOD_RE.match(line)) is not None:
                    member = mm.group(2)
                    if member not in _CS_NOT_MEMBERS:
                        ntype = "constructor" if member == name else "method"
                        member_vis = next(
                            (k for k in ("public", "private", "protected", "internal")
                             if re.search(rf"\b{k}\b", line)), "")
                        _add(member, ntype, abs_idx + 1, type_nid,
                             attributes=_attrs_above(abs_idx),
                             visibility=member_vis,
                             is_async=bool(mm.group(1)))
            depth += raw.count("{") - raw.count("}")
            if depth < 0:
                break

    return nodes, edges


# ---------------------------------------------------------------------------
# Ruby
# ---------------------------------------------------------------------------

_RUBY_MIXINS = ("include", "extend", "prepend")
_RUBY_ATTRS = ("attr_accessor", "attr_reader", "attr_writer")


class _RubyVisitor(_BaseVisitor):
    """tree-sitter Ruby visitor.

    Ruby's structure lives in method calls rather than keywords: `include`,
    `attr_accessor` and `method_missing` say more about a class than its
    declaration does, so each is given a first-class representation.
    """

    def _visit_scope(self, node, ntype: str) -> None:
        name = self._ts_name(node)
        if not name:
            self._recurse(node)
            return
        nid = self._emit(name, ntype, node.start_point[0] + 1)
        for child in node.children:
            if child.type == "superclass":
                for sub in child.children:
                    if sub.type == "constant":
                        self._edge(nid, self._text(sub), "extends")
        self._descend(node, nid)

    def visit_module(self, node) -> None:
        self._visit_scope(node, "module")

    def visit_class(self, node) -> None:
        self._visit_scope(node, "class")

    def visit_singleton_class(self, node) -> None:
        self._recurse(node)

    def visit_method(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            return
        ntype = "dynamic" if name == "method_missing" else "method"
        self._emit(name, ntype, node.start_point[0] + 1, is_class_method=False)

    def visit_singleton_method(self, node) -> None:
        name = self._ts_name(node)
        if name:
            self._emit(name, "method", node.start_point[0] + 1, is_class_method=True)

    def visit_assignment(self, node) -> None:
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or left.type != "constant":
            return
        name = self._text(left)
        right_text = self._text(right)
        ntype = "proc" if re.match(r"^(Proc\.new|lambda|->)", right_text.strip()) else "constant"
        self._emit(name, ntype, node.start_point[0] + 1)

    def visit_call(self, node) -> None:
        method = self._text(node.child_by_field_name("method"))
        args_node = node.child_by_field_name("arguments")
        if method in _RUBY_MIXINS and args_node is not None:
            for arg in args_node.children:
                if arg.type == "constant":
                    self._edge(self._parent, self._text(arg), "mixin")
            return
        if method in _RUBY_ATTRS and args_node is not None:
            for arg in args_node.children:
                if arg.type == "simple_symbol":
                    attr = self._text(arg).lstrip(":")
                    self._emit(attr, "accessor", node.start_point[0] + 1,
                               accessor_kind=method)
            return
        self._recurse(node)


_RUBY_SCOPE_RE = re.compile(r"(?m)^[ \t]*(module|class)\s+([\w:]+)(?:\s*<\s*([\w:]+))?")
_RUBY_METHOD_RE = re.compile(r"(?m)^[ \t]*def\s+(self\.)?([\w?!=\[\]<>+\-*/]+)")
_RUBY_MIXIN_RE = re.compile(r"(?m)^[ \t]*(include|extend|prepend)\s+([\w:]+)")
_RUBY_ATTR_RE = re.compile(r"(?m)^[ \t]*(attr_accessor|attr_reader|attr_writer)\s+(.+)$")
_RUBY_CONST_RE = re.compile(r"(?m)^[ \t]*([A-Z][A-Z0-9_]*)\s*=\s*(.+)$")


def _index_ruby_regex(source: str, rel_path: Path, file_id: str) -> tuple[list[dict], list[dict]]:
    """Regex fallback for Ruby.

    Ruby has no braces to match, so nesting is inferred from indentation: a
    def indented deeper than the enclosing class belongs to it.
    """
    nodes: list[dict] = []
    edges: list[dict] = []

    def _add(name: str, ntype: str, lineno: int, parent: str, **extra) -> str:
        nid = f"{parent}.{name}"
        node = {
            "id": nid, "label": name, "type": ntype, "description": "",
            "source_file": str(rel_path), "source_location": f"L{lineno}",
            "file_type": "code",
        }
        node.update({k: v for k, v in extra.items() if v not in (None, "", [])})
        nodes.append(node)
        edges.append({"source": parent, "target": nid, "relation": "contains"})
        return nid

    # (indent, nid) stack of open module/class scopes.
    scopes: list[tuple[int, str]] = [(-1, file_id)]

    for lineno, raw in enumerate(source.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        while len(scopes) > 1 and indent <= scopes[-1][0]:
            scopes.pop()
        parent = scopes[-1][1]

        if (m := _RUBY_SCOPE_RE.match(raw)) is not None:
            kind, name, superclass = m.group(1), m.group(2), m.group(3)
            nid = _add(name, "module" if kind == "module" else "class", lineno, parent)
            if superclass:
                edges.append({"source": nid, "target": superclass, "relation": "extends"})
            scopes.append((indent, nid))
            continue

        if (m := _RUBY_METHOD_RE.match(raw)) is not None:
            is_class_method = bool(m.group(1))
            name = m.group(2)
            ntype = "dynamic" if name == "method_missing" else "method"
            _add(name, ntype, lineno, parent, is_class_method=is_class_method)
            continue

        if (m := _RUBY_MIXIN_RE.match(raw)) is not None:
            edges.append({"source": parent, "target": m.group(2), "relation": "mixin"})
            continue

        if (m := _RUBY_ATTR_RE.match(raw)) is not None:
            kind = m.group(1)
            for attr in re.findall(r":(\w+)", m.group(2)):
                _add(attr, "accessor", lineno, parent, accessor_kind=kind)
            continue

        if (m := _RUBY_CONST_RE.match(raw)) is not None:
            value = m.group(2).strip()
            ntype = "proc" if re.match(r"^(Proc\.new|lambda|->)", value) else "constant"
            _add(m.group(1), ntype, lineno, parent)

    return nodes, edges


# ---------------------------------------------------------------------------
# Entry points for the new languages
# ---------------------------------------------------------------------------



def _tree_is_broken(tree) -> bool:
    """True if tree-sitter could not parse the file cleanly.

    DECISION: a grammar that returns a partly-ERROR tree silently drops whole
    declarations — tree-sitter-kotlin 1.1.0 loses everything after a class with
    a body followed by an `object`. Regex extracts less per declaration but does
    not lose 80% of the file, so a broken tree is treated as a parser failure.
    """
    root = getattr(tree, "root_node", None)
    return bool(root is not None and getattr(root, "has_error", False))


def _index_with_grammar(
    path: Path, root: Path | None, grammar, available: bool,
    visitor_cls, regex_fn, language_label: str,
    language_attr: str = "language",
) -> tuple[list[dict], list[dict]]:
    """Shared body for the tree-sitter-or-regex language entry points.

    Args:
        language_attr: Name of the grammar module's language accessor. Most
            expose `language()`; tree-sitter-php exposes `language_php()`.
    """
    rel = _rel(path, root) if root else path
    fid = _file_id(rel)
    module_node = {
        "id": fid, "label": rel.stem, "type": "module",
        "description": f"{language_label} file {rel.name}", "source_file": str(rel),
        "source_location": "L1", "file_type": "code",
    }
    try:
        source_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [module_node], []

    if available:
        try:
            tree = _TSParser(_TSLanguage(getattr(grammar, language_attr)())).parse(
                source_text.encode("utf-8"))
            if _tree_is_broken(tree):
                raise ValueError("grammar produced a tree containing ERROR nodes")
            visitor = visitor_cls(rel, fid)
            visitor.visit(tree.root_node)
            return [module_node] + visitor.nodes, visitor.edges
        except Exception as exc:
            import click  # noqa: PLC0415

            click.echo(f"  Warning: tree-sitter failed on {path}, "
                       f"falling back to regex: {exc}", err=True)

    nodes, edges = regex_fn(source_text, rel, fid)
    return [module_node] + nodes, edges


def index_rust(path: Path, root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Index a Rust file (tree-sitter with the 'rust' extra, else regex)."""
    return _index_with_grammar(path, root, _tsrust, _RUST_TS_AVAILABLE,
                               _RustVisitor, _index_rust_regex, "Rust")


def index_csharp(path: Path, root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Index a C# file (tree-sitter with the 'csharp' extra, else regex)."""
    return _index_with_grammar(path, root, _tscsharp, _CSHARP_TS_AVAILABLE,
                               _CSharpVisitor, _index_csharp_regex, "C#")


def index_ruby(path: Path, root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Index a Ruby file (tree-sitter with the 'ruby' extra, else regex)."""
    return _index_with_grammar(path, root, _tsruby, _RUBY_TS_AVAILABLE,
                               _RubyVisitor, _index_ruby_regex, "Ruby")


# ---------------------------------------------------------------------------
# PHP
# ---------------------------------------------------------------------------

_PHP_MAGIC = frozenset({
    "__construct", "__destruct", "__get", "__set", "__isset", "__unset",
    "__call", "__callStatic", "__invoke", "__toString", "__clone",
    "__sleep", "__wakeup", "__serialize", "__unserialize", "__set_state",
    "__debugInfo",
})
_PHP_VISIBILITY = ("public", "private", "protected")


class _PhpVisitor(_BaseVisitor):
    """tree-sitter PHP visitor.

    Records modern `#[Attribute]` syntax, visibility, trait `use` as mixin
    edges, and separates magic methods from ordinary ones — `__get`/`__call`
    change how a class behaves, so they are worth finding on their own.
    """

    _NAME_FIELDS = ("name", "identifier")

    @staticmethod
    def _attributes(node) -> list[str]:
        found: list[str] = []
        for child in node.children:
            if child.type == "attribute_list":
                found.extend(
                    f"#[{a}]" for a in re.findall(
                        r"#\[\s*([\w\\]+)", child.text.decode("utf-8", "replace"))
                )
        return found

    @staticmethod
    def _visibility(node) -> str:
        text = node.text.decode("utf-8", "replace")
        for kw in _PHP_VISIBILITY:
            if re.search(rf"\b{kw}\b", text.split("{")[0]):
                return kw
        return ""

    def visit_namespace_definition(self, node) -> None:
        name = ""
        for child in node.children:
            if child.type == "namespace_name":
                name = self._text(child)
        if name:
            self._emit(name, "namespace", node.start_point[0] + 1)

    def visit_namespace_use_declaration(self, node) -> None:
        module = self._text(node).removeprefix("use").strip().rstrip(";").strip()
        if not module:
            return
        safe = module.replace("\\", "_").split(",")[0].strip()
        nid = f"{self._parent}.import_{safe}"
        self.nodes.append({
            "id": nid, "label": module, "type": "import", "description": "",
            "source_file": str(self.rel_path),
            "source_location": f"L{node.start_point[0] + 1}",
            "file_type": "code",
        })
        self._edge(self._parent, nid, "imports_from")

    def _visit_type(self, node, ntype: str) -> None:
        name = self._ts_name(node)
        if not name:
            self._recurse(node)
            return
        nid = self._emit(
            name, ntype, node.start_point[0] + 1,
            attributes=self._attributes(node), visibility=self._visibility(node),
        )
        for child in node.children:
            if child.type == "base_clause":
                for sub in child.children:
                    if sub.type in ("name", "qualified_name"):
                        self._edge(nid, self._text(sub), "extends")
            elif child.type == "class_interface_clause":
                for sub in child.children:
                    if sub.type in ("name", "qualified_name"):
                        self._edge(nid, self._text(sub), "implements")
        self._descend(node, nid)

    def visit_class_declaration(self, node) -> None:
        self._visit_type(node, "class")

    def visit_interface_declaration(self, node) -> None:
        self._visit_type(node, "interface")

    def visit_trait_declaration(self, node) -> None:
        self._visit_type(node, "trait")

    def visit_enum_declaration(self, node) -> None:
        self._visit_type(node, "enum")

    def visit_use_declaration(self, node) -> None:
        """`use TraitName;` inside a class body — PHP's mixin mechanism."""
        for child in node.children:
            if child.type in ("name", "qualified_name"):
                self._edge(self._parent, self._text(child), "mixin")

    def visit_method_declaration(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            return
        if name == "__construct":
            ntype = "constructor"
        elif name in _PHP_MAGIC:
            ntype = "magic"
        else:
            ntype = "method"
        self._emit(
            name, ntype, node.start_point[0] + 1,
            attributes=self._attributes(node), visibility=self._visibility(node),
        )

    def visit_function_definition(self, node) -> None:
        name = self._ts_name(node)
        if name:
            self._emit(name, "function", node.start_point[0] + 1,
                       attributes=self._attributes(node))

    def visit_property_declaration(self, node) -> None:
        for var in re.findall(r"\$(\w+)", self._text(node)):
            self._emit(var, "property", node.start_point[0] + 1,
                       attributes=self._attributes(node),
                       visibility=self._visibility(node))
            break  # one node per declaration, matching the first variable


_PHP_NS_RE = re.compile(r"(?m)^\s*namespace\s+([\w\\]+)\s*;")
_PHP_USE_RE = re.compile(r"(?m)^\s*use\s+([\w\\]+)\s*;")
_PHP_TYPE_RE = re.compile(
    r"(?m)^[ \t]*(?:(?:final|abstract|readonly)\s+)*(class|interface|trait|enum)\s+(\w+)")
_PHP_MEMBER_RE = re.compile(
    r"^(?:(?:public|private|protected|static|final|abstract|readonly)\s+)*"
    r"function\s+&?\s*(\w+)\s*\(")
_PHP_PROP_RE = re.compile(
    r"^(?:(?:public|private|protected|static|readonly)\s+)+[\w|?\\]*\s*\$(\w+)")
_PHP_TRAIT_USE_RE = re.compile(r"^\s*use\s+([\w\\, ]+);")
_PHP_ATTR_RE = re.compile(r"#\[\s*([\w\\]+)")


def _index_php_regex(source: str, rel_path: Path, file_id: str) -> tuple[list[dict], list[dict]]:
    """Regex fallback for PHP."""
    nodes: list[dict] = []
    edges: list[dict] = []
    lines = source.splitlines()

    def _add(name: str, ntype: str, lineno: int, parent: str, **extra) -> str:
        nid = f"{parent}.{name}"
        node = {
            "id": nid, "label": name, "type": ntype, "description": "",
            "source_file": str(rel_path), "source_location": f"L{lineno}",
            "file_type": "code",
        }
        node.update({k: v for k, v in extra.items() if v not in (None, "", [])})
        nodes.append(node)
        edges.append({"source": parent, "target": nid, "relation": "contains"})
        return nid

    def _attrs_above(idx: int) -> list[str]:
        found: list[str] = []
        i = idx - 1
        while i >= 0 and (stripped := lines[i].strip()).startswith("#["):
            found = [f"#[{a}]" for a in _PHP_ATTR_RE.findall(stripped)] + found
            i -= 1
        return found

    def _vis(line: str) -> str:
        return next((k for k in _PHP_VISIBILITY if re.search(rf"\b{k}\b", line)), "")

    parent_scope = file_id
    if (ns := _PHP_NS_RE.search(source)) is not None:
        parent_scope = _add(ns.group(1), "namespace", _lineno(source, ns.start()), file_id)

    for m in _PHP_USE_RE.finditer(source):
        module = m.group(1)
        # Class-body trait `use` is handled below; top-level use is an import.
        if _class_body_contains(source, m.start()):
            continue
        nid = f"{file_id}.import_{module.replace(chr(92), '_')}"
        nodes.append({
            "id": nid, "label": module, "type": "import", "description": "",
            "source_file": str(rel_path),
            "source_location": f"L{_lineno(source, m.start())}",
            "file_type": "code",
        })
        edges.append({"source": file_id, "target": nid, "relation": "imports_from"})

    for m in _PHP_TYPE_RE.finditer(source):
        kind, name = m.group(1), m.group(2)
        idx = _lineno(source, m.start()) - 1
        type_nid = _add(name, kind, idx + 1, parent_scope,
                        attributes=_attrs_above(idx))

        header = source[m.start():source.find("{", m.start()) + 1]
        if (ext := re.search(r"\bextends\s+([\w\\]+)", header)):
            edges.append({"source": type_nid, "target": ext.group(1), "relation": "extends"})
        if (impl := re.search(r"\bimplements\s+([\w\\,\s]+?)\{", header)):
            for iface in (i.strip() for i in impl.group(1).split(",")):
                if iface:
                    edges.append({"source": type_nid, "target": iface, "relation": "implements"})

        span = _class_body_span(source, m.start())
        if span is None:
            continue
        body_start, body_end = span
        body_line = _lineno(source, body_start)
        depth = 0
        for offset, raw in enumerate(source[body_start:body_end].splitlines()):
            line = raw.strip()
            if depth == 0 and line and not line.startswith(("//", "/*", "*", "#[")):
                abs_idx = body_line + offset - 1
                if (tm := _PHP_TRAIT_USE_RE.match(line)) is not None:
                    for trait in (t.strip() for t in tm.group(1).split(",")):
                        if trait:
                            edges.append({"source": type_nid, "target": trait,
                                          "relation": "mixin"})
                elif (mm := _PHP_MEMBER_RE.match(line)) is not None:
                    member = mm.group(1)
                    if member == "__construct":
                        ntype = "constructor"
                    elif member in _PHP_MAGIC:
                        ntype = "magic"
                    else:
                        ntype = "method"
                    _add(member, ntype, abs_idx + 1, type_nid,
                         attributes=_attrs_above(abs_idx), visibility=_vis(line))
                elif (pm := _PHP_PROP_RE.match(line)) is not None:
                    _add(pm.group(1), "property", abs_idx + 1, type_nid,
                         visibility=_vis(line))
            depth += raw.count("{") - raw.count("}")
            if depth < 0:
                break

    for m in re.finditer(r"(?m)^[ \t]*function\s+&?\s*(\w+)\s*\(", source):
        if not _class_body_contains(source, m.start()):
            _add(m.group(1), "function", _lineno(source, m.start()), file_id)

    return nodes, edges


def _class_body_contains(source: str, pos: int) -> bool:
    """True if *pos* falls inside any brace-delimited body in the source."""
    depth = 0
    for i, ch in enumerate(source):
        if i >= pos:
            return depth > 0
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    return False


# ---------------------------------------------------------------------------
# Kotlin
# ---------------------------------------------------------------------------


class _KotlinVisitor(_BaseVisitor):
    """tree-sitter Kotlin visitor.

    Kotlin's meaning lives in its modifiers: `data`, `sealed`, `suspend` and
    `object` each change what a declaration is, so each becomes a field or a
    distinct node type rather than being flattened into "class"/"function".
    """

    @staticmethod
    def _modifier_set(node) -> set[str]:
        for child in node.children:
            if child.type == "modifiers":
                return set(re.findall(r"\w+", child.text.decode("utf-8", "replace")))
        return set()

    def visit_package_header(self, node) -> None:
        name = self._text(node).removeprefix("package").strip()
        if name:
            self._emit(name, "package", node.start_point[0] + 1)

    def visit_class_declaration(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            self._recurse(node)
            return
        mods = self._modifier_set(node)
        ntype = "interface" if "interface" in mods else "class"
        nid = self._emit(
            name, ntype, node.start_point[0] + 1,
            is_data_class=("data" in mods) or None,
            is_sealed=("sealed" in mods) or None,
        )
        for child in node.children:
            if child.type in ("delegation_specifier", "supertype_list"):
                for tname in re.findall(r"\b([A-Z]\w*)", self._text(child)):
                    self._edge(nid, tname, "extends")
        self._descend(node, nid)

    def visit_object_declaration(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            self._recurse(node)
            return
        nid = self._emit(name, "object", node.start_point[0] + 1)
        self._descend(node, nid)

    def visit_companion_object(self, node) -> None:
        """Companion objects nest under the owning class, which is where a
        reader looks for them — `Repo.Companion.create`, not a free symbol."""
        name = self._ts_name(node) or "Companion"
        nid = self._emit(name, "object", node.start_point[0] + 1, is_companion=True)
        self._descend(node, nid)

    def visit_function_declaration(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            return
        mods = self._modifier_set(node)
        extends_type = ""
        # `fun String.slugify()` — a user_type before the name marks an extension.
        for child in node.children:
            if child.type == "user_type":
                extends_type = self._text(child)
                break
            if child.type == "identifier":
                break
        self._emit(
            name, "function", node.start_point[0] + 1,
            is_suspend=("suspend" in mods) or None,
            extends_type=extends_type,
        )


_KT_PACKAGE_RE = re.compile(r"(?m)^\s*package\s+([\w.]+)")
_KT_TYPE_RE = re.compile(
    r"(?m)^[ \t]*((?:(?:public|private|internal|protected|open|abstract|sealed|data|inner|enum|annotation|value)\s+)*)"
    r"(class|interface|object)\s+(\w+)")
_KT_COMPANION_RE = re.compile(r"(?m)^[ \t]*companion\s+object(?:\s+(\w+))?")
_KT_FUN_RE = re.compile(
    r"(?m)^[ \t]*((?:(?:public|private|internal|protected|open|override|suspend|inline|operator|tailrec)\s+)*)"
    r"fun\s+(?:<[^>]*>\s*)?(?:([\w.]+)\.)?(\w+)\s*\(")


def _index_kotlin_regex(source: str, rel_path: Path, file_id: str) -> tuple[list[dict], list[dict]]:
    """Regex fallback for Kotlin."""
    nodes: list[dict] = []
    edges: list[dict] = []

    def _add(name: str, ntype: str, lineno: int, parent: str = file_id, **extra) -> str:
        nid = f"{parent}.{name}"
        node = {
            "id": nid, "label": name, "type": ntype, "description": "",
            "source_file": str(rel_path), "source_location": f"L{lineno}",
            "file_type": "code",
        }
        node.update({k: v for k, v in extra.items() if v not in (None, "", [])})
        nodes.append(node)
        edges.append({"source": parent, "target": nid, "relation": "contains"})
        return nid

    if (pkg := _KT_PACKAGE_RE.search(source)) is not None:
        _add(pkg.group(1), "package", _lineno(source, pkg.start()))

    type_spans: list[tuple[int, int, str]] = []
    matches = list(_KT_TYPE_RE.finditer(source))
    for i, m in enumerate(matches):
        limit = matches[i + 1].start() if i + 1 < len(matches) else None
        mods, kind, name = m.group(1) or "", m.group(2), m.group(3)
        ntype = {"class": "class", "interface": "interface", "object": "object"}[kind]
        nid = _add(name, ntype, _lineno(source, m.start()),
                   is_data_class=("data" in mods) or None,
                   is_sealed=("sealed" in mods) or None)
        # Supertypes come after the primary constructor, so skip its parens —
        # otherwise `data class User(val id: Int)` reads `Int` as a supertype.
        brace = source.find("{", m.start())
        header = source[m.start():brace if brace != -1 else len(source)]
        line_end = header.find("\n")
        header = header[:line_end] if line_end != -1 else header
        if (paren := header.rfind(")")) != -1:
            header = header[paren + 1:]
        else:
            header = header[len(m.group(0)):]
        if ":" in header:
            for tname in re.findall(r"\b([A-Z]\w*)", header.split(":", 1)[1]):
                edges.append({"source": nid, "target": tname, "relation": "extends"})
        span = _class_body_span(source, m.start(), limit)
        if span:
            type_spans.append((span[0], span[1], nid))

    def _owner(pos: int) -> str:
        for start, end, nid in type_spans:
            if start <= pos < end:
                return nid
        return file_id

    for m in _KT_COMPANION_RE.finditer(source):
        _add(m.group(1) or "Companion", "object", _lineno(source, m.start()),
             parent=_owner(m.start()), is_companion=True)

    for m in _KT_FUN_RE.finditer(source):
        mods, receiver, name = m.group(1) or "", m.group(2), m.group(3)
        _add(name, "function", _lineno(source, m.start()), parent=_owner(m.start()),
             is_suspend=("suspend" in mods) or None, extends_type=receiver or "")

    return nodes, edges


# ---------------------------------------------------------------------------
# Scala
# ---------------------------------------------------------------------------


class _ScalaVisitor(_BaseVisitor):
    _types_seen: set[str]

    """tree-sitter Scala visitor.

    `case` and `implicit` are unnamed tokens in this grammar, so they are read
    off the declaration's own children. `def`/`val`/`var` are preserved as
    distinct node types because the distinction is semantic in Scala.
    """

    def __init__(self, rel_path: Path, file_id: str) -> None:
        super().__init__(rel_path, file_id)
        self._types_seen = set()

    @staticmethod
    def _has_token(node, token: str) -> bool:
        """True if *token* is a leading keyword of this declaration.

        Checks direct unnamed children first, then the declaration's own text
        prefix — `implicit` surfaces one way on some node shapes and the other
        way on the rest.
        """
        if any(not c.is_named and c.type == token for c in node.children):
            return True
        head = node.text.decode("utf-8", "replace").split("(")[0]
        return bool(re.match(rf"^\s*(?:\w+\s+)*?{token}\b", head))

    def visit_package_clause(self, node) -> None:
        name = self._text(node).removeprefix("package").strip()
        if name:
            self._emit(name, "package", node.start_point[0] + 1)

    def _visit_template(self, node, ntype: str) -> None:
        name = self._ts_name(node)
        if not name:
            self._recurse(node)
            return
        nid = self._emit(
            name, ntype, node.start_point[0] + 1,
            is_case=self._has_token(node, "case") or None,
        )
        for child in node.children:
            if child.type == "extends_clause":
                names = [c for c in child.children if c.type == "type_identifier"]
                for i, tname in enumerate(names):
                    # First is `extends`, the rest come from `with`.
                    self._edge(nid, self._text(tname), "extends" if i == 0 else "with")
        self._descend(node, nid)

    def visit_class_definition(self, node) -> None:
        name = self._ts_name(node)
        if name:
            self._types_seen.add(f"{self._parent}.{name}")
        self._visit_template(node, "class")

    def visit_object_definition(self, node) -> None:
        """A companion object shares its class's name; nest it under the class
        so the two do not collide on the same node id."""
        name = self._ts_name(node)
        if name and f"{self._parent}.{name}" in self._types_seen:
            nid = self._emit("Companion", "object", node.start_point[0] + 1,
                             is_companion=True)
            self._descend(node, nid)
            return
        self._visit_template(node, "object")

    def visit_trait_definition(self, node) -> None:
        self._visit_template(node, "trait")

    def _visit_def(self, node) -> None:
        name = self._ts_name(node)
        if name:
            self._emit(name, "def", node.start_point[0] + 1,
                       is_implicit=self._has_token(node, "implicit") or None)

    visit_function_definition = _visit_def
    visit_function_declaration = _visit_def

    def visit_val_definition(self, node) -> None:
        name = self._ts_name(node)
        if name:
            self._emit(name, "val", node.start_point[0] + 1,
                       is_implicit=self._has_token(node, "implicit") or None)

    def visit_var_definition(self, node) -> None:
        name = self._ts_name(node)
        if name:
            self._emit(name, "var", node.start_point[0] + 1)


_SCALA_PACKAGE_RE = re.compile(r"(?m)^\s*package\s+([\w.]+)")
_SCALA_TYPE_RE = re.compile(
    r"(?m)^[ \t]*((?:(?:final|sealed|abstract|implicit|private|protected|case)\s+)*)"
    r"(class|object|trait)\s+(\w+)")
_SCALA_DEF_RE = re.compile(
    r"(?m)^[ \t]*((?:(?:private|protected|override|final|implicit|lazy)\s+)*)"
    r"(def|val|var)\s+(\w+)")


def _index_scala_regex(source: str, rel_path: Path, file_id: str) -> tuple[list[dict], list[dict]]:
    """Regex fallback for Scala."""
    nodes: list[dict] = []
    edges: list[dict] = []

    def _add(name: str, ntype: str, lineno: int, parent: str = file_id, **extra) -> str:
        nid = f"{parent}.{name}"
        node = {
            "id": nid, "label": name, "type": ntype, "description": "",
            "source_file": str(rel_path), "source_location": f"L{lineno}",
            "file_type": "code",
        }
        node.update({k: v for k, v in extra.items() if v not in (None, "", [])})
        nodes.append(node)
        edges.append({"source": parent, "target": nid, "relation": "contains"})
        return nid

    if (pkg := _SCALA_PACKAGE_RE.search(source)) is not None:
        _add(pkg.group(1), "package", _lineno(source, pkg.start()))

    type_spans: list[tuple[int, int, str]] = []
    matches = list(_SCALA_TYPE_RE.finditer(source))
    for i, m in enumerate(matches):
        limit = matches[i + 1].start() if i + 1 < len(matches) else None
        mods, kind, name = m.group(1) or "", m.group(2), m.group(3)
        nid = _add(name, kind, _lineno(source, m.start()),
                   is_case=("case" in mods) or None)
        header = source[m.start():source.find("{", m.start()) + 1 or len(source)]
        if (ext := re.search(r"\bextends\s+([\w.]+)", header)):
            edges.append({"source": nid, "target": ext.group(1), "relation": "extends"})
        for mixin in re.findall(r"\bwith\s+([\w.]+)", header):
            edges.append({"source": nid, "target": mixin, "relation": "with"})
        span = _class_body_span(source, m.start(), limit)
        if span:
            type_spans.append((span[0], span[1], nid))

    def _owner(pos: int) -> str:
        for start, end, nid in type_spans:
            if start <= pos < end:
                return nid
        return file_id

    for m in _SCALA_DEF_RE.finditer(source):
        mods, kind, name = m.group(1) or "", m.group(2), m.group(3)
        _add(name, kind, _lineno(source, m.start()), parent=_owner(m.start()),
             is_implicit=("implicit" in mods) or None)

    return nodes, edges


# ---------------------------------------------------------------------------
# Swift
# ---------------------------------------------------------------------------

_SWIFT_KINDS = ("struct", "class", "extension", "enum", "actor")


class _SwiftVisitor(_BaseVisitor):
    """tree-sitter Swift visitor.

    This grammar collapses struct/class/extension/enum into `class_declaration`,
    so the real kind is read from the leading keyword token. Protocol conformance
    becomes `conforms_to`, and an `extension` gets an `extends` edge to the type
    it augments.
    """

    @staticmethod
    def _kind(node) -> str:
        for child in node.children:
            if not child.is_named and child.type in _SWIFT_KINDS:
                return child.type
        return "class"

    @staticmethod
    def _attributes(text: str) -> list[str]:
        head = text.split("{")[0]
        return [f"@{a}" for a in re.findall(r"@(\w+)", head)]

    def visit_class_declaration(self, node) -> None:
        kind = self._kind(node)
        name = self._ts_name(node)
        if not name:
            # `extension Point` exposes the type via user_type, not type_identifier.
            for child in node.children:
                if child.type == "user_type":
                    name = self._text(child)
                    break
        if not name:
            self._recurse(node)
            return

        ntype = "extension" if kind == "extension" else (
            "enum" if kind == "enum" else ("struct" if kind == "struct" else "class"))
        nid = self._emit(name, ntype, node.start_point[0] + 1,
                         attributes=self._attributes(self._text(node)))
        if kind == "extension":
            self._edge(nid, name, "extends")
        for child in node.children:
            if child.type == "inheritance_specifier":
                self._edge(nid, self._text(child), "conforms_to")
        self._descend(node, nid)

    def visit_protocol_declaration(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            self._recurse(node)
            return
        nid = self._emit(name, "protocol", node.start_point[0] + 1)
        self._descend(node, nid)

    def _visit_fn(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            return
        text = self._text(node)
        head = text.split("{")[0]
        self._emit(
            name, "function", node.start_point[0] + 1,
            attributes=self._attributes(text),
            is_async=bool(re.search(r"\basync\b", head)) or None,
        )

    visit_function_declaration = _visit_fn
    visit_protocol_function_declaration = _visit_fn

    def visit_property_declaration(self, node) -> None:
        text = self._text(node)
        match = re.search(r"\b(?:var|let)\s+(\w+)", text)
        if not match:
            return
        # `var area: Int { return count * 2 }` — a body makes it computed.
        is_computed = bool(re.search(r":[^=]*\{", text))
        self._emit(
            match.group(1), "property", node.start_point[0] + 1,
            attributes=self._attributes(text),
            is_computed=is_computed or None,
        )

    def visit_import_declaration(self, node) -> None:
        module = self._text(node).removeprefix("import").strip()
        if not module:
            return
        nid = f"{self._parent}.import_{module.replace('.', '_')}"
        self.nodes.append({
            "id": nid, "label": module, "type": "import", "description": "",
            "source_file": str(self.rel_path),
            "source_location": f"L{node.start_point[0] + 1}",
            "file_type": "code",
        })
        self._edge(self._parent, nid, "imports_from")


_SWIFT_IMPORT_RE = re.compile(r"(?m)^\s*import\s+([\w.]+)")
_SWIFT_TYPE_RE = re.compile(
    r"(?m)^[ \t]*((?:(?:public|private|internal|fileprivate|open|final|@\w+)\s+)*)"
    r"(struct|class|extension|enum|protocol|actor)\s+(\w+)")
_SWIFT_FUNC_RE = re.compile(
    r"(?m)^[ \t]*((?:(?:public|private|internal|fileprivate|open|final|static|class|override|mutating|@\w+)\s+)*)"
    r"func\s+(\w+)\s*\(([^\n]*?)\)([^\n{]*)")
_SWIFT_PROP_RE = re.compile(
    r"(?m)^[ \t]*((?:(?:public|private|internal|fileprivate|open|static|lazy|@\w+)\s+)*)"
    r"(?:var|let)\s+(\w+)\s*:([^\n]*)")


def _index_swift_regex(source: str, rel_path: Path, file_id: str) -> tuple[list[dict], list[dict]]:
    """Regex fallback for Swift."""
    nodes: list[dict] = []
    edges: list[dict] = []

    def _add(name: str, ntype: str, lineno: int, parent: str = file_id, **extra) -> str:
        nid = f"{parent}.{name}"
        node = {
            "id": nid, "label": name, "type": ntype, "description": "",
            "source_file": str(rel_path), "source_location": f"L{lineno}",
            "file_type": "code",
        }
        node.update({k: v for k, v in extra.items() if v not in (None, "", [])})
        nodes.append(node)
        edges.append({"source": parent, "target": nid, "relation": "contains"})
        return nid

    for m in _SWIFT_IMPORT_RE.finditer(source):
        module = m.group(1)
        nid = f"{file_id}.import_{module.replace('.', '_')}"
        nodes.append({
            "id": nid, "label": module, "type": "import", "description": "",
            "source_file": str(rel_path),
            "source_location": f"L{_lineno(source, m.start())}",
            "file_type": "code",
        })
        edges.append({"source": file_id, "target": nid, "relation": "imports_from"})

    type_spans: list[tuple[int, int, str]] = []
    matches = list(_SWIFT_TYPE_RE.finditer(source))
    for i, m in enumerate(matches):
        limit = matches[i + 1].start() if i + 1 < len(matches) else None
        mods, kind, name = m.group(1) or "", m.group(2), m.group(3)
        ntype = {"struct": "struct", "class": "class", "extension": "extension",
                 "enum": "enum", "protocol": "protocol", "actor": "class"}[kind]
        nid = _add(name, ntype, _lineno(source, m.start()),
                   attributes=[f"@{a}" for a in re.findall(r"@(\w+)", mods)])
        if kind == "extension":
            edges.append({"source": nid, "target": name, "relation": "extends"})
        header = source[m.start():source.find("{", m.start()) + 1 or len(source)]
        if ":" in header:
            for proto in re.findall(r"\b([A-Z]\w*)", header.split(":", 1)[1].split("{")[0]):
                edges.append({"source": nid, "target": proto, "relation": "conforms_to"})
        span = _class_body_span(source, m.start(), limit)
        if span:
            type_spans.append((span[0], span[1], nid))

    def _owner(pos: int) -> str:
        best = file_id
        for start, end, nid in type_spans:
            if start <= pos < end:
                best = nid
        return best

    for m in _SWIFT_FUNC_RE.finditer(source):
        mods, name, _params, tail = m.group(1) or "", m.group(2), m.group(3), m.group(4) or ""
        _add(name, "function", _lineno(source, m.start()), parent=_owner(m.start()),
             attributes=[f"@{a}" for a in re.findall(r"@(\w+)", mods)],
             is_async=bool(re.search(r"\basync\b", tail)) or None)

    for m in _SWIFT_PROP_RE.finditer(source):
        mods, name, tail = m.group(1) or "", m.group(2), m.group(3) or ""
        _add(name, "property", _lineno(source, m.start()), parent=_owner(m.start()),
             attributes=[f"@{a}" for a in re.findall(r"@(\w+)", mods)],
             is_computed=("{" in tail) or None)

    return nodes, edges


def index_php(path: Path, root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Index a PHP file (tree-sitter with the 'php' extra, else regex)."""
    return _index_with_grammar(path, root, _tsphp, _PHP_TS_AVAILABLE,
                               _PhpVisitor, _index_php_regex, "PHP",
                               language_attr="language_php")


def index_kotlin(path: Path, root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Index a Kotlin file (tree-sitter with the 'kotlin' extra, else regex)."""
    return _index_with_grammar(path, root, _tskotlin, _KOTLIN_TS_AVAILABLE,
                               _KotlinVisitor, _index_kotlin_regex, "Kotlin")


def index_scala(path: Path, root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Index a Scala file (tree-sitter with the 'scala' extra, else regex)."""
    return _index_with_grammar(path, root, _tsscala, _SCALA_TS_AVAILABLE,
                               _ScalaVisitor, _index_scala_regex, "Scala")


def index_swift(path: Path, root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Index a Swift file (tree-sitter with the 'swift' extra, else regex)."""
    return _index_with_grammar(path, root, _tsswift, _SWIFT_TS_AVAILABLE,
                               _SwiftVisitor, _index_swift_regex, "Swift")


# ---------------------------------------------------------------------------
# C / C++
# ---------------------------------------------------------------------------

_HEADER_SUFFIXES = frozenset({".h", ".hpp", ".hh", ".hxx"})


def _is_header(rel_path: Path) -> bool:
    """True for a declaration file (.h/.hpp/.hh/.hxx) rather than an implementation."""
    return rel_path.suffix.lower() in _HEADER_SUFFIXES


class _CVisitor(_BaseVisitor):
    """tree-sitter C visitor.

    Every node carries `is_header` because in C the same symbol name means
    something different in a header (a promise) and in a .c (the body), and a
    reader looking for an implementation should not land on the prototype.
    """

    def __init__(self, rel_path: Path, file_id: str) -> None:
        super().__init__(rel_path, file_id)
        self.is_header = _is_header(rel_path)
        self._extern_c = False
        self._class_depth = 0

    def _emit_c(self, name: str, ntype: str, lineno: int, **extra) -> str:
        return self._emit(name, ntype, lineno, is_header=self.is_header or None,
                          is_extern_c=self._extern_c or None, **extra)

    @staticmethod
    def _declarator_name(node) -> str:
        """Dig through pointer/array/parenthesised declarators to the identifier."""
        stack = [node]
        while stack:
            current = stack.pop(0)
            if current.type in ("identifier", "field_identifier",
                                "qualified_identifier", "operator_name",
                                "destructor_name", "type_identifier"):
                return current.text.decode("utf-8", "replace")
            stack.extend(current.children)
        return ""

    def visit_preproc_include(self, node) -> None:
        target = ""
        for child in node.children:
            if child.type in ("system_lib_string", "string_literal"):
                target = self._text(child).strip('<>"')
        if not target:
            return
        safe = re.sub(r"\W", "_", target)
        nid = f"{self._parent}.import_{safe}"
        self.nodes.append({
            "id": nid, "label": target, "type": "import", "description": "",
            "source_file": str(self.rel_path),
            "source_location": f"L{node.start_point[0] + 1}",
            "file_type": "code",
        })
        self._edge(self._parent, nid, "imports_from")

    def visit_preproc_function_def(self, node) -> None:
        name = self._ts_name(node)
        if name:
            self._emit_c(name, "macro", node.start_point[0] + 1)

    def visit_preproc_def(self, node) -> None:
        name = self._ts_name(node)
        if name:
            self._emit_c(name, "macro", node.start_point[0] + 1)

    def visit_function_definition(self, node) -> None:
        declarator = node.child_by_field_name("declarator")
        name = self._declarator_name(declarator) if declarator else ""
        if name:
            self._emit_c(name, "function", node.start_point[0] + 1)

    def visit_declaration(self, node) -> None:
        """A prototype: a header's function, a class member, or an extern "C" entry.

        All three are `declaration` nodes carrying a function_declarator, and all
        three are symbols a reader searches for — a C++ constructor only ever
        appears this way inside a class body.
        """
        for child in node.children:
            if child.type == "function_declarator":
                name = self._declarator_name(child)
                if name:
                    ntype = "method" if self._class_depth else "function"
                    self._emit_c(name, ntype, node.start_point[0] + 1)
                return
        self._recurse(node)

    def visit_type_definition(self, node) -> None:
        name = ""
        for child in node.children:
            if child.type == "type_identifier":
                name = self._text(child)
        if name:
            self._emit_c(name, "typedef", node.start_point[0] + 1)

    def _visit_tagged(self, node, ntype: str) -> None:
        name = ""
        for child in node.children:
            if child.type == "type_identifier":
                name = self._text(child)
                break
        if name:
            nid = self._emit_c(name, ntype, node.start_point[0] + 1)
            if ntype in ("class", "struct"):
                self._class_depth += 1
                self._descend(node, nid)
                self._class_depth -= 1
            else:
                self._descend(node, nid)
        else:
            self._recurse(node)

    def visit_struct_specifier(self, node) -> None:
        self._visit_tagged(node, "struct")

    def visit_union_specifier(self, node) -> None:
        self._visit_tagged(node, "struct")

    def visit_enum_specifier(self, node) -> None:
        self._visit_tagged(node, "enum")

    def visit_linkage_specification(self, node) -> None:
        """`extern "C" { ... }` — everything inside is C-linkage."""
        previous = self._extern_c
        self._extern_c = True
        self._recurse(node)
        self._extern_c = previous


class _CppVisitor(_CVisitor):
    """tree-sitter C++ visitor — adds namespaces, classes and templates."""

    def __init__(self, rel_path: Path, file_id: str) -> None:
        super().__init__(rel_path, file_id)
        self._template_params = ""

    def visit_namespace_definition(self, node) -> None:
        name = ""
        for child in node.children:
            if child.type in ("namespace_identifier", "identifier"):
                name = self._text(child)
                break
        if not name:
            self._recurse(node)
            return
        nid = self._emit_c(name, "namespace", node.start_point[0] + 1)
        self._descend(node, nid)

    def visit_template_declaration(self, node) -> None:
        """Carry the template parameters down to the declaration they wrap."""
        params = ""
        for child in node.children:
            if child.type == "template_parameter_list":
                params = self._text(child)
                break
        previous = self._template_params
        self._template_params = params
        self._recurse(node)
        self._template_params = previous

    def _emit_c(self, name: str, ntype: str, lineno: int, **extra) -> str:
        if self._template_params:
            extra.setdefault("is_template", True)
            extra.setdefault("template_params", self._template_params)
        return super()._emit_c(name, ntype, lineno, **extra)

    def visit_class_specifier(self, node) -> None:
        self._visit_tagged(node, "class")

    def visit_field_declaration(self, node) -> None:
        for child in node.children:
            if child.type == "function_declarator":
                name = self._declarator_name(child)
                if name:
                    self._emit_c(name, "method", node.start_point[0] + 1)
                return


_C_INCLUDE_RE = re.compile(r'(?m)^\s*#\s*include\s*[<"]([^>"]+)[>"]')
_C_DEFINE_RE = re.compile(r"(?m)^\s*#\s*define\s+(\w+)")
_C_TYPEDEF_RE = re.compile(r"(?m)^\s*typedef\s+.*?\b(\w+)\s*;")
_C_TAGGED_RE = re.compile(r"(?m)^[ \t]*(?:typedef\s+)?(struct|union|enum|class)\s+(\w+)")
_C_NAMESPACE_RE = re.compile(r"(?m)^[ \t]*namespace\s+(\w+)")
_C_TEMPLATE_RE = re.compile(r"(?m)^[ \t]*template\s*(<[^\n]*>)")
_C_FUNC_RE = re.compile(
    # A one-line `extern "C" { void f(void); }` is common in headers, so the
    # linkage prefix is allowed before the return type as well as on its own line.
    r"(?m)^[ \t]*(?:extern\s+\"[^\"]*\"\s*\{\s*)?"
    r"(?:(?:static|inline|extern|const|constexpr|virtual|explicit|friend)\s+)*"
    r"(?:[\w:<>,\s\*&]+?[\s\*&]+)(\w+)\s*\([^;{]*\)\s*(?:const\s*)?[;{]")
_C_NOT_FUNCS = frozenset({
    "if", "for", "while", "switch", "return", "sizeof", "catch", "do", "else",
    "typedef", "struct", "union", "enum", "class", "namespace", "template",
})


def _index_c_regex(source: str, rel_path: Path, file_id: str) -> tuple[list[dict], list[dict]]:
    """Regex fallback shared by C and C++."""
    nodes: list[dict] = []
    edges: list[dict] = []
    is_header = _is_header(rel_path)
    extern_spans = [
        (m.start(), _class_body_span(source, m.start()))
        for m in re.finditer(r'extern\s+"C"\s*\{', source)
    ]

    def _in_extern(pos: int) -> bool:
        return any(span and span[0] <= pos < span[1] for _, span in extern_spans)

    def _add(name: str, ntype: str, lineno: int, parent: str = file_id, **extra) -> str:
        nid = f"{parent}.{name}"
        node = {
            "id": nid, "label": name, "type": ntype, "description": "",
            "source_file": str(rel_path), "source_location": f"L{lineno}",
            "file_type": "code",
        }
        node["is_header"] = is_header
        node.update({k: v for k, v in extra.items() if v not in (None, "", [], False)})
        nodes.append(node)
        edges.append({"source": parent, "target": nid, "relation": "contains"})
        return nid

    for m in _C_INCLUDE_RE.finditer(source):
        target = m.group(1)
        nid = f"{file_id}.import_{re.sub(r'\W', '_', target)}"
        nodes.append({
            "id": nid, "label": target, "type": "import", "description": "",
            "source_file": str(rel_path),
            "source_location": f"L{_lineno(source, m.start())}",
            "file_type": "code",
        })
        edges.append({"source": file_id, "target": nid, "relation": "imports_from"})

    for m in _C_DEFINE_RE.finditer(source):
        _add(m.group(1), "macro", _lineno(source, m.start()))

    namespace_nid = file_id
    if (ns := _C_NAMESPACE_RE.search(source)) is not None:
        namespace_nid = _add(ns.group(1), "namespace", _lineno(source, ns.start()))

    template_lines = {_lineno(source, m.start()): m.group(1)
                      for m in _C_TEMPLATE_RE.finditer(source)}

    for m in _C_TAGGED_RE.finditer(source):
        kind, name = m.group(1), m.group(2)
        line = _lineno(source, m.start())
        ntype = {"struct": "struct", "union": "struct",
                 "enum": "enum", "class": "class"}[kind]
        params = template_lines.get(line - 1, "")
        _add(name, ntype, line, namespace_nid,
             is_template=bool(params), template_params=params)

    for m in _C_TYPEDEF_RE.finditer(source):
        _add(m.group(1), "typedef", _lineno(source, m.start()), namespace_nid)

    for m in _C_FUNC_RE.finditer(source):
        name = m.group(1)
        if name in _C_NOT_FUNCS:
            continue
        line = _lineno(source, m.start())
        params = template_lines.get(line - 1, "")
        # Test the name's position, not the match start — the match may begin at
        # an `extern "C" {` prefix that sits outside the block it opens.
        _add(name, "function", line, namespace_nid,
             is_extern_c=_in_extern(m.start(1)),
             is_template=bool(params), template_params=params)

    return nodes, edges


def index_c(path: Path, root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Index a C file (tree-sitter with the 'cpp' extra, else regex)."""
    return _index_with_grammar(path, root, _tsc, _C_TS_AVAILABLE,
                               _CVisitor, _index_c_regex, "C")


def index_cpp(path: Path, root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Index a C++ file (tree-sitter with the 'cpp' extra, else regex)."""
    return _index_with_grammar(path, root, _tscpp, _CPP_TS_AVAILABLE,
                               _CppVisitor, _index_c_regex, "C++")


# ---------------------------------------------------------------------------
# Lua
# ---------------------------------------------------------------------------

_LUA_LOCAL_FUNC_RE = re.compile(r"(?m)^[ \t]*local\s+function\s+([\w.]+)\s*\(")
_LUA_FUNC_RE = re.compile(r"(?m)^[ \t]*function\s+([\w.]+)(?::(\w+))?\s*\(")
_LUA_TABLE_RE = re.compile(r"(?m)^[ \t]*(?:local\s+)?(\w+)\s*=\s*\{\s*\}")
_LUA_TABLE_FUNC_RE = re.compile(r"(?m)^[ \t]*(\w+)\.(\w+)\s*=\s*function\s*\(")
_LUA_LOCAL_ASSIGN_FUNC_RE = re.compile(r"(?m)^[ \t]*local\s+(\w+)\s*=\s*function\s*\(")


def _index_lua_regex(source: str, rel_path: Path, file_id: str) -> tuple[list[dict], list[dict]]:
    """Regex indexer for Lua — regex is the primary parser, not a fallback.

    Lua's module convention is a bare table (`local M = {}`) whose fields are
    functions, so tables are indexed as modules and their functions nested
    underneath. `function Class:method()` is the method form and nests the same
    way.
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    tables: dict[str, str] = {}

    def _add(name: str, ntype: str, lineno: int, parent: str = file_id, **extra) -> str:
        nid = f"{parent}.{name}"
        node = {
            "id": nid, "label": name, "type": ntype, "description": "",
            "source_file": str(rel_path), "source_location": f"L{lineno}",
            "file_type": "code",
        }
        node.update({k: v for k, v in extra.items() if v not in (None, "", [])})
        nodes.append(node)
        edges.append({"source": parent, "target": nid, "relation": "contains"})
        return nid

    for m in _LUA_TABLE_RE.finditer(source):
        name = m.group(1)
        tables[name] = _add(name, "module", _lineno(source, m.start()),
                            is_local=source[m.start():m.end()].lstrip().startswith("local"))

    for m in _LUA_LOCAL_FUNC_RE.finditer(source):
        _add(m.group(1), "function", _lineno(source, m.start()), is_local=True)

    for m in _LUA_LOCAL_ASSIGN_FUNC_RE.finditer(source):
        if m.group(1) not in tables:
            _add(m.group(1), "function", _lineno(source, m.start()), is_local=True)

    for m in _LUA_FUNC_RE.finditer(source):
        owner, method = m.group(1), m.group(2)
        line = _lineno(source, m.start())
        if method:
            # `function Class:method()` — the colon form binds to its table.
            parent = tables.get(owner) or _add(owner, "module", line, is_local=False)
            tables.setdefault(owner, parent)
            _add(method, "method", line, parent, is_local=False)
        elif "." in owner:
            table, _, field = owner.rpartition(".")
            parent = tables.get(table, file_id)
            _add(field, "function", line, parent, is_local=False)
        else:
            _add(owner, "function", line, is_local=False)

    for m in _LUA_TABLE_FUNC_RE.finditer(source):
        table, field = m.group(1), m.group(2)
        parent = tables.get(table, file_id)
        _add(field, "function", _lineno(source, m.start()), parent, is_local=False)

    return nodes, edges


def index_lua(path: Path, root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Index a Lua file. Regex is the only parser — no tree-sitter extra."""
    return _index_regex_only(path, root, _index_lua_regex, "Lua")


# ---------------------------------------------------------------------------
# Elixir
# ---------------------------------------------------------------------------

_EX_MODULE_RE = re.compile(r"(?m)^[ \t]*defmodule\s+([\w.]+)\s+do")
_EX_DEF_RE = re.compile(r"(?m)^[ \t]*(defp?|defmacrop?)\s+([\w?!]+)")
_EX_STRUCT_RE = re.compile(r"(?m)^[ \t]*defstruct\s+(.+)$")
_EX_DIRECTIVE_RE = re.compile(r"(?m)^[ \t]*(use|import|alias|require)\s+([\w.]+)")


def _index_elixir_regex(source: str, rel_path: Path, file_id: str) -> tuple[list[dict], list[dict]]:
    """Regex indexer for Elixir — regex is the primary parser.

    Nesting follows indentation, the same approach the Ruby indexer uses, since
    Elixir has no braces either.
    """
    nodes: list[dict] = []
    edges: list[dict] = []

    def _add(name: str, ntype: str, lineno: int, parent: str, **extra) -> str:
        nid = f"{parent}.{name}"
        node = {
            "id": nid, "label": name, "type": ntype, "description": "",
            "source_file": str(rel_path), "source_location": f"L{lineno}",
            "file_type": "code",
        }
        node.update({k: v for k, v in extra.items() if v not in (None, "", [])})
        nodes.append(node)
        edges.append({"source": parent, "target": nid, "relation": "contains"})
        return nid

    scopes: list[tuple[int, str]] = [(-1, file_id)]

    for lineno, raw in enumerate(source.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        while len(scopes) > 1 and indent <= scopes[-1][0]:
            scopes.pop()
        parent = scopes[-1][1]

        if (m := _EX_MODULE_RE.match(raw)) is not None:
            nid = _add(m.group(1), "module", lineno, parent)
            scopes.append((indent, nid))
            continue

        if (m := _EX_DIRECTIVE_RE.match(raw)) is not None:
            edges.append({"source": parent, "target": m.group(2),
                          "relation": m.group(1)})
            continue

        if (m := _EX_STRUCT_RE.match(raw)) is not None:
            fields = re.findall(r":(\w+)", m.group(1))
            _add("__struct__", "struct", lineno, parent, fields=fields)
            continue

        if (m := _EX_DEF_RE.match(raw)) is not None:
            kind, name = m.group(1), m.group(2)
            ntype = "macro" if kind.startswith("defmacro") else "function"
            _add(name, ntype, lineno, parent, is_private=kind.endswith("p"))

    return nodes, edges


def index_elixir(path: Path, root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Index an Elixir file. Regex is the only parser — no tree-sitter extra."""
    return _index_regex_only(path, root, _index_elixir_regex, "Elixir")


# ---------------------------------------------------------------------------
# PowerShell
# ---------------------------------------------------------------------------

_PS_FUNC_RE = re.compile(r"(?mi)^[ \t]*function\s+([\w-]+)")
_PS_CLASS_RE = re.compile(r"(?mi)^[ \t]*class\s+(\w+)")
_PS_METHOD_RE = re.compile(r"^[ \t]*(?:\[[\w\[\]]+\]\s*)?(\w+)\s*\(")
_PS_REQUIRES_RE = re.compile(r"(?mi)^\s*#requires\s+-(\w+)\s*(.*)$")
_PS_ATTR_RE = re.compile(r"\[(\w+)\s*\(")
_PS_NOT_METHODS = frozenset({"if", "for", "foreach", "while", "switch", "return", "param"})


def _index_powershell_regex(source: str, rel_path: Path, file_id: str) -> tuple[list[dict], list[dict]]:
    """Regex indexer for PowerShell — regex is the primary parser.

    Splits Verb-Noun names because PowerShell's naming convention is a real
    contract: `Get-User` and `Set-User` act on the same noun, and being able to
    query by verb or by noun is what makes a large script module navigable.
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    lines = source.splitlines()

    def _add(name: str, ntype: str, lineno: int, parent: str = file_id, **extra) -> str:
        nid = f"{parent}.{name}"
        node = {
            "id": nid, "label": name, "type": ntype, "description": "",
            "source_file": str(rel_path), "source_location": f"L{lineno}",
            "file_type": "code",
        }
        node.update({k: v for k, v in extra.items() if v not in (None, "", [], False)})
        nodes.append(node)
        edges.append({"source": parent, "target": nid, "relation": "contains"})
        return nid

    def _attrs_above(idx: int) -> list[str]:
        found: list[str] = []
        i = idx - 1
        while i >= 0 and (stripped := lines[i].strip()).startswith("["):
            found = [f"[{a}]" for a in _PS_ATTR_RE.findall(stripped)] + found
            i -= 1
        return found

    for m in _PS_REQUIRES_RE.finditer(source):
        target = (m.group(2) or m.group(1)).strip() or m.group(1)
        edges.append({"source": file_id, "target": target, "relation": "requires"})

    class_spans: list[tuple[int, int, str]] = []
    class_matches = list(_PS_CLASS_RE.finditer(source))
    for i, m in enumerate(class_matches):
        limit = class_matches[i + 1].start() if i + 1 < len(class_matches) else None
        idx = _lineno(source, m.start()) - 1
        nid = _add(m.group(1), "class", idx + 1, attributes=_attrs_above(idx))
        span = _class_body_span(source, m.start(), limit)
        if span:
            class_spans.append((span[0], span[1], nid))

    for start, end, class_nid in class_spans:
        body_line = _lineno(source, start)
        depth = 0
        for offset, raw in enumerate(source[start:end].splitlines()):
            line = raw.strip()
            if depth == 0 and line and not line.startswith("#"):
                if (mm := _PS_METHOD_RE.match(line)) is not None:
                    member = mm.group(1)
                    if member.lower() not in _PS_NOT_METHODS:
                        _add(member, "method", body_line + offset, class_nid)
            depth += raw.count("{") - raw.count("}")
            if depth < 0:
                break

    for m in _PS_FUNC_RE.finditer(source):
        name = m.group(1)
        idx = _lineno(source, m.start()) - 1
        verb, _, noun = name.partition("-")
        body_end = source.find("}", m.start())
        header = source[m.start():body_end if body_end != -1 else len(source)]
        _add(name, "function", idx + 1,
             attributes=_attrs_above(idx),
             verb=verb if noun else "",
             noun=noun,
             has_params=bool(re.search(r"(?i)\bparam\s*\(", header)))

    return nodes, edges


def index_powershell(path: Path, root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Index a PowerShell file. Regex is the only parser — no tree-sitter extra."""
    return _index_regex_only(path, root, _index_powershell_regex, "PowerShell")


def _index_regex_only(
    path: Path, root: Path | None, regex_fn, language_label: str,
) -> tuple[list[dict], list[dict]]:
    """Entry point for languages whose regex parser is the only parser.

    DECISION: no tree-sitter attempt and no fallback warning — for these
    languages regex is the design, not a degradation, exactly like Go.
    """
    rel = _rel(path, root) if root else path
    fid = _file_id(rel)
    module_node = {
        "id": fid, "label": rel.stem, "type": "module",
        "description": f"{language_label} file {rel.name}", "source_file": str(rel),
        "source_location": "L1", "file_type": "code",
    }
    try:
        source_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [module_node], []
    nodes, edges = regex_fn(source_text, rel, fid)
    return [module_node] + nodes, edges

_INDEXED_EXTENSIONS: dict[str, object] = {
    ".py": index_python,
    ".ts": index_typescript,
    ".tsx": index_typescript,
    ".js": index_typescript,
    ".jsx": index_typescript,
    ".go": index_go,
    ".java": index_java,
    ".rs": index_rust,
    ".cs": index_csharp,
    ".rb": index_ruby,
    ".php": index_php,
    ".kt": index_kotlin,
    ".kts": index_kotlin,
    ".scala": index_scala,
    ".swift": index_swift,
    ".c": index_c,
    ".h": index_c,
    ".cpp": index_cpp,
    ".cc": index_cpp,
    ".cxx": index_cpp,
    ".hpp": index_cpp,
    ".hh": index_cpp,
    ".hxx": index_cpp,
    ".lua": index_lua,
    ".ex": index_elixir,
    ".exs": index_elixir,
    ".ps1": index_powershell,
    ".psm1": index_powershell,
}


def parser_summary(extensions: set[str]) -> list[tuple[str, str, bool]]:
    """Describe which parser handles each language present in a set of extensions.

    Args:
        extensions: Lowercase file suffixes seen while indexing (e.g. {".py"}).

    Returns:
        List of (language, parser, is_fallback) for the languages present, in a
        stable display order. is_fallback marks a degraded parser — a language
        whose optional tree-sitter grammar is not installed. Go has no grammar
        wired up, so its regex parser is its only parser and is not a fallback.
    """
    summary: list[tuple[str, str, bool]] = []
    if extensions & {".py"}:
        summary.append(("Python", "ast", False))
    for language, suffixes, available in (
        ("TypeScript", {".ts", ".tsx", ".js", ".jsx"}, _TREE_SITTER_AVAILABLE),
        ("Java", {".java"}, _JAVA_TS_AVAILABLE),
        ("Rust", {".rs"}, _RUST_TS_AVAILABLE),
        ("C#", {".cs"}, _CSHARP_TS_AVAILABLE),
        ("Ruby", {".rb"}, _RUBY_TS_AVAILABLE),
        ("PHP", {".php"}, _PHP_TS_AVAILABLE),
        ("Kotlin", {".kt", ".kts"}, _KOTLIN_TS_AVAILABLE),
        ("Scala", {".scala"}, _SCALA_TS_AVAILABLE),
        ("Swift", {".swift"}, _SWIFT_TS_AVAILABLE),
        ("C", {".c", ".h"}, _C_TS_AVAILABLE),
        ("C++", {".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"}, _CPP_TS_AVAILABLE),
    ):
        if extensions & suffixes:
            summary.append(
                (language, "tree-sitter", False) if available
                else (language, "regex fallback", True)
            )
    # Regex is the design for these, not a degradation — never flagged as fallback.
    for language, suffixes in (
        ("Go", {".go"}),
        ("Lua", {".lua"}),
        ("Elixir", {".ex", ".exs"}),
        ("PowerShell", {".ps1", ".psm1"}),
    ):
        if extensions & suffixes:
            summary.append((language, "regex", False))
    return summary


_PARSER_PREFIX = "  Parsers: "
_PARSER_CONT_INDENT = " " * len(_PARSER_PREFIX)   # 11 — aligns under the first language
_PARSERS_INLINE_MAX = 4                           # up to this many stay on one line
_PARSERS_PER_LINE = 3                             # once wrapped, this many per line


def format_parser_summary(summary: list[tuple[str, str, bool]]) -> list[str]:
    """Render a parser summary as display lines carrying rich markup.

    DECISION: ten languages do not fit on one terminal line, and a wrapped line
    with no indent reads as unrelated output. Beyond four parsers the list wraps
    at three per line, with continuation lines aligned under the first language.

    Args:
        summary: Output of parser_summary().

    Returns:
        One string per display line, empty when there is nothing to report.
    """
    if not summary:
        return []

    parts = [
        f"[orange1]{lang} ({parser})[/]" if fallback else f"{lang} ({parser})"
        for lang, parser, fallback in summary
    ]
    if len(parts) <= _PARSERS_INLINE_MAX:
        return [_PARSER_PREFIX + " · ".join(parts)]

    lines: list[str] = []
    for start in range(0, len(parts), _PARSERS_PER_LINE):
        chunk = " · ".join(parts[start:start + _PARSERS_PER_LINE])
        lines.append((_PARSER_PREFIX if start == 0 else _PARSER_CONT_INDENT) + chunk)
    return lines


def iter_source_files(root: Path) -> Iterator[Path]:
    """Yield every indexable source file under *root*, in deterministic order.

    Applies the same skip-directory and symlink-containment rules as
    :func:`index_project`, so callers can count or preview the exact set of
    files that indexing will process.

    Args:
        root: Root directory to scan.

    Yields:
        Paths of files whose extension has a registered indexer.
    """
    root_resolved = root.resolve()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or _should_skip(path):
            continue
        # Guard against symlink traversal outside the project root
        try:
            path.resolve().relative_to(root_resolved)
        except ValueError:
            continue
        if path.suffix.lower() in _INDEXED_EXTENSIONS:
            yield path


def index_project(
    root: Path,
    ignore: SlurpIgnore | None = None,
    on_file: Callable[[Path], None] | None = None,
) -> dict:
    """Index an entire project directory.

    Discovers Python, TypeScript/JavaScript, and Go source files, indexes them,
    deduplicates nodes by ID, and builds a graphify-compatible graph dict.

    Args:
        root: Root directory to index.
        ignore: Optional SlurpIgnore rules to apply.
        on_file: Optional callback invoked with each source file once it has
            been processed, including files skipped due to a parse error. Used
            to drive progress reporting; the file set matches
            :func:`iter_source_files`.

    Returns:
        dict with 'nodes', 'links', 'built_at_commit' keys (graphify-compatible).
    """
    import sys

    all_nodes: list[dict] = []
    all_edges: list[dict] = []

    for path in iter_source_files(root):
        indexer = _INDEXED_EXTENSIONS[path.suffix.lower()]
        try:
            nodes, edges = indexer(path, root)  # type: ignore[operator]
        except Exception as exc:
            print(f"  Warning: skipping {path}: {exc}", file=sys.stderr)
            continue
        finally:
            if on_file is not None:
                on_file(path)
        all_nodes.extend(nodes)
        all_edges.extend(edges)

    # Deduplicate nodes by id (first occurrence wins)
    seen_ids: set[str] = set()
    unique_nodes: list[dict] = []
    for n in all_nodes:
        if n["id"] not in seen_ids:
            seen_ids.add(n["id"])
            unique_nodes.append(n)

    if ignore is not None:
        unique_nodes = [n for n in unique_nodes if not ignore.should_ignore(n, n["id"])]

    known_ids: set[str] = {n["id"] for n in unique_nodes}

    seen_edges: set[tuple[str, str]] = set()
    filtered_edges: list[dict] = []
    for e in all_edges:
        src, tgt = e.get("source", ""), e.get("target", "")
        if src in known_ids and tgt in known_ids:
            key = (src, tgt)
            if key not in seen_edges:
                seen_edges.add(key)
                filtered_edges.append(e)

    return {
        "nodes": unique_nodes,
        "links": filtered_edges,
        "built_at_commit": None,
    }
