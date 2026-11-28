"""Classify the regex branch's TS `calls` edges against tree-sitter's. Not part of slurp.

    python -I regex_classify.py SRC PROJECT

SRC holds the `slurp` package whose regex branch is measured; tree-sitter edges come from
the same SRC. Regex nodes are matched to tree-sitter's by (file, label, line), because
the regex branch names nested and class-field functions differently. Prints JSON.
"""

import json
import sys
from collections import Counter
from pathlib import Path


def main(src: str, project: str) -> dict:
    sys.path.insert(0, src)
    from slurp import indexer as ix
    assert ix.__file__.startswith(src), ix.__file__
    root = Path.home() / "slurp-bench" / "repos" / project
    ts = ix.index_project(root)
    ix._TREE_SITTER_AVAILABLE = False
    rx = ix.index_project(root)
    ext = (".ts", ".tsx", ".js", ".jsx")
    tsn = {n["id"] for n in ts["nodes"]}
    where = {(n.get("source_file"), n.get("label"), n.get("source_location")): n["id"]
             for n in ts["nodes"]}
    tcalls = {(e["source"], e["target"]) for e in ts["links"] if e["relation"] == "calls"
              and str(next((n["source_file"] for n in ts["nodes"] if n["id"] == e["source"]), "")).endswith(ext)}
    tpairs = {(e["source"], e["target"]): e["relation"] for e in ts["links"]}
    rxn = {n["id"]: n for n in rx["nodes"]}

    def to_ts(nid: str) -> str | None:
        if nid in tsn:
            return nid
        n = rxn.get(nid)
        return where.get((n.get("source_file"), n.get("label"), n.get("source_location"))) if n else None

    cls: Counter = Counter()
    wrong = []
    regex_total = 0
    for e in rx["links"]:
        if e["relation"] != "calls" or not str(rxn.get(e["source"], {}).get("source_file", "")).endswith(ext):
            continue
        regex_total += 1
        if (e["source"], e["target"]) in tcalls:
            cls["same edge"] += 1
            continue
        u, v = to_ts(e["source"]), to_ts(e["target"])
        if u is None or v is None:
            cls["endpoint tree-sitter does not emit"] += 1
        elif (u, v) in tcalls:
            cls["same edge, other node name"] += 1
        elif tpairs.get((u, v)) == "contains" or v.startswith(u + "."):
            cls["calls its own nested function"] += 1
        elif any(tu.startswith(u + ".") and tv == v for tu, tv in tcalls):
            cls["coarser caller"] += 1
        else:
            cls["MISATTRIBUTED"] += 1
            wrong.append([e["source"], e["target"], u, v])
    return {"project": project, "tree_sitter_calls": len(tcalls), "regex_calls": regex_total,
            "classes": dict(cls), "misattributed": wrong}


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1], sys.argv[2])))
