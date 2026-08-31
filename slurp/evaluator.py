"""Quality benchmark: are LLM answers better with slurp than without it?

`slurp benchmark` measures how many tokens the subgraph selector saves. It says
nothing about whether the answer is still correct. This module closes that loop:
each question is answered twice — once from a budgeted subgraph, once from the
entire graph — and an LLM judge scores both against a known ground truth.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

from slurp.budget import count_tokens, select_subgraph
from slurp.explainer import LLMConfig, SlurpExplainError, _call_llm
from slurp.formatter import format_subgraph
from slurp.scorer import score_nodes

DIFFICULTIES = ("easy", "medium", "hard")


@dataclass
class EvalQuestion:
    """One question with the answer it should produce."""

    id: str
    question: str
    area: str
    ground_truth: str
    relevant_nodes: list[str] = field(default_factory=list)
    difficulty: str = "medium"

    @classmethod
    def from_dict(cls, data: dict) -> "EvalQuestion":
        missing = [k for k in ("id", "question", "ground_truth") if not data.get(k)]
        if missing:
            raise ValueError(f"Question is missing required fields: {', '.join(missing)}")
        return cls(
            id=str(data["id"]),
            question=str(data["question"]),
            area=str(data.get("area", "general")),
            ground_truth=str(data["ground_truth"]),
            relevant_nodes=list(data.get("relevant_nodes") or []),
            difficulty=str(data.get("difficulty", "medium")),
        )


@dataclass
class EvalResult:
    """Both answers to one question, scored."""

    question_id: str
    question: str
    response_with_slurp: str = ""
    response_without_slurp: str = ""
    tokens_with_slurp: int = 0
    tokens_without_slurp: int = 0
    score_with_slurp: float = 0.0
    score_without_slurp: float = 0.0
    judge_reasoning: str = ""
    error: str = ""

    @property
    def winner(self) -> str:
        """Which context won. Equal scores are a tie, not a slurp win."""
        if self.score_with_slurp > self.score_without_slurp:
            return "slurp"
        if self.score_without_slurp > self.score_with_slurp:
            return "baseline"
        return "tie"

    @property
    def token_savings_pct(self) -> float:
        if self.tokens_without_slurp <= 0:
            return 0.0
        saved = self.tokens_without_slurp - self.tokens_with_slurp
        return round(max(0.0, saved) / self.tokens_without_slurp * 100, 1)

    def to_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "question": self.question,
            "response_with_slurp": self.response_with_slurp,
            "response_without_slurp": self.response_without_slurp,
            "tokens_with_slurp": self.tokens_with_slurp,
            "tokens_without_slurp": self.tokens_without_slurp,
            "score_with_slurp": self.score_with_slurp,
            "score_without_slurp": self.score_without_slurp,
            "winner": self.winner,
            "judge_reasoning": self.judge_reasoning,
            "error": self.error,
        }


@dataclass
class EvalSuite:
    """Aggregate of every scored question."""

    results: list[EvalResult] = field(default_factory=list)

    @property
    def scored(self) -> list[EvalResult]:
        """Results the judge actually scored. A failed call is not a tie."""
        return [r for r in self.results if not r.error]

    @property
    def slurp_win_rate(self) -> float:
        scored = self.scored
        if not scored:
            return 0.0
        wins = sum(1 for r in scored if r.winner == "slurp")
        return round(wins / len(scored) * 100, 1)

    @property
    def mean_score_slurp(self) -> float:
        return self._mean(r.score_with_slurp for r in self.scored)

    @property
    def mean_score_baseline(self) -> float:
        return self._mean(r.score_without_slurp for r in self.scored)

    @property
    def mean_token_savings(self) -> float:
        return self._mean(r.token_savings_pct for r in self.scored)

    @property
    def quality_per_token_slurp(self) -> float:
        return self._ratio(
            sum(r.score_with_slurp for r in self.scored),
            sum(r.tokens_with_slurp for r in self.scored),
        )

    @property
    def quality_per_token_baseline(self) -> float:
        return self._ratio(
            sum(r.score_without_slurp for r in self.scored),
            sum(r.tokens_without_slurp for r in self.scored),
        )

    @staticmethod
    def _mean(values) -> float:
        items = list(values)
        return round(sum(items) / len(items), 4) if items else 0.0

    @staticmethod
    def _ratio(total_score: float, total_tokens: int) -> float:
        return round(total_score / total_tokens, 8) if total_tokens else 0.0

    def to_dict(self) -> dict:
        return {
            "slurp_win_rate": self.slurp_win_rate,
            "mean_score_slurp": self.mean_score_slurp,
            "mean_score_baseline": self.mean_score_baseline,
            "mean_token_savings": self.mean_token_savings,
            "quality_per_token_slurp": self.quality_per_token_slurp,
            "quality_per_token_baseline": self.quality_per_token_baseline,
            "questions_scored": len(self.scored),
            "questions_failed": len(self.results) - len(self.scored),
            "results": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

ANSWER_PROMPT = """\
You are answering a question about a codebase, using only the context below.

