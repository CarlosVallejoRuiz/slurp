"""The v1.4 ranking study's experimental scorer. Not part of slurp.

Design: docs/design/orden-v1.4.md §3. One configurable function stands for every
variant, and BASE reproduces slurp v1.3's score_nodes exactly (a test compares them):

    final = min(1, 0.4 · structural + 0.6 · lexical + 0.3 if code)

    lexical    tfidf  v1.3's cosine over the name (label + description), restricted to
                      the query's terms; w_path adds the path and scope field (V3)
               bm25   BM25 over name (weight w_name), doc comment (V1, doc=True) and
                      path (w_path), divided by the query's best (0–1)
               stem (V5), stop (V6) and cohesion (V4) modify it
    structural pr     v1.3's global PageRank, divided by its max
               ppr    personalised PageRank from the k best lexical matches, by local
                      push (V2); ties broken by global PageRank × 1e-6

Doc comments come from the #21 branch's own extraction (copied below) applied to the
v1.3 graphs: the full text in a "doc" attribute that only this scorer reads, and, when
printed (V1a), its first line in "description", as the branch prints it.
"""

from __future__ import annotations

import ast
import heapq
import math
import re
from collections import Counter
from pathlib import Path
from typing import Callable

import networkx as nx

from slurp._graphutils import _left_out, _tokenize
from slurp.scorer import _pagerank

BASE = {"lex": "tfidf", "k1": 1.2, "b": 0.75, "w_name": 1.0, "doc": False, "w_path": 0.0,
        "stem": False, "stop": False, "struct": "pr", "restart": 0.3, "r": 0.5, "k": 10,
        "cohesion": 0.0, "print_doc": False}

# V6: generic English function words, fixed in the design before measuring.
STOP_EN = frozenset("""a an the and or but nor so yet if then than as at by for from in into of
off on onto out over to up with without within about above after against before below between
during through under until via per i me my we us our you your he him his she her it its they
them their this that these those who whom which what whose am is are was were be been being do
does did doing have has had having will would shall should can could may might must there here
when where why how all any both each few more most other some such no not only own same too
very just also""".split())

# V3: path terms that name no area of a project.
GENERIC_PATH = frozenset({"src", "lib", "index", "main", "internal", "pkg", "mod", "util", "utils",
                          "core", "test", "tests", "org", "com", "py", "ts", "tsx", "js", "jsx",
                          "mjs", "cjs", "go", "rs", "java"})
PPR_EPS = 1e-4
PPR_TIE = 1e-6


def stem(t: str) -> str:
    """V5: strip -ies, -ing, -ed, -es, -s from terms longer than four letters."""
    for suf, rep in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if len(t) > 4 and t.endswith(suf):
            return t[: -len(suf)] + rep
    return t


def query_terms(query: str, cfg: dict) -> list[str]:
    terms = _tokenize(query)
    if cfg["stop"]:
        kept = [t for t in terms if t not in STOP_EN]
        terms = kept or terms
    if cfg["stem"]:
        terms = [stem(t) for t in terms]
    return terms


