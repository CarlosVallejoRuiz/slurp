"""How much the JSX edges of the prototype change a project's graph. Not part of slurp.

    ~/Desktop/Slurp/slurp/.venv/bin/python -I graph_change.py PROJECT [PROJECT...]

On the bank's HEAD graph (~/slurp-bench/graphs/PROJECT.json) and the source at that
HEAD (~/slurp-bench/repos/PROJECT, clean working tree): new edges by case, agreement
between the tree-sitter and the regex branch, and the risk band (slurp explain's
_risk_level) of every node in a .tsx/.jsx file, imports and tests left out, before and
after. Writes ~/slurp-bench/v15/graph-change-PROJECT.json.
"""

import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import jsx  # noqa: E402

from slurp._graphutils import _is_import, _is_test, _split_callers  # noqa: E402
from slurp.explainer import _risk_level  # noqa: E402
from slurp.loader import build_graph  # noqa: E402

BENCH = Path.home() / "slurp-bench"


def measure(project: str) -> dict:
    data = json.loads((BENCH / "graphs" / f"{project}.json").read_text())
    repo = BENCH / "repos" / project
    dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                           capture_output=True, text=True).stdout.strip()
    assert not dirty, f"{repo} has local changes"
    files = sorted({n["source_file"] for n in data["nodes"]
                    if n.get("type") == "module"
                    and str(n.get("source_file", "")).endswith((".tsx", ".jsx"))})
    existing = {(e["source"], e["target"]) for e in data["links"]}
    out: dict = {"project": project, "files": len(files)}
    edges_by_branch = {}
    for branch, force in (("tree-sitter", False), ("regex", True)):
        t0 = time.perf_counter()
        per_file = [jsx.jsx_file((repo / f).read_text(encoding="utf-8", errors="replace"), f,
                                 force_regex=force) for f in files]
        ms = (time.perf_counter() - t0) * 1000
        edges, counts = jsx.resolve(per_file, data["nodes"], existing)
        edges_by_branch[branch] = edges
        why = Counter(w for f in per_file for _, w in f.unresolved)
        branches = Counter(f.branch for f in per_file)
        out[branch] = {"edges": len(edges), "by_case": counts, "elements": sum(f.elements for f in per_file),
                       "unresolved_by_reason": dict(why), "files_by_branch": dict(branches),
                       "extract_ms": round(ms)}
    ts = {(e["source"], e["target"]) for e in edges_by_branch["tree-sitter"]}
    rx = {(e["source"], e["target"]) for e in edges_by_branch["regex"]}
    out["agreement"] = {"both": len(ts & rx), "tree_sitter_only": len(ts - rx),
                        "regex_only": len(rx - ts),
                        "regex_only_sample": sorted(rx - ts)[:15],
                        "tree_sitter_only_sample": sorted(ts - rx)[:8]}

    before = build_graph(data)
    after = build_graph({**data, "links": data["links"] + edges_by_branch["tree-sitter"]})
    nodes = [n for n in before.nodes
             if str(before.nodes[n].get("source_file", "")).endswith((".tsx", ".jsx"))
             and not _is_import(before, n) and not _is_test(before, n)]
    bands_before = Counter(_risk_level(before, n) for n in nodes)
    bands_after = Counter(_risk_level(after, n) for n in nodes)
    moves = Counter((_risk_level(before, n), _risk_level(after, n)) for n in nodes
                    if _risk_level(before, n) != _risk_level(after, n))
    top = sorted(((len(_split_callers(after, n)[0]), len(_split_callers(before, n)[0]), n)
                  for n in nodes), reverse=True)[:12]
    out["bands"] = {"nodes": len(nodes), "before": dict(bands_before), "after": dict(bands_after),
                    "moves": {f"{a}->{b}": c for (a, b), c in moves.items()},
                    "top_after": [[n, b, a] for a, b, n in top]}
    out["graph"] = {"nodes": before.number_of_nodes(), "edges_before": before.number_of_edges(),
                    "edges_after": after.number_of_edges(),
                    "calls_before": sum(1 for *_, r in before.edges(data="relation") if r == "calls")}
    return out


if __name__ == "__main__":
    (BENCH / "v15").mkdir(exist_ok=True)
    for p in sys.argv[1:]:
        res = measure(p)
        (BENCH / "v15" / f"graph-change-{p}.json").write_text(json.dumps(res, indent=1) + "\n")
        ts, rx, ag, b = res["tree-sitter"], res["regex"], res["agreement"], res["bands"]
        print(f"{p}: {res['files']} files, {ts['elements']} component elements; tree-sitter "
              f"{ts['edges']} edges {ts['by_case']}; regex {rx['edges']}; agreement {ag['both']} "
              f"both, {ag['tree_sitter_only']} ts-only, {ag['regex_only']} regex-only")
        print(f"   bands over {b['nodes']} nodes: before {b['before']} after {b['after']} "
              f"moves {b['moves']}")
        print(f"   unresolved (ts): {ts['unresolved_by_reason']} | files {ts['files_by_branch']}")
