"""The budget study's harness: recall and real tokens per question. Not part of slurp.

Design: docs/design/presupuesto-optimo.md §3 and §4.5. It does what
`run_retrieval_eval` (behind `slurp eval --retrieval`) does, but scores the
graph once per question and then calls `build_context` at each point with the
same arguments; `verify` checks the two agree before a project is measured.

Each question gives one record:

    base[format][budget]          = [recall, tokens_used, subgraph nodes]   (grid + control)
    cut[form:param][format][budget] = the same with min_score = max(τ_q, 0.15)   (grid only)

where τ_q is the relevance cut of §4.5, computed from the query's own candidate
scores (tests and imports left out).
"""

from __future__ import annotations

import statistics

import networkx as nx

from slurp._graphutils import _left_out
from slurp.budget import build_context
from slurp.evaluator import RETRIEVAL_MIN_SCORE, EvalQuestion, run_retrieval_eval
from slurp.scorer import score_nodes

GRID = [1000, 1500, 2000, 3000, 4000, 6000, 8000, 12000, 16000, 24000, 32000]
CONTROL = 64000                  # saturation check only, never a B*
FORMATS = ("markdown", "json")
CUTS = {
    "C1": [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80],    # τ = α · s_max
    "C2": [0.02, 0.05, 0.10, 0.20, 0.30, 0.40],          # τ = m + β · (s_max − m)
    "C3": [0.05, 0.10, 0.15, 0.20, 0.30],                # first drop ≥ γ · (s_max − m)
}
K_MIN = 5                        # C3 always keeps the first five
# DECISION: every ceiling is computed. Copying a cut that already fits whole to
# larger ceilings is not exact: the header prints the budget, and "16,000"
# tokenizes differently from "1,000" (a test caught the one-token difference).


def cut_key(form: str, param: float) -> str:
    return f"{form}:{param:g}"


def thresholds(candidate_scores: list[float]) -> dict[str, float | None]:
    """τ_q for every form and parameter of §4.5; None means no cut."""
    s = sorted(candidate_scores, reverse=True)
    out: dict[str, float | None] = {}
    if not s:
        return {cut_key(f, p): None for f, ps in CUTS.items() for p in ps}
    s_max, m = s[0], statistics.median(s)
    for a in CUTS["C1"]:
        out[cut_key("C1", a)] = a * s_max
    for b in CUTS["C2"]:
        out[cut_key("C2", b)] = m + b * (s_max - m)
    spread = s_max - m
    for g in CUTS["C3"]:
        tau = None
        if spread > 0:
            for i in range(K_MIN - 1, len(s) - 1):      # 0-based i = rank i+1 ≥ K_MIN
                if s[i] - s[i + 1] >= g * spread:
                    tau = s[i]
                    break
        out[cut_key("C3", g)] = tau
    return out


def _point(G: nx.DiGraph, scores: dict, query: str, relevant: list[str], budget: int,
           fmt: str, min_score: float) -> tuple[list, dict]:
    _text, sub, stats = build_context(G, scores, budget=budget, min_score=min_score, format=fmt,
                                      query=query)
    found = sum(1 for n in relevant if n in sub)
    return [round(found / len(relevant), 4), stats["tokens_used"], sub.number_of_nodes()], stats


def measure_question(G: nx.DiGraph, q: dict, grid: list[int] = GRID,
                     control: int | None = CONTROL, cuts: bool = True) -> dict:
    """The record of one question on its own graph (see the module docstring)."""
    relevant = [n for n in q["relevant_nodes"] if n in G]
    left_out = _left_out(G)
    rec = {"id": q["id"], "question": q["question"], "names_symbol": q.get("names_symbol"),
           "relevant": q["relevant_nodes"], "present": len(relevant),
           "graph_nodes": G.number_of_nodes(), "candidates": G.number_of_nodes() - len(left_out),
           "test_nodes": len(_left_out(G, include_tests=False, include_imports=True)),
           "base": {}, "cut": {}}
    if not relevant:
        return rec
    query = q["question"]
    scores = score_nodes(G, query)
    order = sorted(scores, key=lambda n: (-scores[n], n))
    position_all = {n: i + 1 for i, n in enumerate(order)}
    position = {n: i + 1 for i, n in enumerate(n for n in order if n not in left_out)}
    rec["first_rank"] = min((position[n] for n in relevant if n in position), default=None)
    rec["first_rank_all"] = min(position_all[n] for n in relevant if n in position_all)
    cand = [scores[n] for n in G.nodes if n not in left_out]
    rec["s_max"] = round(max(cand), 4) if cand else None
    rec["s_median"] = round(statistics.median(cand), 4) if cand else None

    budgets = list(grid) + ([control] if control else [])
    for fmt in FORMATS:
        rec["base"][fmt] = {str(b): _point(G, scores, query, relevant, b, fmt,
                                           RETRIEVAL_MIN_SCORE)[0] for b in budgets}
    if not cuts:
        return rec
    for key, tau in thresholds(cand).items():
        rec["cut"][key] = {}
        for fmt in FORMATS:
            if tau is None or tau <= RETRIEVAL_MIN_SCORE:
                rec["cut"][key][fmt] = {str(b): rec["base"][fmt][str(b)] for b in grid}
                continue
            rec["cut"][key][fmt] = {str(b): _point(G, scores, query, relevant, b, fmt,
                                                   tau)[0] for b in grid}
    return rec


def verify(G: nx.DiGraph, q: dict, rec: dict, budgets: list[int]) -> list[str]:
    """Where the record differs from run_retrieval_eval; empty when they agree."""
    eq = EvalQuestion(id=q["id"], question=q["question"], ground_truth="-", area="study",
                      relevant_nodes=list(q["relevant_nodes"]))
    diffs = []
    for fmt in FORMATS:
        for b in budgets:
            r = run_retrieval_eval(G, [eq], budget=b, format=fmt).results[0]
            mine = rec["base"][fmt][str(b)]
            theirs = [r.recall, r.tokens_used, r.subgraph_nodes]
            if mine != theirs:
                diffs.append(f"{fmt} {b}: harness {mine} != eval {theirs}")
            if (rec.get("first_rank"), rec.get("first_rank_all")) != (r.first_rank,
                                                                     r.first_rank_all):
                diffs.append(f"rank: harness {rec.get('first_rank')} != eval {r.first_rank}")
    return diffs