class Index:
    """Per-graph caches: tokens per field, groups, neighbours, PageRank."""

    def __init__(self, G: nx.DiGraph) -> None:
        self.G = G
        self._fields: dict[bool, dict] = {}
        self._groups = None
        self._adj = None
        self._pr = None
        self.left_out = _left_out(G)

    def fields(self, stemmed: bool) -> dict:
        """Tokens per field and node; postings and counts are built when first asked for.

        DECISION: a file's path and a scope's terms are tokenized once and shared by
        every node in them, and counts exist only for nodes a query touches: the first
        query on a large graph pays for little more than v1.3's own tokenization.
        """
        if stemmed in self._fields:
            return self._fields[stemmed]
        f = (lambda ts: [stem(t) for t in ts]) if stemmed else (lambda ts: ts)
        name, doc, path = {}, {}, {}
        per_file: dict[str, set] = {}
        per_scope: dict[str, set] = {}
        for n in self.G.nodes:
            a = self.G.nodes[n]
            desc0 = a.get("_desc0", a.get("description", ""))
            name[n] = f(_tokenize(f"{a.get('label', '')} {desc0}"))
            if a.get("doc"):
                doc[n] = f(_tokenize(a["doc"]))
            src = str(a.get("source_file") or a.get("file_path") or "")
            scope = str(n).rsplit(".", 1)[0] if "." in str(n) else ""
            if src not in per_file:
                per_file[src] = {t for t in _tokenize(src) if t not in GENERIC_PATH}
            if scope not in per_scope:
                per_scope[scope] = {t for t in _tokenize(scope.replace(".", " "))
                                    if t not in GENERIC_PATH}
            path[n] = (src, scope)
        cache: dict[tuple, list] = {}

        def path_tokens(n: str) -> list[str]:
            key = path[n]
            if key not in cache:
                cache[key] = f(sorted(per_file[key[0]] | per_scope[key[1]]))
            return cache[key]
        out = {"name": name, "doc": doc, "path_of": path_tokens, "post": {}, "count": {}}
        self._fields[stemmed] = out
        return out

    def tokens(self, F: dict, field: str, n: str) -> list[str]:
        if field == "name":
            return F["name"][n]
        if field == "doc":
            return F["doc"].get(n, [])
        return F["path_of"](n)

    def postings(self, F: dict, field: str) -> dict[str, set]:
        if field not in F["post"]:
            post: dict[str, set] = {}
            for n in self.G.nodes:
                for t in self.tokens(F, field, n):
                    post.setdefault(t, set()).add(n)
            F["post"][field] = post
        return F["post"][field]

    def count(self, F: dict, field: str, n: str) -> Counter:
        key = (field, n)
        if key not in F["count"]:
            F["count"][key] = Counter(self.tokens(F, field, n))
        return F["count"][key]

    def groups(self) -> tuple[dict, dict]:
        """node -> its file's nodes, node -> its siblings (same parent id)."""
        if self._groups is None:
            by_file, by_parent = {}, {}
            for n in self.G.nodes:
                a = self.G.nodes[n]
                by_file.setdefault(a.get("source_file") or a.get("file_path"), []).append(n)
                by_parent.setdefault(str(n).rsplit(".", 1)[0], []).append(n)
            self._groups = (by_file, by_parent)
        return self._groups

    def pr_norm(self) -> dict[str, float]:
        if self._pr is None:
            pagerank = _pagerank(self.G)
            max_pr = max(pagerank.values())
            self._pr = {nid: (pr / max_pr) if max_pr > 0.0 else 0.0
                        for nid, pr in pagerank.items()}
        return self._pr

    def adjacency(self, r: float) -> dict[str, list[tuple[str, float]]]:
        """Transition weights: calls forward 1, backward r; contains both ways 1."""
        self._adj = self._adj or {}
        if r not in self._adj:
            nb: dict[str, dict[str, float]] = {n: {} for n in self.G.nodes}
            for u, v, d in self.G.edges(data=True):
                rel = d.get("relation")
                if rel == "calls":
                    nb[u][v] = nb[u].get(v, 0.0) + 1.0
                    if r > 0:
                        nb[v][u] = nb[v].get(u, 0.0) + r
                elif rel == "contains":
                    nb[u][v] = nb[u].get(v, 0.0) + 1.0
                    nb[v][u] = nb[v].get(u, 0.0) + 1.0
            self._adj[r] = {n: [(m, w / sum(ws.values())) for m, w in ws.items()]
                            for n, ws in nb.items() if ws}
        return self._adj[r]


def _tfidf(G: nx.DiGraph, q: list[str], cfg: dict, idx: Index) -> dict[str, float]:
    """v1.3's _tfidf_scores, with an optional weighted path field (V3)."""
    if not q:
        return {node_id: 0.0 for node_id in G.nodes}
    F = idx.fields(cfg["stem"])
    wp = cfg["w_path"]
    N = G.number_of_nodes()
    idf: dict[str, float] = {}
    pname = idx.postings(F, "name")
    ppath = idx.postings(F, "path") if wp else {}
    for term in set(q):
        holders = set(pname.get(term, ()))
        if wp:
            holders |= ppath.get(term, set())
        idf[term] = math.log((N + 1) / (len(holders) + 1)) + 1
    q_tf = Counter(q)
    q_vec = {t: (cnt / len(q)) * idf[t] for t, cnt in q_tf.items()}
    q_norm = math.sqrt(sum(v * v for v in q_vec.values()))
    touched: set = set()
    for t in idf:
        touched |= pname.get(t, set())
        if wp:
            touched |= ppath.get(t, set())
    scores = {n: 0.0 for n in G.nodes}
    for n in touched:
        cn = idx.count(F, "name", n)
        if wp:
            cp = idx.count(F, "path", n)
            length = len(F["name"][n]) + wp * len(idx.tokens(F, "path", n))
            d_vec = {t: ((cn[t] + wp * cp[t]) / length) * idf[t] for t in idf
                     if cn[t] + wp * cp[t] > 0}
        else:
            length = len(F["name"][n])
            if not length:
                continue
            d_vec = {t: (cn[t] / length) * idf[t] for t in idf if cn[t] > 0}
        d_norm = math.sqrt(sum(v * v for v in d_vec.values()))
        if d_norm:
            scores[n] = sum(q_vec[t] * d_vec[t] for t in d_vec) / (q_norm * d_norm)
    return scores


