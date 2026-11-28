"""Draws the hand-check sample of v1.5's real implementation (#87). Not part of slurp.

    ~/Desktop/Slurp/slurp/.venv/bin/python -I handcheck.py

30 `renders` edges (15 from PrismaStats, 15 from excalidraw) and 15 memo/forwardRef nodes
(8 and 7), drawn with random.Random(87) from the HEAD graphs of the bench repos indexed
with the checkout's index_project. A W2 node is a node absent from the bank's v1.4 HEAD
graph (~/slurp-bench/graphs/<p>.json) whose declaration wraps memo/forwardRef. For each
case the evidence is printed with file:line: the caller's declaration, the JSX lines
that name the target, the import or lazy binding, and the target's declaration. The
verdicts are written by hand in resultados/revision-manual.md.
"""

import json
import random
import re
from functools import cache
from pathlib import Path

import tree_sitter_typescript as tsts
from tree_sitter import Language, Parser

from slurp.indexer import index_project

BENCH = Path.home() / "slurp-bench"
OUT = BENCH / "v15" / "handcheck"
PICKS = {"prismastats": (15, 8), "excalidraw": (15, 7)}


def _lines(root: Path, rel: str) -> list[str]:
    return (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()


def _show(rel: str, lines: list[str], i: int) -> str:
    return f"    {rel}:{i + 1}: {lines[i].strip()[:150]}"


_FUNCS = {"function_declaration", "arrow_function", "function_expression",
          "method_definition", "generator_function_declaration"}


@cache
def _tree(path: Path):
    lang = tsts.language_tsx() if path.suffix in (".tsx", ".jsx") else tsts.language_typescript()
    return Parser(Language(lang)).parse(path.read_bytes())


def _name(node) -> str:
    """A function's name, its variable's or its field's; else where it sits."""
    if node.child_by_field_name("name"):
        return node.child_by_field_name("name").text.decode()
    p = node.parent
    while p is not None and p.type in ("call_expression", "arguments", "parenthesized_expression",
                                       "as_expression", "type_arguments"):
        p = p.parent
    if p is not None and p.type in ("variable_declarator", "public_field_definition", "pair"):
        key = p.child_by_field_name("name") or p.child_by_field_name("key")
        return f"{key.text.decode()} ({p.type.split('_')[0]})"
    return f"<anonymous {node.type} in {node.parent.type}>"


def _enclosing(path: Path, line: int, col: int) -> str:
    """Outermost to innermost function around *line* (1-based), straight from tree-sitter.
    *col* is a character column; tree-sitter counts bytes."""
    tree = _tree(path)
    text = path.read_text(encoding="utf-8").splitlines()[line - 1]
    col = len(text[:col].encode())
    node = tree.root_node.descendant_for_point_range((line - 1, col), (line - 1, col))
    chain = []
    while node is not None:
        if node.type in _FUNCS:
            chain.append(f"{_name(node)} L{node.start_point[0] + 1}-{node.end_point[0] + 1}")
        node = node.parent
    return " > ".join(reversed(chain)) or "(module level)"


def main() -> None:
    rng = random.Random(87)
    OUT.mkdir(parents=True, exist_ok=True)
    report = []
    for p, (n_edges, n_nodes) in PICKS.items():
        root = BENCH / "repos" / p
        graph = index_project(root)
        for n in graph["nodes"]:   # source_location is "L<line>"
            n["line"] = int(n["source_location"].lstrip("L"))
        nodes = {n["id"]: n for n in graph["nodes"]}
        v14 = {n["id"] for n in json.loads((BENCH / "graphs" / f"{p}.json").read_text())["nodes"]}
        renders = sorted((e["source"], e["target"]) for e in graph["links"]
                         if e["relation"] == "renders")
        new = sorted(i for i in nodes if i not in v14)
        report.append(f"## {p}: {len(renders)} renders edges, {len(new)} nodes new in v1.5\n")
        for k, (u, v) in enumerate(rng.sample(renders, n_edges), 1):
            cu, cv = nodes[u], nodes[v]
            fu, fv = cu["source_file"], cv["source_file"]
            lu, lv = _lines(root, fu), _lines(root, fv)
            label = cv["label"].rstrip("()")
            report.append(f"R{p[0]}{k:02d}  {u}  --renders-->  {v}")
            report.append(_show(fu, lu, cu["line"] - 1) + "   [caller]")
            # Import statements (multi-line too) naming the target or its module, and
            # lazy/dynamic bindings.
            mod = Path(fv).with_suffix("").name
            whole = "\n".join(lu)
            for m in re.finditer(r"^\s*import\b[^;]*?\bfrom\s*['\"][^'\"]+['\"]", whole,
                                 re.MULTILINE):
                stmt = m.group(0)
                if re.search(rf"['\"/]{re.escape(mod)}['\"]|\b{re.escape(label)}\b", stmt):
                    first = whole.count("\n", 0, m.start(0) + len(stmt) - len(stmt.lstrip()))
                    tail = " ".join(stmt.split())[-60:]
                    report.append(_show(fu, lu, first) + f"   [binding ... {tail}]")
            for i, text in enumerate(lu):
                if re.search(r"\b(lazy|dynamic)\s*\(", text):
                    report.append(_show(fu, lu, i) + "   [binding]")
            # Every element of the file naming the target (<Label, <Ns.Label), with the
            # last node declared above it: the edge is right only if the JSX is in u's body.
            tag = re.compile(rf"<(?:[\w$]+\.)?{re.escape(label)}(?![\w$])")
            above = sorted((n["line"], n["id"]) for n in graph["nodes"]
                           if n.get("source_file") == fu and n.get("type") != "module")
            for i, text in enumerate(lu):
                if m := tag.search(text):
                    last = max((x for x in above if x[0] <= i + 1), default=(0, "(module)"))
                    report.append(_show(fu, lu, i) + f"   [jsx; below {last[1].rsplit('.', 1)[-1]}]")
                    report.append(f"        inside: {_enclosing(root / fu, i + 1, m.start())}")
            report.append(_show(fv, lv, cv["line"] - 1) + "   [target]")
            report.append("")
        wrapped = [i for i in new if re.search(r"\b(memo|forwardRef)\b",
                                               _lines(root, nodes[i]["source_file"])
                                               [nodes[i]["line"] - 1])]
        report.append(f"### {p}: {len(wrapped)} of the {len(new)} new nodes wrap memo/forwardRef "
                      f"on their declaration line\n")
        for k, i in enumerate(rng.sample(wrapped, n_nodes), 1):
            n = nodes[i]
            lines = _lines(root, n["source_file"])
            report.append(f"W{p[0]}{k:02d}  {i}  ({n.get('type')})")
            for j in range(n["line"] - 1, min(len(lines), n["line"] + 2)):
                report.append(_show(n["source_file"], lines, j) + "   [declaration]")
            for j, text in enumerate(lines):
                # `from "react"` also ends a multi-line import
                if re.search(r"\b(import|from)\b.*['\"]react['\"]", text):
                    report.append(_show(n["source_file"], lines, j) + "   [react import]")
            report.append("")
        others = [i for i in new if i not in wrapped]
        report.append(f"### {p}: new nodes not on a memo/forwardRef line: {others}\n")
        for i in others:
            n = nodes[i]
            lines = _lines(root, n["source_file"])
            for j in range(n["line"] - 1, min(len(lines), n["line"] + 3)):
                report.append(_show(n["source_file"], lines, j) + "   [declaration]")
            report.append("")
    (OUT / "sample.txt").write_text("\n".join(report) + "\n")
    print("\n".join(report))


if __name__ == "__main__":
    main()
