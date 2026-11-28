"""Scores graph nodes by relevance to a query string."""

from __future__ import annotations

import math
import weakref
from collections import Counter

import networkx as nx

from slurp._graphutils import _left_out, _tokenize

# DECISION (v1.4, docs/design/orden-v1.4.md, accepted in
# docs/design/orden-v1.4/resultados/validacion.md): two changes to the ranking,
# measured on 1,525 questions from commit history and validated once on six
# held-out projects (recall at 4000, markdown: 0.543 -> 0.674).
#   - The semantic part reads a node's path and scope as well as its name: the
#     terms of its file path and of the class or module that holds it (its id
#     without the last segment), generic ones left out, at the name's weight.
#     94 % of the definitions v1.3 missed shared no query term with their name.
#   - The structural part is a PageRank personalised on the query: a walk that
#     restarts at the ten best lexical matches (production code only) and
#     follows calls both ways and contains both ways, computed by local push.
#     A query that matches nothing keeps v1.3's global PageRank.
_PATH_WEIGHT = 1.0
_GENERIC_PATH = frozenset({"src", "lib", "index", "main", "internal", "pkg", "mod", "util",
                           "utils", "core", "test", "tests", "org", "com", "py", "ts", "tsx",
                           "js", "jsx", "mjs", "cjs", "go", "rs", "java"})
_PPR_RESTART = 0.15
_PPR_BACK = 1.0          # weight of walking a calls edge from callee to caller
_PPR_SEEDS = 10
_PPR_EPS = 1e-4          # local push stops below this residual
_PPR_TIE = 1e-6          # nodes the walk never reaches stay in global PageRank order


# DECISION: PageRank depends only on G, never on the query, so it is cached for
# the lifetime of the graph object. The value keeps a weakref alongside the
# result because CPython recycles id() after garbage collection — without it a
# freshly loaded graph could land on a dead graph's address and silently inherit
# its ranks. That is exactly the case the MCP server hits when it reloads.
# Since v1.4 an entry also records the node and edge counts it was computed on,
# so a graph that grows or shrinks in place is never served stale ranks.
_pagerank_cache: dict[int, tuple["weakref.ref", tuple[int, int], dict[str, float]]] = {}


# v1.4: a graph's tokens (name, path and scope) and the walk's transition lists
# are cached the same way, keyed also by the node and edge counts. Before v1.4
# every query re-tokenized every label, which was most of a warm query's time.
_text_cache: dict[int, tuple["weakref.ref", tuple[int, int], "_GraphText"]] = {}
_walk_cache: dict[int, tuple["weakref.ref", tuple[int, int], dict]] = {}


def clear_pagerank_cache() -> None:
    """Drop every cached PageRank result, and the tokens and walks cached with them.

    Call this whenever a graph is replaced in-process — the MCP server does it
    on reload — so no stale ranks survive. A graph edited in place without
    changing its node or edge count needs it too.
    """
    _pagerank_cache.clear()
    _text_cache.clear()
    _walk_cache.clear()


def _cached(cache: dict, G: nx.DiGraph, build):
    key, size = id(G), (G.number_of_nodes(), G.number_of_edges())
    hit = cache.get(key)
    if hit is not None and hit[0]() is G and hit[1] == size:
        return hit[2]
    value = build(G)
    try:
        cache[key] = (weakref.ref(G), size, value)
    except TypeError:
        pass  # INTENTIONAL: no weak references, no cache; the value is still right
    return value


class _GraphText:
    """A graph's tokens: names (label + description) and paths (file + scope).

    Postings and per-node counts are built when first asked for; a file's or a
    scope's terms are tokenized once and shared by every node in them.
    """

    def __init__(self, G: nx.DiGraph) -> None:
        self.G = G
        self.name: dict[str, list[str]] = {}
        self._path_key: dict[str, tuple[str, str]] = {}
        self._per_file: dict[str, set[str]] = {}
        self._per_scope: dict[str, set[str]] = {}
        self._path_tokens: dict[tuple[str, str], list[str]] = {}
        self._postings: dict[str, dict[str, set]] = {}
        self._counts: dict[tuple[str, str], Counter] = {}
        for n in G.nodes:
            a = G.nodes[n]
            self.name[n] = _tokenize(f"{a.get('label', '')} {a.get('description', '')}")
            src = str(a.get("source_file") or a.get("file_path") or "")
            scope = str(n).rsplit(".", 1)[0] if "." in str(n) else ""
            if src not in self._per_file:
                self._per_file[src] = {t for t in _tokenize(src) if t not in _GENERIC_PATH}
            if scope not in self._per_scope:
                self._per_scope[scope] = {t for t in _tokenize(scope.replace(".", " "))
                                          if t not in _GENERIC_PATH}
            self._path_key[n] = (src, scope)

    def tokens(self, field: str, n: str) -> list[str]:
        if field == "name":
            return self.name[n]
        key = self._path_key[n]
        if key not in self._path_tokens:
            self._path_tokens[key] = sorted(self._per_file[key[0]] | self._per_scope[key[1]])
        return self._path_tokens[key]

    def postings(self, field: str) -> dict[str, set]:
        if field not in self._postings:
            post: dict[str, set] = {}
            if field == "name":
                for n in self.G.nodes:
                    for t in self.name[n]:
                        post.setdefault(t, set()).add(n)
            else:
                # Every node of a (file, scope) pair has the same path terms.
                groups: dict[tuple[str, str], list[str]] = {}
                for n in self.G.nodes:
                    groups.setdefault(self._path_key[n], []).append(n)
                for key, nodes in groups.items():
                    for t in self.tokens(field, nodes[0]):
                        post.setdefault(t, set()).update(nodes)
            self._postings[field] = post
        return self._postings[field]

    def count(self, field: str, n: str) -> Counter:
        key = (field, n)
        if key not in self._counts:
            self._counts[key] = Counter(self.tokens(field, n))
        return self._counts[key]


