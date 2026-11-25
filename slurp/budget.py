"""Selects the subgraph whose formatted output fits within a token budget.

The budget bounds the text that is actually sent: every node and relationship
as the formatter writes it, plus the header and the closing tip, in the format
requested. Until v1.0.7 it bounded a short per-node summary and ignored the
relationships, so `--budget 8000` produced 19k–42k tokens of output.
"""

import functools
import heapq
import math
import os
from collections.abc import Callable, Mapping

import networkx as nx
import tiktoken

from slurp._graphutils import _left_out, _left_out_sets
from slurp.formatter import format_subgraph, item_text


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


# DECISION: one default threshold for every entry point — CLI, MCP, benchmark,
# eval and the library — so a measurement describes what users get. Until
# v1.1.0 the CLI and MCP used 0.15 while benchmark, eval and the library used
# 0.0. Note what it does and does not do: the +0.3 boost for file_type "code"
# lifts every node of a `slurp index` graph to >= 0.3, so there 0.15 filters
# nothing; in graphify graphs it keeps unrelated documents and concepts out.
# Making it bite on code graphs was measured (slurp eval --retrieval): recall
# on PrismaStats fell from 1.000 to 0.733, or stayed exactly as is when
# neighbours were allowed back in. With the budget bounding the real text, the
# budget is the binding limit, so the boost was left alone.
DEFAULT_MIN_SCORE = 0.15


class BudgetTooSmallError(ValueError):
    """The budget cannot hold even the header and its warning."""


# DECISION: a configurable factor scales every count, because the model reading
# the output may not tokenize like tiktoken. Measured in this project against
# Anthropic's token counter (messages.count_tokens), Claude's tokenizer produced
# about 1.56x the cl100k_base count on slurp's output. That is one measurement
# on one kind of text — an estimate, not a guarantee — so the default stays 1.0
# and users who budget for Claude can opt into ~1.56. Counts round up, so with a
# factor the text stays at or under the budget in the scaled unit.
TOKEN_FACTOR_ENV = "SLURP_TOKEN_FACTOR"
_MIN_FACTOR, _MAX_FACTOR = 0.1, 10.0


def _check_factor(factor: float) -> float:
    if not (_MIN_FACTOR <= factor <= _MAX_FACTOR):
        raise ValueError(f"token factor must be between {_MIN_FACTOR} and {_MAX_FACTOR}, "
                         f"got {factor}")
    return factor


def resolve_token_factor(flag: float | None = None, env: Mapping[str, str] | None = None) -> float:
    """The token factor in force: the flag, else $SLURP_TOKEN_FACTOR, else 1.0.

    Raises:
        ValueError: If the value is not a number in [0.1, 10].
    """
    if flag is not None:
        return _check_factor(float(flag))
    raw = (os.environ if env is None else env).get(TOKEN_FACTOR_ENV, "").strip()
    if not raw:
        return 1.0
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{TOKEN_FACTOR_ENV}={raw!r} is not a number; use e.g. "
                         f"{TOKEN_FACTOR_ENV}=1.56") from None
    try:
        return _check_factor(value)
    except ValueError as exc:
        raise ValueError(f"{TOKEN_FACTOR_ENV}: {exc}") from None


def _scaled(raw: int, factor: float) -> int:
    return raw if factor == 1.0 else math.ceil(raw * factor)


# DECISION: relationships implied by the rest of the output weigh half. A
# `contains` edge repeats the file path printed under each node, and
# `imports_from` points at an import placeholder; a `calls` edge is information
# nothing else in the output carries.
_STRUCTURAL_RELATIONS = frozenset({"contains", "imports_from", "imports", "defines"})
_STRUCTURAL_WEIGHT = 0.5