def _bm25(G: nx.DiGraph, q: list[str], cfg: dict, idx: Index) -> dict[str, float]:
    """BM25 over name (w_name), doc (V1) and path (w_path) fields, divided by the best."""
    scores = {n: 0.0 for n in G.nodes}
    terms = list(dict.fromkeys(q))
    if not terms:
        return scores
    F = idx.fields(cfg["stem"])
    wn, wd, wp = cfg["w_name"], (1.0 if cfg["doc"] else 0.0), cfg["w_path"]
    k1, b = cfg["k1"], cfg["b"]
    key = ("dl", cfg["stem"], wn, wd, wp)
    if key not in F:
        dl = {n: wn * len(F["name"][n]) + wd * len(F["doc"].get(n, ()))
              + (wp * len(idx.tokens(F, "path", n)) if wp else 0.0) for n in G.nodes}
        F[key] = (dl, (sum(dl.values()) / max(1, sum(1 for v in dl.values() if v))) or 1.0)
    dl, avgdl = F[key]
    N = G.number_of_nodes()
    pname = idx.postings(F, "name")
    pdoc = idx.postings(F, "doc") if wd else {}
    ppath = idx.postings(F, "path") if wp else {}
    for t in terms:
        holders = set(pname.get(t, ()))
        if wd:
            holders |= pdoc.get(t, set())
        if wp:
            holders |= ppath.get(t, set())
        if not holders:
            continue
        idf = math.log(1 + (N - len(holders) + 0.5) / (len(holders) + 0.5))
        for n in holders:
            tf = (wn * idx.count(F, "name", n)[t] + (wd * idx.count(F, "doc", n)[t] if wd else 0)
                  + (wp * idx.count(F, "path", n)[t] if wp else 0))
            if tf > 0:
                scores[n] += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl[n] / avgdl))
    best = max(scores.values())
    return {n: v / best for n, v in scores.items()} if best > 0 else scores


def lexical(G: nx.DiGraph, query: str, cfg: dict, idx: Index) -> dict[str, float]:
    q = query_terms(query, cfg)
    lex = _tfidf(G, q, cfg, idx) if cfg["lex"] == "tfidf" else _bm25(G, q, cfg, idx)
    lam = cfg["cohesion"]
    if lam:
        by_file, by_parent = idx.groups()
        best_file = {key: max(lex[m] for m in ms) for key, ms in by_file.items()}
        best_par = {key: max(lex[m] for m in ms) for key, ms in by_parent.items()}
        out = {}
        for n in G.nodes:
            a = G.nodes[n]
            f = best_file[a.get("source_file") or a.get("file_path")]
            p = best_par[str(n).rsplit(".", 1)[0]]
            out[n] = max(lex[n], lam * f, lam * p)
        lex = out
    return lex


def structural(G: nx.DiGraph, lex: dict[str, float], cfg: dict, idx: Index) -> dict[str, float]:
    pr = idx.pr_norm()
    if cfg["struct"] == "pr":
        return pr
    seeds = sorted((n for n in G.nodes if lex[n] > 0 and n not in idx.left_out),
                   key=lambda n: (-lex[n], n))[: cfg["k"]]
    if not seeds:
        return pr
    total = sum(lex[s] for s in seeds)
    alpha, adj = cfg["restart"], idx.adjacency(cfg["r"])
    p: dict[str, float] = {}
    res = {s: lex[s] / total for s in seeds}
    queue = list(seeds)
    queued = set(queue)
    # Local push, LIFO. The bindings below only save lookups: the order of the
    # pushes and every operation are those of the plain loop (a test compares).
    eps, stay = PPR_EPS, 1 - alpha
    res_get, adj_get, p_get = res.get, adj.get, p.get
    pop, push, mark, unmark = queue.pop, queue.append, queued.add, queued.discard
    while queue:
        u = pop()
        unmark(u)
        ru = res_get(u, 0.0)
        if ru < eps:
            continue
        res[u] = 0.0
        p[u] = p_get(u, 0.0) + alpha * ru
        out = adj_get(u)
        if not out:                       # nowhere to go: the walk stays
            p[u] += stay * ru
            continue
        spread = stay * ru
        for v, w in out:
            nv = res_get(v, 0.0) + spread * w
            res[v] = nv
            if nv >= eps and v not in queued:
                push(v)
                mark(v)
    top = max(p.values())
    return {n: p.get(n, 0.0) / top + PPR_TIE * pr[n] for n in G.nodes}


