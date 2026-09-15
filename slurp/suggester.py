"""Suggests the queries worth running next, from what the budget left out.

A subgraph is a cut: everything just outside it was related enough to be
adjacent but not relevant enough to fit. That frontier is the most informative
thing slurp knows after answering a query, and it is normally discarded.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

import networkx as nx

from slurp._graphutils import (
    _RISK_HIGH_MIN,
    _display,
    _is_test,
    _split_callers,
    _tokenize,
)

# Tokens that describe how code is organised rather than what it is about, so
# a suggestion built from them reads as noise: "slurp 'index utils helper'".
_STOPWORDS = frozenset({
    "get", "set", "new", "init", "main", "test", "tests", "run", "src", "lib",
    "index", "util", "utils", "helper", "helpers", "common", "core", "base",
    "data", "value", "values", "item", "items", "list", "type", "types", "id",
    "self", "cls", "args", "kwargs", "none", "true", "false", "str", "int",
    "func", "function", "class", "module", "file", "py", "ts", "tsx", "js",
})
_MIN_TOKEN_LEN = 3
_FRONTIER_HOPS = 2
_MAX_EXPECTED = 5
# Import nodes are plumbing: they carry the name of what they import, so they
# flood the token counts with terms that describe wiring rather than subject
# matter — the first version suggested `slurp "nodes slurp score"` off the back
# of a dozen `import_slurp_scorer_score_nodes` placeholders.
_UNINTERESTING_TYPES = frozenset({"import"})


@dataclass
class SuggestedQuery:
    """One query worth running next, and the reason it is worth running."""

    query: str
    reason: str
    expected_nodes: list[str] = field(default_factory=list)
    relevance_score: float = 0.0


def _frontier(G: nx.DiGraph, subgraph: nx.DiGraph, hops: int = _FRONTIER_HOPS) -> set[str]:
    """Nodes within *hops* of the subgraph that did not make it in.

    This is the whole premise: they were connected enough to be neighbours and
    not relevant enough to be selected, which is exactly the shape of a thing
    the user has not looked at yet.
    """
    selected = set(subgraph.nodes)
    if not selected:
        return set()
    undirected = G.to_undirected(as_view=True)
    reached: set[str] = set()
    for node in selected:
        if node not in undirected:
            continue
        for neighbour in nx.single_source_shortest_path_length(
                undirected, node, cutoff=hops):
            if (neighbour not in selected
                    and G.nodes[neighbour].get("type") not in _UNINTERESTING_TYPES):
                reached.add(neighbour)
    return reached


def _discriminative_tokens(G: nx.DiGraph, nodes: set[str], limit: int = 3) -> list[str]:
    """The tokens that set *nodes* apart from the rest of the graph.

    Plain frequency would return whatever the codebase says most often. Each
    token is weighted by how rare it is graph-wide, so a term that is common
    inside the frontier and uncommon outside it wins.
    """
    if not nodes:
        return []

    local = Counter()
    for node in nodes:
        local.update(set(_clean_tokens(G, node)))

    total = G.number_of_nodes() or 1
    global_counts = Counter()
    for node in G.nodes:
        global_counts.update(set(_clean_tokens(G, node)))

    scored = {
        token: (count / len(nodes)) * math.log(total / (1 + global_counts[token]))
        for token, count in local.items()
        if count > 1 or len(nodes) == 1
    }
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
    return [token for token, score in ranked[:limit] if score > 0]


def _clean_tokens(G: nx.DiGraph, node: str) -> list[str]:
    """Meaningful tokens of a node's label, stopwords and noise removed."""
    label = str(G.nodes[node].get("label", node))
    return [
        t for t in _tokenize(label)
        if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS and not t.isdigit()
    ]


def _top_node(G: nx.DiGraph, subgraph: nx.DiGraph, scores: dict[str, float]) -> str:
    """Highest-scoring node of the subgraph, preferring production code.

    A test can legitimately score highest — it names the thing it tests — but
    "drill into test_selection_order_follows_descending_scores" is not advice
    anyone wants, so tests lose every tie against shipped code.
    """
    if not subgraph.number_of_nodes():
        return ""
    return min(
        subgraph.nodes,
        key=lambda n: (_is_test(G, n), -scores.get(n, 0.0), str(n)),
    )


def _share(part: int, whole: int) -> float:
    """How much of *whole* a suggestion would cover, as a 0–1 estimate."""
    return round(part / whole, 3) if whole else 0.0


def _phrase(tokens: list[str]) -> str:
    return " ".join(tokens)


def _same_query(candidate: str, original: str) -> bool:
    """Whether a suggestion says nothing the original query did not."""
    return set(_tokenize(candidate)) <= set(_tokenize(original))


