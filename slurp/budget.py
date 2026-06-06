"""Selects the optimal subgraph that fits within a token budget."""

import functools

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
) -> tuple[nx.DiGraph, dict]:
    """Greedy subgraph selection constrained to a token budget.

    Algorithm:
    1. Precompute token cost for each node by serializing it to text.
    2. Process nodes in descending score order (mutable priority via effective_scores).
    3. When a node is selected, unprocessed neighbors receive a score of
       max(current_score, selected_score * neighbor_decay), biasing traversal
       toward the local context of high-relevance nodes.
    4. A node is added if its token cost fits within the remaining budget.
    5. Stops when all nodes are processed or the budget is fully exhausted.

    Args:
        G: Source directed graph.
        scores: Dict {node_id: score} produced by scorer.score_nodes.
        budget: Maximum total tokens for the selected subgraph.
        model: Tiktoken encoding used for token counting.
        neighbor_decay: Score multiplier applied to neighbors of selected nodes.

    Returns:
        Tuple (subgraph, stats) where subgraph is a nx.DiGraph containing only
        the selected nodes (with their induced edges) and stats is::

            {
                "nodes_selected": int,
                "nodes_total":    int,
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

    # Pre-compute token cost for every node once (tiktoken is cached).
    token_cost: dict[str, int] = {
        nid: count_tokens(_serialize_node(G, nid), model) for nid in G.nodes
    }

    # Mutable scores — boosted in place as neighbors of selected nodes.
    effective: dict[str, float] = dict(scores)

    processed: set[str] = set()
    selected: list[str] = []
    tokens_used: int = 0

    while len(processed) < nodes_total:
        # Greedily pick the highest-scoring unprocessed node.
        best = max(
            (nid for nid in G.nodes if nid not in processed),
            key=lambda nid: effective.get(nid, 0.0),
        )
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
                if nbr not in processed and boosted > effective.get(nbr, 0.0):
                    effective[nbr] = boosted

        # Early exit: remaining budget cannot accommodate any node.
        if tokens_used >= budget:
            break

    subgraph = G.subgraph(selected).copy()
    coverage = round(len(selected) / nodes_total * 100, 1)

    stats: dict = {
        "nodes_selected": len(selected),
        "nodes_total": nodes_total,
        "tokens_used": tokens_used,
        "tokens_budget": budget,
        "coverage_pct": coverage,
    }

    return subgraph, stats