{context}

Question: {question}

Answer in 2-4 sentences. Name the specific functions and files involved. If the
context does not contain the answer, say so plainly rather than guessing.\
"""

JUDGE_PROMPT = """\
You are evaluating two answers to a question about a codebase.

Question: {question}
Ground truth: {ground_truth}

Answer A: {response_a}

Answer B: {response_b}

Rate each answer from 0.0 to 1.0 based on:
- Accuracy: does it match the ground truth?
- Completeness: does it cover all relevant aspects?
- Specificity: does it name specific functions/files?

Respond ONLY with valid JSON:
{{"score_a": 0.0-1.0, "score_b": 0.0-1.0, "reasoning": "..."}}\
"""


def build_judge_prompt(
    question: str, ground_truth: str, response_a: str, response_b: str
) -> str:
    """Render the judge prompt for one question."""
    return JUDGE_PROMPT.format(
        question=question,
        ground_truth=ground_truth,
        response_a=response_a,
        response_b=response_b,
    )


_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def parse_judge_response(raw: str) -> tuple[float, float, str]:
    """Pull the two scores and the reasoning out of the judge's reply.

    Models wrap JSON in prose or fences often enough that a bare json.loads is
    not reliable; the first balanced-looking object is used instead.

    Raises:
        SlurpExplainError: If no scores can be recovered.
    """
    match = _JSON_BLOCK.search(raw or "")
    if match is None:
        raise SlurpExplainError(f"Judge returned no JSON object: {raw[:160]!r}")
    try:
        data = json.loads(match.group(0))
    except ValueError as exc:
        raise SlurpExplainError(f"Judge returned invalid JSON: {exc}") from None

    def _score(key: str) -> float:
        value = data.get(key)
        if not isinstance(value, (int, float)):
            raise SlurpExplainError(f"Judge response is missing a numeric {key!r}")
        return round(min(1.0, max(0.0, float(value))), 4)

    return _score("score_a"), _score("score_b"), str(data.get("reasoning", ""))


# ---------------------------------------------------------------------------
# Contexts
# ---------------------------------------------------------------------------

def build_slurp_context(
    G: nx.DiGraph, question: str, budget: int, model: str = "cl100k_base"
) -> tuple[str, int]:
    """Budgeted subgraph context for one question."""
    scores = score_nodes(G, question)
    subgraph, stats = select_subgraph(G, scores, budget=budget, model=model)
    text = format_subgraph(subgraph, stats, format="markdown", scores=scores,
                           query=question)
    return text, count_tokens(text, model=model)


def build_baseline_context(
    G: nx.DiGraph, model: str = "cl100k_base"
) -> tuple[str, int]:
    """The whole graph serialized — what you would inject without slurp."""
    total = G.number_of_nodes()
    stats = {
        "nodes_selected": total,
        "nodes_total": total,
        "tokens_used": 0,
        "tokens_budget": 1_000_000,
        "coverage_pct": 100.0,
    }
    text = format_subgraph(G, stats, format="markdown", query="")
    return text, count_tokens(text, model=model)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_eval(
    G: nx.DiGraph,
    questions: list[EvalQuestion],
    config: LLMConfig,
    budget: int = 4000,
    graph_path: Path | None = None,
    model: str = "cl100k_base",
    on_question=None,
) -> EvalSuite:
    """Answer every question twice and have a judge score both answers.

    Args:
        G: The graph under test.
        questions: Eval set.
        config: LLM provider used for both the answers and the judge.
        budget: Token budget for the slurp context.
        graph_path: Recorded for reporting; not used for retrieval.
        model: Tiktoken encoding for token counts.
        on_question: Optional callback invoked after each question, for progress.

    Returns:
        An EvalSuite. A question whose LLM or judge call fails is recorded with
        an `error` and excluded from the aggregates rather than scored as a tie.
    """
    # The baseline context is the same for every question, so it is built and
    # counted once instead of once per question.
    baseline_context, baseline_tokens = build_baseline_context(G, model=model)

    results: list[EvalResult] = []
    for question in questions:
        result = EvalResult(question_id=question.id, question=question.question)
        try:
            slurp_context, slurp_tokens = build_slurp_context(
                G, question.question, budget, model=model
            )
            result.tokens_with_slurp = slurp_tokens
            result.tokens_without_slurp = baseline_tokens

            result.response_with_slurp = _call_llm(
                ANSWER_PROMPT.format(context=slurp_context, question=question.question),
                config,
            )
            result.response_without_slurp = _call_llm(
                ANSWER_PROMPT.format(context=baseline_context, question=question.question),
                config,
            )
            raw = _call_llm(
                build_judge_prompt(
                    question.question,
                    question.ground_truth,
                    result.response_with_slurp,
                    result.response_without_slurp,
                ),
                config,
            )
            score_a, score_b, reasoning = parse_judge_response(raw)
            result.score_with_slurp = score_a
            result.score_without_slurp = score_b
            result.judge_reasoning = reasoning
        except SlurpExplainError as exc:
            result.error = str(exc)
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"

        results.append(result)
        if on_question is not None:
            on_question(result)

    return EvalSuite(results=results)


# ---------------------------------------------------------------------------
# Question sets
# ---------------------------------------------------------------------------

def load_eval_questions(path: Path) -> list[EvalQuestion]:
    """Read an eval set from a JSON file.

    Accepts either a bare list of question objects or an object with a
    "questions" key.

    Raises:
        SlurpExplainError: If the file is missing, malformed, or empty.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SlurpExplainError(f"Question file not found: {path}") from None
    except (OSError, ValueError) as exc:
        raise SlurpExplainError(f"Could not read {path}: {exc}") from None

    if isinstance(data, dict):
        data = data.get("questions", [])
    if not isinstance(data, list) or not data:
        raise SlurpExplainError(f"No questions found in {path}")

    try:
        return [EvalQuestion.from_dict(item) for item in data]
    except (ValueError, TypeError, AttributeError) as exc:
        raise SlurpExplainError(f"Invalid question in {path}: {exc}") from None


