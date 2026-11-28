"""Variant W2 of the JSX design: components wrapped in React.memo / React.forwardRef
become function nodes. Not part of slurp.

Today `const Button = React.forwardRef((props, ref) => ...)` is no node at all (only
arrow and function values are), so a JSX usage of Button has nothing to point at: on
PrismaStats 644 component elements point at such symbols (Button alone, 119), on
excalidraw 536 (Excalidraw = React.memo(...), 317).

W2 rule, tree-sitter: `const X = W(fn)` where W is `memo`/`forwardRef` imported from
"react", or `React.memo`/`React.forwardRef` with React imported from "react", and fn an
arrow function, a function expression, or another such wrapper around one -> X is a
function node and fn is its scope (its calls and JSX are X's). `const X = memo(Y)` with
Y an identifier -> X is a function node with an edge X -> Y (X renders Y).

rebuild_ts(graph, sources, variant) re-indexes the TypeScript/JavaScript files of a
stored graph and assembles them with the rest, the way `slurp index --smart` does.
"""

from __future__ import annotations

from pathlib import Path

import jsx
from slurp import indexer as ix

TS_EXTS = (".ts", ".tsx", ".js", ".jsx")
_WRAPPERS = ("memo", "forwardRef")


def _labels(records: list[dict]) -> dict[str, str]:
    return {r["id"]: r.get("label", "") for r in records if r.get("type") == "import"}


def _is_wrapper(callee, imports, labels, text) -> bool:
    if callee is None:
        return False
    if callee.type == "identifier":
        bound = imports.get(text(callee))
        return bound is not None and bound[1] in _WRAPPERS and labels.get(bound[0]) == "react"
    if callee.type == "member_expression":
        obj, prop = callee.child_by_field_name("object"), callee.child_by_field_name("property")
        if obj is None or prop is None or obj.type != "identifier" or text(prop) not in _WRAPPERS:
            return False
        bound = imports.get(text(obj))
        return bound is not None and bound[1] in ("default", "*") and labels.get(bound[0]) == "react"
    return False


def unwrap(value, imports, labels, text):
    """("inline", function node) | ("alias", identifier) | None."""
    depth = 0
    while value is not None and value.type == "call_expression" and depth < 3:
        if not _is_wrapper(value.child_by_field_name("function"), imports, labels, text):
            return None
        args = value.child_by_field_name("arguments")
        first = next((c for c in (args.children if args else [])
                      if c.type not in ("(", ")", ",", "comment")), None)
        if first is None:
            return None
        if first.type in ("arrow_function", "function", "function_expression"):
            return ("inline", first)
        if first.type == "identifier":
            return ("alias", text(first))
        value, depth = first, depth + 1
    return None


class DeclW(ix._TSVisitor):
    def visit_lexical_declaration(self, node) -> None:
        labels = _labels(self.nodes)
        for child in node.children:
            if child.type != "variable_declarator":
                continue
            name_node = child.child_by_field_name("name")
            value = child.child_by_field_name("value")
            if not name_node or not value:
                continue
            if value.type in ("arrow_function", "function") or (
                    value.type == "call_expression"
                    and unwrap(value, self.imports, labels, self._text) is not None):
                self._emit(name_node.text.decode("utf-8"), "function", child.start_point[0] + 1)


class _ScopeW:
    """Mixin for _TSCallVisitor subclasses: a wrapped function is the wrapper's scope."""

    records: list[dict] = []

    def _visit_declarator(self, node) -> None:
        name_node = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if name_node is not None and value is not None and value.type == "call_expression":
            got = unwrap(value, self.imports, _labels(self.records), self._text)
            nid = f"{self._parent}.{self._text(name_node)}"
            if got is not None and got[0] == "inline" and nid in self.symbols:
                self._locals.append({})
                self._push(nid, "function")
                self._recurse(got[1])
                self._pop()
                self._locals.pop()
                return
        super()._visit_declarator(node)


class CallW(_ScopeW, ix._TSCallVisitor):
    pass


