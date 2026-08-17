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

_TREE_SITTER_AVAILABLE = _tsts is not None
_JAVA_TS_AVAILABLE = _tsjava is not None
_RUST_TS_AVAILABLE = _tsrust is not None
_CSHARP_TS_AVAILABLE = _tscsharp is not None
_RUBY_TS_AVAILABLE = _tsruby is not None

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


def _class_body_span(source: str, class_start: int) -> tuple[int, int] | None:
    """Locate the brace-delimited body of the class beginning at *class_start*.

    Returns:
        (first_index_inside, index_of_closing_brace), or None if unbalanced.
    """
    open_idx = source.find("{", class_start)
    if open_idx == -1:
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


def _index_with_grammar(
    path: Path, root: Path | None, grammar, available: bool,
    visitor_cls, regex_fn, language_label: str,
) -> tuple[list[dict], list[dict]]:
    """Shared body for the tree-sitter-or-regex language entry points."""
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
            tree = _TSParser(_TSLanguage(grammar.language())).parse(
                source_text.encode("utf-8"))
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
    ):
        if extensions & suffixes:
            summary.append(
                (language, "tree-sitter", False) if available
                else (language, "regex fallback", True)
            )
    if extensions & {".go"}:
        summary.append(("Go", "regex", False))
    return summary


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
