"""Analysis of the agent measurement (docs/design/mcp-autonomo.md §7). Not part of slurp.

Usage, after running the ten tasks with run_tasks.sh:

    uv run python docs/design/mcp-autonomo/analyze_sessions.py RESULTS_DIR

RESULTS_DIR holds one directory per task (named by its id in tareas.json), each
with the `session.log` copied from the task's `.slurp/` and `claude.json`, the
output of `claude -p ... --output-format json`.

It prints one row per task and checks the criterion fixed before measuring:

- at least 8 of 10 sessions call slurp on their own, and in at least 7 of 10
  the first call comes before any edit;
- requirements: 0 approvals asked (permission_denials empty), no answer from a
  stale graph without a warning, at most 5 % of answers with a warning, p95 of
  the refresh time <= 2 s;
- informative, no threshold: share of edited files that had a slurp_impact or
  slurp_explain about them first.

With n = 10 every task is worth 10 percentage points. Exit status 0 when the
criterion is met, 1 when not, 2 when a task's files are missing.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

CALL_MIN, BEFORE_EDIT_MIN = 8, 7
WARNED_MAX_SHARE = 0.05
P95_REFRESH_MAX_MS = 2000.0


@dataclass
class TaskResult:
    task: str
    calls: list[dict] = field(default_factory=list)
    edited: list[str] = field(default_factory=list)
    denials: int = 0
    missing: str = ""

    @property
    def spontaneous(self) -> bool:
        return bool(self.calls)

    @property
    def first_call_before_edit(self) -> bool:
        """No file had changed when the first call came (its answer saw none)."""
        if not self.calls:
            return False
        first = self.calls[0]
        return not (first.get("changed") or {}).get("count", 0)

    @property
    def stale_without_warning(self) -> int:
        return sum(1 for c in self.calls
                   if c.get("graph") in ("stale", "failed") and not c.get("warned"))

    @property
    def warned(self) -> int:
        return sum(1 for c in self.calls if c.get("warned"))

    @property
    def refresh_ms(self) -> list[float]:
        return [float(c["refresh_ms"]) for c in self.calls
                if isinstance(c.get("refresh_ms"), (int, float))]

    @property
    def edited_after_impact_or_explain(self) -> int:
        count = 0
        for path in self.edited:
            seen_at = next((i for i, c in enumerate(self.calls)
                            if path in ((c.get("changed") or {}).get("files") or [])),
                           len(self.calls))
            if any(_about(c, path) for c in self.calls[:seen_at]):
                count += 1
        return count


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").removeprefix("./").strip("/")


def _about(call: dict, path: str) -> bool:
    args = call.get("args") or {}
    target = _norm(path)
    if call.get("tool") == "slurp_impact":
        asked = _norm(args.get("file_path", ""))
        return bool(asked) and (target == asked or target.endswith("/" + asked))
    if call.get("tool") == "slurp_explain":
        return _norm(args.get("file", "")) == target
    return False


def load_task(directory: Path) -> TaskResult:
    result = TaskResult(task=directory.name)
    log, claude = directory / "session.log", directory / "claude.json"
    if not log.is_file() or not claude.is_file():
        result.missing = ", ".join(p.name for p in (log, claude) if not p.is_file())
        return result
    entries = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    # One task, one session; if the server restarted, all its sessions count.
    result.calls = [e for e in entries if e.get("event") == "call"]
    for end in (e for e in entries if e.get("event") == "end"):
        result.edited.extend(f for f in (end.get("changed") or {}).get("files") or []
                             if f not in result.edited)
    try:
        output = json.loads(claude.read_text(encoding="utf-8"))
    except ValueError:
        result.missing = "claude.json (not JSON)"
        return result
    result.denials = len(output.get("permission_denials") or [])
    return result


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def analyse(results: list[TaskResult]) -> tuple[bool, list[str]]:
    """(criterion met, report lines)."""
    lines = [f"{'task':<6} {'calls':>5} {'1st<edit':>8} {'denials':>7} {'warned':>6} "
             f"{'stale!':>6} {'edited':>6} {'after i/e':>9}  tools"]
    for r in results:
        if r.missing:
            lines.append(f"{r.task:<6} missing {r.missing}")
            continue
        tools: dict[str, int] = {}
        for c in r.calls:
            name = str(c.get("tool", "?")).removeprefix("slurp_")
            tools[name] = tools.get(name, 0) + 1
        lines.append(f"{r.task:<6} {len(r.calls):>5} {('yes' if r.first_call_before_edit else 'no'):>8} "
                     f"{r.denials:>7} {r.warned:>6} {r.stale_without_warning:>6} "
                     f"{len(r.edited):>6} {r.edited_after_impact_or_explain:>9}  "
                     + " · ".join(f"{k} {v}" for k, v in sorted(tools.items())))
    done = [r for r in results if not r.missing]
    calls = [c for r in done for c in r.calls]
    spontaneous = sum(r.spontaneous for r in done)
    before_edit = sum(r.first_call_before_edit for r in done)
    denials = sum(r.denials for r in done)
    stale = sum(r.stale_without_warning for r in done)
    warned_share = (sum(r.warned for r in done) / len(calls)) if calls else 0.0
    refresh_p95 = p95([v for r in done for v in r.refresh_ms])
    edited = sum(len(r.edited) for r in done)
    after = sum(r.edited_after_impact_or_explain for r in done)
    checks = [
        (f"spontaneous call in {spontaneous}/{len(done)} sessions (need {CALL_MIN})",
         spontaneous >= CALL_MIN),
        (f"first call before any edit in {before_edit}/{len(done)} (need {BEFORE_EDIT_MIN})",
         before_edit >= BEFORE_EDIT_MIN),
        (f"approvals asked: {denials} (need 0)", denials == 0),
        (f"answers from a stale graph without a warning: {stale} (need 0)", stale == 0),
        (f"answers with a warning: {warned_share:.1%} (need <= {WARNED_MAX_SHARE:.0%})",
         warned_share <= WARNED_MAX_SHARE),
        (f"p95 refresh: {'—' if refresh_p95 is None else f'{refresh_p95:.0f} ms'} "
         f"(need <= {P95_REFRESH_MAX_MS:.0f} ms)",
         refresh_p95 is None or refresh_p95 <= P95_REFRESH_MAX_MS),
    ]
    lines.append("")
    for text, ok in checks:
        lines.append(f"{'PASS' if ok else 'FAIL'}  {text}")
    lines.append(f"info  edited files with a slurp_impact/slurp_explain first: {after}/{edited}"
                 + (f" ({after / edited:.0%})" if edited else ""))
    met = len(done) == len(results) and all(ok for _, ok in checks)
    return met, lines


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.split("\n\n")[1])
        return 2
    root = Path(argv[0])
    results = [load_task(d) for d in sorted(p for p in root.iterdir() if p.is_dir())]
    met, lines = analyse(results)
    print("\n".join(lines))
    if any(r.missing for r in results):
        return 2
    return 0 if met else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
