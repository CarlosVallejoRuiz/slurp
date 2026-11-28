"""Risk bands before and after v1.5's real implementation (#87). Not part of slurp.

    ~/Desktop/Slurp/slurp/.venv/bin/python -I bands_real.py PROJECT [PROJECT...]

Before: the bank's v1.4 HEAD graph (~/slurp-bench/graphs/PROJECT.json). After: the same
HEAD (~/slurp-bench/repos/PROJECT, clean) indexed with the checkout's index_project. The
band is slurp explain's _risk_level, over every node of a .tsx/.jsx file, imports and
tests left out, as graph_change.py measured the prototype (its W2 figures are printed
beside, from ~/slurp-bench/v15/graph-change-PROJECT.json).
Writes ~/slurp-bench/v15/bands-real-PROJECT.json.
"""

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

from slurp._graphutils import _is_import, _is_test, _split_callers
from slurp.explainer import _risk_level
from slurp.indexer import index_project
from slurp.loader import build_graph

BENCH = Path.home() / "slurp-bench"


def _jsx_nodes(G) -> list[str]:
    return [n for n in G.nodes
            if str(G.nodes[n].get("source_file", "")).endswith((".tsx", ".jsx"))
            and not _is_import(G, n) and not _is_test(G, n)]


def measure(project: str) -> dict:
    repo = BENCH / "repos" / project
    dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                           capture_output=True, text=True).stdout.strip()
    assert not dirty, f"{repo} has local changes"
    before = build_graph(json.loads((BENCH / "graphs" / f"{project}.json").read_text()))
    data = index_project(repo)
    after = build_graph(data)
    nb, na = _jsx_nodes(before), _jsx_nodes(after)
    new = [n for n in na if n not in before]
    moves = Counter((_risk_level(before, n), _risk_level(after, n)) for n in na
                    if n in before and _risk_level(before, n) != _risk_level(after, n))
    top = sorted(((len(_split_callers(after, n)[0]), n) for n in na), reverse=True)[:12]
    proto = json.loads((BENCH / "v15" / f"graph-change-{project}.json").read_text())["w2"]
    return {"project": project,
            "renders": sum(1 for e in data["links"] if e["relation"] == "renders"),
            "new_nodes": len(data["nodes"]) - before.number_of_nodes(),
            "nodes_before": len(nb), "nodes_after": len(na),
            "bands_before": dict(Counter(_risk_level(before, n) for n in nb)),
            "bands_after": dict(Counter(_risk_level(after, n) for n in na)),
            "moves_existing": {f"{a}->{b}": c for (a, b), c in moves.items()},
            "bands_new_nodes": dict(Counter(_risk_level(after, n) for n in new)),
            "top_after": [[n, d] for d, n in top],
            "prototype_w2": {k: proto[k] for k in ("new_nodes", "jsx_edges", "bands_after",
                                                   "moves_existing", "bands_new_nodes",
                                                   "top_after")}}


def main(projects: list[str]) -> None:
    for p in projects:
        res = measure(p)
        (BENCH / "v15" / f"bands-real-{p}.json").write_text(json.dumps(res, indent=1) + "\n")
        print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