# DECISION: a selected node passes its boost (score x neighbor_decay) to at most
# its 10 candidate neighbours with the highest own score; the others keep their
# own. Measured in v1.2 (#64/#65): on PrismaStats createAdminClient (0.839, 98
# callers) lent 0.587 to every caller, above useChatContext (0.510), the node a
# question asked for, and the callers filled the budget before it; with more
# budget the 98 hub-to-caller relations, worth the same 0.587, took the room
# meant for new nodes (4000 -> 8000 tokens added 14 nodes and 148 relations).
# The cap removes that cause. 3, 5, 10 and 20 were tried on the tuning group
# (scripts/retrieval.py): below 10 it cut a boost slurp's own graph needs (a
# function reached through a 16-neighbour node, 7th among them), at 20 the hub's
# callers crowded JSON at 4000 again. 10 was kept as the simplest mechanism that
# removes the measured cause — not for its tuning score, which over 16 questions
# and 27 variants tried is not significant.
_BOOST_FANOUT = 10

# Selection aims this far under the budget, so the final measurement rarely has
# to trim: costing items one by one misses the tokens merged across their
# boundaries and the digits of the header.
_SELECTION_MARGIN = 0.02

# Rounds allowed for the header's token count to settle: it is part of the
# text it counts, so writing the number can change the number.
_FIXPOINT_ROUNDS = 8

# Costing an item renders and tokenizes it, which on a 4,000-node graph is most
# of the work. Item text never depends on the query (a placeholder score of the
# widest width stands in), so costs are memoised across queries.
#
# INVARIANT: entries are only valid while item text is unchanged. One process
# must not hold two graphs that share ids with different content without
# calling clear_node_token_cache() in between. The MCP server does this on
# reload. A stale cost can only make the selection less tight: the final
# measurement of the real text still enforces the budget.
_item_cost_cache: dict[tuple, int] = {}

Renderer = Callable[[nx.DiGraph, dict], str]


def clear_node_token_cache() -> None:
    """Drop every cached item cost.

    Call this whenever a graph is replaced in-process — the MCP server does it
    on reload — so no stale costs survive.
    """
    _item_cost_cache.clear()


def _item_cost(G: nx.DiGraph, item: tuple, fmt: str, model: str, factor: float = 1.0) -> int:
    key = (item, fmt, model)
    cost = _item_cost_cache.get(key)
    if cost is None:
        cost = count_tokens(item_text(G, item, fmt), model)
        _item_cost_cache[key] = cost
    return _scaled(cost, factor)


def _edge_weight(G: nx.DiGraph, u: str, v: str) -> float:
    relation = G.edges[u, v].get("relation", "")
    return _STRUCTURAL_WEIGHT if relation in _STRUCTURAL_RELATIONS else 1.0


def _assemble(G: nx.DiGraph, nodes: list[str], edges: list[tuple[str, str]]) -> nx.DiGraph:
    """A subgraph holding exactly *nodes* and *edges*, in a fixed order.

    Built explicitly rather than with G.subgraph(): that view iterates a Python
    set when the selection is small, so the order of the relationships in the
    output changed from one process to the next.
    """
    sub = nx.DiGraph()
    for n in sorted(nodes):
        sub.add_node(n, **G.nodes[n])
    for u, v in sorted(edges):
        sub.add_edge(u, v, **G.edges[u, v])
    return sub


# (candidates, min_score[, test nodes left out, import nodes left out]):
# how many nodes could take a place, and why the others could not.
LeftOut = tuple


def left_out_from_stats(stats: dict) -> LeftOut | None:
    """The `left_out` that produced *stats*, for trimming them again.

    A caller that trims a build_context result once more with fit_to_budget
    (the CLI's --inject-code) passes this so the new stats keep every cause.
    None for stats without the breakdown.
    """
    if "nodes_over_budget" not in stats:
        return None
    tests = stats.get("nodes_excluded_tests", 0)
    imports = stats.get("nodes_excluded_imports", 0)
    candidates = stats["nodes_total"] - stats["nodes_below_min_score"] - tests - imports
    return (candidates, stats["min_score"], tests, imports)


