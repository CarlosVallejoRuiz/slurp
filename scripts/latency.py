"""Latency of one slurp_query call through the MCP server's real handler.

Reproduces the figures in the README's Performance section. Each run is a fresh
Python process, so caches start empty and "cold" means cold.

Usage:
    uv run python scripts/latency.py GRAPH [--method original|current|both]
                                            [--runs 3] [--src DIR]

Methods:
    original  The v0.7.0 method: ten queries run once to warm the caches, then
              timed; prints the mean and median of the ten.
    current   The v1.1.0 method: caches cleared, five queries three times
              (15 calls); cold is the first call, warm the median and maximum
              of the other fourteen.

--src measures another source tree instead of this checkout, to compare
releases on one machine, e.g.:

    git archive v0.7.0 slurp | tar -x -C /tmp/v070
    uv run python scripts/latency.py graph.json --method original --src /tmp/v070

Output: one JSON line per run, then one summary line per method with the range
(min, max) of every figure across runs.
"""

from __future__ import annotations

import argparse
import io
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ORIGINAL_QUERIES = ["auth flow", "prisma schema", "database pool", "player stats",
                    "admin permissions", "match events", "dashboard data", "chat context",
                    "league standings", "login logout"]
CURRENT_QUERIES = ["auth flow", "prisma schema", "database pool", "player stats calculation",
                   "admin permissions check"]
CURRENT_ROUNDS = 3
BUDGET = 4000

_REPO = Path(__file__).resolve().parent.parent


def run_once(graph: Path, method: str) -> dict[str, float]:
    """One measurement in this process, with the `slurp` already importable.

    Args:
        graph: Graph file to load.
        method: 'original' or 'current'.

    Returns:
        The figures of that method, in milliseconds.
    """
    from slurp import mcp
    from slurp.loader import load_graph

    graph = Path(graph)
    G = load_graph(graph)
    # v0.6.5 has no GraphState and takes the graph directly.
    state = mcp.GraphState(graph, G) if hasattr(mcp, "GraphState") else None
    for module, fn in (("slurp.scorer", "clear_pagerank_cache"),
                       ("slurp.budget", "clear_node_token_cache")):
        m = __import__(module, fromlist=[fn])
        if hasattr(m, fn):
            getattr(m, fn)()

    def call(i: int, query: str) -> float:
        buf = io.StringIO()
        msg = {"jsonrpc": "2.0", "id": i, "method": "tools/call",
               "params": {"name": "slurp_query", "arguments": {"query": query,
                                                               "budget": BUDGET}}}
        start = time.perf_counter()
        if state is not None:
            mcp._handle(msg, None, buf, session_log=False, state=state)
        else:
            mcp._handle(msg, G, buf, session_log=False)
        ms = (time.perf_counter() - start) * 1000
        response = json.loads(buf.getvalue())
        if "error" in response or response["result"].get("isError"):
            raise RuntimeError(f"slurp_query failed: {buf.getvalue()[:300]}")
        return ms

    if method == "original":
        for i, query in enumerate(ORIGINAL_QUERIES):
            call(i, query)  # warm-up pass
        times = [call(100 + i, q) for i, q in enumerate(ORIGINAL_QUERIES)]
        return {"mean_ms": round(statistics.mean(times), 2),
                "median_ms": round(statistics.median(times), 2)}
    if method == "current":
        times = [call(i, q) for i, q in enumerate(CURRENT_QUERIES * CURRENT_ROUNDS)]
        return {"cold_ms": round(times[0], 1),
                "warm_median_ms": round(statistics.median(times[1:]), 1),
                "warm_max_ms": round(max(times[1:]), 1)}
    raise ValueError(f"unknown method {method!r}")


def _one_process(src: Path, graph: Path, method: str) -> dict[str, float]:
    """Run one measurement in a fresh interpreter importing slurp from *src*."""
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(1, sys.argv[2]);"
            "import latency; print(json.dumps(latency.run_once(sys.argv[3], sys.argv[4])))")
    res = subprocess.run([sys.executable, "-I", "-c", code, str(src), str(Path(__file__).parent),
                          str(graph), method], capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise SystemExit(f"measurement failed:\n{res.stderr}")
    return json.loads(res.stdout.strip().splitlines()[-1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("graph", type=Path, help="graph.json to load")
    parser.add_argument("--method", choices=["original", "current", "both"], default="both")
    parser.add_argument("--runs", type=int, default=3, help="fresh processes per method")
    parser.add_argument("--src", type=Path, default=_REPO,
                        help="source tree holding the slurp package (default: this checkout)")
    args = parser.parse_args(argv)

    if not (args.src / "slurp" / "mcp.py").is_file():
        parser.error(f"no slurp package under {args.src}")
    methods = ["original", "current"] if args.method == "both" else [args.method]
    for method in methods:
        runs = [_one_process(args.src, args.graph, method) for _ in range(args.runs)]
        for n, figures in enumerate(runs, 1):
            print(json.dumps({"method": method, "run": n, **figures}))
        print(json.dumps({"method": method, "summary": True, "runs": len(runs),
                          **{k: [min(r[k] for r in runs), max(r[k] for r in runs)]
                             for k in runs[0]}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