def _walk_lists(G: nx.DiGraph) -> dict[str, list[tuple[str, float]]]:
    """Transition lists: calls forward 1 and backward _PPR_BACK, contains both ways 1."""
    nb: dict[str, dict[str, float]] = {n: {} for n in G.nodes}
    for u, v, d in G.edges(data=True):
        rel = d.get("relation")
        if rel == "calls":
            nb[u][v] = nb[u].get(v, 0.0) + 1.0
            if _PPR_BACK > 0:
                nb[v][u] = nb[v].get(u, 0.0) + _PPR_BACK
        elif rel == "contains":
            nb[u][v] = nb[u].get(v, 0.0) + 1.0
            nb[v][u] = nb[v].get(u, 0.0) + 1.0
    out: dict[str, list[tuple[str, float]]] = {}
    for n, ws in nb.items():
        if ws:
            total = sum(ws.values())
            out[n] = [(m, w / total) for m, w in ws.items()]
    return out


def _pagerank(
    G: nx.DiGraph,
    alpha: float = 0.85,
    max_iter: int = 100,
    tol: float = 1e-6,
) -> dict[str, float]:
    """Power-iteration PageRank without numpy/scipy.

    Handles dangling nodes (no out-edges) by redistributing their rank
    uniformly across all nodes, matching the standard Google-matrix formula.

    Results are cached per graph object; see clear_pagerank_cache(). Only the
    default parameters are cached — a call with non-default alpha/max_iter/tol
    always recomputes and never poisons the cache.
    """
    cacheable = (alpha, max_iter, tol) == (0.85, 100, 1e-6)
    key = id(G)
    size = (G.number_of_nodes(), G.number_of_edges())
    if cacheable and (hit := _pagerank_cache.get(key)) is not None:
        ref, cached_size, ranks = hit
        if ref() is G and cached_size == size:
            return ranks
        # id() was recycled by a different object, or the graph changed size.
        del _pagerank_cache[key]

    nodes = list(G.nodes)
    N = len(nodes)
    rank: dict[str, float] = {n: 1.0 / N for n in nodes}

    # Precompute weighted out-degrees once.
    out_w = {n: G.out_degree(n, weight="weight") for n in nodes}
    dangling = [n for n in nodes if out_w[n] == 0]

    for _ in range(max_iter):
        prev = rank.copy()
        dangling_sum = alpha * sum(prev[n] for n in dangling) / N

        for n in nodes:
            incoming = sum(
                prev[p] * G[p][n].get("weight", 1) / out_w[p]
                for p in G.predecessors(n)
            )
            rank[n] = alpha * incoming + dangling_sum + (1.0 - alpha) / N

        if sum(abs(rank[n] - prev[n]) for n in nodes) < N * tol:
            break

    if cacheable:
        try:
            _pagerank_cache[key] = (weakref.ref(G), size, rank)
        except TypeError:
            # INTENTIONAL: some graph types do not support weak references.
            # The cache is an optimisation, so losing it costs time, never
            # correctness — the computed rank is returned either way.
            pass

    return rank


