"""Nodes and `renders` edges of the W2 prototype against v1.5's implementation (#87).
Not part of slurp.

    ~/Desktop/Slurp/slurp/.venv/bin/python -I proto_vs_real.py PROJECT[@REV] [...]

The prototype (w2.py + jsx.py, as graph_change.py ran it) imports slurp's indexer, so it
runs here on v1.4.0's (~/slurp-bench/v15/src/v1.4.0; indexer.py is the same at 0794888,
where it was measured), in a subprocess. The implementation is the checkout's
index_project. Both on the bank's HEAD (~/slurp-bench/repos/PROJECT, clean) or, with @REV,
on a question's parent (the bank_real.py worktree checked out at REV, the bank's stored
graph of REV as v1.4's). Every difference is printed with its file:line. Writes
~/slurp-bench/v15/proto-vs-real-PROJECT[@REV].json.
"""

import gzip
import json
import runpy
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = Path.home() / "slurp-bench"
V14 = BENCH / "v15" / "src" / "v1.4.0"

_PROTO = r"""
import json, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import slurp; assert slurp.__file__.startswith(sys.argv[2]), slurp.__file__
import jsx, w2
data = json.loads(Path(sys.argv[3]).read_text())
repo = Path(sys.argv[4])
files = sorted({n["source_file"] for n in data["nodes"] if n.get("type") == "module"
                and str(n.get("source_file", "")).endswith((".tsx", ".jsx"))})
sources = {f: (repo / f).read_text(encoding="utf-8", errors="replace") for f in w2.ts_files(data)}
g2 = w2.rebuild_ts(data, sources, True)
existing = {(e["source"], e["target"]) for e in g2["links"]}
e2, _ = jsx.resolve([w2.jsx_file_w2(sources[f], f) for f in files], g2["nodes"], existing)
print(json.dumps({"nodes": g2["nodes"], "renders": [[e["source"], e["target"]] for e in e2]}))
"""


def _where(nodes: dict, nid: str) -> str:
    n = nodes.get(nid)
    return f"{n['source_file']}:{n['source_location'].lstrip('L')}" if n else "?"


def compare(target: str) -> dict:
    from slurp.indexer import index_project
    project, _, rev = target.partition("@")
    if rev:
        bank = runpy.run_path(str(HERE / "bank_real.py"), run_name="bank_real")
        repo = bank["_worktree"](project)
        bank["_checkout"](repo, rev)
        base = BENCH / "v15" / "proto-vs-real" / f"{project}@{rev[:12]}.json"
        base.parent.mkdir(exist_ok=True)
        base.write_bytes(gzip.open(BENCH / "graphs" / "q" / project / f"{rev[:12]}.json.gz").read())
    else:
        repo = BENCH / "repos" / project
        base = BENCH / "graphs" / f"{project}.json"
    dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                           capture_output=True, text=True).stdout.strip()
    assert not dirty, f"{repo} has local changes"
    try:
        out = subprocess.run([sys.executable, "-I", "-c", _PROTO, str(HERE), str(V14), str(base),
                              str(repo)], capture_output=True, text=True, check=True).stdout
        real = index_project(repo)
    finally:
        if rev:
            bank["_checkout"](repo, "HEAD")
    proto = json.loads(out)
    v14 = {n["id"] for n in json.loads(base.read_text())["nodes"]}
    pn = {n["id"]: n for n in proto["nodes"]}
    rn = {n["id"]: n for n in real["nodes"]}
    p_new, r_new = set(pn) - v14, set(rn) - v14
    p_edges = {tuple(e) for e in proto["renders"]}
    r_edges = {(e["source"], e["target"]) for e in real["links"] if e["relation"] == "renders"}
    res = {
        "project": target,
        "new_nodes": {"prototype": len(p_new), "real": len(r_new),
                      "only_prototype": sorted(p_new - r_new), "only_real": sorted(r_new - p_new)},
        "renders": {"prototype": len(p_edges), "real": len(r_edges), "both": len(p_edges & r_edges),
                    "only_prototype": sorted(p_edges - r_edges),
                    "only_real": sorted(r_edges - p_edges)},
        # nodes of either graph that are not v1.4's and not new nodes: expect none
        "other_node_changes": sorted((set(pn) ^ set(rn)) - p_new - r_new),
    }
    print(f"## {target}: new nodes {len(p_new)} -> {len(r_new)}, renders {len(p_edges)} -> "
          f"{len(r_edges)} ({len(p_edges & r_edges)} in both)")
    for i in res["new_nodes"]["only_prototype"]:
        print(f"  node only in prototype: {i}  {_where(pn, i)}")
    for i in res["new_nodes"]["only_real"]:
        print(f"  node only in v1.5:      {i}  {_where(rn, i)}")
    for u, v in res["renders"]["only_prototype"]:
        print(f"  edge only in prototype: {u} -> {v}  [{_where(pn, u)} -> {_where(pn, v)}]")
    for u, v in res["renders"]["only_real"]:
        print(f"  edge only in v1.5:      {u} -> {v}  [{_where(rn, u)} -> {_where(rn, v)}]")
    return res


def main(projects: list[str]) -> None:
    for p in projects:
        res = compare(p)
        (BENCH / "v15" / f"proto-vs-real-{p}.json").write_text(json.dumps(res, indent=1) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
