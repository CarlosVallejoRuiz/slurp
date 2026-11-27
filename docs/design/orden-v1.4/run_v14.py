"""Measure v1.4 ranking variants on the question bank. Not part of slurp.

Design: docs/design/orden-v1.4.md. Run with the slurp checkout's interpreter:

    ~/Desktop/Slurp/slurp/.venv/bin/python -I run_v14.py ROUND GROUP CONFIGS.json [--full]

GROUP is derivation, validation or references. CONFIGS maps a name to a list of
family tokens (see FAMILIES; [] is BASE). Every question is loaded once and scored
with every configuration, then selected by v1.3's build_context at 4000 in markdown
(--full adds JSON 4000 and markdown 8000 and 16000). "V0" is slurp's own score_nodes:
each question checks it against the bank's figure and BASE against it, score by score.
Writes ~/slurp-bench/v14/ROUND/<project>.jsonl.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import runpy
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = Path.home() / "slurp-bench"
OUT = BENCH / "v14"

FAMILIES: dict[str, list[dict]] = {
    "V1": [{"lex": "bm25", "doc": True, "k1": k1, "b": b, "w_name": wn}
           for k1 in (0.9, 1.2, 1.5) for b in (0.5, 0.75) for wn in (2.0, 3.0)],
    "V2": [{"struct": "ppr", "restart": a, "r": r, "k": k}
           for a in (0.15, 0.3, 0.5) for r in (0.5, 1.0) for k in (10, 30)],
    "V3": [{"w_path": w} for w in (0.25, 0.5, 1.0)],
    "V4": [{"cohesion": lam} for lam in (0.3, 0.5, 0.7)],
    "V5": [{"stem": True}],
    "V6": [{"stop": True}],
    # Controls, never selected: BM25 on the name alone, and the docstring printed.
    "V1-0": [{"lex": "bm25", "doc": False, "k1": k1, "b": b, "w_name": 1.0}
             for k1 in (0.9, 1.2, 1.5) for b in (0.5, 0.75)],
    "PRINT": [{"print_doc": True}],
}
GROUPS = {
    "derivation": ["sqlalchemy", "tokio", "jsoup", "caddy", "hono", "prismastats", "slurp", "ky"],
    "validation": ["prometheus", "commons-lang", "excalidraw", "ripgrep", "click", "gin"],
}
EXTRA = [("json", 4000), ("markdown", 8000), ("markdown", 16000)]


def token(family: str, i: int) -> str:
    return f"{family}#{i}"


def cfg_of(tokens: list[str], base: dict) -> dict:
    cfg = dict(base)
    for t in tokens:
        family, i = t.split("#")
        cfg.update(FAMILIES[family][int(i)])
    return cfg


class Blobs:
    """Files at a commit, through one `git cat-file --batch`."""

    def __init__(self, repo: Path) -> None:
        self.p = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def read(self, rev: str, path: str) -> str | None:
        self.p.stdin.write(f"{rev}:{path}\n".encode())
        self.p.stdin.flush()
        header = self.p.stdout.readline().decode().split()
        if len(header) < 3 or header[1] != "blob":
            return None
        data = self.p.stdout.read(int(header[2]))
        self.p.stdout.read(1)
        return data.decode("utf-8", "replace")

    def close(self) -> None:
        self.p.stdin.close()
        self.p.wait()


def _evaluate(G, q: dict, configs: dict, V: dict, full: bool) -> dict:
    from slurp.budget import build_context
    from slurp.scorer import score_nodes

    rel = [n for n in q["relevant"] if n in G]
    idx = V["Index"](G)
    out: dict = {}
    extra: dict = {}
    v0 = score_nodes(G, q["question"])
    base = V["score"](G, q["question"], V["BASE"], idx)
    for name, tokens in configs.items():
        cfg = cfg_of(tokens, V["BASE"])
        t0 = time.perf_counter()
        scores = v0 if name == "V0" else V["score"](G, q["question"], cfg, idx)
        ms = (time.perf_counter() - t0) * 1000
        if cfg["print_doc"]:
            V["print_docs"](G, True)
        try:
            points = [("markdown", 4000)] + (EXTRA if full else [])
            res = []
            for fmt, budget in points:
                _t, sub, stats = build_context(G, scores, budget=budget, format=fmt,
                                               query=q["question"])
                res.append([round(sum(1 for n in rel if n in sub) / len(rel), 4),
                            stats["tokens_used"]])
        finally:
            if cfg["print_doc"]:
                V["print_docs"](G, False)
        order = sorted((n for n in G.nodes if n not in idx.left_out),
                       key=lambda n: (-scores[n], n))
        pos = {n: i + 1 for i, n in enumerate(order)}
        first = min((pos[n] for n in rel if n in pos), default=None)
        out[name] = [res[0][0], res[0][1], first, round(ms, 1)]
        if full:
            extra[name] = {f"{f}:{b}": r for (f, b), r in zip(points[1:], res[1:])}
    return {"r": out, "extra": extra, "base_matches_v0": base == v0}


def run_project(args: tuple) -> str:
    project, rnd, configs, full = args
    from slurp.loader import load_graph

    V = runpy.run_path(str(HERE / "variants.py"))
    need_docs = any(cfg_of(t, V["BASE"])["doc"] or cfg_of(t, V["BASE"])["print_doc"]
                    for t in configs.values())
    dest = OUT / rnd / f"{project}.jsonl"
    dest.parent.mkdir(parents=True, exist_ok=True)
    recs = [json.loads(line) for line in
            (BENCH / "results" / f"{project}.jsonl").read_text().splitlines()]
    blobs = Blobs(BENCH / "repos" / project) if need_docs else None
    mismatches = 0
    t0 = time.time()
    with open(dest, "w") as f:
        for rec in recs:
            if not rec.get("present"):
                continue
            gz = BENCH / "graphs" / "q" / project / f"{rec['parent'][:12]}.json.gz"
            subprocess.run(["gunzip", "-kf", str(gz)], check=True)
            G = load_graph(gz.with_suffix(""))
            gz.with_suffix("").unlink()
            if need_docs:
                V["attach_docs"](G, lambda p, c=rec["parent"]: blobs.read(c, p))
            q = {"question": rec["question"], "relevant": rec["relevant"]}
            ev = _evaluate(G, q, configs, V, full)
            bank = rec["base"]["markdown"]["4000"][0]
            if "V0" in ev["r"] and ev["r"]["V0"][0] != bank:
                mismatches += 1
            if not ev["base_matches_v0"]:
                mismatches += 1
            f.write(json.dumps({"id": rec["id"], "names_symbol": rec["names_symbol"], **ev})
                    + "\n")
    if blobs:
        blobs.close()
    return f"{project}: {len(recs)} questions, {time.time() - t0:.0f}s, mismatches {mismatches}"


def run_references(rnd: str, configs: dict, full: bool) -> str:
    """The labelled sets (PrismaStats 15, slurp 16), on the graphs the budget study used."""
    from slurp.evaluator import default_questions
    from slurp.loader import load_graph

    V = runpy.run_path(str(HERE / "variants.py"))
    retrieval = runpy.run_path(str(HERE.parents[2] / "scripts" / "retrieval.py"))
    src = Path("/tmp/slurp-af2ab66")
    if not (src / "slurp").is_dir():
        src.mkdir(parents=True, exist_ok=True)
        tar = subprocess.run(["git", "-C", str(HERE.parents[2]), "archive", "af2ab66"],
                             check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", str(src)], input=tar, check=True)
    sets = {
        "prismastats": (BENCH / "graphs" / "prismastats.json", BENCH / "repos" / "prismastats",
                        [{"id": q.id, "question": q.question, "relevant": q.relevant_nodes}
                         for q in default_questions()]),
        "slurp": (BENCH / "graphs" / "ref-slurp-af2ab66.json", src,
                  [{"id": q["id"], "question": q["question"], "relevant": q["relevant_nodes"]}
                   for q in retrieval["SLURP_QUESTIONS"]]),
    }
    dest = OUT / rnd
    dest.mkdir(parents=True, exist_ok=True)
    lines = []
    for name, (graph, root, qs) in sets.items():
        G = load_graph(graph)

        def read(p: str, root: Path = root) -> str | None:
            try:
                return (root / p).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
        V["attach_docs"](G, read)
        with open(dest / f"ref-{name}.jsonl", "w") as f:
            for q in qs:
                ev = _evaluate(G, q, configs, V, full)
                f.write(json.dumps({"id": q["id"], **ev}) + "\n")
        lines.append(f"ref-{name}: {len(qs)}")
    return ", ".join(lines)


def means(rnd: str, projects: list[str]) -> dict[str, dict[str, float]]:
    """{config: {project: mean recall at 4000 markdown}}."""
    out: dict[str, dict[str, float]] = {}
    for p in projects:
        rows = [json.loads(line) for line in (OUT / rnd / f"{p}.jsonl").read_text().splitlines()]
        for name in rows[0]["r"]:
            out.setdefault(name, {})[p] = statistics.fmean(r["r"][name][0] for r in rows)
    return out


def main(argv: list[str]) -> int:
    rnd, group, cfg_file, *rest = argv
    configs = json.loads(Path(cfg_file).read_text())
    configs.setdefault("V0", [])
    configs.setdefault("base", [])
    full = "--full" in rest
    if group == "references":
        print(run_references(rnd, configs, full))
        return 0
    with mp.get_context("spawn").Pool(4) as pool:
        for line in pool.imap_unordered(run_project, [(p, rnd, configs, full)
                                                      for p in GROUPS[group]]):
            print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