def _tfidf_scores(G: nx.DiGraph, query: str) -> dict[str, float]:
    """Cosine similarity between the query and each node's name and path.

    The document of a node is its name (label + description) plus, at
    _PATH_WEIGHT, the terms of its file path and of its scope (v1.4). Uses
    smoothed IDF (sklearn-style: log((N+1)/(df+1)) + 1) restricted to query
    terms only — avoids building a full vocabulary while remaining correct for
    our retrieval use-case. Only nodes holding a query term are visited.
    """
    query_tokens = _tokenize(query)
    if not query_tokens:
        return {node_id: 0.0 for node_id in G.nodes}

    text = _cached(_text_cache, G, _GraphText)
    wp = _PATH_WEIGHT
    N = G.number_of_nodes()
    pname, ppath = text.postings("name"), text.postings("path")

    # IDF for query terms only — computed once, reused across all docs.
    idf: dict[str, float] = {}
    for term in set(query_tokens):
        holders = set(pname.get(term, ()))
        holders |= ppath.get(term, set())
        idf[term] = math.log((N + 1) / (len(holders) + 1)) + 1

    # Query vector (TF-IDF).
    q_tf = Counter(query_tokens)
    q_vec = {t: (cnt / len(query_tokens)) * idf[t] for t, cnt in q_tf.items()}
    q_norm = math.sqrt(sum(v * v for v in q_vec.values()))

    touched: set = set()
    for t in idf:
        touched |= pname.get(t, set())
        touched |= ppath.get(t, set())
    scores = {n: 0.0 for n in G.nodes}
    for n in touched:
        cn, cp = text.count("name", n), text.count("path", n)
        length = len(text.name[n]) + wp * len(text.tokens("path", n))
        # Only keep terms that appear in the query (others contribute 0 to dot product).
        d_vec = {t: ((cn[t] + wp * cp[t]) / length) * idf[t] for t in idf
                 if cn[t] + wp * cp[t] > 0}
        d_norm = math.sqrt(sum(v * v for v in d_vec.values()))
        if d_norm:
            scores[n] = sum(q_vec[t] * d_vec[t] for t in d_vec) / (q_norm * d_norm)
    return scores


def _structural(G: nx.DiGraph, lexical: dict[str, float],
                pr_norm: dict[str, float]) -> dict[str, float]:
    """PageRank personalised on the query's best lexical matches, by local push (v1.4).

    The walk restarts at the _PPR_SEEDS best-scoring production nodes (tests and
    imports never seed it), in proportion to their scores. Push order is LIFO
    and fixed, so the result is deterministic. With no lexical match the global
    PageRank stands, as in v1.3; nodes the walk never reaches keep its order.
    """
    left_out = _left_out(G)
    seeds = sorted((n for n in G.nodes if lexical[n] > 0 and n not in left_out),
                   key=lambda n: (-lexical[n], n))[:_PPR_SEEDS]
    if not seeds:
        return pr_norm
    total = sum(lexical[s] for s in seeds)
    alpha, adj = _PPR_RESTART, _cached(_walk_cache, G, _walk_lists)
    p: dict[str, float] = {}
    res = {s: lexical[s] / total for s in seeds}
    queue = list(seeds)
    queued = set(queue)
    # The bindings below only save lookups; every operation keeps its order.
    eps, stay = _PPR_EPS, 1 - alpha
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
    # Capped at 1.0: the tie-break would lift the walk's best node a hair above it.
    return {n: min(1.0, p.get(n, 0.0) / top + _PPR_TIE * pr_norm[n]) for n in G.nodes}