def _measure(sub: nx.DiGraph, render: Renderer, budget: int, nodes_total: int,
             model: str, warning: str = "", factor: float = 1.0,
             left_out: LeftOut | None = None) -> tuple[str, dict, int, bool]:
    """Render *sub* with stats that state its own true token count.

    Args:
        left_out: (candidates, min_score[, tests, imports]) — how many nodes
            cleared min_score, and how many tests and imports were left out
            of selection. With it, the stats split the nodes left out by cause.

    Returns:
        (text, stats, tokens, settled). `settled` is False when the count never
        stabilised, which the caller treats as not fitting.
    """
    n = sub.number_of_nodes()
    stats: dict = {
        "nodes_selected": n,
        "nodes_total": nodes_total,
        "tokens_used": 0,
        "tokens_budget": budget,
        "coverage_pct": round(n / nodes_total * 100, 1) if nodes_total else 0.0,
    }
    if left_out is not None:
        # DECISION: two causes, counted apart, because they call for opposite
        # fixes: a node that did not fit comes in with more budget; one below
        # min_score never does. Until v1.1.0 the tip lumped both together as
        # "connected nodes" and sent everyone to --budget.
        candidates, min_score, *kinds = left_out
        tests, imports = (*kinds, 0, 0)[:2]
        stats["nodes_over_budget"] = max(0, candidates - n)
        stats["nodes_below_min_score"] = nodes_total - candidates - tests - imports
        stats["min_score"] = min_score
        # A third cause, with its own fix: these never competed (see _left_out).
        if tests:
            stats["nodes_excluded_tests"] = tests
        if imports:
            stats["nodes_excluded_imports"] = imports
    if warning:
        stats["warning"] = warning
    if factor != 1.0:
        stats["token_factor"] = factor
    text, tokens = "", 0
    for _ in range(_FIXPOINT_ROUNDS):
        text = render(sub, stats)
        tokens = _scaled(count_tokens(text, model), factor)
        if tokens == stats["tokens_used"]:
            return text, stats, tokens, True
        stats = {**stats, "tokens_used": tokens}
    return text, stats, tokens, False


def _too_small_warning(budget: int, needed: int) -> str:
    return (f"Budget {budget:,} tokens is too small for even one node: the "
            f"top-scoring node needs about {needed:,} tokens with this header. "
            f"Use --budget {needed:,} or more.")


