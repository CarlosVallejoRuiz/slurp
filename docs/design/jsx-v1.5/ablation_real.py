"""Where each question the v1.5 bank changed gets its change from, commit by commit (#87).
Not part of slurp.

    ~/Desktop/Slurp/slurp/.venv/bin/python -I ablation_real.py PROJECT [PROJECT...]

For every question whose recall in bank_real.py differs from v1.4's, the parent is checked
out in its worktree (~/slurp-bench/v15/work/<project>) and indexed with three snapshots of
the package (~/slurp-bench/v15/src/<commit>, made with `git archive` when missing):

- 7bafa0a: the three fixes of the TypeScript regex branch, no W2, no JSX;
- c6b9378: plus memo/forwardRef components as nodes (W2);
- c210b1d: plus `renders` edges, i.e. v1.5.

Each graph is scored as bank_real.py does (recall at 4000 in markdown); v1.4 is the
bank's own figure and the W2 prototype's recall (bank.py) is printed beside. One process
at a time; the worktree is left at HEAD. Writes ~/slurp-bench/v15/ablation-PROJECT.json.
"""

import json
import runpy
import subprocess
import sys
from pathlib import Path

BENCH = Path.home() / "slurp-bench"
REPO = Path(__file__).resolve().parents[3]
STEPS = ["7bafa0a", "c6b9378", "c210b1d"]
_HERE = Path(__file__).resolve().parent
_bank = runpy.run_path(str(_HERE / "bank_real.py"), run_name="bank_real")

_SCORE = r"""
import json, runpy, sys
sys.path.insert(0, sys.argv[1])
import slurp; assert slurp.__file__.startswith(sys.argv[1]), slurp.__file__
from pathlib import Path
from slurp.indexer import index_project
from slurp.loader import build_graph
ev = runpy.run_path(sys.argv[2], run_name="bank_real")["_evaluate"]
qs = json.loads(sys.argv[4])
G = build_graph(index_project(Path(sys.argv[3])))
print(json.dumps({q["id"]: ev(G, q["question"], q["relevant"]) for q in qs}))
"""


def _snapshot(commit: str) -> Path:
    dest = BENCH / "v15" / "src" / commit
    if not (dest / "slurp").is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        tar = subprocess.run(["git", "-C", str(REPO), "archive", commit, "slurp"], check=True,
                             capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", str(dest)], input=tar, check=True)
    return dest


def _rows(path: Path) -> dict[str, dict]:
    return {r["id"]: r for r in map(json.loads, path.read_text().splitlines())}


def _v14(project: str) -> dict[str, float]:
    for group in ("impl-der", "impl-val"):
        p = BENCH / "v14" / group / f"{project}.jsonl"
        if p.exists():
            return {i: r["r"]["V0"][0] for i, r in _rows(p).items()}
    raise FileNotFoundError(project)


def ablate(project: str) -> dict:
    v14 = _v14(project)
    real = _rows(BENCH / "v15" / "bank-real" / f"{project}.jsonl")
    bank = _rows(BENCH / "results" / f"{project}.jsonl")
    proto_path = BENCH / "v15" / "bank" / f"{project}.jsonl"
    proto = {i: r["r"]["W2"][0] for i, r in _rows(proto_path).items()} if proto_path.exists() \
        else {}
    changed = [i for i in real if real[i]["r"][0] != v14[i]]
    by_parent: dict[str, list[dict]] = {}
    for i in changed:
        by_parent.setdefault(bank[i]["parent"], []).append(
            {"id": i, "question": bank[i]["question"], "relevant": bank[i]["relevant"]})
    work = _bank["_worktree"](project)
    out: dict[str, dict] = {}
    try:
        for parent, qs in by_parent.items():
            _bank["_checkout"](work, parent)
            for step in STEPS:
                res = subprocess.run(
                    [sys.executable, "-I", "-c", _SCORE, str(_snapshot(step)),
                     str(_HERE / "bank_real.py"), str(work), json.dumps(qs)],
                    capture_output=True, text=True, check=True)
                for qid, r in json.loads(res.stdout).items():
                    out.setdefault(qid, {"v14": v14[qid], "proto_w2": proto.get(qid)})[step] = r[0]
    finally:
        _bank["_checkout"](work, "HEAD")
    for qid, row in out.items():
        assert row["c210b1d"] == real[qid]["r"][0], (qid, row, real[qid]["r"])
        # which step moved it: the first commit whose recall differs from the one before
        prev, row["moved_by"] = row["v14"], []
        for step in STEPS:
            if row[step] != prev:
                row["moved_by"].append(step)
            prev = row[step]
    return out


def main(projects: list[str]) -> None:
    for p in projects:
        res = ablate(p)
        (BENCH / "v15" / f"ablation-{p}.json").write_text(json.dumps(res, indent=1) + "\n")
        print(f"## {p}: {len(res)} questions changed against v1.4")
        print(f"   {'question':24s} {'v1.4':>6s} {'proto':>6s} " +
              " ".join(f"{s:>8s}" for s in STEPS) + "  moved by")
        for qid, r in sorted(res.items()):
            proto = "" if r["proto_w2"] is None else f"{r['proto_w2']:.3f}"
            print(f"   {qid:24s} {r['v14']:6.3f} {proto:>6s} " +
                  " ".join(f"{r[s]:8.3f}" for s in STEPS) + f"  {', '.join(r['moved_by'])}")


if __name__ == "__main__":
    main(sys.argv[1:])
