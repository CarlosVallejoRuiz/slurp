"""Prototype of JSX usage edges (#87) for measuring the design. Not part of slurp.

Design: docs/design/jsx-v1.5.md. It reuses slurp's own TypeScript machinery so that
node ids, scopes, import bindings and cross-file resolution are exactly the indexer's:

- tree-sitter: _JSXVisitor subclasses slurp's _TSCallVisitor (same scope stack, same
  local lookup, same import bindings) and only looks at JSX elements;
- regex: the same body spans and import bindings as _ts_regex_call_edges;
- cross-file: slurp's _resolve_ts_symbol, with _resolve_ts_cross_file_calls's rules
  (a default import resolves only to a symbol named like the local binding; `*` and
  self-edges never resolve).

A JSX usage becomes an edge (caller -> component) only when the component resolves with
certainty to a function or class node of the project. One edge per pair: a pair that
already has an edge (contains, calls) keeps it, as the indexer's dedup does today.

API: jsx_file(source, rel_path) -> FileJSX; resolve(files, nodes, existing) -> edges.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from slurp import indexer as ix

# A JSX element whose name starts with an upper-case letter is a component reference;
# lower case is an intrinsic element (<div>, <svg>), never a project symbol.
_COMPONENT = re.compile(r"^[A-Z][\w$]*$")


@dataclass
class FileJSX:
    """JSX usages of one file, before cross-file resolution."""

    branch: str                                   # "tree-sitter" | "regex"
    local: list[tuple[str, str]] = field(default_factory=list)       # (caller, target id)
    pending: list[tuple[str, str, str]] = field(default_factory=list)  # (caller, import id, symbol)
    lazy: list[tuple[str, str, str]] = field(default_factory=list)    # (caller, module, symbol)
    imports: dict[str, dict] = field(default_factory=dict)            # import id -> record
    unresolved: list[tuple[str, str]] = field(default_factory=list)   # (name, why)
    elements: int = 0                                                  # component elements seen


# ---------------------------------------------------------------------------- lazy/dynamic

def _import_label(records: list[dict], import_id: str) -> str:
    for r in records:
        if r["id"] == import_id:
            return r.get("label", "")
    return ""


def _is_lazy_callee(callee, imports: dict, records: list[dict], text) -> bool:
    """`lazy` / `React.lazy` from react, or `dynamic` from next/dynamic — and nothing else."""
    if callee is None:
        return False
    if callee.type == "identifier":
        name = text(callee)
        bound = imports.get(name)
        if bound is None:
            return False
        label = _import_label(records, bound[0])
        return (name == "lazy" and label == "react" and bound[1] == "lazy") or \
               (label == "next/dynamic" and bound[1] == "default")
    if callee.type == "member_expression":
        obj, prop = callee.child_by_field_name("object"), callee.child_by_field_name("property")
        if obj is None or prop is None or obj.type != "identifier" or text(prop) != "lazy":
            return False
        bound = imports.get(text(obj))
        return bound is not None and _import_label(records, bound[0]) == "react" and \
            bound[1] in ("default", "*")
    return False


def _dynamic_target(arg, text) -> tuple[str, str] | None:
    """`() => import("spec")` -> (spec, "default"); `.then(m => m.X)` -> (spec, "X")."""
    if arg is None or arg.type != "arrow_function":
        return None
    body = arg.child_by_field_name("body")
    if body is None or body.type != "call_expression":
        return None
    fn = body.child_by_field_name("function")
    if fn is not None and fn.type == "import":
        args = body.child_by_field_name("arguments")
        strings = [c for c in (args.children if args else []) if c.type == "string"]
        return (text(strings[0]).strip("'\"`"), "default") if len(strings) == 1 else None
    if fn is None or fn.type != "member_expression" or text(fn.child_by_field_name("property")) != "then":
        return None
    inner = fn.child_by_field_name("object")
    base = _dynamic_target_import(inner, text)
    args = body.child_by_field_name("arguments")
    cb = next((c for c in (args.children if args else []) if c.type == "arrow_function"), None)
    if base is None or cb is None:
        return None
    params = cb.child_by_field_name("parameter") or cb.child_by_field_name("parameters")
    pname = text(params).strip("() ")
    cbody = cb.child_by_field_name("body")
    if cbody is None or cbody.type != "member_expression":
        return None
    if text(cbody.child_by_field_name("object")) != pname or not re.fullmatch(r"[\w$]+", pname):
        return None
    return (base, text(cbody.child_by_field_name("property")))


def _dynamic_target_import(node, text) -> str | None:
    if node is None or node.type != "call_expression":
        return None
    fn = node.child_by_field_name("function")
    if fn is None or fn.type != "import":
        return None
    args = node.child_by_field_name("arguments")
    strings = [c for c in (args.children if args else []) if c.type == "string"]
    return text(strings[0]).strip("'\"`") if len(strings) == 1 else None


# ---------------------------------------------------------------------------- tree-sitter

class _JSXVisitor(ix._TSCallVisitor):
    """Walks the tree exactly like _TSCallVisitor, but records JSX elements only."""

    def __init__(self, rel_path, file_id, symbols, imports, records):
        super().__init__(rel_path, file_id, symbols, imports)
        self.records = records
        self.out = FileJSX("tree-sitter")
        self.lazy_bindings: dict[str, tuple[str, str]] = {}   # local -> (module, symbol)

    # calls are not ours: walk through them without emitting
    def visit_call_expression(self, node) -> None:
        self._recurse(node)

    def visit_new_expression(self, node) -> None:
        self._recurse(node)

    def _visit_declarator(self, node) -> None:
        name_node = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if (name_node is not None and name_node.type == "identifier" and value is not None
                and value.type == "call_expression"
                and _is_lazy_callee(value.child_by_field_name("function"), self.imports,
                                    self.records, self._text)):
            args = value.child_by_field_name("arguments")
            first = next((c for c in (args.children if args else [])
                          if c.type not in ("(", ")", ",")), None)
            target = _dynamic_target(first, self._text)
            if target is not None:
                module = ix._ts_resolve_module_path(target[0], self.rel_path)
                if module:
                    self.lazy_bindings[self._text(name_node)] = (module, target[1])
        super()._visit_declarator(node)

    def _element(self, node) -> None:
        name = node.child_by_field_name("name")
        if name is not None:
            self._resolve_element(name)
        self._recurse(node)

    visit_jsx_opening_element = _element
    visit_jsx_self_closing_element = _element

    def _resolve_element(self, name) -> None:
        out = self.out
        if name.type == "identifier":
            ident = self._text(name)
            if not _COMPONENT.match(ident):
                return                              # <div>, <svg>: intrinsic
            out.elements += 1
            local = self._lookup_name(ident)
            if local is not None:
                if local != self._parent:
                    out.local.append((self._parent, local))
                return
            bound = self.imports.get(ident)
            if bound is not None:
                if bound[1] == "*":
                    out.unresolved.append((ident, "namespace used as a component"))
                else:
                    out.pending.append((self._parent, bound[0], bound[1]))
                return
            lazy = self.lazy_bindings.get(ident)
            if lazy is not None:
                symbol = ident if lazy[1] == "default" else lazy[1]
                out.lazy.append((self._parent, lazy[0], symbol))
                return
            out.unresolved.append((ident, "not a local definition or an import"))
            return
        if name.type == "member_expression":
            out.elements += 1
            obj, prop = name.child_by_field_name("object"), name.child_by_field_name("property")
            if obj is not None and obj.type == "identifier":
                bound = self.imports.get(self._text(obj))
                if bound is not None and bound[1] == "*":
                    out.pending.append((self._parent, bound[0], self._text(prop)))
                    return
            out.unresolved.append((self._text(name), "member of a non-namespace object"))
        # jsx_namespace_name (<svg:rect>): intrinsic


def _jsx_tree_sitter(source: str, rel: Path) -> FileJSX | None:
    fid = ix._file_id(rel)
    tree = ix._parse_ts_tree(source.encode("utf-8"), is_tsx=True)
    if ix._tree_is_broken(tree):
        return None
    visitor = ix._TSVisitor(rel, fid)
    visitor.visit(tree.root_node)
    symbols = {n["id"]: n["type"] for n in visitor.nodes if n.get("type") in ("function", "class")}
    records = [n for n in visitor.nodes if n.get("type") == "import"]
    jsx = _JSXVisitor(rel, fid, symbols, visitor.imports, records)
    jsx.visit(tree.root_node)
    jsx.out.imports = {r["id"]: r for r in records}
    return jsx.out


# ---------------------------------------------------------------------------- regex

# `<Name` or `<Ns.Name` where a JSX element can start: not after an identifier, `.`,
# `)` or `]` (generics, comparisons on expressions), and followed by a JSX boundary.
_JSX_OPEN_RE = re.compile(r"(?<![\w$.\])])<([A-Za-z_$][\w$]*)(?:\.([A-Za-z_$][\w$]*))?(?=[\s/>{])")
_LAZY_RE = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:([A-Za-z_$][\w$]*)\.)?"
    r"(lazy|dynamic)\s*\(\s*\(\s*\)\s*=>\s*import\s*\(\s*(['\"])([^'\"]+)\4\s*\)"
    r"(?:\s*\.then\s*\(\s*\(?\s*([A-Za-z_$][\w$]*)\s*\)?\s*=>\s*\6\s*\.\s*([A-Za-z_$][\w$]*)\s*\))?"
)


def _match(text: str, i: int, open_ch: str, close_ch: str) -> int | None:
    depth = 0
    for k in range(i, len(text)):
        if text[k] == open_ch:
            depth += 1
        elif text[k] == close_ch:
            depth -= 1
            if depth == 0:
                return k
    return None


def _body_span(clean: str, start: int) -> tuple[int, int] | None:
    """Body of the declaration whose name ends at *start*, parameters skipped first.

    `_ts_body_span` takes the first `{` after the name, which in `function Page({ a })`
    is the destructured parameter. Here the parameter list is closed first, then:
    `=> {` or a declaration's `{` opens a block; `=> (` an expression body in
    parentheses (the usual JSX return); another arrow expression runs to `;` or the
    end of its line; a `;` first means a signature without a body.
    """
    n = len(clean)

    def ws(i: int) -> int:
        while i < n and clean[i].isspace():
            i += 1
        return i

    i = ws(start)
    if i < n and clean[i] == "=" and clean[i + 1:i + 2] != ">":
        i = ws(i + 1)
        if clean.startswith("async", i):
            i = ws(i + 5)
        if clean.startswith("function", i):
            i = ws(i + 8)
            while i < n and (clean[i].isalnum() or clean[i] in "_$*"):
                i += 1
            i = ws(i)
    if i < n and clean[i] == "<":
        j = _match(clean, i, "<", ">")
        if j is None:
            return None
        i = ws(j + 1)
    if i < n and clean[i] == "(":
        j = _match(clean, i, "(", ")")
        if j is None:
            return None
        i = j + 1
    elif i < n and (clean[i].isalpha() or clean[i] in "_$"):
        while i < n and (clean[i].isalnum() or clean[i] in "_$"):
            i += 1
    for k in range(i, n):
        ch = clean[k]
        if ch == ";":
            return None
        if ch == "{":
            end = _match(clean, k, "{", "}")
            return (k, end) if end is not None else None
        if clean.startswith("=>", k):
            b = ws(k + 2)
            if b < n and clean[b] == "{":
                end = _match(clean, b, "{", "}")
                return (b, end) if end is not None else None
            if b < n and clean[b] == "(":
                end = _match(clean, b, "(", ")")
                return (b, end) if end is not None else None
            stop = [x for x in (clean.find(";", b), clean.find("\n", b)) if x != -1]
            return (b, min(stop)) if stop else None
    return None


def _blank_keep_lines(source: str) -> str:
    """Like slurp's _ts_blank_noise, but a blanked comment or template keeps its line
    breaks. _ts_blank_noise turns them into spaces, so every line after a multi-line
    comment or template literal is shifted, and _ts_decl_offset then looks for a
    declaration on the wrong line (a false attribution: the cause of a regex-only edge
    in excalidraw's App.tsx)."""
    return ix._TS_NOISE_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), source)