def fit_to_budget(
    sub: nx.DiGraph,
    budget: int,
    render: Renderer,
    values: dict[tuple, float],
    nodes_total: int,
    model: str = "cl100k_base",
    costs: dict[tuple, int] | None = None,
    item_cost: Callable[[tuple], int] | None = None,
    token_factor: float = 1.0,
    left_out: LeftOut | None = None,
) -> tuple[str, nx.DiGraph, dict]:
    """Trim *sub* until its rendered text measures at most *budget* tokens.

    The guarantee lives here: the full text is rendered and counted, and while
    it is over budget the lowest-value items go — relationships before nodes on
    a tie, and a node takes its relationships with it.

    Args:
        sub: Candidate subgraph.
        budget: Token budget for the final text.
        render: Produces the final text from (subgraph, stats).
        values: Value of each item, ("n", id) or ("e", u, v); lower goes first.
        nodes_total: Size of the full graph, for the stats.
        model: Tiktoken encoding.
        costs: Estimated cost per item, to size each trimming step.
        item_cost: Computes a missing cost. With neither, one item goes per
            round — slower, never too many.
        token_factor: Multiplies every count (see resolve_token_factor).
        left_out: (candidates, min_score[, tests, imports]): how many nodes of
            the full graph cleared min_score, and how many tests and imports
            were left out of selection (left_out_from_stats rebuilds it). With
            it the stats carry nodes_over_budget, nodes_below_min_score and the
            nodes_excluded_* counts, which the closing tip reports.

    Returns:
        (text, subgraph, stats). The text measures <= budget. If trimming
        leaves no node, the text is the header with a warning naming the budget
        the most valuable node needs, and stats carry it as "min_budget" — an
        empty answer is never returned in silence.
    """
    costs = dict(costs or {})
    original_nodes = list(sub.nodes)
    nodes = list(original_nodes)
    edges = list(sub.edges)

    def cost_of(item: tuple) -> int | None:
        if item not in costs and item_cost is not None:
            costs[item] = item_cost(item)
        return costs.get(item)

    while True:
        if not nodes:
            break  # trimmed to nothing (or given nothing): handled below
        current = _assemble(sub, nodes, edges)
        text, stats, tokens, settled = _measure(current, render, budget, nodes_total, model,
                                                factor=token_factor, left_out=left_out)
        if settled and tokens <= budget:
            return text, current, stats
        excess = max(1, tokens - budget)
        ranked = sorted(
            [(values.get(("e", u, v), 0.0), 0, ("e", u, v)) for u, v in edges]
            + [(values.get(("n", n), 0.0), 1, ("n", n)) for n in nodes]
        )
        dropped_nodes: set[str] = set()
        dropped_edges: set[tuple[str, str]] = set()
        freed = 0
        for _value, _kind, item in ranked:
            if freed >= excess:
                break
            if item[0] == "e":
                edge = (item[1], item[2])
                if edge in dropped_edges or item[1] in dropped_nodes or item[2] in dropped_nodes:
                    continue
                dropped_edges.add(edge)
            else:
                dropped_nodes.add(item[1])
                for u, v in edges:
                    if item[1] in (u, v):
                        dropped_edges.add((u, v))
            cost = cost_of(item)
            if cost is None:
                break  # unknown cost: drop one item, then measure again
            freed += cost
        nodes = [n for n in nodes if n not in dropped_nodes]
        edges = [e for e in edges if e not in dropped_edges and e[0] not in dropped_nodes
                 and e[1] not in dropped_nodes]

    # Nothing fits. Say what would: the most valuable node alone, measured.
    empty = nx.DiGraph()
    if not original_nodes:
        text, stats, _, _ = _measure(empty, render, budget, nodes_total, model,
                                     factor=token_factor, left_out=left_out)
        return text, empty, stats
    top = max(original_nodes, key=lambda n: (values.get(("n", n), 0.0), n))
    _, _, alone, _ = _measure(_assemble(sub, [top], []), render, budget, nodes_total, model,
                              factor=token_factor, left_out=left_out)
    needed = alone + 1
    text, stats, _, _ = _measure(empty, render, budget, nodes_total, model,
                                 _too_small_warning(budget, needed), factor=token_factor,
                                 left_out=left_out)
    stats["min_budget"] = needed
    return text, empty, stats