def default_questions() -> list[EvalQuestion]:
    """The built-in eval set: 15 questions over a PrismaStats-shaped codebase.

    Every ground truth was checked against an indexed graph of the project.
    Counts are stated as call edges, which is what `slurp explain` reports as
    callers; a node's total predecessor count is one higher, because the module
    that declares it also points at it.
    """
    return [
        # -- auth ---------------------------------------------------------
        EvalQuestion(
            id="auth-01",
            question="Which function validates admin permissions before executing admin operations?",
            area="auth",
            ground_truth=(
                "requireAdmin() in lib/supabase/admin-actions.ts. It is called by 41 "
                "functions and itself calls createClient()."
            ),
            relevant_nodes=["lib.supabase.admin_actions.requireAdmin"],
            difficulty="easy",
        ),
        EvalQuestion(
            id="auth-02",
            question="What creates the Supabase admin client used for privileged operations?",
            area="auth",
            ground_truth=(
                "createAdminClient() in lib/supabase/admin.ts, which uses the service "
                "role key to bypass RLS. It has 97 call edges and no dependencies of "
                "its own."
            ),
            relevant_nodes=["lib.supabase.admin.createAdminClient"],
            difficulty="easy",
        ),
        EvalQuestion(
            id="auth-03",
            question="What is the difference between createClient() and createAdminClient()?",
            area="auth",
            ground_truth=(
                "createClient() in lib/supabase/server.ts is the user-scoped client "
                "(42 call edges, used by loginAction and forgotPasswordAction); "
                "createAdminClient() in lib/supabase/admin.ts is the privileged one "
                "(97 call edges). Both are leaves. Some functions, such as getClubId() "
                "and getDashboardData(), call both."
            ),
            relevant_nodes=[
                "lib.supabase.server.createClient",
                "lib.supabase.admin.createAdminClient",
            ],
            difficulty="medium",
        ),
        EvalQuestion(
            id="auth-04",
            question="Which functions handle user login and logout?",
            area="auth",
            ground_truth=(
                "loginAction(), logoutAction() and forgotPasswordAction() in "
                "lib/auth/actions.ts. loginAction() calls createClient() and is called "
                "by handleSubmit()."
            ),
            relevant_nodes=["lib.auth.actions.loginAction"],
            difficulty="medium",
        ),
        EvalQuestion(
            id="auth-05",
            question="What is the standard pattern an admin server action follows?",
            area="auth",
            ground_truth=(
                "It calls requireAdmin() to check permissions and createAdminClient() "
                "for a privileged client. insertMatchEventAdmin(), "
                "deleteMatchEventAdmin() and insertSubstitutionEventAdmin() all follow "
                "exactly this pattern."
            ),
            relevant_nodes=[
                "lib.supabase.admin_actions.requireAdmin",
                "lib.supabase.admin_actions.insertMatchEventAdmin",
            ],
            difficulty="hard",
        ),
        # -- stats --------------------------------------------------------
        EvalQuestion(
            id="stats-01",
            question="Which function recalculates player statistics after a match event?",
            area="stats",
            ground_truth=(
                "recalcularPlayerStats() in lib/supabase/admin-actions.ts. It calls "
                "createAdminClient() and has 3 callers."
            ),
            relevant_nodes=["lib.supabase.admin_actions.recalcularPlayerStats"],
            difficulty="easy",
        ),
        EvalQuestion(
            id="stats-02",
            question="What triggers a player stats recalculation?",
            area="stats",
            ground_truth=(
                "Exactly three functions: insertMatchEventAdmin(), "
                "deleteMatchEventAdmin() and insertSubstitutionEventAdmin(), all in "
                "lib/supabase/admin-actions.ts."
            ),
            relevant_nodes=[
                "lib.supabase.admin_actions.insertMatchEventAdmin",
                "lib.supabase.admin_actions.deleteMatchEventAdmin",
                "lib.supabase.admin_actions.insertSubstitutionEventAdmin",
            ],
            difficulty="medium",
        ),
        EvalQuestion(
            id="stats-03",
            question="What is the full call chain from a user adding a match event to stats being updated?",
            area="stats",
            ground_truth=(
                "handleAddEvent() calls insertMatchEventAdmin(), which calls "
                "requireAdmin(), createAdminClient() and recalcularPlayerStats(); "
                "recalcularPlayerStats() then calls createAdminClient()."
            ),
            relevant_nodes=[
                "lib.supabase.admin_actions.insertMatchEventAdmin",
                "lib.supabase.admin_actions.recalcularPlayerStats",
            ],
            difficulty="hard",
        ),
        EvalQuestion(
            id="stats-04",
            question="Which function returns the league standings for the admin area?",
            area="stats",
            ground_truth=(
                "getStandingsAdmin() in lib/supabase/admin-actions.ts, alongside "
                "getAvailableStandingsMatchdaysAdmin(). Both follow the "
                "requireAdmin() + createAdminClient() pattern."
            ),
            relevant_nodes=["lib.supabase.admin_actions.getStandingsAdmin"],
            difficulty="medium",
        ),
        EvalQuestion(
            id="stats-05",
            question="Where is the PlayerStats type defined?",
            area="stats",
            ground_truth=(
                "As an interface in types/database.ts (node types.database.PlayerStats)."
            ),
            relevant_nodes=["types.database.PlayerStats"],
            difficulty="medium",
        ),
        # -- architecture -------------------------------------------------
        EvalQuestion(
            id="arch-01",
            question="What is the most called function in the project?",
            area="architecture",
            ground_truth=(
                "createAdminClient() in lib/supabase/admin.ts, with 97 call edges — "
                "far ahead of createClient() (42) and requireAdmin() (41)."
            ),
            relevant_nodes=["lib.supabase.admin.createAdminClient"],
            difficulty="easy",
        ),
        EvalQuestion(
            id="arch-02",
            question="Which files concentrate the most functions?",
            area="architecture",
            ground_truth=(
                "lib/supabase/admin-actions.ts with 39 functions and "
                "lib/supabase/dashboard-actions.ts with 24."
            ),
            relevant_nodes=["lib.supabase.admin_actions", "lib.supabase.dashboard_actions"],
            difficulty="medium",
        ),
        EvalQuestion(
            id="arch-03",
            question="What would break if createAdminClient() changed its signature?",
            area="architecture",
            ground_truth=(
                "HIGH impact: 97 direct call edges spread across pages, API route "
                "handlers and server actions, reaching roughly 141 nodes within three "
                "hops."
            ),
            relevant_nodes=["lib.supabase.admin.createAdminClient"],
            difficulty="medium",
        ),
        EvalQuestion(
            id="arch-04",
            question="What does the dashboard home page load its data from?",
            area="architecture",
            ground_truth=(
                "DashboardHomePage calls getDashboardData() in "
                "lib/supabase/dashboard-actions.ts, which calls calcularRacha(), "
                "createAdminClient(), createClient(), getMatchPoints(), "
                "getMatchResult() and getTemporadaActiva()."
            ),
            relevant_nodes=["lib.supabase.dashboard_actions.getDashboardData"],
            difficulty="hard",
        ),
        EvalQuestion(
            id="arch-05",
            question="How do client components consume chat state?",
            area="architecture",
            ground_truth=(
                "Through the useChatContext() hook in lib/chat/chat-context.tsx, used "
                "by 6 client components including PremiumChatWidget, JugadoresClient "
                "and RendimientoClient."
            ),
            relevant_nodes=["lib.chat.chat_context.useChatContext"],
            difficulty="medium",
        ),
    ]
