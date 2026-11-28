"""The question bank with v1.5's real implementation (#87): every parent re-indexed.

    ~/Desktop/Slurp/slurp/.venv/bin/python -I bank_real.py SRC [--workers 2]

One process per project (`--project P`, `--labelled`), at most --workers at a time. Before
launching each one the scheduler runs `memory_pressure` and waits while the kernel's
pressure level (kern.memorystatus_vm_pressure_level) is not 1, normal. A project whose
questions are all in its .jsonl is not launched again; a line cut short is dropped and the
project goes on from there. Logs in ~/slurp-bench/v15/bank-real/logs/.

SRC holds the `slurp` package to measure (a `git archive` snapshot of v1.5). For each of
the 1,525 questions the parent commit is checked out clean in a worktree of its own
(~/slurp-bench/v15/work/<project>: `checkout -f` + `clean -fdx`, as the bank was built),
indexed with SRC's index_project, then scored and selected at 4000 in markdown. v1.5
changes more than JSX (the regex branch of TS and Java, which also serves files whose
tree-sitter parse breaks), so no project is assumed unchanged.

`--precheck` first indexes the first question of each project with v1.4.0 (~/slurp-bench/
v15/src/v1.4.0, in a subprocess): it must give back the bank's stored graph, so the bank's
results are v1.4's. The run itself keeps to one process per worker.
The labelled sets are re-indexed too: PrismaStats at the bank's HEAD, slurp at af2ab66.
One JSON line per question in ~/slurp-bench/v15/bank-real/<project>.jsonl; resumable.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path.home() / "slurp-bench"
OUT = BENCH / "v15" / "bank-real"
WORK = BENCH / "v15" / "work"
V14 = BENCH / "v15" / "src" / "v1.4.0"
# Biggest first.
PROJECTS = ["sqlalchemy", "prometheus", "commons-lang", "tokio", "jsoup", "excalidraw",
            "caddy", "hono", "ripgrep", "gin", "click", "slurp", "prismastats", "ky"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def _worktree(project: str) -> Path:
    work = WORK / project
    if not (work / ".git").exists():
        WORK.mkdir(parents=True, exist_ok=True)
        _git(BENCH / "repos" / project, "worktree", "add", "-q", "--detach", str(work), "HEAD")
    return work


def _checkout(work: Path, rev: str) -> None:
    _git(work, "checkout", "-q", "--detach", "-f", rev)
    _git(work, "clean", "-qfdx")


def _index_with(src: str, root: Path) -> dict:
    """index_project from the package under *src*, in a fresh interpreter's import state."""
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); "
            "from slurp.indexer import index_project; from pathlib import Path; "
            "import slurp; assert slurp.__file__.startswith(sys.argv[1]); "
            "print(json.dumps(index_project(Path(sys.argv[2]))))")
    res = subprocess.run([sys.executable, "-I", "-c", code, src, str(root)],
                         capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def _same(a: dict, b: dict) -> bool:
    key = lambda xs: sorted(json.dumps(x, sort_keys=True) for x in xs)  # noqa: E731
    return key(a["nodes"]) == key(b["nodes"]) and key(a["links"]) == key(b["links"])


def _evaluate(G, question: str, relevant: list[str]) -> list:
    from slurp._graphutils import _left_out
    from slurp.budget import build_context
    from slurp.scorer import score_nodes
    rel = [n for n in relevant if n in G]
    scores = score_nodes(G, question)
    _t, sub, stats = build_context(G, scores, budget=4000, format="markdown", query=question)
    left = _left_out(G)
    order = sorted((n for n in G.nodes if n not in left), key=lambda n: (-scores[n], n))
    pos = {n: i + 1 for i, n in enumerate(order)}
    first = min((pos[n] for n in rel if n in pos), default=None)
    return [round(sum(1 for n in rel if n in sub) / len(rel), 4) if rel else None,
            stats["tokens_used"], first, len(rel)]


def _done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    data = path.read_bytes()
    good = data[: data.rfind(b"\n") + 1]
    if len(good) != len(data):
        path.write_bytes(good)
    return {json.loads(x)["id"] for x in good.decode().splitlines()}


def run_project(args: tuple[str, str]) -> str:
    src, project = args
    sys.path.insert(0, src)
    import slurp
    assert slurp.__file__.startswith(src), slurp.__file__
    from slurp.indexer import index_project
    from slurp.loader import build_graph

    dest = OUT / f"{project}.jsonl"
    done = _done(dest)
    work = _worktree(project)
    t0, n = time.time(), 0
    recs = [json.loads(x) for x in (BENCH / "results" / f"{project}.jsonl").read_text().splitlines()]
    with dest.open("a") as fh:
        for i, rec in enumerate(r for r in recs if r.get("present")):
            if rec["id"] in done:
                continue
            _checkout(work, rec["parent"])
            graph = index_project(work)
            out = {"id": rec["id"], "names_symbol": rec["names_symbol"]}
            if i == 0:
                stored = json.loads(gzip.open(
                    BENCH / "graphs" / "q" / project / f"{rec['parent'][:12]}.json.gz").read())
                out["v15_equals_bank"] = _same(graph, stored)
            out["renders"] = sum(1 for e in graph["links"] if e["relation"] == "renders")
            out["nodes"] = len(graph["nodes"])
            out["r"] = _evaluate(build_graph(graph), rec["question"], rec["relevant"])
            fh.write(json.dumps(out) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
            n += 1
    return f"{project}: +{n} questions, {time.time() - t0:.0f} s"


def run_labelled(src: str) -> str:
    sys.path.insert(0, src)
    import runpy

    from slurp.evaluator import default_questions
    from slurp.indexer import index_project
    from slurp.loader import build_graph
    dest = OUT / "ref.jsonl"
    if dest.exists():
        return "labelled: done"
    repo = Path("/Users/juancarlosvallejoruiz/Desktop/Slurp/slurp")
    retrieval = runpy.run_path(str(repo / "scripts" / "retrieval.py"))
    af2 = BENCH / "v15" / "src" / "af2ab66"
    if not (af2 / "slurp").is_dir():
        af2.mkdir(parents=True, exist_ok=True)
        tar = subprocess.run(["git", "-C", str(repo), "archive", "af2ab66"], check=True,
                             capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", str(af2)], input=tar, check=True)
    lines = []
    sets = [("prismastats", BENCH / "repos" / "prismastats",
             [(q.id, q.question, q.relevant_nodes) for q in default_questions()]),
            ("slurp", af2, [(q["id"], q["question"], q["relevant_nodes"])
                            for q in retrieval["SLURP_QUESTIONS"]])]
    for name, root, qs in sets:
        G = build_graph(index_project(root))
        for qid, question, relevant in qs:
            lines.append(json.dumps({"set": name, "id": qid, "r": _evaluate(G, question, relevant)}))
    dest.write_text("\n".join(lines) + "\n")
    return f"labelled: {len(lines)} questions"


def precheck() -> None:
    """v1.4.0 reproduces the stored parent graph of each project's first question."""
    lines = []
    for p in PROJECTS:
        rec = next(r for r in map(json.loads, (BENCH / "results" / f"{p}.jsonl")
                                  .read_text().splitlines()) if r.get("present"))
        work = _worktree(p)
        _checkout(work, rec["parent"])
        stored = json.loads(gzip.open(
            BENCH / "graphs" / "q" / p / f"{rec['parent'][:12]}.json.gz").read())
        g14 = _index_with(str(V14), work)
        line = (f"{p} {rec['parent'][:12]} v1.4 == bank: {_same(g14, stored)} "
                f"nodes {len(g14['nodes'])}/{len(stored['nodes'])} "
                f"links {len(g14['links'])}/{len(stored['links'])}")
        print(line, flush=True)
        lines.append(line)
    (OUT / "precheck-v14.txt").write_text("\n".join(lines) + "\n")


def _complete(project: str) -> bool:
    present = {json.loads(x)["id"] for x in (BENCH / "results" / f"{project}.jsonl")
               .read_text().splitlines() if json.loads(x).get("present")}
    return present <= _done(OUT / f"{project}.jsonl")


def _pressure() -> tuple[int, str]:
    """(kernel pressure level: 1 normal, 2 warn, 4 critical; memory_pressure's free line)."""
    free = subprocess.run(["memory_pressure"], capture_output=True, text=True).stdout
    line = next((x for x in free.splitlines() if "free percentage" in x), "?").strip()
    level = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                           capture_output=True, text=True).stdout.strip()
    return int(level or 0), line


def _log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def schedule(src: str, workers: int) -> None:
    pending = [p for p in PROJECTS if not _complete(p)]
    _log(f"complete: {[p for p in PROJECTS if p not in pending]}")
    if not (OUT / "ref.jsonl").exists():
        pending.append("--labelled")
    (OUT / "logs").mkdir(exist_ok=True)
    running: dict[str, subprocess.Popen] = {}
    last_launch = 0.0
    while pending or running:
        for name, proc in list(running.items()):
            if proc.poll() is not None:
                _log(f"{name} exited {proc.returncode}")
                del running[name]
        # Let a launch show in memory before judging the next one.
        if pending and len(running) < workers and time.time() - last_launch >= 60:
            level, free = _pressure()
            if level != 1:
                _log(f"waiting: pressure level {level}, {free}")
                time.sleep(60)
                continue
            name = pending.pop(0)
            args = ["--labelled"] if name == "--labelled" else ["--project", name]
            log = (OUT / "logs" / f"{name.lstrip('-')}.log").open("a")
            running[name] = subprocess.Popen(
                [sys.executable, "-I", __file__, src, *args], stdout=log, stderr=log)
            last_launch = time.time()
            _log(f"launched {name} (pressure level {level}, {free})")
        time.sleep(10)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--precheck", action="store_true")
    ap.add_argument("--project")
    ap.add_argument("--labelled", action="store_true")
    a = ap.parse_args()
    src = str(Path(a.src).resolve())
    OUT.mkdir(parents=True, exist_ok=True)
    if a.precheck:
        precheck()
    elif a.project:
        _log(run_project((src, a.project)))
    elif a.labelled:
        _log(run_labelled(src))
    else:
        schedule(src, a.workers)


if __name__ == "__main__":
    main()