def build_context(
    G: nx.DiGraph,
    scores: dict[str, float],
    budget: int,
    model: str = "cl100k_base",
    neighbor_decay: float = 0.7,
    min_score: float = DEFAULT_MIN_SCORE,
    *,
    format: str = "markdown",
    query: str = "",
    render: Renderer | None = None,
    token_factor: float = 1.0,
    include_tests: bool = False,
    include_imports: bool = False,
) -> tuple[str, nx.DiGraph, dict]:
    """Select a subgraph and return the text that fits *budget*, measured.

    Algorithm:
    1. The smallest valid output — the header with a "budget too small"
       warning — must fit, or BudgetTooSmallError names the minimum.
    2. Greedy selection over one queue of nodes and relationships, by value:
       a node's value is its score, raised to `parent * neighbor_decay` when a
       selected neighbour boosts it (each node boosts at most _BOOST_FANOUT
       neighbours, the most relevant on their own); a relationship becomes eligible once both
       its ends are in and is worth min(their values) x its relation weight. So
       at every score level the output holds the nodes and the relationships
       among them, and what is left out first is the structural edges of
       low-scoring nodes, then low-scoring nodes. Each item is costed with the
       formatter's own renderer in *format*. Test and import nodes are not
       candidates unless let in (see _graphutils._left_out); they are neither
       selected nor boosted, and pass no boost on.
    3. fit_to_budget renders the whole text, counts it, and trims until it
       fits; the stats state the true count of the text returned.

    Args:
        G: Source directed graph.
        scores: Dict {node_id: score} produced by scorer.score_nodes.
        budget: Maximum tokens for the returned text.
        model: Tiktoken encoding used for token counting.
        neighbor_decay: Score multiplier applied to neighbours of selected nodes.
        min_score: Nodes scoring below this are not candidates.
        format: 'markdown', 'json' or 'yaml' — the output being budgeted.
        query: The query, which the markdown header prints.
        render: Overrides the final rendering, for callers that wrap the
            formatted subgraph (exporter). Defaults to format_subgraph.
        token_factor: Multiplies every token count, for a reader whose
            tokenizer differs from tiktoken (see resolve_token_factor).
        include_tests: Let test nodes compete for a place.
        include_imports: Let import nodes compete for a place.

    Returns:
        (text, subgraph, stats). count_tokens(text) <= budget, and stats report
        exactly the nodes and tokens in text. If no node fits, the text is the
        header with a warning giving the budget that would fit one.

    Raises:
        BudgetTooSmallError: If the budget cannot hold the header and warning.
    """
    if render is None:
        def render(sub: nx.DiGraph, stats: dict) -> str:
            return format_subgraph(sub, stats, format, scores=scores, query=query)

    _check_factor(token_factor)
    nodes_total = G.number_of_nodes()
    empty = nx.DiGraph()
    excluded = _left_out(G, include_tests, include_imports)
    excluded_tests = len(excluded & _left_out_sets(G)[0])
    candidates = {n for n in G.nodes
                  if n not in excluded and scores.get(n, 0.0) >= min_score}
    left_out = (len(candidates), min_score, excluded_tests, len(excluded) - excluded_tests)

    # 1. The smallest output this call can return.
    widest = _too_small_warning(budget, 10 ** 7)
    floor_text, _, floor_tokens, _ = _measure(empty, render, budget, nodes_total, model, widest,
                                              factor=token_factor, left_out=left_out)
    if budget < floor_tokens:
        raise BudgetTooSmallError(
            f"--budget {budget:,} is below the {floor_tokens:,} tokens the header "
            f"and a warning need for this query in {format}. Use --budget "
            f"{floor_tokens:,} or more.")

    # 2. Greedy selection against the room the header leaves.
    _, _, overhead, _ = _measure(empty, render, budget, nodes_total, model, factor=token_factor,
                                 left_out=left_out)
    room = math.floor((budget - overhead) * (1 - _SELECTION_MARGIN))

    effective: dict[str, float] = {n: scores.get(n, 0.0) for n in candidates}
    values: dict[tuple, float] = {}
    costs: dict[tuple, int] = {}

    # Entries are (-value, kind, key): nodes (kind 0) win ties against edges, and
    # the key makes the order deterministic.
    heap: list[tuple[float, int, tuple]] = [(-effective[n], 0, ("n", n)) for n in candidates]
    heapq.heapify(heap)
    processed: set[str] = set()
    selected: list[str] = []
    selected_set: set[str] = set()
    chosen_edges: list[tuple[str, str]] = []
    used = 0
    top_cost: int | None = None

    while heap and room - used > 0:
        neg_value, _kind, item = heapq.heappop(heap)
        cost = costs.get(item)
        if cost is None:
            cost = costs[item] = _item_cost(G, item, format, model, token_factor)
        if item[0] == "e":
            if cost <= room - used:
                chosen_edges.append((item[1], item[2]))
                values[item] = -neg_value
                used += cost
            continue

        node = item[1]
        if node in processed:
            continue  # a stale entry superseded by a boosted one
        processed.add(node)
        if top_cost is None:
            top_cost = cost
        if cost > room - used:
            continue
        value = effective[node]
        selected.append(node)
        selected_set.add(node)
        values[item] = value
        used += cost

        # Relationships to nodes already in become eligible now.
        for u, v in [(p, node) for p in G.predecessors(node) if p in selected_set] + \
                    [(node, s) for s in G.successors(node) if s in selected_set]:
            if u == v:
                continue
            edge_value = min(effective[u], effective[v]) * _edge_weight(G, u, v)
            heapq.heappush(heap, (-edge_value, 1, ("e", u, v)))

        # Neighbours inherit a decayed share of this node's relevance: the
        # _BOOST_FANOUT of them most relevant on their own, so a hub lends it to
        # ten callers rather than to all of them.
        boosted = value * neighbor_decay
        heirs = sorted((nbr for nbr in set(G.predecessors(node)) | set(G.successors(node))
                        if nbr in candidates and nbr not in processed),
                       key=lambda n: (-scores.get(n, 0.0), n))[:_BOOST_FANOUT]
        for nbr in heirs:
            if boosted > effective[nbr]:
                effective[nbr] = boosted
                heapq.heappush(heap, (-boosted, 0, ("n", nbr)))

    # 3. Measure the real text and trim until it fits.
    text, sub, stats = fit_to_budget(_assemble(G, selected, chosen_edges), budget,
                                     render, values, nodes_total, model, costs,
                                     token_factor=token_factor, left_out=left_out)
    if sub.number_of_nodes() == 0 and top_cost is not None and "min_budget" not in stats:
        needed = math.ceil((overhead + top_cost) / (1 - _SELECTION_MARGIN)) + 1
        text, stats, _, _ = _measure(empty, render, budget, nodes_total, model,
                                     _too_small_warning(budget, needed), factor=token_factor,
                                     left_out=left_out)
        stats["min_budget"] = needed
    return text, sub, stats