def _compute_scores(
    G: nx.DiGraph,
    query: str,
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """Core scoring logic shared by score_nodes and score_nodes_detailed.

    Returns:
        Tuple of (final, pr_norm, tfidf) where pr_norm and tfidf are the
        raw component values (0–1) before the 0.4/0.6 weighting.
    """
    # --- Structural component ---
    pagerank = _pagerank(G)
    max_pr = max(pagerank.values())
    # DECISION: normalize by max so the best-connected node always reaches 1.0
    # on the structural axis, making the 0.4 weight predictable in tests.
    pr_norm = {nid: (pr / max_pr) if max_pr > 0.0 else 0.0 for nid, pr in pagerank.items()}

    # --- Semantic component ---
    tfidf = _tfidf_scores(G, query)

    # --- Structural component, personalised on the query (v1.4) ---
    structural = _structural(G, tfidf, pr_norm)

    # DECISION: nodes with file_type="code" get a +0.3 boost, clamped to 1.0.
    # Documentation files (READMEs, changelogs) compete unfairly with code in
    # technical queries; this bias keeps architecture/API results on top.
    result: dict[str, float] = {}
    for nid in G.nodes:
        score = 0.4 * structural[nid] + 0.6 * tfidf[nid]
        if G.nodes[nid].get("file_type") == "code":
            score = min(1.0, score + 0.3)
        result[nid] = score

    return result, structural, tfidf


# ---------------------------------------------------------------------------
# Embedding backends (optional — require external packages)
# ---------------------------------------------------------------------------

def _node_text(attrs: dict) -> str:
    """Build a combined text representation of a node for embedding."""
    parts = [
        attrs.get("label", ""),
        attrs.get("type") or attrs.get("file_type") or "",
        attrs.get("description") or "",
    ]
    return " ".join(p for p in parts if p).strip()


def _cosine_sim(a: list[float], b: list[float]) -> float:
    """Cosine similarity clamped to [0.0, 1.0]. Returns 0.0 for zero vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


def _embed_openai(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts using OpenAI text-embedding-3-small.

    Raises:
        ImportError: If the openai package is not installed.
    """
    try:
        import openai
    except ImportError:
        raise ImportError(
            "The openai backend needs the openai package, which is the llm-openai "
            "extra.\n"
            'Install it with: pip install "slurp-graph[llm-openai]"'
        )
    client = openai.OpenAI()
    response = client.embeddings.create(model="text-embedding-3-small", input=texts)
    return [item.embedding for item in response.data]


# DECISION: there is no Anthropic embeddings backend. Anthropic offers no
# embedding model — POST /v1/embeddings answers 404 and the official docs point
# to Voyage AI — so the former _embed_anthropic could only ever fail with an API
# error. Asking for it now fails at once, with the reason, before any client
# is built or any node text leaves the machine.
_ANTHROPIC_BACKEND_RETIRED = (
    "backend='anthropic' is not available: Anthropic does not offer an embeddings "
    "endpoint or model. Use backend='openai' for real embeddings, or the default "
    "backend='tfidf'."
)


def _check_backend(backend: str) -> None:
    if backend == "anthropic":
        raise ValueError(_ANTHROPIC_BACKEND_RETIRED)


def _score_nodes_embeddings(
    G: nx.DiGraph,
    query: str,
    backend: str,
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """Score nodes using real embedding similarity instead of TF-IDF.

    Batches query + all node texts in a single API call for efficiency.

    Returns:
        (final, structural, semantic) where structural is all zeros and
        semantic equals final (cosine similarity scores in [0.0, 1.0]).
    """
    node_ids = list(G.nodes)
    node_texts = [_node_text(G.nodes[nid]) or str(nid) for nid in node_ids]
    all_texts = [query] + node_texts

    if backend == "openai":
        all_embeddings = _embed_openai(all_texts)
    else:
        raise ValueError(
            f"Unknown backend {backend!r}. Expected 'tfidf' or 'openai'."
        )

    query_emb = all_embeddings[0]
    zeros: dict[str, float] = {nid: 0.0 for nid in node_ids}
    semantic: dict[str, float] = {
        nid: _cosine_sim(query_emb, emb)
        for nid, emb in zip(node_ids, all_embeddings[1:])
    }
    return semantic, zeros, semantic


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def score_nodes(G: nx.DiGraph, query: str, backend: str = "tfidf") -> dict[str, float]:
    """Returns {node_id: score} with scores normalized between 0 and 1.

    Default backend combines two signals:
    - Structural (40%): PageRank normalized by the max value in the graph.
    - Semantic  (60%): TF-IDF cosine similarity between query and node text.

    With backend='openai', uses real embedding cosine similarity instead of
    TF-IDF. The structural PageRank component is dropped when using an
    embedding backend.

    Args:
        G: Directed graph with node attributes (label, description).
        query: Free-text query string.
        backend: Scoring backend — 'tfidf' (default) or 'openai'.

    Returns:
        Dict mapping every node ID to a score in [0.0, 1.0].

    Raises:
        ValueError: If backend is unknown, or is the retired 'anthropic'.
        ImportError: If the required package for an embedding backend is missing.
    """
    _check_backend(backend)
    if G.number_of_nodes() == 0:
        return {}
    if backend == "tfidf":
        final, _, _ = _compute_scores(G, query)
        return final
    final, _, _ = _score_nodes_embeddings(G, query, backend)
    return final


def score_nodes_detailed(
    G: nx.DiGraph,
    query: str,
    backend: str = "tfidf",
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """Like score_nodes but also returns the structural and semantic components.

    For the 'tfidf' backend:
        structural = normalized PageRank (0–1, before 0.4 weight).
        semantic   = TF-IDF cosine similarity (0–1, before 0.6 weight).

    For the embedding backend ('openai'):
        structural = all zeros (PageRank is not computed).
        semantic   = cosine similarity to query embedding (same as final).

    Args:
        G: Directed graph with node attributes (label, description).
        query: Free-text query string.
        backend: Scoring backend — 'tfidf' or 'openai'.

    Returns:
        Tuple of (final_scores, structural_scores, semantic_scores).

    Raises:
        ValueError: If backend is unknown, or is the retired 'anthropic'.
    """
    _check_backend(backend)
    if G.number_of_nodes() == 0:
        return {}, {}, {}
    if backend == "tfidf":
        return _compute_scores(G, query)
    return _score_nodes_embeddings(G, query, backend)
