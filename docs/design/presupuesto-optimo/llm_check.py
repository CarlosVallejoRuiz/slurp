"""The optional LLM check of the budget study (design §5). Not part of slurp.

Does a model answer better with more context than the optimal budget? Only run
if a rule was accepted, and only when the author says so: by default this
prepares the prompts and calls nothing.

    PY=~/Desktop/Slurp/slurp/.venv/bin/python
    $PY -I llm_check.py prepare          # writes ~/slurp-bench/llm/prompts.jsonl, no calls
    $PY -I llm_check.py run --launch     # calls `claude -p` on the author's subscription
    $PY -I llm_check.py report           # scores per budget, against the criterion

Sample (§5): hono, tokio and sqlalchemy (derivation), 20 questions each drawn
with a fixed seed, markdown at B*/2, B*, 2·B* and 4·B* (rounded up to the grid,
the control budget allowed for 4·B*): 240 answers, each judged once.
Criterion: the mean score at 2·B* and at 4·B* is at most 0.05 above B*'s
(90 % bootstrap interval); more than 0.10 above is flagged.
"""

from __future__ import annotations

import json
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = Path.home() / "slurp-bench"
OUT = BENCH / "llm"
PROJECTS = ["hono", "tokio", "sqlalchemy"]
PER_PROJECT = 20
SEED = 5
MULTIPLES = {"half": 0.5, "star": 1, "double": 2, "quad": 4}
LADDER = [1000, 1500, 2000, 3000, 4000, 6000, 8000, 12000, 16000, 24000, 32000, 64000]
CLAUDE = Path.home() / ".vscode/extensions/anthropic.claude-code-2.1.331-darwin-arm64" \
    / "resources/native-binary/claude"

ANSWER_SYSTEM = ("You help a developer find code. You only see the context given: a slice of "
                 "the project's code graph. Answer with the functions, methods or classes the "
                 "developer would change, most likely first, at most 8, one per line as "
                 "`qualified.name (file)`. No explanation.")
JUDGE_SYSTEM = ("You grade an answer against what a commit actually changed. Reply with JSON "
                "only: {\"score\": <0..1>, \"why\": \"<one sentence>\"}. 1 means the answer "
                "names the main definitions that changed; 0 means it names none of them; "
                "partial credit for some.")


def _round_up(x: float) -> int:
    return next((b for b in LADDER if b >= x), LADDER[-1])


def prepare() -> Path:
    from slurp.budget import build_context
    from slurp.loader import load_graph
    from slurp.scorer import score_nodes

    summary = json.loads((HERE / "resultados" / "resumen.json").read_text())
    if not summary.get("rule_accepted"):
        raise SystemExit("No rule was accepted: the design runs the LLM check only if one is.")
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for project in PROJECTS:
        b_star = summary["projects"][project]["optimal"]["markdown"]["b_star"]
        recs = [json.loads(line) for line in
                (BENCH / "results" / f"{project}.jsonl").read_text().splitlines()]
        sample = random.Random(f"{SEED}:{project}").sample(recs, PER_PROJECT)
        for rec in sample:
            gz = BENCH / "graphs" / "q" / project / f"{rec['parent'][:12]}.json.gz"
            subprocess.run(["gunzip", "-kf", str(gz)], check=True)
            G = load_graph(gz.with_suffix(""))
            scores = score_nodes(G, rec["question"])
            labels = [(str(G.nodes[n].get("label", n)), str(G.nodes[n].get("source_file", "")))
                      for n in rec["relevant"] if n in G]
            for name, mult in MULTIPLES.items():
                budget = _round_up(b_star * mult)
                text, _, _ = build_context(G, scores, budget=budget, format="markdown",
                                           query=rec["question"])
                rows.append({"id": rec["id"], "project": project, "multiple": name,
                             "budget": budget, "question": rec["question"],
                             "subject": rec["subject"], "relevant": labels,
                             "prompt": f"{text}\n\nThe developer's task: {rec['question']}"})
            gz.with_suffix("").unlink()
    path = OUT / "prompts.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"{len(rows)} prompts in {path}; nothing was sent")
    return path