# A budget no graph reaches, so the "whole graph" holds every node and edge.
_UNBOUNDED = 10_000_000


def full_graph_tokens(
    G: nx.DiGraph,
    scores: dict[str, float],
    query: str = "",
    format: str = "markdown",
    model: str = "cl100k_base",
    token_factor: float = 1.0,
) -> int:
    """Tokens of the whole graph sent exactly the way an answer is sent.

    The honest baseline for "tokens saved": same formatter, same header with
    the same query, same scores, every node and relationship — tests and
    imports included, since without slurp they would be sent too.
    """
    _, _, stats = build_context(G, scores, _UNBOUNDED, model, min_score=0.0,
                                format=format, query=query, token_factor=token_factor,
                                include_tests=True, include_imports=True)
    return stats["tokens_used"]


def select_subgraph(
    G: nx.DiGraph,
    scores: dict[str, float],
    budget: int,
    model: str = "cl100k_base",
    neighbor_decay: float = 0.7,
    min_score: float = DEFAULT_MIN_SCORE,
    *,
    format: str = "markdown",
    query: str = "",
    render: Renderer | None = None,
    token_factor: float = 1.0,
    include_tests: bool = False,
    include_imports: bool = False,
) -> tuple[nx.DiGraph, dict]:
    """Like build_context, returning only (subgraph, stats).

    format_subgraph(subgraph, stats, format, scores, query) reproduces the text
    whose size stats["tokens_used"] reports — pass the same format and query
    used here. Callers that need the text should call build_context.
    """
    _, sub, stats = build_context(G, scores, budget, model, neighbor_decay, min_score,
                                  format=format, query=query, render=render,
                                  token_factor=token_factor, include_tests=include_tests,
                                  include_imports=include_imports)
    return sub, stats