def _decl_offset(clean: str, lineno: int, name: str) -> int | None:
    """Offset just past *name* on line *lineno*, or None when the name is not there."""
    lines = clean.splitlines(keepends=True)
    if not 1 <= lineno <= len(lines):
        return None
    column = re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", lines[lineno - 1])
    if column is None:
        return None
    return sum(len(x) for x in lines[: lineno - 1]) + column.end()


def _jsx_regex(source: str, rel: Path) -> FileJSX:
    fid = ix._file_id(rel)
    nodes, _edges = ix._index_typescript_regex(source, rel, fid)
    out = FileJSX("regex")
    out.imports = {n["id"]: n for n in nodes if n.get("type") == "import"}
    symbols = {n["id"]: n["type"] for n in nodes if n.get("type") in ("function", "class")}

    bindings: dict[str, tuple[str, str]] = {}
    labels: dict[str, str] = {}
    for m in ix._TS_IMPORT_CLAUSE_RE.finditer(source):
        if m.group(1):
            continue
        module_path = m.group(3)
        safe = module_path.rsplit("/", 1)[-1].replace("-", "_").replace(".", "_")
        _, clause = ix._ts_parse_clause_text(m.group(2))
        for local, exported in clause.items():
            bindings[local] = (f"{fid}.import_{safe}", exported)
            labels[local] = module_path

    lazy: dict[str, tuple[str, str]] = {}
    for m in _LAZY_RE.finditer(source):
        local, owner, fn, _q, spec, _p, prop = m.groups()
        ok = (fn == "lazy" and ((owner and labels.get(owner) == "react" and
                                 bindings[owner][1] in ("default", "*")) or
                                (not owner and labels.get("lazy") == "react" and
                                 bindings["lazy"][1] == "lazy"))) or \
             (fn == "dynamic" and not owner and labels.get("dynamic") == "next/dynamic" and
              bindings["dynamic"][1] == "default")
        module = ix._ts_resolve_module_path(spec, rel) if ok else None
        if module:
            lazy[local] = (module, prop or "default")

    clean = _blank_keep_lines(source)
    spans: list[tuple[int, int, str]] = []
    for node in nodes:
        if node.get("type") not in ("function", "class"):
            continue
        loc = str(node.get("source_location", ""))
        if not loc.startswith("L") or not loc[1:].isdigit():
            continue
        start = _decl_offset(clean, int(loc[1:]), node["label"])
        span = _body_span(clean, start) if start is not None else None
        if span is not None and span[1] > span[0]:
            spans.append((span[0], span[1], node["id"]))

    def enclosing(offset: int) -> str | None:
        best = None
        for s, e, nid in spans:
            if s < offset < e and (best is None or e - s < best[0]):
                best = (e - s, nid)
        return best[1] if best else None

    for m in _JSX_OPEN_RE.finditer(clean):
        head, member = m.group(1), m.group(2)
        caller = enclosing(m.start())
        if caller is None:
            # Outside every body found: top-level JSX, or a body the regex could not
            # delimit. Like the regex calls, nothing is attributed (a gap, never a guess).
            if _COMPONENT.match(head) or member:
                out.elements += 1
                out.unresolved.append((head, "outside any body the regex delimits"))
            continue
        if member is None:
            if not _COMPONENT.match(head):
                continue
            out.elements += 1
            # innermost scope first, like _lookup_name (class scopes are not lexical here)
            scope = caller
            target = None
            while True:
                cand = f"{scope}.{head}"
                if cand in symbols and symbols.get(scope) != "class":
                    target = cand
                    break
                if scope == fid:
                    break
                scope = scope.rsplit(".", 1)[0]
            if target is not None:
                if target != caller:
                    out.local.append((caller, target))
                continue
            if head in bindings:
                imp, exported = bindings[head]
                if exported == "*":
                    out.unresolved.append((head, "namespace used as a component"))
                else:
                    out.pending.append((caller, imp, exported))
                continue
            if head in lazy:
                module, sym = lazy[head]
                out.lazy.append((caller, module, head if sym == "default" else sym))
                continue
            out.unresolved.append((head, "not a local definition or an import"))
        else:
            out.elements += 1
            bound = bindings.get(head)
            if bound is not None and bound[1] == "*":
                out.pending.append((caller, bound[0], member))
            else:
                out.unresolved.append((f"{head}.{member}", "member of a non-namespace object"))
    return out


