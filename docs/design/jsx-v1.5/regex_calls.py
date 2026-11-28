"""The regex branch's `calls` edges against tree-sitter's, on one project. Not part of slurp.

    python -I regex_calls.py SRC PROJECT [--regex]

SRC holds a `slurp` package (the code to measure). Without --regex, prints the
tree-sitter edges of the project's TS/JS files as JSON; with it, the regex branch's
(tree-sitter switched off). compare() classifies the regex edges against tree-sitter's.
"""

import json
import sys
from pathlib import Path


def edges(src: str, project: str, regex: bool) -> dict:
    sys.path.insert(0, src)
    from slurp import indexer as ix
    assert ix.__file__.startswith(src), ix.__file__
    if regex:
        ix._TREE_SITTER_AVAILABLE = False
    root = Path.home() / "slurp-bench" / "repos" / project
    graph = ix.index_project(root)
    ts_ext = (".ts", ".tsx", ".js", ".jsx")
    in_ts = {n["id"] for n in graph["nodes"] if str(n.get("source_file", "")).endswith(ts_ext)}
    calls = sorted([e["source"], e["target"]] for e in graph["links"]
                   if e["relation"] == "calls" and e["source"] in in_ts)
    return {"calls": calls, "nodes": sorted(in_ts)}


def compare(ts: dict, rx: dict) -> dict:
    t = {tuple(e) for e in ts["calls"]}
    r = {tuple(e) for e in rx["calls"]}
    ts_nodes = set(ts["nodes"])
    same = t & r
    coarse, wrong, foreign = [], [], []
    for u, v in sorted(r - t):
        if u not in ts_nodes or v not in ts_nodes:
            foreign.append((u, v))
        elif any(tu.startswith(u + ".") and tv == v for tu, tv in t):
            coarse.append((u, v))
        else:
            wrong.append((u, v))
    return {"tree_sitter": len(t), "regex": len(r), "same": len(same),
            "coarse": len(coarse), "misattributed": len(wrong), "endpoint_not_in_ts": len(foreign),
            "tree_sitter_only": len(t - r), "misattributed_sample": wrong[:12],
            "endpoint_sample": foreign[:6]}


if __name__ == "__main__":
    print(json.dumps(edges(sys.argv[1], sys.argv[2], "--regex" in sys.argv)))
