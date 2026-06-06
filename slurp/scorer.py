"""Scores graph nodes by relevance to a query string."""

import math
import re
from collections import Counter

import networkx as nx


def _pagerank(
    G: nx.DiGraph,
    alpha: float = 0.85,
    max_iter: int = 100,
    tol: float = 1e-6,
) -> dict[str, float]:
    """Power-iteration PageRank without numpy/scipy.

    Handles dangling nodes (no out-edges) by redistributing their rank
    uniformly across all nodes, matching the standard Google-matrix formula.
    """
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

    return rank


def _tokenize(text: str) -> list[str]:
    """Splits text into tokens handling snake_case, camelCase, and PascalCase.

    Pipeline:
    1. Insert spaces at camelCase/PascalCase boundaries (before lowercasing so
       boundary info is preserved): "recalcularPlayerStats" → "recalcular Player Stats".
    2. Lowercase and split on every non-alphanumeric character (covers snake_case,
       hyphens, dots, spaces, etc.).
    3. Append any original (pre-split) tokens that are not already in the result,
       so full compound identifiers remain searchable alongside their parts.
    """
    # Step 1: split camelCase/PascalCase before losing case info.
    #   "XMLParser"         → "XML Parser"    (uppercase run before capitalized word)
    #   "recalcularPlayer"  → "recalcular Player"  (lowercase-to-uppercase boundary)
    split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", split)

    # Step 2: lowercase + extract alphanumeric runs (covers _ . - spaces …).
    primary = re.findall(r"[a-z0-9]+", split.lower())

    # Step 3: also extract original tokens so compound identifiers are searchable.
    original = re.findall(r"[a-z0-9]+", text.lower())
    seen = set(primary)
    extras = [t for t in original if t not in seen]

    return primary + extras


def _tfidf_scores(G: nx.DiGraph, query: str) -> dict[str, float]:
    """Cosine similarity between the query and each node's label + description.

    Uses smoothed IDF (sklearn-style: log((N+1)/(df+1)) + 1) restricted to
    query terms only — avoids building a full vocabulary while remaining
    correct for our retrieval use-case.
    """
    query_tokens = _tokenize(query)
    if not query_tokens:
        return {node_id: 0.0 for node_id in G.nodes}

    docs: dict[str, list[str]] = {}
    for node_id in G.nodes:
        attrs = G.nodes[node_id]
        text = f"{attrs.get('label', '')} {attrs.get('description', '')}"
        docs[node_id] = _tokenize(text)

    N = len(docs)

    # IDF for query terms only — computed once, reused across all docs.
    idf: dict[str, float] = {}
    for term in set(query_tokens):
        df = sum(1 for tokens in docs.values() if term in set(tokens))
        idf[term] = math.log((N + 1) / (df + 1)) + 1

    # Query vector (TF-IDF).
    q_tf = Counter(query_tokens)
    q_vec = {t: (cnt / len(query_tokens)) * idf[t] for t, cnt in q_tf.items()}
    q_norm = math.sqrt(sum(v * v for v in q_vec.values()))

    scores: dict[str, float] = {}
    for node_id, tokens in docs.items():
        if not tokens:
            scores[node_id] = 0.0
            continue

        d_tf = Counter(tokens)
        # Only keep terms that appear in the query (others contribute 0 to dot product).
        d_vec = {t: (d_tf[t] / len(tokens)) * idf[t] for t in idf if d_tf[t] > 0}
        d_norm = math.sqrt(sum(v * v for v in d_vec.values()))

        if d_norm == 0.0:
            scores[node_id] = 0.0
        else:
            dot = sum(q_vec[t] * d_vec[t] for t in d_vec)
            scores[node_id] = dot / (q_norm * d_norm)

    return scores


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

    # DECISION: nodes with file_type="code" get a +0.3 boost, clamped to 1.0.
    # Documentation files (READMEs, changelogs) compete unfairly with code in
    # technical queries; this bias keeps architecture/API results on top.
    result: dict[str, float] = {}
    for nid in G.nodes:
        score = 0.4 * pr_norm[nid] + 0.6 * tfidf[nid]
        if G.nodes[nid].get("file_type") == "code":
            score = min(1.0, score + 0.3)
        result[nid] = score

    return result, pr_norm, tfidf


def score_nodes(G: nx.DiGraph, query: str) -> dict[str, float]:
    """Returns {node_id: score} with scores normalized between 0 and 1.

    Combines two signals:
    - Structural (40%): PageRank normalized by the max value in the graph.
    - Semantic  (60%): TF-IDF cosine similarity between query and node text.

    Args:
        G: Directed graph with node attributes (label, description).
        query: Free-text query string.

    Returns:
        Dict mapping every node ID to a score in [0.0, 1.0].
    """
    if G.number_of_nodes() == 0:
        return {}
    final, _, _ = _compute_scores(G, query)
    return final


def score_nodes_detailed(
    G: nx.DiGraph,
    query: str,
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """Like score_nodes but also returns the structural and semantic components.

    Args:
        G: Directed graph with node attributes (label, description).
        query: Free-text query string.

    Returns:
        Tuple of (final_scores, structural_scores, semantic_scores).
        structural_scores: normalized PageRank values (0–1, before 0.4 weight).
        semantic_scores: TF-IDF cosine similarities (0–1, before 0.6 weight).
    """
    if G.number_of_nodes() == 0:
        return {}, {}, {}
    return _compute_scores(G, query)