def jsx_file(source: str, rel_path: str | Path, force_regex: bool = False) -> FileJSX:
    """JSX usages of one .tsx/.jsx file, with the branch the indexer would take."""
    rel = Path(rel_path)
    if ix._TREE_SITTER_AVAILABLE and not force_regex:
        try:
            got = _jsx_tree_sitter(source, rel)
            if got is not None:
                return got
        except Exception:
            pass
    return _jsx_regex(source, rel)


# ---------------------------------------------------------------------------- resolution

def resolve(files: list[FileJSX], nodes: list[dict], existing: set[tuple[str, str]],
            relation: str = "renders") -> tuple[list[dict], dict]:
    """Edges for every certain JSX usage, and counts by case.

    Args:
        files: jsx_file() results for the graph's .tsx/.jsx files.
        nodes: The graph's nodes (dicts with id, type, source_file).
        existing: (source, target) pairs that already have an edge; they keep it.
        relation: Relation to write on the new edges.
    """
    kinds = {n["id"]: n.get("type", "") for n in nodes}
    index = ix._build_ts_symbol_index(nodes)
    seen = set(existing)
    edges: list[dict] = []
    counts = {"local": 0, "imported": 0, "namespace": 0, "lazy": 0, "unresolved_import": 0,
              "unresolved_lazy": 0, "existing_pair": 0, "missing_endpoint": 0}

    def add(u: str, v: str, case: str) -> None:
        if u not in kinds or v not in kinds:
            counts["missing_endpoint"] += 1
            return
        if u == v:
            return
        if (u, v) in seen:
            counts["existing_pair"] += 1
            return
        seen.add((u, v))
        counts[case] += 1
        edges.append({"source": u, "target": v, "relation": relation})

    for f in files:
        for u, v in f.local:
            add(u, v, "local")
        for u, imp, symbol in f.pending:
            record = f.imports.get(imp)
            module = record.get(ix._TS_SOURCE_MODULE) if record else None
            if not module:
                counts["unresolved_import"] += 1          # external package, or unresolvable
                continue
            case = "imported"
            if symbol == "default":
                cands = record.get(ix._TS_IMPORTED_SYMBOLS) or []
                symbol = cands[0] if len(cands) == 1 else ""
            elif record.get(ix._TS_IMPORT_KIND) == "namespace":
                case = "namespace"
            if not symbol or symbol == "*":
                counts["unresolved_import"] += 1
                continue
            target = ix._resolve_ts_symbol(module, symbol, index, kinds)
            if target is None:
                counts["unresolved_import"] += 1
                continue
            add(u, target, case)
        for u, module, symbol in f.lazy:
            target = ix._resolve_ts_symbol(module, symbol, index, kinds)
            if target is None:
                counts["unresolved_lazy"] += 1
                continue
            add(u, target, "lazy")
    return edges, counts
