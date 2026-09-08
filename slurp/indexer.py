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


# ---------------------------------------------------------------------------
# Python call-graph extraction
#
# DECISION: this runs as two extra passes over the same AST rather than being
# folded into _PythonVisitor. Resolving a call needs the *complete* symbol table
# — `checkout` calls `PaymentGateway.charge` before that class is reached in a
# single top-down walk — so collection must finish before resolution starts.
# Keeping it separate also means the existing node/contains/imports_from output
# is byte-identical to before.
# ---------------------------------------------------------------------------

class _PythonDefCollector(ast.NodeVisitor):
    """Pass 1 — index every definition in the module by fully-qualified id.

    Attributes:
        kind: node id -> "function" or "class", for every definition found.
        bases: class node id -> the bare names of its base classes, used to
            resolve an inherited ``self.method()`` to the class that defines it.
    """

    def __init__(self, module_id: str) -> None:
        self.module_id = module_id
        self.kind: dict[str, str] = {}
        self.bases: dict[str, list[str]] = {}
        # Local binding name -> (import node id, full dotted path it refers to).
        # Node ids mirror _PythonVisitor exactly so the two passes agree.
        self.imports: dict[str, tuple[str, str]] = {}
        self._scope: list[str] = [module_id]

    # DECISION: the import node id uses the *current* scope, not the module,
    # because _PythonVisitor nests a lazy `from x import y` inside the function
    # that declares it (`cli.run.import_slurp_viz_build_html`). Anchoring these
    # to the module instead would point every pending edge at a node that does
    # not exist, and the final validity filter would silently drop them all.
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            raw = alias.asname or alias.name
            safe = raw.replace(".", "_").replace("-", "_")
            self.imports[raw] = (f"{self._scope[-1]}.import_{safe}", alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                continue
            raw = alias.asname or alias.name
            safe = f"{module}_{raw}".replace(".", "_").replace("-", "_")
            label = f"{module}.{alias.name}" if module else alias.name
            self.imports[raw] = (f"{self._scope[-1]}.import_{safe}", label)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        nid = f"{self._scope[-1]}.{node.name}"
        self.kind[nid] = "class"
        self.bases[nid] = [b.id for b in node.bases if isinstance(b, ast.Name)]
        self._scope.append(nid)
        self.generic_visit(node)
        self._scope.pop()

    def _collect_func(self, node) -> None:
        nid = f"{self._scope[-1]}.{node.name}"
        self.kind[nid] = "function"
        self._scope.append(nid)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._collect_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._collect_func(node)


# Receivers whose attribute access resolves against the enclosing class.
_PY_SELF_NAMES = frozenset({"self", "cls"})

# Private edge key holding the dotted symbol an import-node call refers to.
# index_project consumes it during cross-file resolution and strips it, so it
# never reaches a finished graph.
_PENDING_SYMBOL = "_pending_symbol"


class _PythonCallVisitor(ast.NodeVisitor):
    """Pass 2 — resolve ast.Call nodes to definitions and emit `calls` edges.

    Only calls that resolve to a definition in this module produce an edge.
    Anything else — stdlib, third-party, a method on an object of unknown type,
    a chained or computed callee — is skipped. A missing edge is a gap; a wrong
    edge is a lie about the codebase, so ambiguity always resolves to silence.
    """

    def __init__(self, defs: _PythonDefCollector) -> None:
        self.defs = defs
        self.edges: list[dict] = []
        self._seen: set[tuple[str, str, str | None]] = set()
        # (node id, kind) so bare-name lookup can skip class scopes, which are
        # not visible to functions nested inside them.
        self._scope: list[tuple[str, str]] = [(defs.module_id, "module")]
        self._class_stack: list[str] = []
        # One frame per function: local variable name -> class node id, for
        # resolving `gateway.charge()` after `gateway = PaymentGateway()`.
        self._locals: list[dict[str, str]] = [{}]

    # -- scope helpers ----------------------------------------------------

    @property
    def _caller(self) -> str:
        return self._scope[-1][0]

    def _lookup_name(self, name: str) -> str | None:
        """Resolve a bare name against the module and enclosing function scopes.

        Class scopes are skipped: in Python a name inside a method does not see
        its class's attributes, so treating them as candidates would resolve a
        module-level call to a same-named method.
        """
        for nid, kind in reversed(self._scope):
            if kind == "class":
                continue
            candidate = f"{nid}.{name}"
            if candidate in self.defs.kind:
                return candidate
        return None

    def _lookup_member(self, class_id: str, attr: str) -> str | None:
        """Resolve *attr* on *class_id*, following locally-defined base classes."""
        seen: set[str] = set()
        queue = [class_id]
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            candidate = f"{current}.{attr}"
            if self.defs.kind.get(candidate) == "function":
                return candidate
            for base_name in self.defs.bases.get(current, []):
                base_id = f"{self.defs.module_id}.{base_name}"
                if self.defs.kind.get(base_id) == "class":
                    queue.append(base_id)
        return None

    # -- resolution -------------------------------------------------------

    def _resolve(self, func: ast.expr) -> tuple[str, str | None] | None:
        """Resolve a callee expression to (node id, pending symbol) or None.

        The second element is set only when the target is an *import* node: it
        carries the dotted symbol the call actually refers to, which
        index_project resolves against the whole project. Within a single file
        there is nothing further to resolve, so it stays None.
        """
        if isinstance(func, ast.Name):
            # Covers both `helper()` and the constructor form `ClassName()`.
            local = self._lookup_name(func.id)
            if local is not None:
                return (local, None)
            imported = self.defs.imports.get(func.id)
            if imported is not None:
                # `from x import f` + `f()`: the import's label is the symbol.
                return (imported[0], imported[1])
            return None

        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            receiver = func.value.id
            if receiver in _PY_SELF_NAMES and self._class_stack:
                member = self._lookup_member(self._class_stack[-1], func.attr)
                return (member, None) if member else None
            class_id = self._locals[-1].get(receiver)
            if class_id is not None:
                member = self._lookup_member(class_id, func.attr)
                return (member, None) if member else None
            imported = self.defs.imports.get(receiver)
            if imported is not None:
                # `import x as m` + `m.f()`: the symbol is module + attribute.
                return (imported[0], f"{imported[1]}.{func.attr}")

        # Chained calls, subscripts, lambdas: not resolvable to a node.
        return None

    def _class_of_value(self, value: ast.expr | None) -> str | None:
        """Class node id a value is an instance of, when that is certain."""
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            target = self._lookup_name(value.func.id)  # local classes only
            # `token = hash_token(card)` has the same shape as
            # `gateway = PaymentGateway()`; only the class case binds a type.
            if target is not None and self.defs.kind.get(target) == "class":
                return target
        return None

    def _add(self, target: str, symbol: str | None = None) -> None:
        key = (self._caller, target, symbol)
        if key in self._seen:
            return
        self._seen.add(key)
        edge = {"source": self._caller, "target": target, "relation": "calls"}
        if symbol is not None:
            edge[_PENDING_SYMBOL] = symbol
        self.edges.append(edge)

    # -- traversal --------------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        nid = f"{self._scope[-1][0]}.{node.name}"
        self._scope.append((nid, "class"))
        self._class_stack.append(nid)
        for stmt in node.body:
            self.visit(stmt)
        self._class_stack.pop()
        self._scope.pop()

    def _visit_func(self, node) -> None:
        # DECISION: decorators and default arguments are evaluated where the
        # function is *defined*, not inside it, so they are attributed to the
        # enclosing scope before the function scope is pushed.
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default is not None:
                self.visit(default)

        nid = f"{self._scope[-1][0]}.{node.name}"
        self._scope.append((nid, "function"))
        self._locals.append({})
        for stmt in node.body:
            self.visit(stmt)
        self._locals.pop()
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_func(node)

    def visit_Call(self, node: ast.Call) -> None:
        resolved = self._resolve(node.func)
        if resolved is not None:
            self._add(resolved[0], resolved[1])
        # Nested calls in the arguments still count.
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        class_id = self._class_of_value(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                if class_id is not None:
                    self._locals[-1][target.id] = class_id
                else:
                    # Rebinding to something else must clear a stale type, or a
                    # later `x.method()` resolves against the wrong class.
                    self._locals[-1].pop(target.id, None)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.generic_visit(node)
        if not isinstance(node.target, ast.Name):
            return
        class_id = None
        if isinstance(node.annotation, ast.Name):
            annotated = self._lookup_name(node.annotation.id)
            if annotated is not None and self.defs.kind.get(annotated) == "class":
                class_id = annotated
        if class_id is None:
            class_id = self._class_of_value(node.value)
        if class_id is not None:
            self._locals[-1][node.target.id] = class_id
        else:
            self._locals[-1].pop(node.target.id, None)


def _python_call_edges(tree: ast.Module, module_id: str, known: set[str]) -> list[dict]:
    """Extract `calls` edges for one parsed Python module.

    Args:
        tree: Parsed module AST.
        module_id: Node id of the module, used as the root scope.
        known: Ids of nodes that exist in the graph. Edges touching anything
            outside this set are dropped.

    Returns:
        Deduplicated `calls` edges, both endpoints guaranteed to be real nodes.
    """
    defs = _PythonDefCollector(module_id)
    defs.visit(tree)
    calls = _PythonCallVisitor(defs)
    calls.visit(tree)
    return [
        e for e in calls.edges
        if e["source"] in known and e["target"] in known
    ]

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

    known = {n["id"] for n in visitor.nodes}
    call_edges = _python_call_edges(tree, _file_id(rel), known)
    return visitor.nodes, visitor.edges + call_edges


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
# Same statement, but capturing the clause so the regex branch can recover the
# individual symbols the tree-sitter branch reads off the AST.
_TS_IMPORT_CLAUSE_RE = re.compile(
    r'import\s+(type\s+)?([^;\n]*?)\s+from\s+[\'"]([^\'"]+)[\'"]'
)


def _ts_parse_clause_text(clause: str) -> tuple[str, dict[str, str]]:
    """Parse an import clause's source text into (kind, bindings).

    Mirrors _ts_parse_import_clause, which reads the same information off the
    tree-sitter AST, so both branches record identical metadata.
    """
    clause = clause.strip()
    bindings: dict[str, str] = {}

    namespace = re.search(r'\*\s+as\s+([A-Za-z_$][\w$]*)', clause)
    if namespace:
        return "namespace", {namespace.group(1): "*"}

    braces = re.search(r'\{([^}]*)\}', clause)
    if braces:
        for piece in braces.group(1).split(","):
            entry = piece.strip()
            if not entry or entry.startswith("type "):
                continue
            if " as " in entry:
                original, _, local = entry.partition(" as ")
                bindings[local.strip()] = original.strip()
            else:
                bindings[entry] = entry
        if bindings:
            return "named", bindings

    default = re.match(r'^([A-Za-z_$][\w$]*)\s*(?:,|$)', clause)
    if default:
        return "default", {default.group(1): "default"}
    return "side_effect", {}


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

    for m in _TS_IMPORT_CLAUSE_RE.finditer(source):
        type_only, clause, module_path = m.group(1), m.group(2), m.group(3)
        safe = module_path.rsplit("/", 1)[-1].replace("-", "_").replace(".", "_")
        nid = f"{file_id}.import_{safe}"
        record = {
            "id": nid,
            "label": module_path,
            "type": "import",
            "description": "",
            "source_file": str(rel_path),
            "source_location": f"L{_lineno(source, m.start())}",
            "file_type": "code",
        }
        kind, bindings = ("side_effect", {}) if type_only else _ts_parse_clause_text(clause)
        if bindings:
            record[_TS_IMPORTED_SYMBOLS] = sorted(bindings)
            record[_TS_IMPORT_KIND] = kind
            resolved_module = _ts_resolve_module_path(module_path, rel_path)
            if resolved_module:
                record[_TS_SOURCE_MODULE] = resolved_module
        nodes.append(record)
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


def _ts_module_dots(parts: list[str]) -> str:
    """Join path components into a node-id prefix, matching _file_id()."""
    return ".".join(part.replace("-", "_").replace(" ", "_") for part in parts if part)


def _ts_resolve_module_path(specifier: str, rel_path: Path) -> str | None:
    """Turn an import specifier into the dotted module id it refers to.

    Args:
        specifier: The raw string in the import statement.
        rel_path: Path of the importing file, relative to the project root.

    Returns:
        Dotted module path (`lib.supabase.admin`), or None for a bare package
        specifier — `react`, `next/navigation` — which lives in node_modules and
        can never be a node in this graph.
    """
    spec = specifier.strip().strip("'\"`")
    if not spec:
        return None

    if spec.startswith("@/"):
        parts = spec[2:].split("/")
    elif spec.startswith("./") or spec.startswith("../"):
        base = list(rel_path.parent.parts)
        for piece in spec.split("/"):
            if piece in ("", "."):
                continue
            if piece == "..":
                if base:
                    base.pop()
                continue
            base.append(piece)
        parts = base
    else:
        return None

    if not parts:
        return None
    parts[-1] = re.sub(r"\.(ts|tsx|js|jsx|mjs|cjs)$", "", parts[-1])
    return _ts_module_dots(parts) or None


def _ts_parse_import_clause(node, text) -> tuple[str, dict[str, str]]:
    """Extract the kind of import and its local-name -> exported-name map.

    Returns:
        (kind, bindings) where kind is "named", "namespace", "default" or
        "side_effect", and bindings maps each local binding to the name it
        refers to in the source module. Type-only specifiers are dropped: they
        vanish at runtime and can never be called.
    """
    bindings: dict[str, str] = {}
    kind = "side_effect"
    for child in node.children:
        if child.type == "named_imports":
            kind = "named"
            for spec in child.children:
                if spec.type != "import_specifier":
                    continue
                raw = text(spec).strip()
                if raw.startswith("type "):
                    continue
                if " as " in raw:
                    original, _, local = raw.partition(" as ")
                    bindings[local.strip()] = original.strip()
                else:
                    bindings[raw] = raw
        elif child.type == "namespace_import":
            kind = "namespace"
            for sub in child.children:
                if sub.type == "identifier":
                    bindings[text(sub)] = "*"
        elif child.type == "identifier":
            kind = "default"
            bindings[text(child)] = "default"
    return kind, bindings

# Public metadata recorded on TypeScript import nodes, consumed by the
# cross-file resolution pass in index_project().
_TS_IMPORTED_SYMBOLS = "_imported_symbols"
_TS_SOURCE_MODULE = "_source_module"
_TS_IMPORT_KIND = "_import_type"


class _TSVisitor(_BaseVisitor):
    """tree-sitter–based TypeScript/JavaScript visitor."""

    def __init__(self, rel_path: Path, file_id: str) -> None:
        super().__init__(rel_path, file_id)
        # Local binding -> (import node id, name exported by the source module).
        self.imports: dict[str, tuple[str, str]] = {}

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

        # `import type { User } from ...` erases at compile time — nothing it
        # names can appear in a call, so it carries no symbols at all.
        type_only = any(child.type == "type" for child in node.children)
        kind, bindings = "side_effect", {}
        if not type_only:
            for child in node.children:
                if child.type == "import_clause":
                    kind, bindings = _ts_parse_import_clause(child, self._text)
                    break

        record = {
            "id": nid,
            "label": module,
            "type": "import",
            "description": "",
            "source_file": str(self.rel_path),
            "source_location": f"L{lineno}",
            "file_type": "code",
        }
        if bindings:
            record[_TS_IMPORTED_SYMBOLS] = sorted(bindings)
            record[_TS_IMPORT_KIND] = kind
            resolved = _ts_resolve_module_path(module, self.rel_path)
            if resolved:
                record[_TS_SOURCE_MODULE] = resolved
        self.nodes.append(record)
        self.edges.append({"source": self._parent, "target": nid, "relation": "imports_from"})
        self.imports.update(
            {local: (nid, original) for local, original in bindings.items()}
        )


# ---------------------------------------------------------------------------
# TypeScript / JavaScript call-graph extraction
#
# Same two-phase shape as the Python extractor: collect every definition first,
# then resolve calls against that complete table. TS is easier in one respect —
# `new X()` is its own node type, so a constructor call can never be confused
# with a plain function call the way `x = C()` and `x = f()` are in Python.
# ---------------------------------------------------------------------------

# Receiver expression that resolves against the enclosing class.
_TS_THIS_TYPES = frozenset({"this"})

# Declarations that open a nested scope, mirroring _TSVisitor._DECL_TYPES so
# both passes build identical node ids.
_TS_SCOPE_TYPES = frozenset({
    "function_declaration", "generator_function_declaration",
    "class_declaration", "abstract_class_declaration",
    "interface_declaration", "method_definition",
    "function_signature", "abstract_method_signature",
})


def _ts_class_bases(node) -> list[str]:
    """Names in a class declaration's extends clause."""
    bases: list[str] = []
    for child in node.children:
        if child.type not in ("class_heritage", "extends_clause"):
            continue
        for sub in child.children:
            if sub.type in ("identifier", "type_identifier"):
                bases.append(sub.text.decode("utf-8", "replace"))
            elif sub.type == "extends_clause":
                for inner in sub.children:
                    if inner.type in ("identifier", "type_identifier"):
                        bases.append(inner.text.decode("utf-8", "replace"))
    return bases


class _TSCallVisitor(_BaseVisitor):
    """Pass 2 — resolve call/new expressions to definitions and emit `calls`.

    Walks the same tree as _TSVisitor and rebuilds the identical scope stack, so
    a caller's id here always matches the node id emitted there. Only calls that
    resolve to a definition in this file produce an edge; console.log, an import,
    or a method on an object of unknown type resolve to nothing and are skipped.
    """

    def __init__(
        self,
        rel_path: Path,
        file_id: str,
        symbols: dict[str, str],
        imports: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(rel_path, file_id)
        self.symbols = symbols
        # Local binding -> (import node id, exported name), from _TSVisitor.
        self.imports = imports or {}
        self.bases: dict[str, list[str]] = {}
        self.module_id = file_id
        self._kinds: list[str] = ["module"]
        self._class_stack: list[str] = []
        self._locals: list[dict[str, str]] = [{}]
        self._seen: set[tuple[str, str, str | None]] = set()

    # -- scope ------------------------------------------------------------

    def _push(self, nid: str, kind: str) -> None:
        self._scope.append(nid)
        self._kinds.append(kind)

    def _pop(self) -> None:
        self._scope.pop()
        self._kinds.pop()

    def _lookup_name(self, name: str) -> str | None:
        """Resolve a bare name outward through function and module scopes.

        Class scopes are skipped: inside a method, a bare `foo()` refers to a
        module-level function, never to a sibling method (that needs `this.`).
        """
        for nid, kind in zip(reversed(self._scope), reversed(self._kinds)):
            if kind == "class":
                continue
            candidate = f"{nid}.{name}"
            if candidate in self.symbols:
                return candidate
        return None

    def _lookup_member(self, class_id: str, prop: str) -> str | None:
        """Resolve *prop* on *class_id*, following locally-declared base classes."""
        seen: set[str] = set()
        queue = [class_id]
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            candidate = f"{current}.{prop}"
            if self.symbols.get(candidate) == "function":
                return candidate
            for base in self.bases.get(current, []):
                base_id = f"{self.module_id}.{base}"
                if self.symbols.get(base_id) == "class":
                    queue.append(base_id)
        return None

    def _class_from_annotation(self, node) -> str | None:
        """Class id named by a `: Type` annotation, when it is a local class."""
        if node is None:
            return None
        for child in node.children:
            if child.type in ("type_identifier", "identifier"):
                target = self._lookup_name(child.text.decode("utf-8", "replace"))
                if target is not None and self.symbols.get(target) == "class":
                    return target
        return None

    def _class_from_new(self, node) -> str | None:
        """Class id constructed by a `new X()` expression, when X is local."""
        if node is None or node.type != "new_expression":
            return None
        ctor = node.child_by_field_name("constructor")
        if ctor is None or ctor.type != "identifier":
            return None
        target = self._lookup_name(ctor.text.decode("utf-8", "replace"))
        return target if self.symbols.get(target) == "class" else None

    def _add(self, target: str, symbol: str | None = None) -> None:
        key = (self._parent, target, symbol)
        if key in self._seen:
            return
        self._seen.add(key)
        edge = {"source": self._parent, "target": target, "relation": "calls"}
        if symbol is not None:
            edge[_PENDING_SYMBOL] = symbol
        self.edges.append(edge)

    # -- traversal --------------------------------------------------------

    def _visit_scope_decl(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            self._recurse(node)
            return
        nid = f"{self._parent}.{name}"
        is_class = node.type in ("class_declaration", "abstract_class_declaration")
        if is_class:
            self.bases[nid] = _ts_class_bases(node)
            self._class_stack.append(nid)
        else:
            self._locals.append({})
        self._push(nid, "class" if is_class else "function")
        self._recurse(node)
        self._pop()
        if is_class:
            self._class_stack.pop()
        else:
            self._locals.pop()

    visit_function_declaration = _visit_scope_decl
    visit_generator_function_declaration = _visit_scope_decl
    visit_class_declaration = _visit_scope_decl
    visit_abstract_class_declaration = _visit_scope_decl
    visit_interface_declaration = _visit_scope_decl
    visit_method_definition = _visit_scope_decl
    visit_function_signature = _visit_scope_decl
    visit_abstract_method_signature = _visit_scope_decl

    def visit_lexical_declaration(self, node) -> None:
        for child in node.children:
            if child.type != "variable_declarator":
                continue
            self._visit_declarator(child)

    visit_variable_declaration = visit_lexical_declaration

    def _visit_declarator(self, node) -> None:
        name_node = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        name = self._text(name_node) if name_node is not None else ""

        # `const handler = () => {...}` is emitted as a function node by
        # _TSVisitor, so its body belongs to that node, not the enclosing one.
        if name and value is not None and value.type in ("arrow_function", "function", "function_expression"):
            nid = f"{self._parent}.{name}"
            if nid in self.symbols:
                self._locals.append({})
                self._push(nid, "function")
                self._recurse(value)
                self._pop()
                self._locals.pop()
                return

        if value is not None:
            self.visit(value)

        if not name or not isinstance(name_node, type(node)) or name_node.type != "identifier":
            return
        class_id = (
            self._class_from_annotation(node.child_by_field_name("type"))
            or self._class_from_new(value)
        )
        if class_id is not None:
            self._locals[-1][name] = class_id
        else:
            # A rebind to something else must clear a stale type.
            self._locals[-1].pop(name, None)

    def visit_variable_declarator(self, node) -> None:
        self._visit_declarator(node)

    def visit_call_expression(self, node) -> None:
        resolved = self._resolve_callee(node.child_by_field_name("function"))
        if resolved is not None:
            self._add(resolved[0], resolved[1])
        self._recurse(node)

    def visit_new_expression(self, node) -> None:
        target = self._class_from_new(node)
        if target is not None:
            self._add(target)
        else:
            ctor = node.child_by_field_name("constructor")
            if ctor is not None and ctor.type == "identifier":
                imported = self.imports.get(self._text(ctor))
                if imported is not None:
                    self._add(imported[0], imported[1])
        self._recurse(node)

    def _resolve_callee(self, callee) -> tuple[str, str | None] | None:
        """Resolve a callee to (node id, pending symbol) or None.

        A pending symbol is set only when the target is an import node: it names
        the symbol in the *source* module, which index_project resolves once
        every file has been indexed.
        """
        if callee is None:
            return None

        if callee.type == "identifier":
            name = self._text(callee)
            local = self._lookup_name(name)
            if local is not None:
                return (local, None)
            imported = self.imports.get(name)
            if imported is not None:
                return (imported[0], imported[1])
            return None

        if callee.type == "member_expression":
            obj = callee.child_by_field_name("object")
            prop = callee.child_by_field_name("property")
            if obj is None or prop is None:
                return None
            prop_name = self._text(prop)
            if obj.type in _TS_THIS_TYPES and self._class_stack:
                member = self._lookup_member(self._class_stack[-1], prop_name)
                return (member, None) if member else None
            if obj.type == "identifier":
                obj_name = self._text(obj)
                class_id = self._locals[-1].get(obj_name)
                if class_id is not None:
                    member = self._lookup_member(class_id, prop_name)
                    return (member, None) if member else None
                imported = self.imports.get(obj_name)
                if imported is not None and imported[1] == "*":
                    # `import * as db` + `db.f()`: the property is the symbol.
                    return (imported[0], prop_name)
        return None


# --- regex fallback ---------------------------------------------------------

# Comment and string literals, blanked before scanning so a call-looking
# fragment inside them can never produce an edge.
_TS_NOISE_RE = re.compile(
    r"//[^\n]*|/\*.*?\*/|`(?:[^`\\]|\\.)*`|'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"",
    re.S,
)
_TS_NEW_CALL_RE = re.compile(r"\bnew\s+([A-Za-z_$][\w$]*)\s*\(")
_TS_THIS_CALL_RE = re.compile(r"\bthis\.([A-Za-z_$][\w$]*)\s*\(")
_TS_BARE_CALL_RE = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(")
# `const gw: Gw = new Gw(` — the `new` keyword makes the variable's type certain,
# so the same local-type inference the tree-sitter branch does is safe here too.
_TS_LOCAL_NEW_RE = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::\s*([A-Za-z_$][\w$]*))?\s*=\s*"
    r"new\s+([A-Za-z_$][\w$]*)\s*\("
)
_TS_VAR_CALL_RE = re.compile(
    r"(?<![.\w$])([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)\s*\("
)
# Declaration keywords: the name after them is being defined, not called.
_TS_DECL_KEYWORDS = frozenset({"function", "class", "interface", "new", "return",
                               "typeof", "instanceof", "await", "yield", "throw",
                               "in", "of", "as", "extends", "implements"})


def _ts_blank_noise(source: str) -> str:
    """Replace comments and string literals with spaces, preserving offsets."""
    return _TS_NOISE_RE.sub(lambda m: " " * len(m.group(0)), source)


def _ts_body_span(source: str, start: int) -> tuple[int, int] | None:
    """Span of the body belonging to the declaration at offset *start*.

    DECISION: whichever of `{` or `;` comes first decides the shape. A brace
    opens a block body and is matched to its closer; a semicolon means either a
    concise arrow body (`const f = () => expr;`) or a bodyless signature
    (`abstract load(id: string): unknown;`), and the span stops there. Scanning
    for the next `{` unconditionally would hand an abstract signature the body
    of whatever method follows it, and attribute that method's calls to the
    wrong node.
    """
    open_idx = source.find("{", start)
    semi_idx = source.find(";", start)
    if open_idx == -1 or (semi_idx != -1 and semi_idx < open_idx):
        return (start, semi_idx) if semi_idx != -1 else None
    depth = 0
    for i in range(open_idx, len(source)):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return open_idx, i
    return None


def _ts_decl_offset(source: str, lineno: int, name: str) -> int | None:
    """Offset of *name* on 1-based line *lineno*, the declaration's own line.

    Anchoring on the recorded declaration line rather than the first textual
    match keeps two same-named members (an abstract signature and its concrete
    override) from resolving to each other's body.
    """
    lines = source.splitlines(keepends=True)
    if not 1 <= lineno <= len(lines):
        return None
    offset = sum(len(line) for line in lines[: lineno - 1])
    column = lines[lineno - 1].find(name)
    return offset + column + len(name) if column != -1 else offset + len(lines[lineno - 1])


def _ts_regex_call_edges(
    source: str, nodes: list[dict], file_id: str
) -> list[dict]:
    """Extract `calls` edges from TS/JS source without tree-sitter.

    Args:
        source: Raw file text.
        nodes: Nodes already produced by the regex parser, used as the symbol
            table so both parsers resolve against the same ids.
        file_id: Module node id.

    Returns:
        Deduplicated `calls` edges. Only names that match a definition in this
        file resolve; everything else is skipped.
    """
    symbols = {
        n["id"]: n["type"] for n in nodes if n.get("type") in ("function", "class")
    }
    if not symbols:
        return []

    # Local binding -> (import node id, exported name). Re-derived here rather
    # than threaded through _index_typescript_regex, whose two-value signature
    # several callers already depend on.
    bindings: dict[str, tuple[str, str]] = {}
    for match in _TS_IMPORT_CLAUSE_RE.finditer(source):
        if match.group(1):          # `import type ...` erases at compile time
            continue
        module_path = match.group(3)
        safe = module_path.rsplit("/", 1)[-1].replace("-", "_").replace(".", "_")
        _, clause_bindings = _ts_parse_clause_text(match.group(2))
        bindings.update({
            local: (f"{file_id}.import_{safe}", exported)
            for local, exported in clause_bindings.items()
        })

    clean = _ts_blank_noise(source)

    # Function/method bodies, innermost-last, so a call can be attributed to the
    # smallest enclosing definition.
    spans: list[tuple[int, int, str, str | None]] = []
    for node in nodes:
        if node.get("type") not in ("function", "class"):
            continue
        nid = node["id"]
        owner = nid.rsplit(".", 1)[0]
        class_id = owner if symbols.get(owner) == "class" else None
        location = str(node.get("source_location", ""))
        if not location.startswith("L") or not location[1:].isdigit():
            continue
        start = _ts_decl_offset(clean, int(location[1:]), node["label"])
        if start is None:
            continue
        span = _ts_body_span(clean, start)
        if span is not None and span[1] > span[0]:
            spans.append((span[0], span[1], nid, class_id))

    def _enclosing(offset: int) -> tuple[str, str | None] | None:
        best: tuple[int, str, str | None] | None = None
        for start, end, nid, class_id in spans:
            if start < offset < end:
                width = end - start
                if best is None or width < best[0]:
                    best = (width, nid, class_id)
        return (best[1], best[2]) if best else None

    edges: list[dict] = []
    seen: set[tuple[str, str, str | None]] = set()

    def _add(caller: str, target: str, symbol: str | None = None) -> None:
        key = (caller, target, symbol)
        if key in seen:
            return
        seen.add(key)
        edge = {"source": caller, "target": target, "relation": "calls"}
        if symbol is not None:
            edge[_PENDING_SYMBOL] = symbol
        edges.append(edge)

    for match in _TS_NEW_CALL_RE.finditer(clean):
        enclosing = _enclosing(match.start())
        if not enclosing:
            continue
        target = f"{file_id}.{match.group(1)}"
        if symbols.get(target) == "class":
            _add(enclosing[0], target)
            continue
        imported = bindings.get(match.group(1))
        if imported is not None and imported[1] != "*":
            _add(enclosing[0], imported[0], imported[1])

    for match in _TS_THIS_CALL_RE.finditer(clean):
        enclosing = _enclosing(match.start())
        if not enclosing or enclosing[1] is None:
            continue
        target = f"{enclosing[1]}.{match.group(1)}"
        if symbols.get(target) == "function":
            _add(enclosing[0], target)

    for match in _TS_BARE_CALL_RE.finditer(clean):
        name = match.group(1)
        if name in _TS_DECL_KEYWORDS or name in _TS_NOT_MEMBERS:
            continue
        preceding = clean[max(0, match.start() - 12):match.start()]
        if re.search(r"\b(function|class|interface|new)\s+$", preceding):
            continue
        enclosing = _enclosing(match.start())
        if not enclosing:
            continue
        target = f"{file_id}.{name}"
        if symbols.get(target) == "function":
            _add(enclosing[0], target)
            continue
        imported = bindings.get(name)
        if imported is not None and imported[1] != "*":
            _add(enclosing[0], imported[0], imported[1])

    # Local variables whose class is pinned by `new` (or an annotation naming a
    # local class), scoped to the function they are declared in.
    local_types: dict[tuple[str, str], str] = {}
    for match in _TS_LOCAL_NEW_RE.finditer(clean):
        enclosing = _enclosing(match.start())
        if not enclosing:
            continue
        for candidate in (match.group(2), match.group(3)):
            if not candidate:
                continue
            class_id = f"{file_id}.{candidate}"
            if symbols.get(class_id) == "class":
                local_types[(enclosing[0], match.group(1))] = class_id
                break

    for match in _TS_VAR_CALL_RE.finditer(clean):
        enclosing = _enclosing(match.start())
        if not enclosing:
            continue
        receiver, prop = match.group(1), match.group(2)
        class_id = local_types.get((enclosing[0], receiver))
        if class_id is not None:
            target = f"{class_id}.{prop}"
            if symbols.get(target) == "function":
                _add(enclosing[0], target)
            continue
        imported = bindings.get(receiver)
        if imported is not None and imported[1] == "*":
            # `import * as db` + `db.f()`: the property names the symbol.
            _add(enclosing[0], imported[0], prop)

    return edges

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

            symbols = {
                n["id"]: n["type"]
                for n in visitor.nodes
                if n.get("type") in ("function", "class")
            }
            calls = _TSCallVisitor(rel, fid, symbols, visitor.imports)
            calls.visit(tree.root_node)
            known = {fid} | {n["id"] for n in visitor.nodes}
            call_edges = [
                e for e in calls.edges
                if e["source"] in known and e["target"] in known
            ]
            return [module_node] + visitor.nodes, visitor.edges + call_edges
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
    known = {fid} | {n["id"] for n in nodes}
    call_edges = [
        e for e in _ts_regex_call_edges(source_text, nodes, fid)
        if e["source"] in known and e["target"] in known
    ]
    return [module_node] + nodes, edges + call_edges


# ---------------------------------------------------------------------------
# Go indexer (regex)
# ---------------------------------------------------------------------------

_GO_PACKAGE_RE = re.compile(r"^package\s+(\w+)", re.MULTILINE)
# Captures the receiver variable and type alongside the name, so a method can be
# nested under the struct it belongs to and `p.method()` can be resolved.
_GO_FUNC_RE = re.compile(
    r"^func\s+(?:\(\s*(\w+)\s+\*?([\w.]+)\s*\)\s+)?(\w+)\s*\(",
    re.MULTILINE,
)
_GO_STRUCT_RE = re.compile(r"^type\s+(\w+)\s+struct\b", re.MULTILINE)
_GO_INTERFACE_RE = re.compile(r"^type\s+(\w+)\s+interface\b", re.MULTILINE)
_GO_IMPORT_BLOCK_RE = re.compile(r"import\s*\((.*?)\)", re.DOTALL)
_GO_IMPORT_SINGLE_RE = re.compile(r'^import\s+"([^"]+)"', re.MULTILINE)
_GO_IMPORT_PATH_RE = re.compile(r'"([^"]+)"')


# Go call-graph extraction. Go is regex-primary, so both phases work on text:
# definitions are collected while nodes are emitted, then each function body is
# scanned for calls that resolve to one of them.

# Comments and literals, blanked before scanning so a call-looking fragment
# inside them can never produce an edge. Backticks are Go's raw strings.
_GO_NOISE_RE = re.compile(
    r"//[^\n]*|/\*.*?\*/|`[^`]*`|\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'",
    re.S,
)
_GO_BARE_CALL_RE = re.compile(r"(?<![.\w])([A-Za-z_]\w*)\s*\(")
_GO_QUALIFIED_CALL_RE = re.compile(r"(?<![.\w])([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*\(")
_GO_COMPOSITE_LIT_RE = re.compile(r"&?\b([A-Z]\w*)\s*\{")
# `gw := &PaymentGateway{}` / `gw := PaymentGateway{}` / `var gw PaymentGateway`
_GO_SHORT_DECL_RE = re.compile(r"\b(\w+)\s*:=\s*&?([A-Za-z_]\w*)\s*\{")
_GO_VAR_DECL_RE = re.compile(r"\bvar\s+(\w+)\s+\*?([A-Za-z_]\w*)\b")

# Keywords and builtins that match the call pattern but are not calls to a
# package-level function.
_GO_NOT_CALLS = frozenset({
    "if", "for", "switch", "select", "func", "return", "go", "defer", "range",
    "case", "else", "type", "var", "const", "package", "import", "struct",
    "interface", "map", "chan", "make", "new", "len", "cap", "append", "copy",
    "delete", "panic", "recover", "print", "println", "close", "complex",
    "real", "imag", "string", "int", "int8", "int16", "int32", "int64", "uint",
    "uint8", "uint16", "uint32", "uint64", "float32", "float64", "bool", "byte",
    "rune", "error", "any", "uintptr", "min", "max", "clear",
})


def _go_blank_noise(source: str) -> str:
    """Replace comments and literals with spaces, preserving offsets."""
    return _GO_NOISE_RE.sub(lambda m: " " * len(m.group(0)), source)


def _go_body_span(source: str, start: int) -> tuple[int, int] | None:
    """Span of the function body, given *start* just after the parameter `(`.

    DECISION: the parameter list is closed first, and empty type literals are
    skipped. `func Checkout(cart map[string]interface{}) *string {` otherwise
    hands the scanner the brace of `interface{}` and yields a two-character
    body, which silently drops every call the function makes.
    """
    depth = 1
    index = start
    while index < len(source) and depth > 0:
        if source[index] == "(":
            depth += 1
        elif source[index] == ")":
            depth -= 1
        index += 1
    if depth > 0:
        return None

    while index < len(source):
        if source[index] == "{":
            # Skip only a type literal's brace, identified by the keyword right
            # before it. Skipping on emptiness instead would also swallow a
            # legitimately empty function body, handing that function the next
            # one's body and every call inside it.
            preceding = source[:index].rstrip()
            if preceding.endswith(("interface", "struct")):
                depth_t = 0
                for j in range(index, len(source)):
                    if source[j] == "{":
                        depth_t += 1
                    elif source[j] == "}":
                        depth_t -= 1
                        if depth_t == 0:
                            index = j + 1
                            break
                else:
                    return None
                continue
            break
        index += 1
    if index >= len(source) or source[index] != "{":
        return None

    open_idx, depth = index, 0
    for i in range(open_idx, len(source)):
        char = source[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return open_idx, i
    return None


def _go_call_edges(
    source: str,
    file_id: str,
    functions: list[tuple[str, str, str | None, int]],
    types: dict[str, str],
    symbols: dict[str, str],
) -> list[dict]:
    """Extract `calls` edges from one Go file.

    Args:
        source: Raw file text.
        file_id: Package node id.
        functions: (node id, receiver variable, receiver type node id, decl end)
            for every function and method in the file.
        types: Local type name -> node id, for structs and interfaces.
        symbols: node id -> type, for every definition in the file.

    Returns:
        Deduplicated `calls` edges. A call that cannot be resolved to a
        definition in this file — an imported package, a builtin, a method on a
        value of unknown type — produces nothing.
    """
    clean = _go_blank_noise(source)

    spans: list[tuple[int, int, str, str, str | None]] = []
    for nid, recv_var, recv_type_id, decl_end in functions:
        span = _go_body_span(clean, decl_end)
        if span is not None:
            spans.append((span[0], span[1], nid, recv_var, recv_type_id))

    def _enclosing(offset: int):
        """Innermost function whose body contains *offset*."""
        best = None
        for start, end, nid, recv_var, recv_type_id in spans:
            if start < offset < end and (best is None or end - start < best[0]):
                best = (end - start, nid, recv_var, recv_type_id)
        return best[1:] if best else None

    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _emit(caller: str, target: str) -> None:
        if (caller, target) in seen:
            return
        seen.add((caller, target))
        edges.append({"source": caller, "target": target, "relation": "calls"})

    # Local variables whose struct type is pinned by a composite literal or an
    # explicit `var` declaration, scoped to the function they appear in.
    local_types: dict[tuple[str, str], str] = {}
    for pattern in (_GO_SHORT_DECL_RE, _GO_VAR_DECL_RE):
        for match in pattern.finditer(clean):
            enclosing = _enclosing(match.start())
            type_id = types.get(match.group(2))
            if enclosing and type_id is not None:
                local_types[(enclosing[0], match.group(1))] = type_id

    for match in _GO_BARE_CALL_RE.finditer(clean):
        name = match.group(1)
        if name in _GO_NOT_CALLS:
            continue
        enclosing = _enclosing(match.start())
        if not enclosing:
            continue
        target = f"{file_id}.{name}"
        if symbols.get(target) == "function":
            _emit(enclosing[0], target)

    for match in _GO_QUALIFIED_CALL_RE.finditer(clean):
        receiver, method = match.group(1), match.group(2)
        enclosing = _enclosing(match.start())
        if not enclosing:
            continue
        caller, recv_var, recv_type_id = enclosing
        owner = None
        if recv_var and receiver == recv_var and recv_type_id is not None:
            owner = recv_type_id          # `p.submit()` inside a method on p
        else:
            owner = local_types.get((caller, receiver))
        if owner is None:
            # An imported package (fmt.Println) or a value of unknown type.
            continue
        target = f"{owner}.{method}"
        if symbols.get(target) == "function":
            _emit(caller, target)

    for match in _GO_COMPOSITE_LIT_RE.finditer(clean):
        enclosing = _enclosing(match.start())
        type_id = types.get(match.group(1))
        if enclosing and type_id is not None:
            _emit(enclosing[0], type_id)

    return edges

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

    def _add(name: str, ntype: str, lineno: int, parent: str = fid) -> str:
        nid = f"{parent}.{name}"
        nodes.append({
            "id": nid,
            "label": name,
            "type": ntype,
            "description": "",
            "source_file": str(rel),
            "source_location": f"L{lineno}",
            "file_type": "code",
        })
        edges.append({"source": parent, "target": nid, "relation": "contains"})
        return nid

    # Types first: a method's receiver must already exist as a node so the
    # method can be nested under it rather than under the package.
    types: dict[str, str] = {}
    for m in _GO_STRUCT_RE.finditer(source):
        types[m.group(1)] = _add(m.group(1), "class", _lineno(source, m.start()))
    for m in _GO_INTERFACE_RE.finditer(source):
        types[m.group(1)] = _add(m.group(1), "interface", _lineno(source, m.start()))

    # (function node id, receiver variable, receiver type node id) per definition,
    # handed to the call scanner below.
    functions: list[tuple[str, str, str | None, int]] = []
    for m in _GO_FUNC_RE.finditer(source):
        recv_var, recv_type, name = m.group(1), m.group(2), m.group(3)
        owner = types.get(recv_type or "", fid)
        nid = _add(name, "function", _lineno(source, m.start()), parent=owner)
        functions.append((nid, recv_var or "", owner if recv_type else None, m.end()))

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

    known = {n["id"] for n in nodes}
    symbols = {n["id"]: n["type"] for n in nodes}
    edges.extend(
        e for e in _go_call_edges(source, fid, functions, types, symbols)
        if e["source"] in known and e["target"] in known
    )

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


# ---------------------------------------------------------------------------
# Java call-graph extraction
#
# Java is the easiest of the four languages to resolve: every variable and field
# carries a declared type, so nothing has to be inferred from an initialiser the
# way `x = ClassName()` does in Python. The work is mapping a declared type name
# back to a class node in this file, and refusing to guess when it is not one.
# ---------------------------------------------------------------------------

_JAVA_TYPE_DECLS = frozenset({
    "class_declaration", "interface_declaration", "enum_declaration",
    "record_declaration", "annotation_type_declaration",
})
_JAVA_MEMBER_DECLS = frozenset({
    "method_declaration", "constructor_declaration", "compact_constructor_declaration",
})


def _java_bare_type(text: str) -> str:
    """Strip generics, arrays and package qualifiers from a declared type.

    `Map<String, Object>` -> `Map`, `com.example.Gateway[]` -> `Gateway`. The
    result is what a class node in this file would be named.
    """
    name = text.split("<", 1)[0].strip()
    name = name.replace("[", " ").replace("]", " ").strip()
    return name.rsplit(".", 1)[-1].strip()


class _JavaCallVisitor(_BaseVisitor):
    """Pass 2 — resolve Java method invocations to definitions in this file.

    Rebuilds the same scope stack as _JavaVisitor so a caller's id always
    matches the node id emitted there. Only invocations that resolve to a
    declaration in this file produce an edge; the JDK, Spring, and any field
    whose type is declared elsewhere resolve to nothing and are skipped.
    """

    def __init__(self, rel_path: Path, file_id: str, symbols: dict[str, str]) -> None:
        super().__init__(rel_path, file_id)
        self.symbols = symbols
        self.module_id = file_id
        self.bases: dict[str, list[str]] = {}
        self._class_stack: list[str] = []
        # Per-method frame: variable name -> class node id it is declared as.
        self._locals: list[dict[str, str]] = [{}]
        # Per-class: field name -> class node id, from the declared field type.
        self._fields: dict[str, dict[str, str]] = {}
        self._seen: set[tuple[str, str]] = set()

    # -- lookups ----------------------------------------------------------

    def _class_node(self, type_name: str) -> str | None:
        """Class node in this file for a declared type name, or None."""
        candidate = f"{self.module_id}.{_java_bare_type(type_name)}"
        return candidate if self.symbols.get(candidate) in (
            "class", "interface", "enum", "record"
        ) else None

    def _lookup_member(self, class_id: str, name: str) -> str | None:
        """Resolve *name* on *class_id*, following superclasses in this file."""
        seen: set[str] = set()
        queue = [class_id]
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            candidate = f"{current}.{name}"
            if self.symbols.get(candidate) in ("method", "constructor"):
                return candidate
            for base in self.bases.get(current, []):
                base_id = self._class_node(base)
                if base_id is not None:
                    queue.append(base_id)
        return None

    def _first_base(self, class_id: str) -> str | None:
        """Node id of the class's superclass, when it is declared in this file."""
        for base in self.bases.get(class_id, []):
            base_id = self._class_node(base)
            if base_id is not None:
                return base_id
        return None

    def _add(self, target: str) -> None:
        key = (self._parent, target)
        if key in self._seen:
            return
        self._seen.add(key)
        self.edges.append(
            {"source": self._parent, "target": target, "relation": "calls"}
        )

    # -- traversal --------------------------------------------------------

    def _visit_type(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            self._recurse(node)
            return
        nid = f"{self._parent}.{name}"
        self.bases[nid] = _JavaVisitor._type_names(node) if any(
            c.type in ("superclass", "extends_interfaces", "super_interfaces")
            for c in node.children
        ) else []
        # Field types are needed before any method body is walked, because a
        # method declared first may use a field declared last.
        self._fields.setdefault(nid, {}).update(self._collect_fields(node, nid))
        self._scope.append(nid)
        self._class_stack.append(nid)
        self._recurse(node)
        self._class_stack.pop()
        self._scope.pop()

    visit_class_declaration = _visit_type
    visit_interface_declaration = _visit_type
    visit_enum_declaration = _visit_type
    visit_record_declaration = _visit_type
    visit_annotation_type_declaration = _visit_type

    def _collect_fields(self, class_node, class_id: str) -> dict[str, str]:
        """Map each field of a class body to the class node its type names."""
        found: dict[str, str] = {}
        body = class_node.child_by_field_name("body")
        for member in (body.children if body is not None else ()):
            if member.type != "field_declaration":
                continue
            type_node = member.child_by_field_name("type")
            if type_node is None:
                continue
            target = self._class_node(self._text(type_node))
            if target is None:
                continue
            for child in member.children:
                if child.type == "variable_declarator":
                    field_name = self._text(child.child_by_field_name("name"))
                    if field_name:
                        found[field_name] = target
        return found

    def _visit_member(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            return
        nid = f"{self._parent}.{name}"
        self._scope.append(nid)
        self._locals.append({})
        self._recurse(node)
        self._locals.pop()
        self._scope.pop()

    visit_method_declaration = _visit_member
    visit_constructor_declaration = _visit_member
    visit_compact_constructor_declaration = _visit_member

    def visit_local_variable_declaration(self, node) -> None:
        type_node = node.child_by_field_name("type")
        target = self._class_node(self._text(type_node)) if type_node else None
        for child in node.children:
            if child.type != "variable_declarator":
                continue
            var_name = self._text(child.child_by_field_name("name"))
            if var_name:
                if target is not None:
                    self._locals[-1][var_name] = target
                else:
                    self._locals[-1].pop(var_name, None)
        self._recurse(node)

    def visit_method_invocation(self, node) -> None:
        target = self._resolve_invocation(node)
        if target is not None:
            self._add(target)
        self._recurse(node)

    def visit_object_creation_expression(self, node) -> None:
        type_node = node.child_by_field_name("type")
        target = self._class_node(self._text(type_node)) if type_node else None
        if target is not None:
            self._add(target)
        self._recurse(node)

    def _resolve_invocation(self, node) -> str | None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = self._text(name_node)
        receiver = node.child_by_field_name("object")

        # `validateCard(card)` and `this.validateCard(card)` are the same thing.
        if receiver is None or receiver.type == "this":
            return (
                self._lookup_member(self._class_stack[-1], name)
                if self._class_stack else None
            )

        if receiver.type == "super":
            base = self._first_base(self._class_stack[-1]) if self._class_stack else None
            return self._lookup_member(base, name) if base else None

        if receiver.type == "identifier":
            receiver_name = self._text(receiver)
            # A local variable shadows a field, which shadows a class name.
            class_id = self._locals[-1].get(receiver_name)
            if class_id is None and self._class_stack:
                class_id = self._fields.get(self._class_stack[-1], {}).get(receiver_name)
            if class_id is None:
                # `Child.helper()` — a static call on a class declared here.
                class_id = self._class_node(receiver_name)
            if class_id is not None:
                return self._lookup_member(class_id, name)

        # field_access (System.out.println), array access, chained calls:
        # not resolvable to a node in this file.
        return None

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


# --- Java regex fallback ----------------------------------------------------

# Text blocks first, then block comments, line comments, strings and char
# literals. Blanked before scanning so a call-looking fragment inside one can
# never produce an edge.
_JAVA_NOISE_RE = re.compile(
    r'"""(?:[^"\\]|\\.|"(?!""))*"""|/\*.*?\*/|//[^\n]*'
    r"|\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'",
    re.S,
)
_JAVA_NEW_RE = re.compile(r"\bnew\s+([A-Z]\w*)\s*[(<]")
_JAVA_THIS_CALL_RE = re.compile(r"\bthis\.(\w+)\s*\(")
_JAVA_SUPER_CALL_RE = re.compile(r"\bsuper\.(\w+)\s*\(")
_JAVA_QUALIFIED_RE = re.compile(r"(?<![.\w])(\w+)\.(\w+)\s*\(")
_JAVA_BARE_CALL_RE = re.compile(r"(?<![.\w])(\w+)\s*\(")
# A declared local: `Gateway gw = ...` or `Gateway gw;`.
_JAVA_LOCAL_DECL_RE = re.compile(
    r"(?<![.\w])([A-Z]\w*)(?:<[^;=()]*>)?(?:\[\s*\])?\s+(\w+)\s*[=;]"
)
# `class Child extends Base` — needed to resolve `super.m()` without an AST.
_JAVA_EXTENDS_RE = re.compile(
    r"\b(?:class|interface)\s+(\w+)[^{;]*?\bextends\s+([A-Z]\w*)"
)
# Words that take a parenthesis but are control flow, not a call.
_JAVA_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "catch", "return", "new", "throw", "throws",
    "synchronized", "super", "this", "try", "do", "else", "assert",
    "instanceof", "case", "yield", "record", "class", "interface", "enum",
    "void", "public", "private", "protected", "static", "final", "native",
})


def _java_blank_noise(source: str) -> str:
    """Replace comments and literals with spaces, preserving every offset."""
    return _JAVA_NOISE_RE.sub(lambda m: " " * len(m.group(0)), source)


def _java_regex_call_edges(
    source: str, nodes: list[dict], file_id: str
) -> list[dict]:
    """Extract `calls` edges from Java source without tree-sitter.

    Resolves the same shapes as the AST branch except fields injected by DI:
    the regex parser emits no field nodes, so a field's declared type is not
    available. That is a missing edge, never a wrong one.

    Args:
        source: Raw file text.
        nodes: Nodes already produced by the regex parser, used as the symbol
            table so both branches resolve against identical ids.
        file_id: Module node id.

    Returns:
        Deduplicated `calls` edges between nodes that exist in the graph.
    """
    kinds = {n["id"]: n.get("type", "") for n in nodes}
    classes = {
        nid.rsplit(".", 1)[-1]: nid
        for nid, kind in kinds.items()
        if kind in ("class", "interface", "enum", "record")
    }
    members = {nid for nid, kind in kinds.items() if kind in ("method", "constructor")}
    if not members:
        return []

    clean = _java_blank_noise(source)

    # Method bodies, anchored on the declaration line the parser recorded.
    spans: list[tuple[int, int, str, str]] = []
    for node in nodes:
        if node.get("type") not in ("method", "constructor"):
            continue
        location = str(node.get("source_location", ""))
        if not location.startswith("L") or not location[1:].isdigit():
            continue
        start = _ts_decl_offset(clean, int(location[1:]), node["label"])
        if start is None:
            continue
        span = _ts_body_span(clean, start)
        if span is None or span[1] <= span[0]:
            continue
        spans.append((span[0], span[1], node["id"], node["id"].rsplit(".", 1)[0]))

    def _enclosing(offset: int) -> tuple[str, str] | None:
        best: tuple[int, str, str] | None = None
        for begin, end, nid, class_id in spans:
            if begin < offset < end:
                width = end - begin
                if best is None or width < best[0]:
                    best = (width, nid, class_id)
        return (best[1], best[2]) if best else None

    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(caller: str, target: str) -> None:
        key = (caller, target)
        if key in seen or target not in kinds:
            return
        seen.add(key)
        edges.append({"source": caller, "target": target, "relation": "calls"})

    def _member_of(class_id: str, name: str) -> str | None:
        candidate = f"{class_id}.{name}"
        return candidate if candidate in members else None

    # Locals declared inside each method body, scoped to that method.
    local_types: dict[tuple[str, str], str] = {}
    for match in _JAVA_LOCAL_DECL_RE.finditer(clean):
        enclosing = _enclosing(match.start())
        if not enclosing:
            continue
        class_id = classes.get(match.group(1))
        if class_id is not None:
            local_types[(enclosing[0], match.group(2))] = class_id

    for match in _JAVA_NEW_RE.finditer(clean):
        enclosing = _enclosing(match.start())
        target = classes.get(match.group(1))
        if enclosing and target:
            _add(enclosing[0], target)

    for match in _JAVA_THIS_CALL_RE.finditer(clean):
        enclosing = _enclosing(match.start())
        if not enclosing:
            continue
        member = _member_of(enclosing[1], match.group(1))
        if member:
            _add(enclosing[0], member)

    # `class Child extends Base` lets super.m() resolve the same way the AST
    # branch does, instead of silently dropping every inherited call.
    superclasses: dict[str, str] = {}
    for match in _JAVA_EXTENDS_RE.finditer(clean):
        child_id = classes.get(match.group(1))
        parent_id = classes.get(match.group(2))
        if child_id and parent_id:
            superclasses[child_id] = parent_id

    for match in _JAVA_SUPER_CALL_RE.finditer(clean):
        enclosing = _enclosing(match.start())
        if not enclosing:
            continue
        parent = superclasses.get(enclosing[1])
        if parent is None:
            continue
        member = _member_of(parent, match.group(1))
        if member:
            _add(enclosing[0], member)

    for match in _JAVA_QUALIFIED_RE.finditer(clean):
        receiver, name = match.group(1), match.group(2)
        if receiver in ("this", "super"):
            continue
        enclosing = _enclosing(match.start())
        if not enclosing:
            continue
        # A local variable shadows a class name, exactly as in the AST branch.
        class_id = local_types.get((enclosing[0], receiver)) or classes.get(receiver)
        if class_id is None:
            continue
        member = _member_of(class_id, name)
        if member:
            _add(enclosing[0], member)

    for match in _JAVA_BARE_CALL_RE.finditer(clean):
        name = match.group(1)
        if name in _JAVA_KEYWORDS:
            continue
        enclosing = _enclosing(match.start())
        if not enclosing:
            continue
        member = _member_of(enclosing[1], name)
        if member:
            _add(enclosing[0], member)

    return edges

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

            symbols = {n["id"]: n["type"] for n in visitor.nodes}
            calls = _JavaCallVisitor(rel, fid, symbols)
            calls.visit(tree.root_node)
            known = {fid} | set(symbols)
            call_edges = [
                e for e in calls.edges
                if e["source"] in known and e["target"] in known
            ]
            return [module_node] + visitor.nodes, visitor.edges + call_edges
        except Exception as exc:
            import click  # noqa: PLC0415

            click.echo(f"  Warning: tree-sitter failed on {path}, "
                       f"falling back to regex: {exc}", err=True)

    nodes, edges = _index_java_regex(source_text, rel, fid)
    known = {fid} | {n["id"] for n in nodes}
    call_edges = [
        e for e in _java_regex_call_edges(source_text, nodes, fid)
        if e["source"] in known and e["target"] in known
    ]
    return [module_node] + nodes, edges + call_edges


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
        # `impl<'a> Walker<'a>` must attach to the `Walker` struct node. Keeping
        # the generics made `Walker<'a>.next` an id no other edge could reach.
        type_name = _rust_bare_type(self._text(type_node))
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



_RUST_TYPE_DECLS = frozenset({"struct", "enum", "trait"})
_RUST_TYPE_CONTAINERS = frozenset({
    "struct_item", "enum_item", "trait_item", "union_item",
})
_RUST_REF_PREFIXES = ("&", "mut ", "dyn ", "impl ")
_RUST_LIFETIME_RE = re.compile(r"^'\w+\s*")


def _rust_bare_type(text: str) -> str:
    """Reduce a type expression to the bare name a node id can be built from.

    `&'a mut Gateway` -> `Gateway`, `crate::pay::Gateway` -> `Gateway`,
    `Vec<Gateway>` -> `Vec`. References, mutability and lifetimes carry
    ownership, not identity, so they are stripped; the generic *head* is kept
    because that is the type a method would be looked up on.
    """
    name = text.strip()
    while True:
        stripped = name
        for prefix in _RUST_REF_PREFIXES:
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):].strip()
        stripped = _RUST_LIFETIME_RE.sub("", stripped)
        if stripped == name:
            break
        name = stripped
    name = name.split("<", 1)[0].strip()
    return name.rsplit("::", 1)[-1].strip()


class _RustCallVisitor(_BaseVisitor):
    """Pass 2 — resolve Rust calls to definitions in this file.

    Rebuilds the same scope stack as _RustVisitor, so a caller's id always
    matches the node id emitted there. A prescan runs first to record trait
    implementations, struct field types and function return types, because a
    method may be called from a function declared above the `impl` block that
    defines it.

    Macros never produce an edge: `println!` is a `macro_invocation`, a
    different node type from `call_expression`, so they cost nothing to skip.
    """

    def __init__(self, rel_path: Path, file_id: str, symbols: dict[str, str]) -> None:
        super().__init__(rel_path, file_id)
        self.symbols = symbols
        self.module_id = file_id
        # Type node id -> trait names named in `impl Trait for Type`.
        self.traits: dict[str, list[str]] = {}
        # Type node id -> field name -> type node id, from declared field types.
        self.fields: dict[str, dict[str, str]] = {}
        # Function node id -> type node id it returns, when that is knowable.
        self.returns: dict[str, str] = {}
        self._impl_stack: list[str] = []
        self._locals: list[dict[str, str]] = [{}]
        # Scopes a bare `foo()` may resolve against: the file and its modules,
        # never an impl block. Rust has no implicit receiver, so inside
        # `impl Gateway` a bare `submit()` is a free function, not `self.submit`.
        self._value_scopes: list[str] = [file_id]
        self._seen: set[tuple[str, str]] = set()

    # -- lookups ----------------------------------------------------------

    def _type_node(self, type_text: str) -> str | None:
        """Node id of a type declared in this file, or None."""
        bare = _rust_bare_type(type_text)
        if not bare:
            return None
        for scope in reversed(self._value_scopes):
            candidate = f"{scope}.{bare}"
            if self.symbols.get(candidate) in _RUST_TYPE_DECLS:
                return candidate
        return None

    def _lookup_method(self, type_id: str | None, name: str) -> str | None:
        """Resolve *name* on *type_id*, then on the traits it implements.

        A trait method only resolves to the trait's own node when the type does
        not define it, which is what `impl Trait for Type` means at runtime.
        """
        if not type_id or not name:
            return None
        seen: set[str] = set()
        queue = [type_id]
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            candidate = f"{current}.{name}"
            if self.symbols.get(candidate) == "function":
                return candidate
            for trait in self.traits.get(current, []):
                trait_id = self._type_node(trait)
                if trait_id is not None:
                    queue.append(trait_id)
        return None

    def _lookup_function(self, name: str) -> str | None:
        """Resolve a bare `name()` against the enclosing module scopes."""
        for scope in reversed(self._value_scopes):
            candidate = f"{scope}.{name}"
            if self.symbols.get(candidate) == "function":
                return candidate
        return None

    def _type_of_expr(self, node) -> str | None:
        """Type node id of a receiver expression, when it is knowable.

        Resolves `self`, a local binding or parameter, and `self.field`.
        A chained call, a literal or anything else resolves to nothing.
        """
        if node is None:
            return None
        if node.type == "self":
            return self._impl_stack[-1] if self._impl_stack else None
        if node.type == "identifier":
            return self._locals[-1].get(self._text(node))
        if node.type == "field_expression":
            owner = self._type_of_expr(node.child_by_field_name("value"))
            field = self._text(node.child_by_field_name("field"))
            return self.fields.get(owner, {}).get(field) if owner else None
        return None

    def _resolve_call(self, fn) -> str | None:
        if fn is None:
            return None
        if fn.type == "identifier":
            return self._lookup_function(self._text(fn))
        if fn.type == "scoped_identifier":
            path = fn.child_by_field_name("path")
            name = self._text(fn.child_by_field_name("name"))
            # `std::mem::swap` nests a scoped_identifier; only a single
            # segment can name a type declared in this file.
            if path is None or path.type != "identifier" or not name:
                return None
            path_text = self._text(path)
            if path_text == "Self":
                owner = self._impl_stack[-1] if self._impl_stack else None
            else:
                owner = self._type_node(path_text)
            return self._lookup_method(owner, name)
        if fn.type == "field_expression":
            owner = self._type_of_expr(fn.child_by_field_name("value"))
            return self._lookup_method(owner, self._text(fn.child_by_field_name("field")))
        return None

    def _add(self, target: str) -> None:
        key = (self._parent, target)
        if key in self._seen:
            return
        self._seen.add(key)
        self.edges.append(
            {"source": self._parent, "target": target, "relation": "calls"}
        )

    # -- prescan ----------------------------------------------------------

    def prescan(self, node, scope: str | None = None, impl_type: str | None = None) -> None:
        """Record traits, field types and return types before resolving calls."""
        scope = self.module_id if scope is None else scope
        for child in node.children:
            if child.type == "mod_item":
                name = self._ts_name(child)
                nested = f"{scope}.{name}" if name else scope
                if name:
                    self._value_scopes.append(nested)
                self.prescan(child, nested, None)
                if name:
                    self._value_scopes.pop()
            elif child.type in _RUST_TYPE_CONTAINERS:
                name = self._ts_name(child)
                if not name:
                    continue
                nested = f"{scope}.{name}"
                self._collect_fields(child, nested)
                self.prescan(child, nested, nested)
            elif child.type == "impl_item":
                type_name = _rust_bare_type(self._text(child.child_by_field_name("type")))
                if not type_name:
                    continue
                nested = f"{scope}.{type_name}"
                trait_node = child.child_by_field_name("trait")
                if trait_node is not None:
                    self.traits.setdefault(nested, []).append(self._text(trait_node))
                self.prescan(child, nested, nested)
            elif child.type in ("function_item", "function_signature_item"):
                name = self._ts_name(child)
                if name and impl_type:
                    self._record_return(f"{scope}.{name}", child, impl_type)
            else:
                self.prescan(child, scope, impl_type)

    def _record_return(self, fn_id: str, node, impl_type: str) -> None:
        """Bind an associated function to the type it returns, when declared.

        Only an exact `Self` or the impl type itself counts. `Option<Self>` is
        deliberately excluded: the value is an Option, not the type.
        """
        declared = self._text(node.child_by_field_name("return_type")).strip()
        if not declared:
            return
        if declared == "Self" or declared == impl_type.rsplit(".", 1)[-1]:
            self.returns[fn_id] = impl_type

    def _collect_fields(self, node, type_id: str) -> None:
        """Map a struct's field names to the types they declare."""
        for child in node.children:
            if child.type != "field_declaration_list":
                continue
            for field in child.children:
                if field.type != "field_declaration":
                    continue
                name = self._text(field.child_by_field_name("name"))
                declared = self._text(field.child_by_field_name("type"))
                if name and declared:
                    self.fields.setdefault(type_id, {})[name] = declared

    def resolve_field_types(self) -> None:
        """Rewrite declared field types to node ids, dropping foreign ones."""
        for type_id, fields in self.fields.items():
            self.fields[type_id] = {
                name: resolved
                for name, declared in fields.items()
                if (resolved := self._type_node(declared)) is not None
            }

    # -- traversal --------------------------------------------------------

    def visit_mod_item(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            self._recurse(node)
            return
        nid = f"{self._parent}.{name}"
        self._value_scopes.append(nid)
        self._descend(node, nid)
        self._value_scopes.pop()

    def _visit_type_container(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            self._recurse(node)
            return
        # A trait's default method bodies live here, so descend rather than skip.
        self._impl_stack.append(f"{self._parent}.{name}")
        self._descend(node, f"{self._parent}.{name}")
        self._impl_stack.pop()

    visit_struct_item = _visit_type_container
    visit_enum_item = _visit_type_container
    visit_trait_item = _visit_type_container
    visit_union_item = _visit_type_container

    def visit_impl_item(self, node) -> None:
        type_name = _rust_bare_type(self._text(node.child_by_field_name("type")))
        if not type_name:
            self._recurse(node)
            return
        nid = f"{self._parent}.{type_name}"
        self._impl_stack.append(nid)
        self._descend(node, nid)
        self._impl_stack.pop()

    def _visit_fn(self, node) -> None:
        name = self._ts_name(node)
        if not name:
            return
        self._locals.append(self._param_types(node))
        self._descend(node, f"{self._parent}.{name}")
        self._locals.pop()

    visit_function_item = _visit_fn
    visit_function_signature_item = _visit_fn

    def _param_types(self, node) -> dict[str, str]:
        """Parameter name -> type node id. Declared, so nothing is inferred."""
        frame: dict[str, str] = {}
        params = node.child_by_field_name("parameters")
        if params is None:
            return frame
        for param in params.children:
            if param.type != "parameter":
                continue
            name = self._text(param.child_by_field_name("pattern"))
            declared = self._type_node(self._text(param.child_by_field_name("type")))
            if name and declared:
                frame[name] = declared
        return frame

    def visit_let_declaration(self, node) -> None:
        pattern = node.child_by_field_name("pattern")
        if pattern is not None and pattern.type == "identifier":
            bound = self._binding_type(node)
            if bound is not None:
                self._locals[-1][self._text(pattern)] = bound
        self._recurse(node)

    def _binding_type(self, node) -> str | None:
        """Type of a `let`, from the annotation, a struct literal, or a
        constructor whose return type is declared."""
        annotated = node.child_by_field_name("type")
        if annotated is not None:
            return self._type_node(self._text(annotated))
        value = node.child_by_field_name("value")
        if value is None:
            return None
        if value.type == "struct_expression":
            return self._type_node(self._text(value.child_by_field_name("name")))
        if value.type == "call_expression":
            target = self._resolve_call(value.child_by_field_name("function"))
            return self.returns.get(target) if target else None
        return None

    def visit_call_expression(self, node) -> None:
        target = self._resolve_call(node.child_by_field_name("function"))
        if target is not None:
            self._add(target)
        self._recurse(node)

    def visit_macro_invocation(self, node) -> None:
        """`println!`, `vec!`, `format!` — never a call edge."""

    def visit_use_declaration(self, node) -> None:
        """A `use` path is not a call."""


def _rust_call_edges(tree, rel_path: Path, file_id: str,
                     symbols: dict[str, str]) -> list[dict]:
    """Run both passes and return the resolved `calls` edges."""
    visitor = _RustCallVisitor(rel_path, file_id, symbols)
    visitor.prescan(tree.root_node)
    visitor.resolve_field_types()
    visitor.visit(tree.root_node)
    return visitor.edges


_RUST_USE_RE = re.compile(r"(?m)^\s*use\s+([^;]+);")
_RUST_ITEM_RE = re.compile(
    r"(?m)^[ \t]*(pub(?:\([^)]*\))?\s+)?(mod|struct|enum|trait|union)\s+(\w+)"
)
_RUST_FN_RE = re.compile(
    r"(?m)^[ \t]*(pub(?:\([^)]*\))?\s+)?(?:async\s+|const\s+|unsafe\s+|extern\s+\"[^\"]*\"\s+)*"
    r"fn\s+(\w+)(<[^>{]*>)?\s*(\([^\n]*?\))?"
)
_RUST_MACRO_RE = re.compile(r"(?m)^[ \t]*macro_rules!\s*(\w+)")


def _rust_blank_noise(source: str) -> str:
    """Blank comments, strings and char literals, preserving every offset.

    Lifetimes are the trap: `&'a str` opens what looks like a char literal that
    never closes. A quote only starts a char literal when a closing quote
    follows within the width of an escape sequence.
    """
    out = list(source)
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            while i < n and source[i] != "\n":
                out[i] = " "
                i += 1
        elif ch == "/" and i + 1 < n and source[i + 1] == "*":
            # Rust block comments nest, unlike C's.
            depth, start = 0, i
            while i < n:
                if source.startswith("/*", i):
                    depth += 1
                    i += 2
                elif source.startswith("*/", i):
                    depth -= 1
                    i += 2
                    if depth == 0:
                        break
                else:
                    i += 1
            for k in range(start, min(i, n)):
                if out[k] != "\n":
                    out[k] = " "
        elif ch == "r" and i + 1 < n and source[i + 1] in '#"':
            j = i + 1
            hashes = 0
            while j < n and source[j] == "#":
                hashes += 1
                j += 1
            if j >= n or source[j] != '"':
                i += 1
                continue
            close = '"' + "#" * hashes
            end = source.find(close, j + 1)
            end = n if end == -1 else end + len(close)
            for k in range(i, end):
                if out[k] != "\n":
                    out[k] = " "
            i = end
        elif ch == '"':
            j = i + 1
            while j < n and source[j] != '"':
                j += 2 if source[j] == "\\" else 1
            for k in range(i, min(j + 1, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 1
        elif ch == "'":
            j = i + 1
            if j < n and source[j] == "\\":
                j += 2
            elif j < n:
                j += 1
            if j < n and source[j] == "'":
                for k in range(i, j + 1):
                    out[k] = " "
                i = j + 1
            else:
                i += 1  # a lifetime, not a literal
        else:
            i += 1
    return "".join(out)


_RUST_MACRO_CALL_RE = re.compile(r"\b(\w+)\s*!\s*[({\[]")
_RUST_MACRO_DEF_RE = re.compile(r"\bmacro_rules!\s*\w+\s*[({\[]")
_RUST_CLOSERS = {"(": ")", "{": "}", "[": "]"}


def _rust_blank_macros(blanked: str) -> str:
    """Blank the token tree of every macro invocation, preserving offsets.

    tree-sitter parses `assert_eq!(build(), 1)` as a `macro_invocation` whose
    arguments are an unstructured token tree, so it yields no call at all. The
    regex branch would otherwise read those tokens as calls and the two
    branches would disagree on every test module in a codebase.
    """
    out = list(blanked)
    # A macro_rules body is a template, not code: its `$name` placeholders
    # would otherwise be read as types and its `fn` items as definitions.
    for m in list(_RUST_MACRO_DEF_RE.finditer(blanked)) + list(
            _RUST_MACRO_CALL_RE.finditer(blanked)):
        opener = blanked[m.end() - 1]
        closer = _RUST_CLOSERS[opener]
        depth, i, n = 0, m.end() - 1, len(blanked)
        while i < n:
            if blanked[i] == opener:
                depth += 1
            elif blanked[i] == closer:
                depth -= 1
                if depth == 0:
                    break
            i += 1
        for k in range(m.end(), min(i, n)):
            if out[k] != "\n":
                out[k] = " "
    return "".join(out)


def _rust_block_span(blanked: str, start: int) -> tuple[int, int] | None:
    """Span of the `{...}` body that follows *start*, or None if there is none.

    Parameter parentheses close first: `fn f(cart: &HashMap<String, f64>)` has
    no brace, but a `-> impl Fn() {` signature would otherwise be misread. A
    `;` reached before any `{` means a declaration without a body — a trait
    method signature, whose calls belong to nobody.
    """
    i, n = start, len(blanked)
    depth = 0
    while i < n:
        ch = blanked[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth <= 0 and ch == ";":
            return None
        elif depth <= 0 and ch == "{":
            break
        i += 1
    if i >= n:
        return None
    body, j = 1, i + 1
    while j < n and body:
        if blanked[j] == "{":
            body += 1
        elif blanked[j] == "}":
            body -= 1
        j += 1
    return i + 1, j - 1


_RUST_IMPL_KEYWORD_RE = re.compile(r"(?m)^[ \t]*(?:unsafe\s+)?impl\b")


def _rust_impl_head(blanked: str, start: int) -> tuple[str | None, str, int]:
    """Parse an impl header into `(trait, type, offset_after_head)`.

    Scanning beats a regex here: `impl From<Vec<MailAddr>> for MailAddrList`
    nests its generics, and a `<[^>]*>` group stops at the first `>` and reads
    the whole header as `impl From`.
    """
    n = len(blanked)
    i = start
    while i < n and blanked[i].isspace():
        i += 1
    if i < n and blanked[i] == "<":  # impl<'a, T: Into<String>>
        depth = 0
        while i < n:
            if blanked[i] == "<":
                depth += 1
            elif blanked[i] == ">":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
    head_start, depth = i, 0
    while i < n:
        ch = blanked[i]
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        elif depth <= 0 and (ch in "{;" or (
                ch.isspace() and blanked.startswith("where", i + 1)
                and not blanked[i + 6:i + 7].isalnum())):
            break
        i += 1
    head, depth, split = blanked[head_start:i], 0, -1
    for k in range(len(head) - 4):
        ch = head[k]
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        elif depth <= 0 and head[k:k + 5] == " for " and split < 0:
            split = k
    if split >= 0:
        return head[:split].strip(), _rust_bare_type(head[split + 5:]), i
    return None, _rust_bare_type(head), i


def _rust_impl_spans(blanked: str) -> list[tuple[int, int, str, str | None]]:
    """`(body_start, body_end, type_name, trait_name)` for every impl block."""
    spans: list[tuple[int, int, str, str | None]] = []
    for m in _RUST_IMPL_KEYWORD_RE.finditer(blanked):
        trait, type_name, after = _rust_impl_head(blanked, m.end())
        span = _rust_block_span(blanked, after)
        if span is not None and type_name:
            spans.append((span[0], span[1], type_name, trait))
    return spans


_RUST_TRAIT_HEAD_RE = re.compile(
    r"(?m)^[ \t]*(?:pub(?:\([^)]*\))?\s+)?(?:unsafe\s+)?trait\s+(\w+)"
)
_RUST_MOD_HEAD_RE = re.compile(r"(?m)^[ \t]*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)")


def _rust_scope_spans(blanked: str) -> list[tuple[int, int, str, str]]:
    """Every block that contributes a segment to the node ids inside it.

    `(body_start, body_end, name, kind)` for `mod`, `impl` and `trait` blocks.
    The regex branch has to reproduce the nesting tree-sitter gets for free;
    without it a `#[cfg(test)] mod tests` flattens onto the file and its
    functions collide with the ones they are testing.
    """
    spans: list[tuple[int, int, str, str]] = []
    for regex, kind in ((_RUST_MOD_HEAD_RE, "mod"), (_RUST_TRAIT_HEAD_RE, "trait")):
        for m in regex.finditer(blanked):
            span = _rust_block_span(blanked, m.end())
            if span is not None:
                spans.append((span[0], span[1], m.group(1), kind))
    spans += [(start, end, type_name, "impl")
              for start, end, type_name, _trait in _rust_impl_spans(blanked)]
    return sorted(spans)


def _rust_prefix(spans, offset: int, file_id: str, kinds: frozenset[str] | None = None) -> str:
    """Node-id prefix for a declaration at *offset*, innermost block last."""
    parts = [name for start, end, name, kind in spans
             if start <= offset < end and (kinds is None or kind in kinds)]
    return ".".join([file_id, *parts])


def _rust_module_scopes(spans, offset: int, file_id: str) -> list[str]:
    """Scopes a bare `foo()` may resolve against, innermost first.

    Only modules: Rust has no implicit receiver, so inside `impl Gateway` a
    bare `submit()` is a free function, never `self.submit`.
    """
    parts = [name for start, end, name, kind in spans
             if start <= offset < end and kind == "mod"]
    return [".".join([file_id, *parts[:i]]) for i in range(len(parts), -1, -1)]


def _rust_impl_traits(blanked: str, spans, file_id: str) -> dict[str, list[str]]:
    """Type node id -> trait names it implements, from `impl Trait for Type`."""
    traits: dict[str, list[str]] = {}
    for start, _end, type_name, trait in _rust_impl_spans(blanked):
        if not trait:
            continue
        prefix = _rust_prefix(spans, start, file_id, frozenset({"mod"}))
        traits.setdefault(f"{prefix}.{type_name}", []).append(trait)
    return traits


def _index_rust_regex(source: str, rel_path: Path, file_id: str) -> tuple[list[dict], list[dict]]:
    """Regex fallback for Rust."""
    nodes: list[dict] = []
    edges: list[dict] = []

    # tree-sitter sees a macro body as an opaque token tree and never reads a
    # comment, so neither declares symbols. Blanking both here is what keeps
    # the two branches emitting the same node ids. Offsets are preserved, so
    # every line number still points at the real source.
    blanked_source = _rust_blank_macros(_rust_blank_noise(source))
    scopes = _rust_scope_spans(blanked_source)

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

    for m in _RUST_USE_RE.finditer(blanked_source):
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

    # Items nest under the mod, impl or trait block that contains them, which
    # is what tree-sitter produces; a flat file id makes the two branches
    # disagree on every id inside a module.
    kinds = {"mod": "module", "struct": "struct", "enum": "enum",
             "trait": "trait", "union": "struct"}
    for m in _RUST_ITEM_RE.finditer(blanked_source):
        vis = "pub" if m.group(1) else "private"
        # A block never contains its own header, so a mod's id is built from
        # the scopes around it, not including itself.
        _add(m.group(3), kinds[m.group(2)], _lineno(source, m.start()),
             parent=_rust_prefix(scopes, m.start(), file_id), visibility=vis)

    for m in _RUST_MACRO_RE.finditer(blanked_source):
        _add(m.group(1), "macro", _lineno(source, m.start()),
             parent=_rust_prefix(scopes, m.start(), file_id))

    # `impl Trait for Type` -> Type implements Trait
    for start, _end, type_name, trait in _rust_impl_spans(blanked_source):
        if trait:
            prefix = _rust_prefix(scopes, start, file_id, frozenset({"mod"}))
            edges.append({"source": f"{prefix}.{type_name}", "target": trait,
                          "relation": "implements"})

    _all, top_level = _rust_fn_records(blanked_source)
    for m, _params, _declared, _body in top_level:
        vis = "pub" if m.group(1) else "private"
        name, generics, params = m.group(2), m.group(3) or "", m.group(4) or ""
        signature = f"{name}{generics}{params}"
        _add(name, "function", _lineno(source, m.start()),
             parent=_rust_prefix(scopes, m.start(), file_id),
             visibility=vis, signature=signature,
             lifetimes=list(dict.fromkeys(re.findall(r"'(\w+)", signature))))

    return nodes, edges


_RUST_STRUCT_HEAD_RE = re.compile(
    r"(?m)^[ \t]*(?:pub(?:\([^)]*\))?\s+)?struct\s+(\w+)"
)
_RUST_FIELD_RE = re.compile(r"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?(\w+)\s*:\s*([^,\n]+)")
_RUST_LET_RE = re.compile(r"\blet\s+(?:mut\s+)?(\w+)\s*(?::\s*([^=;]+?))?\s*=\s*([^;]*)")
_RUST_LET_STRUCT_RE = re.compile(r"^\s*(\w+)\s*\{")
_RUST_LET_ASSOC_RE = re.compile(r"^\s*(\w+)\s*::\s*(\w+)\s*\(")
_RUST_SELF_FIELD_CALL_RE = re.compile(r"\bself\s*\.\s*(\w+)\s*\.\s*(\w+)\s*\(")
_RUST_METHOD_CALL_RE = re.compile(r"\b(\w+)\s*\.\s*(\w+)\s*\(")
_RUST_PATH_CALL_RE = re.compile(r"(?<![\w:])(\w+)\s*::\s*(\w+)\s*\(")
# No `!` guard is needed for macros: `println!(` puts the bang between the name
# and the parenthesis, so it never matches. Excluding a preceding `!` would
# instead drop the negation operator in `if !validate_card(card)`.
_RUST_BARE_CALL_RE = re.compile(r"(?<![\w.:])(\w+)\s*\(")


def _rust_split_top(text: str) -> list[str]:
    """Split on commas that are not inside <>, () or []."""
    parts, depth, current = [], 0, []
    for ch in text:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth <= 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p for p in parts if p.strip()]


def _rust_signature(blanked: str, start: int) -> tuple[str, str, tuple[int, int] | None]:
    """Parameters, declared return type and body span of a fn starting at *start*."""
    n = len(blanked)
    i = start
    while i < n and blanked[i] not in "({;":
        i += 1
    params = ""
    if i < n and blanked[i] == "(":
        depth, j = 0, i
        while j < n:
            if blanked[j] == "(":
                depth += 1
            elif blanked[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        params = blanked[i + 1:j]
        i = j + 1
    k = i
    while k < n and blanked[k] not in "{;":
        k += 1
    declared = blanked[i:k].strip()
    declared = declared[2:].strip() if declared.startswith("->") else ""
    return params, declared.split(" where", 1)[0].strip(), _rust_block_span(blanked, i)


def _rust_fn_records(blanked: str):
    """Every `fn` and its `(match, params, return_type, body_span)`.

    Returns `(all_records, top_level_records)`. tree-sitter's _RustVisitor does
    not descend into a function body, so a helper declared inside one is not a
    symbol on either branch; the full list still matters, because those bodies
    have to be masked out of the enclosing function's.
    """
    records = []
    for m in _RUST_FN_RE.finditer(blanked):
        params, declared, body = _rust_signature(blanked, m.end(2))
        records.append((m, params, declared, body))
    spans = [r[3] for r in records if r[3] is not None]
    top = [r for r in records
           if not any(start <= r[0].start() < end for start, end in spans)]
    return records, top


def _rust_regex_call_edges(source: str, nodes: list[dict], file_id: str) -> list[dict]:
    """`calls` edges for the regex branch.

    Resolves the same shapes as the tree-sitter branch — bare functions,
    `self.m()`, `Self::m()`, `Type::m()`, a typed local or parameter, and a
    field of a locally-declared type. Macros never match: `println!(` puts a
    `!` between the name and the parenthesis every call pattern requires.
    """
    blanked = _rust_blank_macros(_rust_blank_noise(source))
    symbols = {n["id"]: n["type"] for n in nodes}
    scopes = _rust_scope_spans(blanked)
    traits = _rust_impl_traits(blanked, scopes, file_id)
    # Set per function body, so a type resolves against its own module first.
    visible: list[str] = [file_id]

    def type_node(text: str) -> str | None:
        bare = _rust_bare_type(text)
        if not bare:
            return None
        for scope in visible:
            candidate = f"{scope}.{bare}"
            if symbols.get(candidate) in _RUST_TYPE_DECLS:
                return candidate
        return None

    def lookup_method(type_id: str | None, name: str) -> str | None:
        if not type_id or not name:
            return None
        seen: set[str] = set()
        queue = [type_id]
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            if symbols.get(f"{current}.{name}") == "function":
                return f"{current}.{name}"
            for trait in traits.get(current, []):
                trait_id = type_node(trait)
                if trait_id is not None:
                    queue.append(trait_id)
        return None

    # Struct fields, so `self.field.method()` can resolve to a local type.
    fields: dict[str, dict[str, str]] = {}
    for m in _RUST_STRUCT_HEAD_RE.finditer(blanked):
        visible = _rust_module_scopes(scopes, m.start(), file_id)
        owner = f"{_rust_prefix(scopes, m.start(), file_id)}.{m.group(1)}"
        span = _rust_block_span(blanked, m.end())
        if span is None:
            continue
        for fm in _RUST_FIELD_RE.finditer(blanked[span[0]:span[1]]):
            resolved = type_node(fm.group(2))
            if resolved is not None:
                fields.setdefault(owner, {})[fm.group(1)] = resolved

    # Pass 1 — every function's id, owner, body span and return type.
    funcs: list[tuple[str, str | None, str, tuple[int, int], list[str]]] = []
    returns: dict[str, str] = {}
    all_records, top_level = _rust_fn_records(blanked)
    for m, params, declared, body in top_level:
        prefix = _rust_prefix(scopes, m.start(), file_id)
        owner = prefix if prefix != _rust_prefix(
            scopes, m.start(), file_id, frozenset({"mod"})) else None
        fn_id = f"{prefix}.{m.group(2)}"
        if owner and declared in ("Self", owner.rsplit(".", 1)[-1]):
            returns[fn_id] = owner
        if body is not None:
            funcs.append((fn_id, owner, params, body,
                          _rust_module_scopes(scopes, m.start(), file_id)))

    # A nested `fn` is not a symbol, so its calls belong to nobody. Without
    # masking, the enclosing body span swallows them.
    bodies = [r[3] for r in all_records if r[3] is not None]

    def mask_nested(start: int, end: int) -> str:
        chunk = list(blanked[start:end])
        for a, b in bodies:
            if (a, b) != (start, end) and start <= a and b <= end:
                for k in range(a - start, b - start):
                    if chunk[k] != "\n":
                        chunk[k] = " "
        return "".join(chunk)

    # Pass 2 — resolve the calls in each body.
    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for fn_id, owner, params, (start, end), scope_chain in funcs:
        body = mask_nested(start, end)
        visible = scope_chain

        locals_: dict[str, str] = {}
        for param in _rust_split_top(params):
            name, _, declared = param.partition(":")
            resolved = type_node(declared) if declared else None
            if resolved is not None:
                locals_[name.strip()] = resolved
        for lm in _RUST_LET_RE.finditer(body):
            name, annotated, value = lm.group(1), lm.group(2), lm.group(3) or ""
            bound = type_node(annotated) if annotated else None
            if bound is None and (sm := _RUST_LET_STRUCT_RE.match(value)):
                bound = type_node(sm.group(1))
            if bound is None and (am := _RUST_LET_ASSOC_RE.match(value)):
                head = "Self" if am.group(1) == "Self" else am.group(1)
                base = owner if head == "Self" else type_node(am.group(1))
                target = lookup_method(base, am.group(2))
                bound = returns.get(target) if target else None
            if bound is not None:
                locals_[name] = bound

        def add(target: str | None, _fn_id: str = fn_id) -> None:
            if target and (_fn_id, target) not in seen:
                seen.add((_fn_id, target))
                edges.append({"source": _fn_id, "target": target, "relation": "calls"})

        for cm in _RUST_SELF_FIELD_CALL_RE.finditer(body):
            add(lookup_method(fields.get(owner, {}).get(cm.group(1)), cm.group(2)))
        for cm in _RUST_METHOD_CALL_RE.finditer(body):
            receiver = cm.group(1)
            base = owner if receiver == "self" else locals_.get(receiver)
            add(lookup_method(base, cm.group(2)))
        for cm in _RUST_PATH_CALL_RE.finditer(body):
            base = owner if cm.group(1) == "Self" else type_node(cm.group(1))
            add(lookup_method(base, cm.group(2)))
        for cm in _RUST_BARE_CALL_RE.finditer(body):
            # A nested `fn helper()` declaration is not a call to itself.
            if body[:cm.start()].rstrip().endswith("fn"):
                continue
            for scope in scope_chain:
                candidate = f"{scope}.{cm.group(1)}"
                if symbols.get(candidate) == "function":
                    add(candidate)
                    break

    return edges


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
    call_edges_fn=None, regex_call_fn=None,
) -> tuple[list[dict], list[dict]]:
    """Shared body for the tree-sitter-or-regex language entry points.

    Args:
        language_attr: Name of the grammar module's language accessor. Most
            expose `language()`; tree-sitter-php exposes `language_php()`.
        call_edges_fn: Optional `(tree, rel, file_id, symbols) -> edges` pass
            that resolves `calls` edges from the parsed tree.
        regex_call_fn: Optional `(source, nodes, file_id) -> edges` equivalent
            for the regex branch. Both are filtered so every endpoint is a node
            that was actually emitted.
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
            call_edges = []
            if call_edges_fn is not None:
                symbols = {n["id"]: n["type"] for n in visitor.nodes}
                known = {fid} | set(symbols)
                call_edges = [
                    e for e in call_edges_fn(tree, rel, fid, symbols)
                    if e["source"] in known and e["target"] in known
                ]
            return [module_node] + visitor.nodes, visitor.edges + call_edges
        except Exception as exc:
            import click  # noqa: PLC0415

            click.echo(f"  Warning: tree-sitter failed on {path}, "
                       f"falling back to regex: {exc}", err=True)

    nodes, edges = regex_fn(source_text, rel, fid)
    if regex_call_fn is not None:
        known = {fid} | {n["id"] for n in nodes}
        edges = edges + [
            e for e in regex_call_fn(source_text, nodes, fid)
            if e["source"] in known and e["target"] in known
        ]
    return [module_node] + nodes, edges


def index_rust(path: Path, root: Path | None = None) -> tuple[list[dict], list[dict]]:
    """Index a Rust file (tree-sitter with the 'rust' extra, else regex)."""
    return _index_with_grammar(path, root, _tsrust, _RUST_TS_AVAILABLE,
                               _RustVisitor, _index_rust_regex, "Rust",
                               call_edges_fn=_rust_call_edges,
                               regex_call_fn=_rust_regex_call_edges)


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


# ---------------------------------------------------------------------------
# Cross-file call resolution
#
# Per-file indexing cannot resolve `from slurp.budget import select_subgraph`
# followed by `select_subgraph(...)` — the target lives in another module that
# has not been parsed yet. Those calls are emitted pointing at the file's own
# import node, carrying the dotted symbol they mean; once every file is indexed,
# index_project rewrites them to the real definition.
# ---------------------------------------------------------------------------

_GLOBAL_INDEX_TYPES = ("function", "class", "module")


def _build_global_symbol_index(
    nodes: list[dict], types: tuple[str, ...] = _GLOBAL_INDEX_TYPES
) -> dict[str, str]:
    """Index definitions by each dotted suffix of their id, when unambiguous.

    `budget.select_subgraph` is indexed under both `select_subgraph` and
    `budget.select_subgraph`. A suffix shared by two different nodes is dropped,
    so an ambiguous short name never resolves and only the longer, unique form
    does.

    Args:
        nodes: Every node in the project graph.
        types: Node types to index. Narrowing it is what makes a name unique
            within one kind of node even when it collides across kinds.

    Returns:
        Mapping of unambiguous suffix to node id.
    """
    candidates: dict[str, set[str]] = {}
    for node in nodes:
        if node.get("type") not in types:
            continue
        nid = node["id"]
        parts = nid.split(".")
        for start in range(len(parts)):
            candidates.setdefault(".".join(parts[start:]), set()).add(nid)
    return {
        suffix: next(iter(ids))
        for suffix, ids in candidates.items()
        if len(ids) == 1
    }


def _build_module_index(nodes: list[dict]) -> dict[str, str]:
    """Index module nodes by each dotted suffix of their id, when unambiguous.

    DECISION: modules get their own index rather than sharing the symbol one.
    `audit` names both the module and the `slurp audit` CLI command, so in a
    combined index the suffix is ambiguous and gets dropped — taking every
    `from slurp.audit import ...` resolution down with it. Among modules alone
    the name is unique.
    """
    return _build_global_symbol_index(nodes, types=("module",))


def _resolve_imported_symbol(
    symbol: str, index: dict[str, str], kinds: dict[str, str]
) -> str | None:
    """Resolve a dotted import path to a definition in this project.

    DECISION: the *module* half must resolve to a module node before the symbol
    half is looked up. Matching on the bare name alone would map
    `from pathlib import Path` onto any project class called `Path` — a
    confident, wrong edge. Requiring the module to exist in the project is what
    separates a real intra-project import from a third-party one.

    Args:
        symbol: Dotted path from an import node, e.g. slurp.budget.select_subgraph.
        index: Output of _build_global_symbol_index.
        kinds: node id -> node type, for the whole project.

    Returns:
        Node id of the imported definition, or None when it is external,
        ambiguous, or simply absent.
    """
    if "." not in symbol:
        return None
    module_path, _, name = symbol.rpartition(".")

    module_parts = module_path.split(".")
    for start in range(len(module_parts)):
        module_id = index.get(".".join(module_parts[start:]))
        if module_id is None:
            continue
        target = f"{module_id}.{name}"
        if kinds.get(target) in ("function", "class"):
            return target
    return None


def _resolve_cross_file_calls(
    nodes: list[dict], edges: list[dict], module_index: dict[str, str]
) -> list[dict]:
    """Rewrite calls that route through an import node to their real target.

    Args:
        nodes: Every node in the project graph.
        edges: Every edge, including the pending import-node calls.
        module_index: Output of _build_module_index.

    Returns:
        The edge list with pending calls resolved, unresolvable ones dropped,
        and the private pending-symbol key removed from every edge.
    """
    kinds = {n["id"]: n.get("type", "") for n in nodes}
    labels = {n["id"]: n.get("label", "") for n in nodes}
    sources = {n["id"]: n.get("source_file", "") for n in nodes}
    import_ids = {nid for nid, kind in kinds.items() if kind == "import"}

    resolved: list[dict] = []
    seen_calls: set[tuple[str, str]] = set()

    # Direct calls keep priority: an intra-file edge already covers the pair.
    for edge in edges:
        if edge.get("relation") == "calls" and edge.get("target") not in import_ids:
            seen_calls.add((edge["source"], edge["target"]))

    python_imports = {
        nid for nid in import_ids
        if str(sources.get(nid, "")).endswith(".py")
    }

    for edge in edges:
        if edge.get("relation") != "calls" or edge.get("target") not in import_ids:
            resolved.append({k: v for k, v in edge.items() if k != _PENDING_SYMBOL})
            continue
        if edge["target"] not in python_imports:
            # A TypeScript pending edge. Leave it, pending key and all, for the
            # TS resolver that runs next — consuming it here would discard it,
            # since a TS symbol name carries no module prefix to resolve.
            resolved.append(edge)
            continue

        symbol = edge.get(_PENDING_SYMBOL) or labels.get(edge["target"], "")
        target = _resolve_imported_symbol(symbol, module_index, kinds)
        if target is None or target == edge["source"]:
            # External library, ambiguous name, or a self-reference: dropping the
            # edge loses nothing, while keeping it would point at a placeholder.
            continue
        key = (edge["source"], target)
        if key in seen_calls:
            continue
        seen_calls.add(key)
        resolved.append(
            {"source": edge["source"], "target": target, "relation": "calls"}
        )

    return resolved

def _build_ts_symbol_index(nodes: list[dict]) -> dict[str, str]:
    """Index TypeScript/JavaScript definitions by dotted suffix, when unique.

    Same shape as _build_global_symbol_index, restricted to nodes that came from
    a TS/JS file so a Python symbol of the same name can never be the answer.

    Args:
        nodes: Every node in the project graph.

    Returns:
        Mapping of unambiguous suffix to node id.
    """
    ts_nodes = [
        n for n in nodes
        if str(n.get("source_file", "")).endswith((".ts", ".tsx", ".js", ".jsx",
                                                   ".mjs", ".cjs"))
    ]
    return _build_global_symbol_index(ts_nodes)


def _resolve_ts_symbol(
    module_path: str, symbol: str, index: dict[str, str], kinds: dict[str, str]
) -> str | None:
    """Find *symbol* inside *module_path*, or None when it is not resolvable.

    The module must exist in the project before the symbol is looked up, which
    is what stops a bare package name from binding to a same-named local file.
    A `foo/index.ts` barrel is tried as well, since `from './foo'` resolves to
    it in TypeScript.
    """
    parts = module_path.split(".")
    for start in range(len(parts)):
        prefix = ".".join(parts[start:])
        for module_id in (index.get(prefix), index.get(f"{prefix}.index")):
            if module_id is None or kinds.get(module_id) != "module":
                continue
            target = f"{module_id}.{symbol}"
            if kinds.get(target) in ("function", "class", "interface"):
                return target
    return None


def _resolve_ts_cross_file_calls(
    nodes: list[dict], edges: list[dict], ts_symbol_index: dict[str, str]
) -> list[dict]:
    """Rewrite TypeScript calls routed through an import node to their target.

    Args:
        nodes: Every node in the project graph.
        edges: Every edge, including pending import-node calls.
        ts_symbol_index: Output of _build_ts_symbol_index.

    Returns:
        The edge list with TypeScript pending calls resolved, unresolvable ones
        dropped, and the private pending key removed.
    """
    kinds = {n["id"]: n.get("type", "") for n in nodes}
    imports = {
        n["id"]: n for n in nodes
        if n.get("type") == "import" and _TS_SOURCE_MODULE in n
    }
    import_ids = {nid for nid, kind in kinds.items() if kind == "import"}

    resolved: list[dict] = []
    seen_calls: set[tuple[str, str]] = set()
    for edge in edges:
        if edge.get("relation") == "calls" and edge.get("target") not in import_ids:
            seen_calls.add((edge["source"], edge["target"]))

    for edge in edges:
        clean = {k: v for k, v in edge.items() if k != _PENDING_SYMBOL}
        if edge.get("relation") != "calls" or edge.get("target") not in import_ids:
            resolved.append(clean)
            continue

        node = imports.get(edge["target"])
        symbol = edge.get(_PENDING_SYMBOL)
        if node is None or not symbol:
            # No source module recorded (external package, side-effect import,
            # or a type-only import): nothing to point at. Running last, this
            # pass is also what guarantees no calls edge survives pointing at an
            # import placeholder.
            continue

        if symbol == "default":
            # TypeScript does not record which export is the default, so the
            # only certain match is the conventional one: a symbol in that
            # module sharing the local binding's name.
            candidates = node.get(_TS_IMPORTED_SYMBOLS) or []
            symbol = candidates[0] if len(candidates) == 1 else ""
        if not symbol or symbol == "*":
            continue

        target = _resolve_ts_symbol(
            node[_TS_SOURCE_MODULE], symbol, ts_symbol_index, kinds
        )
        if target is None or target == edge["source"]:
            continue
        key = (edge["source"], target)
        if key in seen_calls:
            continue
        seen_calls.add(key)
        resolved.append(
            {"source": edge["source"], "target": target, "relation": "calls"}
        )

    return resolved

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

    seen_edges: set[tuple[str, str, str]] = set()
    filtered_edges: list[dict] = []
    for e in all_edges:
        src, tgt = e.get("source", ""), e.get("target", "")
        if src in known_ids and tgt in known_ids:
            # Two calls into the same module alias (`m.f()` and `m.g()`) share a
            # target import node, so the pending symbol is part of the identity
            # until cross-file resolution separates them.
            key = (src, tgt, e.get(_PENDING_SYMBOL) or "")
            if key not in seen_edges:
                seen_edges.add(key)
                filtered_edges.append(e)

    filtered_edges = _resolve_cross_file_calls(
        unique_nodes, filtered_edges, _build_module_index(unique_nodes)
    )
    filtered_edges = _resolve_ts_cross_file_calls(
        unique_nodes, filtered_edges, _build_ts_symbol_index(unique_nodes)
    )

    return {
        "nodes": unique_nodes,
        "links": filtered_edges,
        "built_at_commit": None,
    }
