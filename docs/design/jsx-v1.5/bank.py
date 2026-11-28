"""The question bank with and without JSX edges (#87). Not part of slurp.

    ~/Desktop/Slurp/slurp/.venv/bin/python -I bank.py [--workers 2]

Variants, scored and selected with slurp v1.4's code (score_nodes, build_context at
4000 in markdown):
- W0: the bank's stored parent graph;
- W1: W0 plus the prototype's `renders` edges (jsx.py), walked by the personalised
  PageRank like `calls` (scorer._walk_lists patched in this process);
- W2: the parent's TS/JS files re-indexed with React.memo/forwardRef components as
  nodes (w2.py), plus the `renders` edges on that graph.

Only PrismaStats, hono, excalidraw and prometheus have .tsx/.jsx files; the other ten
projects cannot change (checked by check_unaffected()). Each question is one JSON line
in ~/slurp-bench/v15/bank/<project>.jsonl, appended and fsynced: a rerun resumes. The
first 3 questions of each project also check that re-indexing without the W2 rule gives
back the stored graph exactly (field "rebuild_identical").
"""

from __future__ import annotations

import argparse
import gzip
import json
import multiprocessing as mp
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = Path.home() / "slurp-bench"
OUT = BENCH / "v15" / "bank"
AFFECTED = ["prometheus", "excalidraw", "hono", "prismastats"]
OTHERS = ["sqlalchemy", "commons-lang", "tokio", "caddy", "jsoup", "ripgrep", "click", "gin",
          "ky", "slurp"]


def _patch_walk() -> None:
    """`renders` walked like `calls` (forward 1, backward _PPR_BACK), as proposed."""
    from slurp import scorer

    def walk_lists(G):
        nb = {n: {} for n in G.nodes}
        for u, v, d in G.edges(data=True):
            rel = d.get("relation")
            if rel in ("calls", "renders"):
                nb[u][v] = nb[u].get(v, 0.0) + 1.0
                if scorer._PPR_BACK > 0:
                    nb[v][u] = nb[v].get(u, 0.0) + scorer._PPR_BACK
            elif rel == "contains":
                nb[u][v] = nb[u].get(v, 0.0) + 1.0
                nb[v][u] = nb[v].get(u, 0.0) + 1.0
        out = {}
        for n, ws in nb.items():
            if ws:
                total = sum(ws.values())
                out[n] = [(m, w / total) for m, w in ws.items()]
        return out
    scorer._walk_lists = walk_lists


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


def _variants(graph: dict, sources: dict[str, str], check_rebuild: bool) -> tuple[dict, dict]:
    import jsx
    import w2

    from slurp.loader import build_graph

    jsx_files = [f for f in sources if f.endswith((".tsx", ".jsx"))]
    existing = {(e["source"], e["target"]) for e in graph["links"]}
    f1 = [jsx.jsx_file(sources[f], f) for f in jsx_files]
    e1, c1 = jsx.resolve(f1, graph["nodes"], existing)
    g2 = w2.rebuild_ts(graph, sources, True)
    existing2 = {(e["source"], e["target"]) for e in g2["links"]}
    f2 = [w2.jsx_file_w2(sources[f], f) for f in jsx_files]
    e2, c2 = jsx.resolve(f2, g2["nodes"], existing2)
    info = {"jsx_w1": len(e1), "jsx_w2": len(e2),
            "nodes_w2": len(g2["nodes"]) - len(graph["nodes"]), "cases_w1": c1}
    if check_rebuild:
        g0 = w2.rebuild_ts(graph, sources, False)
        key = lambda xs: sorted(json.dumps(x, sort_keys=True) for x in xs)  # noqa: E731
        info["rebuild_identical"] = key(g0["nodes"]) == key(graph["nodes"]) and \
            key(g0["links"]) == key(graph["links"])
    graphs = {"W0": build_graph(graph),
              "W1": build_graph({**graph, "links": graph["links"] + e1}),
              "W2": build_graph({**g2, "links": g2["links"] + e2})}
    return graphs, info


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
            stats["tokens_used"], first]


def _done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    data = path.read_bytes()
    good = data[: data.rfind(b"\n") + 1]
    if len(good) != len(data):
        path.write_bytes(good)
    return {json.loads(line)["id"] for line in good.decode().splitlines()}


def run_project(project: str) -> str:
    sys.path.insert(0, str(HERE))
    _patch_walk()
    dest = OUT / f"{project}.jsonl"
    done = _done(dest)
    blobs = Blobs(BENCH / "repos" / project)
    import w2
    t0, n = time.time(), 0
    with dest.open("a") as fh:
        for i, line in enumerate((BENCH / "results" / f"{project}.jsonl").read_text().splitlines()):
            rec = json.loads(line)
            if not rec.get("present") or rec["id"] in done:
                continue
            gz = BENCH / "graphs" / "q" / project / f"{rec['parent'][:12]}.json.gz"
            graph = json.loads(gzip.open(gz).read())
            sources = {}
            for f in w2.ts_files(graph):
                text = blobs.read(rec["parent"], f)
                if text is not None:
                    sources[f] = text
            graphs, info = _variants(graph, sources, check_rebuild=i < 3)
            res = {v: _evaluate(G, rec["question"], rec["relevant"]) for v, G in graphs.items()}
            out = {"id": rec["id"], "names_symbol": rec["names_symbol"],
                   "bank_w0": rec["base"]["markdown"]["4000"][0], **info, "r": res}
            fh.write(json.dumps(out) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
            n += 1
    blobs.close()
    return f"{project}: +{n} questions, {time.time() - t0:.0f} s"


def run_labelled() -> str:
    """PrismaStats's 15 labelled questions on its HEAD graph (slurp's has no .tsx)."""
    sys.path.insert(0, str(HERE))
    _patch_walk()
    import w2

    from slurp.evaluator import default_questions
    dest = OUT / "ref-prismastats.jsonl"
    if dest.exists():
        return "ref-prismastats: done"
    graph = json.loads((BENCH / "graphs" / "prismastats.json").read_text())
    repo = BENCH / "repos" / "prismastats"
    sources = {f: (repo / f).read_text(encoding="utf-8", errors="replace")
               for f in w2.ts_files(graph)}
    graphs, info = _variants(graph, sources, check_rebuild=True)
    lines = []
    for q in default_questions():
        res = {v: _evaluate(G, q.question, q.relevant_nodes) for v, G in graphs.items()}
        lines.append(json.dumps({"id": q.id, **info, "r": res}))
    dest.write_text("\n".join(lines) + "\n")
    return f"ref-prismastats: {len(lines)} questions"


def check_unaffected() -> dict[str, int]:
    """Parent graphs of the other ten projects holding any .tsx/.jsx node (expect 0)."""
    found = {}
    for p in OTHERS:
        count = 0
        for gz in sorted((BENCH / "graphs" / "q" / p).glob("*.json.gz")):
            data = gzip.open(gz).read()
            if b'.tsx"' in data or b'.jsx"' in data:
                count += 1
        found[p] = count
    return found


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    print("parent graphs with .tsx/.jsx in the other projects:", check_unaffected(), flush=True)
    with mp.get_context("spawn").Pool(a.workers, maxtasksperchild=1) as pool:
        jobs = [pool.apply_async(run_project, (p,)) for p in AFFECTED]
        jobs.append(pool.apply_async(run_labelled))
        for j in jobs:
            print(time.strftime("%H:%M:%S"), j.get(), flush=True)


if __name__ == "__main__":
    main()
