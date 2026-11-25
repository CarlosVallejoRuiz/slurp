"""Selection quality of `slurp eval --retrieval` on a fixed tuning/holdout split.

Reproduces the retrieval figures in the README. Every number comes from running
`slurp eval --retrieval` itself, in a fresh process, once per project, question
group, format and budget.

Usage:
    uv run python scripts/retrieval.py --prisma PATH --slurp PATH
        [--group tune|holdout|all] [--budgets 4000 8000]
        [--formats markdown json] [--src DIR] [--json OUT]

PATH is a graph file, used as is, or a source directory, indexed first with
`slurp index` from the code under test — so a change to the indexer is measured
on the graph it produces. The figures in the README use:

    PrismaStats   its source tree at commit 5d345dc (1,582 nodes in v1.1.1)
    slurp         git archive af2ab66 | tar -x -C /tmp/slurp-af2ab66   (4,165 nodes)

The split. Each project's questions were divided once, before the v1.2 scorer
work, into a tuning group (decisions are taken on it) and a holdout group
(looked at only to report): the ids sorted, shuffled with
random.Random(f"{SEED}:{project}"), and the first half, rounded up, is the
tuning group. SPLIT pins the result; it must never be redrawn. --group defaults
to tune so the holdout is not looked at by accident.

--src measures another source tree instead of this checkout, e.g.:

    git archive v1.1.1 slurp | tar -x -C /tmp/v111
    uv run python scripts/retrieval.py --prisma ... --slurp ... --src /tmp/v111
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

SEED = 12
SPLIT: dict[str, dict[str, list[str]]] = {
    "prismastats": {
        "tune": ["arch-01", "arch-05", "auth-01", "auth-02", "auth-04", "stats-01",
                 "stats-03", "stats-04"],
        "holdout": ["arch-02", "arch-03", "arch-04", "auth-03", "auth-05", "stats-02",
                    "stats-05"],
    },
    "slurp": {
        "tune": ["self-02", "self-04", "self-07", "self-08", "self-09", "self-12",
                 "self-13", "self-15"],
        "holdout": ["self-01", "self-03", "self-05", "self-06", "self-10", "self-11",
                    "self-14", "self-16"],
    },
}

# Labelled by the author of the v1.1.0 measurement (Claude Code) on slurp's own
# graph, one relevant node each: indicative, not an independent ground truth.
_SELF = [
    ("how does slurp pick which nodes fit in the token budget", "slurp.budget.select_subgraph"),
    ("count tokens of a string with tiktoken", "slurp.budget.count_tokens"),
    ("structural ranking of nodes with power iteration", "slurp.scorer._pagerank"),
    ("read graph json file into a networkx graph", "slurp.loader.load_graph"),
    ("append query to the audit log", "slurp.audit.log_query"),
    ("compare two graph snapshots added removed nodes", "slurp.diff.diff_graphs"),
    ("explain a node in natural language with an LLM", "slurp.explainer.explain_node"),
    ("propose follow-up queries from the frontier of the subgraph",
     "slurp.suggester.suggest_queries"),
    ("incremental reindex of files changed in git", "slurp.smart.smart_reindex"),
    ("blast radius of editing a file", "slurp.impact.analyze_impact"),
    ("resolve python imports across files into call edges",
     "slurp.indexer._resolve_cross_file_calls"),
    ("mcp server reloads the graph when the file changes on disk",
     "slurp.mcp.GraphState.check_reload"),
    ("merge several project graphs into one federated graph", "slurp.federation.merge_graphs"),
    ("extract real source code for the selected nodes", "slurp.injector.inject_code"),
    ("recommend a budget from past query history", "slurp.advisor.recommend_budget"),
    ("interactive html visualization of the subgraph", "slurp.viz.build_html"),
]
SLURP_QUESTIONS: list[dict] = [
    {"id": f"self-{i:02d}", "question": q, "ground_truth": "-", "relevant_nodes": [node]}
    for i, (q, node) in enumerate(_SELF, 1)
]

_REPO = Path(__file__).resolve().parent.parent
_CLI = "import sys; sys.path.insert(0, sys.argv.pop(1)); from slurp.cli import cli; cli()"
_BUILTIN = ("import dataclasses, json, sys; sys.path.insert(0, sys.argv[1]);"
            "from slurp.evaluator import default_questions;"
            "print(json.dumps([dataclasses.asdict(q) for q in default_questions()]))")


def draw_split(ids: list[str], project: str) -> dict[str, list[str]]:
    """The seeded draw that produced SPLIT, for checking it, never for redrawing it."""
    order = sorted(ids)
    random.Random(f"{SEED}:{project}").shuffle(order)
    half = (len(order) + 1) // 2
    return {"tune": sorted(order[:half]), "holdout": sorted(order[half:])}


def _python(src: Path, code: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", "-c", code, str(src), *args],
                          capture_output=True, text=True, check=False)


def _slurp(src: Path, *args: str) -> subprocess.CompletedProcess:
    return _python(src, _CLI, *args)


def questions_for(project: str, group: str, src: Path) -> list[dict]:
    """The questions of one group, in id order."""
    if project == "slurp":
        pool = SLURP_QUESTIONS
    else:
        res = _python(src, _BUILTIN)
        if res.returncode != 0:
            raise SystemExit(f"could not read the built-in questions:\n{res.stderr}")
        pool = json.loads(res.stdout)
    wanted = set(SPLIT[project][group])
    return sorted((q for q in pool if q["id"] in wanted), key=lambda q: q["id"])


def prepare_graph(path: Path, work: Path, name: str, src: Path) -> Path:
    """*path* itself if it is a graph file, else the graph `slurp index` makes of it."""
    if path.is_file():
        return path
    out = work / f"{name}.json"
    res = _slurp(src, "index", str(path), "--output", str(out), "--force")
    if res.returncode != 0 or not out.is_file():
        raise SystemExit(f"slurp index {path} failed:\n{res.stderr}{res.stdout}")
    return out


def measure(graph: Path, questions: list[dict], budget: int, fmt: str, src: Path,
            work: Path) -> dict:
    """One `slurp eval --retrieval` run, summarised; per-question results kept."""
    qfile = work / "questions.json"
    qfile.write_text(json.dumps({"questions": questions}), encoding="utf-8")
    out = work / "eval.json"
    out.unlink(missing_ok=True)
    res = _slurp(src, "eval", "--retrieval", "--graph", str(graph), "--questions", str(qfile),
                 "--budget", str(budget), "--format", fmt, "--output", str(out))
    if res.returncode != 0:
        if "relevant_nodes exist" in res.stderr:   # none of them is in this graph
            return {"questions_scored": 0, "recall": None, "mrr": None, "mrr_all": None,
                    "hit_rate": None, "mean_tokens": None, "tokens_per_node": None,
                    "results": []}
        raise SystemExit(f"slurp eval --retrieval failed:\n{res.stderr}")
    suite = json.loads(out.read_text(encoding="utf-8"))
    scored = [r for r in suite["results"] if r["scorable"]]
    nodes = sum(r["subgraph_nodes"] for r in scored)
    return {
        "questions_scored": suite["questions_scored"],
        "recall": suite["mean_recall"], "mrr": suite["mrr"],
        # Until v1.1.1 the eval ranked every node, so its "mrr" is this one.
        "mrr_all": suite.get("mrr_all", suite["mrr"]),
        "hit_rate": suite["hit_rate"],
        "mean_tokens": suite["mean_tokens"],
        "tokens_per_node": round(sum(r["tokens_used"] for r in scored) / nodes, 1) if nodes
        else None,
        "results": suite["results"],
    }


def _fmt(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def print_table(rows: list[dict]) -> None:
    print("| Project | Group | Format | Budget | n | Recall | MRR | MRR all nodes | Hit "
          "| Tokens | Tok/node |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['project']} | {r['group']} | {r['format']} | {r['budget']} | "
              f"{r['questions_scored']} | {_fmt(r['recall'])} | {_fmt(r['mrr'])} | "
              f"{_fmt(r['mrr_all'])} | "
              f"{_fmt(r['hit_rate'])} | {_fmt(r['mean_tokens'], 0)} | "
              f"{_fmt(r['tokens_per_node'], 1)} |")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--prisma", type=Path, required=True,
                        help="PrismaStats graph file or source directory")
    parser.add_argument("--slurp", type=Path, required=True,
                        help="slurp graph file or source directory")
    parser.add_argument("--group", choices=["tune", "holdout", "all"], default="tune")
    parser.add_argument("--budgets", type=int, nargs="+", default=[4000, 8000])
    parser.add_argument("--formats", nargs="+", choices=["markdown", "json", "yaml"],
                        default=["markdown", "json"])
    parser.add_argument("--src", type=Path, default=_REPO,
                        help="source tree holding the slurp package (default: this checkout)")
    parser.add_argument("--json", type=Path, default=None, help="write every row, with "
                        "its per-question results, to this file")
    args = parser.parse_args(argv)

    if not (args.src / "slurp" / "cli.py").is_file():
        parser.error(f"no slurp package under {args.src}")
    groups = ["tune", "holdout"] if args.group == "all" else [args.group]
    rows: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for project, path in (("prismastats", args.prisma), ("slurp", args.slurp)):
            graph = prepare_graph(path, work, project, args.src)
            graph_nodes = len(json.loads(graph.read_text(encoding="utf-8"))["nodes"])
            for group in groups:
                questions = questions_for(project, group, args.src)
                for fmt in args.formats:
                    for budget in args.budgets:
                        rows.append({"project": project, "group": group, "format": fmt,
                                     "budget": budget, "graph_nodes": graph_nodes,
                                     **measure(graph, questions, budget, fmt, args.src,
                                               work)})
    print_table(rows)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