def _drill_down(G, query, scores, subgraph, frontier) -> SuggestedQuery | None:
    """Follow the subgraph's most relevant node into what surrounds it."""
    top = _top_node(G, subgraph, scores)
    if not top:
        return None
    undirected = G.to_undirected(as_view=True)
    if top not in undirected:
        return None
    around = {n for n in undirected.neighbors(top) if n in frontier}
    if not around:
        reachable = nx.single_source_shortest_path_length(
            undirected, top, cutoff=_FRONTIER_HOPS)
        around = {n for n in reachable if n in frontier}
    if not around:
        return None

    tokens = _discriminative_tokens(G, around, limit=2)
    label = _display(G, top).rstrip("()")
    candidate = f"{label} {_phrase(tokens)}".strip()
    if not tokens or _same_query(candidate, query):
        return None
    return SuggestedQuery(
        query=candidate,
        reason=f"drill down into what surrounds {label}",
        expected_nodes=sorted(around)[:_MAX_EXPECTED],
        relevance_score=_share(len(around), len(frontier)),
    )


def _sibling_area(G, query, subgraph, frontier) -> SuggestedQuery | None:
    """Name the themed cluster sitting just outside the cut."""
    outside = frontier - set(subgraph.nodes)
    tokens = _discriminative_tokens(G, outside, limit=3)
    if not tokens:
        return None
    candidate = _phrase(tokens)
    if _same_query(candidate, query):
        return None
    wanted = set(tokens)
    expected = sorted(
        (n for n in outside if wanted & set(_clean_tokens(G, n))),
        key=lambda n: (-len(wanted & set(_clean_tokens(G, n))), str(n)),
    )
    if not expected:
        return None
    return SuggestedQuery(
        query=candidate,
        reason="explore the connected area the budget left out",
        expected_nodes=expected[:_MAX_EXPECTED],
        relevance_score=_share(len(expected), len(outside)),
    )


def _risk_exploration(G, frontier) -> SuggestedQuery | None:
    """Point at the riskiest thing the answer depends on but did not explain."""
    riskiest, highest = "", 0
    for node in frontier:
        production, tests = _split_callers(G, node)
        dependents = len(production) + len(tests)
        if dependents >= _RISK_HIGH_MIN and dependents > highest:
            riskiest, highest = node, dependents
    if not riskiest:
        return None
    label = _display(G, riskiest).rstrip("()")
    return SuggestedQuery(
        query=f"explain:{label}",
        reason=f"high-risk dependency — {highest} nodes depend on it",
        expected_nodes=[riskiest],
        relevance_score=_share(highest, G.number_of_nodes()),
    )


def suggest_queries(
    G: nx.DiGraph,
    query: str,
    scores: dict[str, float],
    subgraph: nx.DiGraph,
    n: int = 3,
) -> list[SuggestedQuery]:
    """Propose up to *n* queries that explore what this answer left out.

    Args:
        G: The full graph.
        query: The query that produced *subgraph*.
        scores: Relevance scores from the scorer.
        subgraph: The selected subgraph.
        n: Maximum number of suggestions.

    Returns:
        Suggestions ordered by estimated relevance, highest first. An empty
        list when the subgraph has no unexplored frontier — nothing to say is
        preferable to filling the space.
    """
    if n <= 0 or not subgraph.number_of_nodes():
        return []

    frontier = _frontier(G, subgraph)
    if not frontier:
        return []

    candidates = [
        _drill_down(G, query, scores, subgraph, frontier),
        _sibling_area(G, query, subgraph, frontier),
        _risk_exploration(G, frontier),
    ]
    found = [c for c in candidates if c is not None]

    # Two suggestions that send the user to the same place are one suggestion.
    seen: set[str] = set()
    unique: list[SuggestedQuery] = []
    for suggestion in sorted(found, key=lambda s: -s.relevance_score):
        key = " ".join(sorted(set(_tokenize(suggestion.query))))
        if key in seen:
            continue
        seen.add(key)
        unique.append(suggestion)
    return unique[:n]


def format_suggestions(suggestions: list[SuggestedQuery], graph_flag: str = "") -> str:
    """Render suggestions as runnable commands, aligned for reading."""
    if not suggestions:
        return ""
    graph = f" --graph {graph_flag}" if graph_flag else ""
    commands = [
        f"slurp explain '{s.query.removeprefix('explain:')}'{graph}"
        if s.query.startswith("explain:")
        else f'slurp "{s.query}"{graph}'
        for s in suggestions
    ]
    pad = max(len(c) for c in commands)
    lines = ["SUGGESTED QUERIES"]
    lines.extend(
        f"  {cmd.ljust(pad)}  — {s.reason}"
        for cmd, s in zip(commands, suggestions, strict=True)
    )
    return "\n".join(lines)