def score(G: nx.DiGraph, query: str, cfg: dict, idx: Index) -> dict[str, float]:
    if G.number_of_nodes() == 0:
        return {}
    lex = lexical(G, query, cfg, idx)
    st = structural(G, lex, cfg, idx)
    result: dict[str, float] = {}
    for nid in G.nodes:
        s = 0.4 * st[nid] + 0.6 * lex[nid]
        if G.nodes[nid].get("file_type") == "code":
            s = min(1.0, s + 0.3)
        result[nid] = s
    return result


# --- doc comments, as the #21 branch reads them (copied from b588e81) -------------------

_DECORATOR_LINE = re.compile(r"^\s*@[\w.]+(\(.*\))?\s*$")
_ATTRIBUTE_LINE = re.compile(r"^\s*#!?\[.*\]\s*$")
_GO_DIRECTIVE = re.compile(r"^//(?:go:|line |export |extern |nolint|\+build)")
_DOC_SKIPPABLE = {"block": _DECORATOR_LINE, "rust": _ATTRIBUTE_LINE}


def doc_comment_above(lines: list[str], lineno: int, style: str) -> str:
    """The doc comment written above line *lineno* (1-based), cleaned, or ""."""
    i = lineno - 2
    skippable = _DOC_SKIPPABLE.get(style)
    while skippable is not None and 0 <= i < len(lines) and skippable.match(lines[i]):
        i -= 1
    if not 0 <= i < len(lines):
        return ""
    if style == "block":
        end = i
        if not lines[end].rstrip().endswith("*/"):
            return ""
        start = end
        while start >= 0 and "/*" not in lines[start]:
            start -= 1
        opening = lines[start].lstrip() if start >= 0 else ""
        if not opening.startswith("/**") or opening.startswith("/**/"):
            return ""
        text = "\n".join(lines[start:end + 1]).strip()
        body = text[3:text.rindex("*/")]
        cleaned = [re.sub(r"^\s*\*(?!/) ?", "", ln) if ln.lstrip().startswith("*")
                   else ln.strip() for ln in body.splitlines()]
        return "\n".join(ln.rstrip() for ln in cleaned).strip()
    marker = "///" if style == "rust" else "//"
    collected: list[str] = []
    while i >= 0:
        line = lines[i].strip()
        if not line.startswith(marker) or (style == "rust" and line.startswith("////")):
            break
        if not (style == "go" and _GO_DIRECTIVE.match(line)):
            rest = line[len(marker):]
            collected.append(rest[1:] if rest.startswith(" ") else rest)
        i -= 1
    return "\n".join(reversed(collected)).strip()


def python_docstrings(source: str) -> dict[int, str]:
    """line -> docstring of each def and class; 0 -> the module's."""
    out: dict[int, str] = {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out
    if doc := ast.get_docstring(tree):
        out[0] = doc
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if doc := ast.get_docstring(node):
                out[node.lineno] = doc
    return out


_STYLE = {".ts": "block", ".tsx": "block", ".js": "block", ".jsx": "block", ".mjs": "block",
          ".cjs": "block", ".java": "block", ".go": "go", ".rs": "rust"}


def attach_docs(G: nx.DiGraph, read_source: Callable[[str], str | None]) -> int:
    """Store each definition's doc comment in "doc"; how many got one."""
    count = 0
    cache: dict[str, tuple] = {}
    for n in G.nodes:
        a = G.nodes[n]
        a.setdefault("_desc0", a.get("description", ""))
        path = str(a.get("source_file") or "")
        ext = Path(path).suffix.lower()
        loc = str(a.get("source_location", ""))
        if not path or a.get("type") == "import" or a["_desc0"]:
            continue
        if ext != ".py" and a.get("type") == "module":
            continue
        if path not in cache:
            src = read_source(path)
            cache[path] = (None, None) if src is None else (
                src.splitlines(), python_docstrings(src) if ext == ".py" else None)
        lines, pydocs = cache[path]
        if lines is None:
            continue
        doc = ""
        if ext == ".py":
            line = 0 if a.get("type") == "module" else (
                int(loc[1:]) if loc[:1] == "L" and loc[1:].isdigit() else -1)
            doc = pydocs.get(line, "")
        elif ext in _STYLE and loc[:1] == "L" and loc[1:].isdigit():
            doc = doc_comment_above(lines, int(loc[1:]), _STYLE[ext])
        if doc:
            a["doc"] = doc
            count += 1
    return count


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def print_docs(G: nx.DiGraph, on: bool) -> None:
    """V1a: show the doc's first line as the description; V1b: the original description."""
    for n in G.nodes:
        a = G.nodes[n]
        if "doc" in a:
            a["description"] = _first_line(a["doc"]) if on else a.get("_desc0", "")


__all__ = ["BASE", "STOP_EN", "Index", "lexical", "structural", "score", "attach_docs",
           "print_docs", "doc_comment_above", "python_docstrings", "query_terms", "stem",
           "heapq"]
