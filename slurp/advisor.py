"""Budget recommendations derived from a project's own query history.

Picking `--budget` is guesswork on a new project: too low truncates the answer,
too high pays for nodes the LLM never needed. Once `.slurp/audit.jsonl` has a few
dozen entries, the right number is already in the data — this module finds the
past queries that resemble the new one and reads the budget off their outcomes.
"""

import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from slurp.audit import read_audit
from slurp.benchmark import _cost_usd, _percentile, price_per_mtok
from slurp.scorer import _tokenize

DEFAULT_BUDGET = 4000

# Cosine similarity at or above which two queries count as "the same kind of ask".
SIMILARITY_THRESHOLD = 0.3

# Percentile of historical token use to recommend. p75 fits three out of four
# comparable queries; the mean would under-serve the heavier half, and the max
# would pay for one outlier every time.
BUDGET_PERCENTILE = 75

# Tokens per node when estimating what the full graph would have cost. Matches
# the baseline used by the visualiser, so savings figures agree across commands.
_BASELINE_TOKENS_PER_NODE = 50

_CONFIDENCE_HIGH = 10


@dataclass
class SimilarQuery:
    """One historical query judged similar to the one being advised on."""

    query: str
    similarity: float
    tokens_used: int
    nodes_selected: int
    nodes_total: int
    savings_pct: float


@dataclass
class BudgetAdvice:
    """A budget recommendation and the evidence behind it.

    Attributes:
        query: The query being advised on.
        recommended_budget: Suggested `--budget` value, in tokens.
        expected_coverage_pct: Share of the content that similar past queries
            actually selected which this budget would have fitted. 100% means
            the budget covers every comparable query in full.
        estimated_cost_usd: Input-token cost of the recommended budget.
        price_model: Pricing model the cost was computed with.
        confidence: HIGH, MEDIUM, LOW, or NONE.
        similar: Similar past queries, most similar first.
        note: Explanation shown when the recommendation is weak or absent.
        has_history: False when the audit log held no usable entries at all.
    """

    query: str
    recommended_budget: int
    expected_coverage_pct: float
    estimated_cost_usd: float
    price_model: str
    confidence: str
    similar: list[SimilarQuery] = field(default_factory=list)
    note: str = ""
    has_history: bool = True

    @property
    def similar_count(self) -> int:
        return len(self.similar)


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------