def _claude(system: str, prompt: str) -> dict:
    """One `claude -p` call with no tools, no MCP and no project or user settings."""
    with tempfile.TemporaryDirectory() as empty:
        res = subprocess.run(
            [str(CLAUDE), "-p", "--output-format", "json", "--tools", "",
             "--strict-mcp-config", "--setting-sources", "project", "--no-session-persistence",
             "--system-prompt", system],
            input=prompt, capture_output=True, text=True, cwd=empty, timeout=600)
    if res.returncode != 0:
        raise RuntimeError(res.stderr[-500:])
    return json.loads(res.stdout)


def run(launch: bool) -> None:
    if not launch:
        raise SystemExit("Refusing to call claude without --launch (the author decides).")
    if not CLAUDE.exists() and not shutil.which("claude"):
        raise SystemExit(f"claude not found at {CLAUDE}")
    prompts = [json.loads(line) for line in (OUT / "prompts.jsonl").read_text().splitlines()]
    out = OUT / "answers.jsonl"
    done = {(json.loads(line)["id"], json.loads(line)["multiple"])
            for line in out.read_text().splitlines()} if out.exists() else set()
    for p in prompts:
        if (p["id"], p["multiple"]) in done:
            continue
        ans = _claude(ANSWER_SYSTEM, p["prompt"])
        answer = ans.get("result", "")
        truth = "\n".join(f"- {lbl} ({f})" for lbl, f in p["relevant"])
        judge = _claude(JUDGE_SYSTEM, f"Commit: {p['subject']}\nDefinitions it changed:\n{truth}"
                                      f"\n\nAnswer to grade:\n{answer}")
        try:
            score = float(json.loads(judge.get("result", "{}"))["score"])
        except (ValueError, KeyError, TypeError):
            score = None
        named = sum(1 for lbl, _ in p["relevant"] if lbl.rstrip("()") in answer)
        with open(out, "a") as f:
            f.write(json.dumps({"id": p["id"], "project": p["project"],
                                "multiple": p["multiple"], "budget": p["budget"],
                                "answer": answer, "judge": judge.get("result"), "score": score,
                                "named": named / max(1, len(p["relevant"])),
                                "model": ans.get("model") or ans.get("modelUsage"),
                                "cost_usd": (ans.get("total_cost_usd") or 0)
                                + (judge.get("total_cost_usd") or 0)}) + "\n")


def report() -> dict:
    rows = [json.loads(line) for line in (OUT / "answers.jsonl").read_text().splitlines()]
    by = {m: {r["id"]: r for r in rows if r["multiple"] == m and r["score"] is not None}
          for m in MULTIPLES}
    out = {}
    rng = random.Random(0)
    ids = sorted(set(by["star"]))
    for m in ("double", "quad"):
        common = [i for i in ids if i in by[m]]
        diffs = [by[m][i]["score"] - by["star"][i]["score"] for i in common]
        boots = sorted(statistics.fmean(rng.choice(diffs) for _ in diffs) for _ in range(2000))
        out[m] = {"mean_diff": statistics.fmean(diffs), "p5": boots[100], "p95": boots[1900],
                  "passed": boots[1900] <= 0.05, "flag": statistics.fmean(diffs) > 0.10}
    out["means"] = {m: statistics.fmean(r["score"] for r in by[m].values()) for m in MULTIPLES}
    out["named"] = {m: statistics.fmean(r["named"] for r in rows if r["multiple"] == m)
                    for m in MULTIPLES}
    print(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "prepare"
    if cmd == "prepare":
        prepare()
    elif cmd == "run":
        run("--launch" in sys.argv)
    elif cmd == "report":
        report()
