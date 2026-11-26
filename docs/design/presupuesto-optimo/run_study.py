"""Run the budget study: questions, parent graphs and measurements, 4 projects at a time.

Not part of slurp. Design: docs/design/presupuesto-optimo.md. Run with the slurp
checkout's interpreter, from anywhere:

    ~/Desktop/Slurp/slurp/.venv/bin/python -I run_study.py [PROJECT...]

Writes ~/slurp-bench/results/<project>.jsonl (one record per question, see
measure.py), <project>.meta.json and progress.log. Re-running resumes: questions
already measured are skipped. The reference question sets (PrismaStats 15, slurp
16) are measured too, as ref-prismastats.jsonl and ref-slurp.jsonl.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import runpy
import statistics
import subprocess
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
Q = runpy.run_path(str(HERE / "questions.py"))
M = runpy.run_path(str(HERE / "measure.py"))
BENCH: Path = Q["BENCH"]
RESULTS = BENCH / "results"

GROUPS = {
    "derivation": ["ky", "prismastats", "hono", "slurp", "jsoup", "caddy", "tokio", "sqlalchemy"],
    "validation": ["gin", "click", "ripgrep", "excalidraw", "commons-lang", "prometheus"],
}
N_QUESTIONS = 120
WORKERS = 4
VERIFY_FIRST = 2                      # questions per project checked against run_retrieval_eval
VERIFY_BUDGETS = [2000, 16000]
# Longest first, so the slowest project never starts last.
ORDER = ["sqlalchemy", "prometheus", "commons-lang", "tokio", "excalidraw", "caddy", "jsoup",
         "hono", "ripgrep", "click", "gin", "ky", "slurp", "prismastats"]


def group_of(project: str) -> str:
    return next(g for g, ps in GROUPS.items() if project in ps)


def _log(msg: str) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    with open(RESULTS / "progress.log", "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def _done_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {json.loads(line)["id"] for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()}


def _gzip(path: Path) -> None:
    if path.exists():
        subprocess.run(["gzip", "-f", str(path)], check=True)


def run_project(project: str) -> str:
    from slurp.budget import full_graph_tokens
    from slurp.loader import load_graph
    from slurp.scorer import score_nodes

    out = RESULTS / f"{project}.jsonl"
    done = _done_ids(out)
    t0 = time.time()
    verified, n, rejected = 0, 0, {}
    _log(f"{project}: start ({len(done)} already measured)")
    try:
        for q, rejected in Q["generate"](project, N_QUESTIONS):
            n += 1
            graph = Path(q["graph"])
            if q["id"] in done:
                _gzip(graph)
                continue
            G = load_graph(graph)
            rec = M["measure_question"](G, q)
            if verified < VERIFY_FIRST:
                diffs = M["verify"](G, q, rec, VERIFY_BUDGETS)
                if diffs:
                    _log(f"{project}: HARNESS DIFFERS from run_retrieval_eval on {q['id']}: {diffs}")
                    raise SystemExit(f"{project}: harness differs from run_retrieval_eval")
                verified += 1
            rec.update({k: q[k] for k in ("commit", "parent", "subject", "index_s")},
                       group=group_of(project))
            with open(out, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            _gzip(graph)
            if n % 20 == 0:
                _log(f"{project}: {n} questions, {time.time() - t0:.0f}s")
        # Full-graph tokens on the parent graph of median size (§3).
        recs = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
        mid = sorted(recs, key=lambda r: r["graph_nodes"])[len(recs) // 2]
        gz = BENCH / "graphs" / "q" / project / f"{mid['parent'][:12]}.json.gz"
        subprocess.run(["gunzip", "-kf", str(gz)], check=True)
        G = load_graph(gz.with_suffix(""))
        scores = score_nodes(G, "overview")
        meta = {"project": project, "group": group_of(project), "questions": len(recs),
                "stage_b_rejected": rejected, "seconds": round(time.time() - t0),
                "median_graph": {"parent": mid["parent"], "nodes": G.number_of_nodes(),
                                 "tokens_markdown": full_graph_tokens(G, scores, "overview",
                                                                      "markdown"),
                                 "tokens_json": full_graph_tokens(G, scores, "overview", "json")},
                "graph_nodes_median": statistics.median(r["graph_nodes"] for r in recs)}
        gz.with_suffix("").unlink()
        (RESULTS / f"{project}.meta.json").write_text(json.dumps(meta, indent=2))
        _log(f"{project}: done, {len(recs)} questions in {time.time() - t0:.0f}s")
        return f"{project}: {len(recs)}"
    except BaseException as exc:  # noqa: BLE001 - logged, then reported to the parent
        _log(f"{project}: FAILED {exc!r}\n{traceback.format_exc()}")
        return f"{project}: FAILED {exc!r}"


def run_references() -> None:
    """The existing hand-labelled sets, through the same harness (§2.6, §4.3)."""
    from slurp.evaluator import default_questions
    from slurp.loader import load_graph

    retrieval = runpy.run_path(str(REPO / "scripts" / "retrieval.py"))
    ps = [{"id": q.id, "question": q.question, "relevant_nodes": q.relevant_nodes}
          for q in default_questions()]
    src = Path("/tmp/slurp-af2ab66")
    if not (src / "slurp").is_dir():
        src.mkdir(parents=True, exist_ok=True)
        tar = subprocess.run(["git", "-C", str(REPO), "archive", "af2ab66"], check=True,
                             capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", str(src)], input=tar, check=True)
    sg = BENCH / "graphs" / "ref-slurp-af2ab66.json"
    if not sg.exists():
        subprocess.run([str(Q["SLURP"]), "index", str(src), "-o", str(sg), "--force"],
                       check=True, capture_output=True)
    for name, graph, qs in (("prismastats", BENCH / "graphs" / "prismastats.json", ps),
                            ("slurp", sg, retrieval["SLURP_QUESTIONS"])):
        G = load_graph(graph)
        out = RESULTS / f"ref-{name}.jsonl"
        with open(out, "w", encoding="utf-8") as f:
            for q in qs:
                rec = M["measure_question"](G, q)
                f.write(json.dumps({**rec, "group": "reference"}) + "\n")
        _log(f"ref-{name}: {len(qs)} questions")


def main(argv: list[str]) -> int:
    projects = argv or ORDER
    RESULTS.mkdir(parents=True, exist_ok=True)
    _log(f"study start: {projects}")
    with mp.get_context("spawn").Pool(WORKERS) as pool:
        for line in pool.imap_unordered(run_project, projects):
            print(line, flush=True)
    if not argv:
        run_references()
    _log("study done")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