def _tfidf_vector(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    """Build an L2-normalised TF-IDF vector for one tokenised document."""
    if not tokens:
        return {}
    counts = Counter(tokens)
    total = len(tokens)
    vec = {
        term: (count / total) * idf.get(term, 0.0)
        for term, count in counts.items()
    }
    norm = math.sqrt(sum(w * w for w in vec.values()))
    if norm == 0.0:
        return {}
    return {term: w / norm for term, w in vec.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine similarity of two L2-normalised sparse vectors."""
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(weight * b.get(term, 0.0) for term, weight in a.items())


def query_similarities(query: str, history: list[str]) -> list[float]:
    """Score how closely each historical query resembles *query*.

    Uses the same tokenizer as the scorer, so "authFlow", "auth_flow" and
    "auth flow" are the same ask here as they are when ranking nodes.

    Args:
        query: The new query.
        history: Past query strings, in log order.

    Returns:
        One cosine similarity in [0, 1] per history entry, same order.
    """
    if not history:
        return []

    query_tokens = _tokenize(query)
    history_tokens = [_tokenize(h) for h in history]
    corpus = [query_tokens, *history_tokens]

    # Smoothed IDF, so a two-document corpus still produces usable weights.
    n_docs = len(corpus)
    doc_freq: Counter[str] = Counter()
    for doc in corpus:
        doc_freq.update(set(doc))
    idf = {
        term: math.log((n_docs + 1) / (df + 1)) + 1.0
        for term, df in doc_freq.items()
    }

    query_vec = _tfidf_vector(query_tokens, idf)
    return [
        _cosine(query_vec, _tfidf_vector(doc, idf))
        for doc in history_tokens
    ]


def _entry_savings_pct(tokens_used: int, nodes_total: int) -> float:
    """Percentage saved versus injecting the whole graph.

    The audit log records outcomes, not the full-graph token count, so the
    baseline is estimated at _BASELINE_TOKENS_PER_NODE per node rather than
    re-serialising a graph that may no longer exist.
    """
    baseline = nodes_total * _BASELINE_TOKENS_PER_NODE
    if baseline <= 0:
        return 0.0
    saved = max(0, baseline - tokens_used)
    return round(saved / baseline * 100, 1)


def find_similar_queries(
    query: str,
    entries: list[dict],
    threshold: float = SIMILARITY_THRESHOLD,
) -> list[SimilarQuery]:
    """Select the audit entries whose query resembles *query*.

    Args:
        query: The new query.
        entries: Audit entries as returned by audit.read_audit.
        threshold: Minimum cosine similarity to qualify.

    Returns:
        Matching entries as SimilarQuery, most similar first. Entries missing
        the fields needed to reason about budget are skipped.
    """
    usable = [
        e for e in entries
        if isinstance(e.get("query"), str)
        and e.get("query", "").strip()
        and isinstance(e.get("tokens_used"), int)
        and e["tokens_used"] > 0
    ]
    if not usable:
        return []

    scores = query_similarities(query, [e["query"] for e in usable])

    matches = [
        SimilarQuery(
            query=entry["query"],
            similarity=round(score, 4),
            tokens_used=entry["tokens_used"],
            nodes_selected=int(entry.get("nodes_selected") or 0),
            nodes_total=int(entry.get("nodes_total") or 0),
            savings_pct=_entry_savings_pct(
                entry["tokens_used"], int(entry.get("nodes_total") or 0)
            ),
        )
        for entry, score in zip(usable, scores, strict=True)
        if score >= threshold
    ]
    matches.sort(key=lambda m: (-m.similarity, m.query))
    return matches


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------

def _confidence_for(count: int, min_similar: int) -> str:
    if count == 0:
        return "NONE"
    if count >= _CONFIDENCE_HIGH:
        return "HIGH"
    if count >= min_similar:
        return "MEDIUM"
    return "LOW"


def _expected_coverage(budget: int, similar: list[SimilarQuery]) -> float:
    """Share of similar queries' selected content that *budget* would fit.

    A query whose selection was smaller than the budget contributes 100%; one
    that used more contributes the fraction that would have survived.
    """
    if not similar:
        return 0.0
    ratios = [min(1.0, budget / m.tokens_used) for m in similar if m.tokens_used > 0]
    if not ratios:
        return 0.0
    return round(sum(ratios) / len(ratios) * 100, 1)


def recommend_budget(
    query: str,
    audit_dir: Path = Path(".slurp"),
    price_model: str = "default",
    min_similar: int = 3,
    threshold: float = SIMILARITY_THRESHOLD,
) -> BudgetAdvice:
    """Recommend a token budget for *query* from past query outcomes.

    Args:
        query: The query to advise on.
        audit_dir: Directory holding audit.jsonl.
        price_model: Pricing model for the cost estimate.
        min_similar: Similar-query count below which the recommendation is
            flagged LOW confidence.
        threshold: Minimum similarity for a past query to count.

    Returns:
        A BudgetAdvice. When no similar history exists it recommends
        DEFAULT_BUDGET and says so in `note`.

    Raises:
        ValueError: If price_model is not a known pricing model.
    """
    price = price_per_mtok(price_model)

    try:
        entries = read_audit(audit_dir)
    except (OSError, ValueError):
        entries = []

    similar = find_similar_queries(query, entries, threshold=threshold)
    confidence = _confidence_for(len(similar), min_similar)

    if not similar:
        note = (
            "No historical data for this query type"
            if entries else
            "No query history yet — run some queries to build one"
        )
        return BudgetAdvice(
            query=query,
            recommended_budget=DEFAULT_BUDGET,
            expected_coverage_pct=0.0,
            estimated_cost_usd=_cost_usd(DEFAULT_BUDGET, price),
            price_model=price_model,
            confidence=confidence,
            similar=[],
            note=note,
            has_history=bool(entries),
        )

    budget = int(round(_percentile([float(m.tokens_used) for m in similar],
                                   BUDGET_PERCENTILE)))
    budget = max(1, budget)

    note = ""
    if confidence == "LOW":
        note = (
            f"Only {len(similar)} similar "
            f"{'query' if len(similar) == 1 else 'queries'} found "
            f"(need {min_similar} for a confident recommendation) — "
            "treat this as a starting point"
        )

    return BudgetAdvice(
        query=query,
        recommended_budget=budget,
        expected_coverage_pct=_expected_coverage(budget, similar),
        estimated_cost_usd=_cost_usd(budget, price),
        price_model=price_model,
        confidence=confidence,
        similar=similar,
        note=note,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_MAX_SIMILAR_SHOWN = 5


def format_advice(advice: BudgetAdvice, graph_path: Path | None = None) -> str:
    """Render a BudgetAdvice as the plain-text body shown under the panel.

    Args:
        advice: The recommendation to render.
        graph_path: Graph path to use in the copy-paste command line.

    Returns:
        Multi-line text, without a trailing newline.
    """
    lines: list[str] = [
        f"Recommended budget:  {advice.recommended_budget:,} tokens",
    ]
    if advice.similar:
        lines.append(
            f"Expected coverage:   {advice.expected_coverage_pct}% "
            "of similar queries' selections"
        )
    lines.append(
        f"Estimated cost:      ${advice.estimated_cost_usd:.4f}  "
        f"({advice.price_model})"
    )
    detail = (
        f"{advice.similar_count} similar "
        f"{'query' if advice.similar_count == 1 else 'queries'}"
    )
    lines.append(f"Confidence:          {advice.confidence}  ({detail})")

    # With no similar queries the note *is* the headline, shown above this body.
    if advice.note and advice.similar:
        lines.append(f"\nNote: {advice.note}")

    if advice.similar:
        lines.append("\nSimilar past queries:")
        shown = advice.similar[:_MAX_SIMILAR_SHOWN]
        width = max(len(f'"{m.query}"') for m in shown)
        for m in shown:
            label = f'"{m.query}"'.ljust(width)
            lines.append(
                f"  {label} → {m.tokens_used:,} tokens · "
                f"{m.savings_pct}% savings"
            )
        if advice.similar_count > len(shown):
            lines.append(f"  … and {advice.similar_count - len(shown)} more")

    graph_arg = f" --graph {graph_path}" if graph_path else ""
    lines.append("\nRun with recommended budget:")
    lines.append(
        f'  slurp "{advice.query}"{graph_arg} --budget {advice.recommended_budget}'
    )
    return "\n".join(lines)