class JSXW(jsx._JSXVisitor, _ScopeW, ix._TSCallVisitor):
    """JSX visitor with W2 scopes, plus the X -> Y edge of `const X = memo(Y)`."""

    def _visit_declarator(self, node) -> None:
        name_node = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if name_node is not None and value is not None and value.type == "call_expression":
            got = unwrap(value, self.imports, _labels(self.records), self._text)
            nid = f"{self._parent}.{self._text(name_node)}"
            if got is not None and got[0] == "alias" and nid in self.symbols:
                local = self._lookup_name(got[1])
                if local is not None and local != nid:
                    self.out.local.append((nid, local))
                elif got[1] in self.imports and self.imports[got[1]][1] != "*":
                    imp, exported = self.imports[got[1]]
                    self.out.pending.append((nid, imp, exported))
        super()._visit_declarator(node)


def index_ts_source(source: str, rel: Path, w2: bool) -> tuple[list[dict], list[dict]]:
    """index_typescript() on a string, with the W2 visitors when *w2*."""
    fid = ix._file_id(rel)
    module_node = {"id": fid, "label": rel.name, "type": "module",
                   "description": f"TypeScript module {rel.name}", "source_file": str(rel),
                   "source_location": "L1", "file_type": "code"}
    is_tsx = rel.suffix.lower() in (".tsx", ".jsx")
    if ix._TREE_SITTER_AVAILABLE:
        try:
            tree = ix._parse_ts_tree(source.encode("utf-8"), is_tsx=is_tsx)
            if ix._tree_is_broken(tree):
                raise ValueError("broken tree")
            visitor = (DeclW if w2 else ix._TSVisitor)(rel, fid)
            visitor.visit(tree.root_node)
            symbols = {n["id"]: n["type"] for n in visitor.nodes
                       if n.get("type") in ("function", "class")}
            calls = (CallW if w2 else ix._TSCallVisitor)(rel, fid, symbols, visitor.imports)
            calls.records = visitor.nodes
            calls.returns = {n["label"]: n["return_type"] for n in visitor.nodes
                             if n.get("return_type") and n.get("type") == "function"}
            calls.visit(tree.root_node)
            known = {fid} | {n["id"] for n in visitor.nodes}
            call_edges = [e for e in calls.edges if e["source"] in known and e["target"] in known]
            return [module_node] + visitor.nodes, visitor.edges + call_edges
        except Exception:
            pass
    nodes, edges = ix._index_typescript_regex(source, rel, fid)
    known = {fid} | {n["id"] for n in nodes}
    call_edges = [e for e in ix._ts_regex_call_edges(source, nodes, fid)
                  if e["source"] in known and e["target"] in known]
    return [module_node] + nodes, edges + call_edges


def jsx_file_w2(source: str, rel_path: str | Path) -> jsx.FileJSX:
    """jsx.jsx_file() with W2 symbols and scopes (tree-sitter; regex as in W1)."""
    rel = Path(rel_path)
    fid = ix._file_id(rel)
    try:
        tree = ix._parse_ts_tree(source.encode("utf-8"), is_tsx=True)
        if not ix._tree_is_broken(tree):
            decl = DeclW(rel, fid)
            decl.visit(tree.root_node)
            symbols = {n["id"]: n["type"] for n in decl.nodes
                       if n.get("type") in ("function", "class")}
            records = [n for n in decl.nodes if n.get("type") == "import"]
            v = JSXW(rel, fid, symbols, decl.imports, records)
            v.visit(tree.root_node)
            v.out.imports = {r["id"]: r for r in records}
            return v.out
    except Exception:
        pass
    return jsx.jsx_file(source, rel, force_regex=True)


def ts_files(graph: dict) -> list[str]:
    """Every TS/JS file the graph has a node from (not only module nodes: `util.ts` and
    `util.js` share a module id, and the first file's module node wins)."""
    return sorted({str(n.get("source_file", "")) for n in graph["nodes"]
                   if str(n.get("source_file", "")).endswith(TS_EXTS)})


def rebuild_ts(graph: dict, sources: dict[str, str], w2: bool) -> dict:
    """The stored *graph* with its TS/JS files re-indexed (W2 visitors when *w2*)."""
    is_ts = {n["id"]: str(n.get("source_file", "")).endswith(TS_EXTS) for n in graph["nodes"]}
    kept_nodes = [n for n in graph["nodes"] if not is_ts[n["id"]]]
    kept_edges = [e for e in graph["links"] if not is_ts.get(e["source"], False)]
    new_nodes: list[dict] = []
    new_edges: list[dict] = []
    for rel in sorted(sources):
        nodes, edges = index_ts_source(sources[rel], Path(rel), w2)
        new_nodes += nodes
        new_edges += edges
    return ix._assemble_graph(kept_nodes + new_nodes, kept_edges + new_edges)
