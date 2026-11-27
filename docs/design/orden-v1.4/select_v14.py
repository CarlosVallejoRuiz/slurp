"""The v1.4 ranking study's decisions, as fixed in docs/design/orden-v1.4.md. Not part of slurp.

Pure functions over mean recall at 4000 in markdown per project and configuration;
run_v14.py measures, this decides.
"""

from __future__ import annotations

import statistics

DECIDERS = ["ky", "prismastats", "hono", "jsoup", "caddy", "tokio", "sqlalchemy"]
VALIDATION = ["gin", "click", "ripgrep", "excalidraw", "commons-lang", "prometheus"]
STEP_MIN = 0.01          # a variant joins the combination only for +0.01 ...
PROJECT_LOSS_MAX = 0.03  # ... with no project losing more than 0.03
GAIN_MIN = 0.05          # the derivation gate and the validation criterion
DOC_PRINT_MAX_COST = 0.01
REF_SIZES = {"prismastats": 15, "slurp": 16}


def macro(by_project: dict[str, float], projects: list[str]) -> float:
    return statistics.fmean(by_project[p] for p in projects)


def _min_delta(means: dict, cfg: str, ref: str, projects: list[str]) -> float:
    return min(means[cfg][p] - means[ref][p] for p in projects)


def best_addition(means: dict, current: str, candidates: list[str],
                  projects: list[str]) -> str | None:
    """The candidate combination that may join (§3, Combinaciones), or None."""
    base = macro(means[current], projects)
    ok = [c for c in candidates
          if macro(means[c], projects) - base >= STEP_MIN - 1e-9
          and _min_delta(means, c, current, projects) >= -PROJECT_LOSS_MAX - 1e-9]
    return max(ok, key=lambda c: (macro(means[c], projects), -len(c), c)) if ok else None


def gate(means: dict, cand: str, projects: list[str], base: str = "base",
         latency_ok: bool = True) -> dict:
    """The derivation gate (§6): +0.05 on average, no project losing more than 0.03."""
    gain = macro(means[cand], projects) - macro(means[base], projects)
    worst = _min_delta(means, cand, base, projects)
    return {"gain": gain, "worst": worst, "latency_ok": latency_ok,
            "passed": gain >= GAIN_MIN - 1e-9 and worst >= -PROJECT_LOSS_MAX - 1e-9
            and latency_ok}


def v6_allowed(without: dict[str, float], with_v6: dict[str, float]) -> bool:
    """V6 stays out if any labelled question's recall drops (author's condition)."""
    return all(with_v6[q] >= without[q] - 1e-9 for q in without)


def print_docstring(r_printed: float, r_scoring_only: float) -> bool:
    """Print the first docstring line if that costs less than 0.01 (author's rule)."""
    return r_scoring_only - r_printed < DOC_PRINT_MAX_COST - 1e-12


def accept(means: dict, cand: str, projects: list[str], refs: dict[str, tuple],
           latency_ok: bool, base: str = "base") -> dict:
    """Validation (§6): the gain, the per-project loss, latency and the labelled sets.

    refs: {set name: (recall at 4000 v1.3, recall at 4000 candidate)}; each set may
    lose at most one question.
    """
    g = gate(means, cand, projects, base, latency_ok)
    ref_ok = {n: (b - c) <= 1 / REF_SIZES[n] + 1e-9 for n, (b, c) in refs.items()}
    return {**g, "refs_ok": ref_ok, "passed": g["passed"] and all(ref_ok.values())}
