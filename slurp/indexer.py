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
    import tree_sitter_typescript as _tsts

    _TREE_SITTER_AVAILABLE = True
except ImportError:  # optional extra not installed — regex fallback is expected
    _TSLanguage = _TSParser = _tsts = None  # type: ignore[assignment]
    _TREE_SITTER_AVAILABLE = False

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


class _TSVisitor:
    """tree-sitter–based TypeScript/JavaScript visitor.

    Dispatches to visit_<node_type> methods; recurses on unknown types.
    """

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

    def __init__(self, rel_path: Path, file_id: str) -> None:
        self.rel_path = rel_path
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self._scope: list[str] = [file_id]

    @property
    def _parent(self) -> str:
        return self._scope[-1]

    @staticmethod
    def _ts_name(node) -> str:
        name_node = node.child_by_field_name("name")
        if name_node:
            return name_node.text.decode("utf-8")
        for child in node.children:
            if child.type in ("identifier", "type_identifier", "property_identifier"):
                return child.text.decode("utf-8")
        return ""

    def _emit(self, name: str, ntype: str, lineno: int) -> str:
        nid = f"{self._parent}.{name}"
        self.nodes.append({
            "id": nid,
            "label": name,
            "type": ntype,
            "description": "",
            "source_file": str(self.rel_path),
            "source_location": f"L{lineno}",
            "file_type": "code",
        })
        self.edges.append({"source": self._parent, "target": nid, "relation": "contains"})
        return nid

    def visit(self, node) -> None:
        handler = getattr(self, f"visit_{node.type}", None)
        if handler is not None:
            handler(node)
        else:
            self._recurse(node)

    def _recurse(self, node) -> None:
        for child in node.children:
            self.visit(child)

    def _visit_decl(self, node) -> None:
        ntype = self._DECL_TYPES.get(node.type, "function")
        name = self._ts_name(node)
        if not name:
            self._recurse(node)
            return
        lineno = node.start_point[0] + 1
        nid = self._emit(name, ntype, lineno)
        self._scope.append(nid)
        self._recurse(node)
        self._scope.pop()

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

_INDEXED_EXTENSIONS: dict[str, object] = {
    ".py": index_python,
    ".ts": index_typescript,
    ".tsx": index_typescript,
    ".js": index_typescript,
    ".jsx": index_typescript,
    ".go": index_go,
}


def parser_summary(extensions: set[str]) -> list[tuple[str, str, bool]]:
    """Describe which parser handles each language present in a set of extensions.

    Args:
        extensions: Lowercase file suffixes seen while indexing (e.g. {".py"}).

    Returns:
        List of (language, parser, is_fallback) for the languages present, in a
        stable display order. is_fallback marks a degraded parser — currently
        only TypeScript/JS when the optional tree-sitter extra is missing.
    """
    summary: list[tuple[str, str, bool]] = []
    if extensions & {".py"}:
        summary.append(("Python", "ast", False))
    if extensions & {".ts", ".tsx", ".js", ".jsx"}:
        if _TREE_SITTER_AVAILABLE:
            summary.append(("TypeScript", "tree-sitter", False))
        else:
            summary.append(("TypeScript", "regex fallback", True))
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
