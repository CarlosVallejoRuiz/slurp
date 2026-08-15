"""Selects the optimal subgraph that fits within a token budget."""

import functools
import heapq

import networkx as nx
import tiktoken


@functools.lru_cache(maxsize=8)
def _get_encoding(model: str) -> tiktoken.Encoding:
    return tiktoken.get_encoding(model)


def count_tokens(text: str, model: str = "cl100k_base") -> int:
    """Counts tokens in a string using tiktoken.

    Args:
        text: The text to tokenize.
        model: Tiktoken encoding name (default: cl100k_base).

    Returns:
        Number of tokens as an integer.
    """
    return len(_get_encoding(model).encode(text))


# DECISION: keyed by (node_id, model), not node_id alone — the same node costs a
# different number of tokens under a different encoding, and `--model` is a public
# CLI flag on both `slurp QUERY` and `slurp benchmark`.
#
# INVARIANT: entries are only valid while node text is unchanged. One process must
# not hold two graphs that share node ids with different content without calling
# clear_node_token_cache() in between. The MCP server does this on reload.
_node_token_cache: dict[tuple[str, str], int] = {}


def clear_node_token_cache() -> None:
    """Drop every cached per-node token count.

    Call this whenever a graph is replaced in-process — the MCP server does it
    on reload — so no stale counts survive.
    """
    _node_token_cache.clear()


def _node_token_cost(G: nx.DiGraph, node_id: str, model: str) -> int:
    """Token cost of a node, memoised across queries.

    Skips both the string rebuild and the tiktoken encode on a hit, which is
    where the bulk of select_subgraph's time goes on large graphs.
    """
    key = (node_id, model)
    cached = _node_token_cache.get(key)
    if cached is not None:
        return cached
    cost = count_tokens(_serialize_node(G, node_id), model)
    _node_token_cache[key] = cost
    return cost


def _serialize_node(G: nx.DiGraph, node_id: str) -> str:
    """Converts a node to a plain-text string for token counting and display."""
    attrs = G.nodes[node_id]
    parts: list[str] = [str(node_id)]
    if label := attrs.get("label"):
        parts.append(str(label))
    if ntype := attrs.get("type"):
        parts.append(f"({ntype})")
    if desc := attrs.get("description"):
        parts.append(str(desc))
    if fpath := attrs.get("file_path"):
        parts.append(f"-> {fpath}")
    return " ".join(parts)


def select_subgraph(
    G: nx.DiGraph,
    scores: dict[str, float],
    budget: int,
    model: str = "cl100k_base",
    neighbor_decay: float = 0.7,
    min_score: float = 0.0,
) -> tuple[nx.DiGraph, dict]:
    """Greedy subgraph selection constrained to a token budget.

    Algorithm:
    1. Pre-filter: discard nodes whose initial score is below min_score.
    2. Precompute token cost for each candidate node.
    3. Process candidates in descending score order (mutable priority via
       effective_scores).
    4. When a node is selected, unprocessed candidates receive a score of
       max(current_score, selected_score * neighbor_decay).
    5. A node is added if its token cost fits within the remaining budget.
    6. Stops when all candidates are processed or the budget is exhausted.

    Args:
        G: Source directed graph.
        scores: Dict {node_id: score} produced by scorer.score_nodes.
        budget: Maximum total tokens for the selected subgraph.
        model: Tiktoken encoding used for token counting.
        neighbor_decay: Score multiplier applied to neighbors of selected nodes.
        min_score: Minimum score threshold; nodes below this are excluded before
            the greedy loop. Useful for large graphs where most nodes are
            irrelevant to the query and would otherwise fill the budget.

    Returns:
        Tuple (subgraph, stats) where subgraph is a nx.DiGraph containing only
        the selected nodes (with their induced edges) and stats is::

            {
                "nodes_selected": int,
                "nodes_total":    int,   # always the full graph size
                "tokens_used":    int,
                "tokens_budget":  int,
                "coverage_pct":   float,  # 0.0–100.0, rounded to 1 decimal
            }
    """
    nodes_total = G.number_of_nodes()
    empty_stats: dict = {
        "nodes_selected": 0,
        "nodes_total": nodes_total,
        "tokens_used": 0,
        "tokens_budget": budget,
        "coverage_pct": 0.0,
    }

    if nodes_total == 0 or budget <= 0:
        return nx.DiGraph(), empty_stats

    # Pre-compute token cost for every node once, memoised across queries.
    token_cost: dict[str, int] = {
        nid: _node_token_cost(G, nid, model) for nid in G.nodes
    }

    # DECISION: min_score filters before the greedy loop so irrelevant nodes in
    # large graphs (scores ≈ 0) don't consume the budget just because they're cheap.
    candidates: set[str] = {
        nid for nid in G.nodes if scores.get(nid, 0.0) >= min_score
    }

    # Mutable scores — boosted in place as neighbors of selected nodes.
    effective: dict[str, float] = {nid: scores.get(nid, 0.0) for nid in candidates}

    processed: set[str] = set()
    selected: list[str] = []
    tokens_used: int = 0

    # DECISION: a lazy max-heap replaces a max() scan over all unprocessed
    # candidates, which made the loop O(n²) — on a 2k-node graph that scan, not
    # tokenisation, was the dominant cost. Scores mutate during the loop (the
    # neighbour boost below), so a heap built once cannot simply be trusted:
    # a boost pushes a *new* entry and the node's stale lower-scored entries are
    # discarded when they surface, because the node is already in `processed`.
    #
    # Entries are (-score, node_id): heapq is a min-heap, and the node id makes
    # ties deterministic instead of depending on set iteration order.
    heap: list[tuple[float, str]] = [(-effective[nid], nid) for nid in candidates]
    heapq.heapify(heap)

    while heap:
        _, best = heapq.heappop(heap)
        if best in processed:
            continue  # stale entry superseded by a boosted one
        processed.add(best)

        cost = token_cost[best]
        if cost <= budget - tokens_used:
            selected.append(best)
            tokens_used += cost

            # Propagate a decayed score to unprocessed neighbors so the
            # algorithm naturally explores the local neighbourhood of
            # high-relevance nodes.
            # DECISION: boost applies to both predecessors and successors —
            # relevance can flow in either direction along a dependency edge.
            boosted = effective.get(best, 0.0) * neighbor_decay
            for nbr in set(G.predecessors(best)) | set(G.successors(best)):
                if nbr in candidates and nbr not in processed and boosted > effective.get(nbr, 0.0):
                    effective[nbr] = boosted
                    heapq.heappush(heap, (-boosted, nbr))

        # Early exit: remaining budget cannot accommodate any candidate.
        if tokens_used >= budget:
            break

    subgraph = G.subgraph(selected).copy()
    # DECISION: coverage_pct is relative to the FULL graph, not just candidates,
    # so callers always see what fraction of the total knowledge base was included.
    coverage = round(len(selected) / nodes_total * 100, 1)

    stats: dict = {
        "nodes_selected": len(selected),
        "nodes_total": nodes_total,
        "tokens_used": tokens_used,
        "tokens_budget": budget,
        "coverage_pct": coverage,
    }

    return subgraph, stats
