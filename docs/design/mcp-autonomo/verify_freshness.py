"""Phase verification for #76: the graph follows the working tree. Not part of slurp.

Usage, from the repository (the slurp under test is this checkout's):

    uv run python docs/design/mcp-autonomo/verify_freshness.py COPY [COPY...]

Each COPY is a throwaway project (git init + one commit) — the script edits,
creates, deletes and renames its files. Every graph is written to
COPY/.bench-out/graph.json. For each copy it:

1. builds the graph with `slurp index`;
2. times the per-call check (ManifestChecker.check, and a whole
   Refresher.prepare with nothing to do) in process;
3. drives a real `slurp serve` over pipes through six kinds of change —
   body edit, new symbol, new file, deleted file, renamed file, .slurpignore —
   five times each, and after every call compares the graph on disk with a full
   index of the tree as it is (nodes and edges, public attributes); then does
   the same through the CLI (`slurp QUERY`);
4. runs `slurp index` by hand, repeatedly, while the server refreshes, with a
   reader parsing graph.json in a loop: no read may see a half-written file.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

from slurp.graphfile import write_graph
from slurp.manifest import Manifest, ManifestChecker
from slurp.refresh import GraphTracker, Refresher, index_with_manifest

SLURP = str(Path(sys.executable).parent / "slurp")
REPS = 5


def public(graph: dict) -> tuple:
    nodes = {n["id"]: {k: v for k, v in n.items() if not k.startswith("_")}
             for n in graph["nodes"]}
    edges = sorted(json.dumps({k: v for k, v in e.items() if not k.startswith("_")},
                              sort_keys=True) for e in graph["links"])
    return nodes, edges


def full_index(root: Path) -> tuple:
    scratch = root / ".bench-out" / "full.json"
    return public(index_with_manifest(root, scratch, root / ".slurpignore"))


def stats(values: list[float]) -> str:
    return (f"median {statistics.median(values):7.1f} ms   worst {max(values):7.1f} ms   "
            f"(n={len(values)})")


class Server:
    """`slurp serve` over pipes; call() returns (answer text, _meta state, wall ms)."""

    def __init__(self, graph: Path, cwd: Path) -> None:
        self.proc = subprocess.Popen(
            [SLURP, "serve", "--graph", str(graph), "--no-log"], cwd=cwd, text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.next_id = 0
        self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                "clientInfo": {"name": "verify", "version": "1"}})

    def rpc(self, method: str, params: dict) -> dict:
        self.next_id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id,
                                          "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        reply = json.loads(line)           # every stdout line must be JSON-RPC
        assert reply["id"] == self.next_id, reply
        return reply

    def call(self, query: str) -> tuple[str, dict, float]:
        start = time.perf_counter()
        reply = self.rpc("tools/call", {"name": "slurp_query",
                                        "arguments": {"query": query, "budget": 2000}})
        wall = (time.perf_counter() - start) * 1000
        result = reply["result"]
        return (result["content"][0]["text"], result["_meta"]["dev.imjuank.slurp/graph"], wall)

    def close(self) -> str:
        out, _ = self.proc.communicate(timeout=60)
        return out


def pick_file(root: Path) -> Path:
    """A Python or TypeScript file of moderate size to edit."""
    graph = json.loads((root / ".bench-out" / "graph.json").read_text(encoding="utf-8"))
    files = sorted(graph["slurp"]["files"])
    for suffix in (".py", ".ts"):
        for rel in files:
            if rel.endswith(suffix) and "test" not in rel and 2_000 < (root / rel).stat().st_size < 20_000:
                return root / rel
    return root / files[0]


def scenarios(root: Path, target: Path, i: int) -> dict:
    """Six kinds of change; each returns (description, a word the answer should show)."""
    ts = target.suffix != ".py"
    original = target.read_text(encoding="utf-8")

    def body():
        marker = f"\n// edit {i}\n" if ts else f"\n# edit {i}\n"
        target.write_text(original + marker, encoding="utf-8")
        return None

    def symbol():
        name = f"verifyAddedSymbol{i}"
        extra = (f"\nexport function {name}(): number {{ return {i}; }}\n" if ts
                 else f"\n\ndef {name}():\n    return {i}\n")
        target.write_text(target.read_text(encoding="utf-8") + extra, encoding="utf-8")
        return name

    def new_file():
        name = f"verifyNewModule{i}"
        (root / "verify_tmp").mkdir(exist_ok=True)
        (root / "verify_tmp" / f"{name}.py").write_text(f"def {name}Fn():\n    return {i}\n",
                                                        encoding="utf-8")
        return f"{name}Fn"

    def delete():
        (root / "verify_tmp" / f"verifyNewModule{i}.py").unlink()
        return None

    def rename():
        src = root / "verify_tmp" / f"verifyRenamed{i}.py"
        src.write_text(f"def verifyRenamedFn{i}():\n    return {i}\n", encoding="utf-8")
        return src

    def slurpignore():
        rules = root / ".slurpignore"
        text = rules.read_text(encoding="utf-8") if rules.exists() else ""
        rules.write_text(text + f"id:verify_never_{i}*\n", encoding="utf-8")
        return None

    return {"body edit": body, "new symbol": symbol, "new file": new_file,
            "deleted file": delete, "renamed file": rename, ".slurpignore": slurpignore}


def run(root: Path) -> None:
    print(f"\n=== {root}")
    graph = root / ".bench-out" / "graph.json"
    graph.parent.mkdir(exist_ok=True)
    subprocess.run([SLURP, "index", str(root), "-o", str(graph), "--ignore-file",
                    str(root / ".slurpignore")], check=True, capture_output=True)
    data = json.loads(graph.read_text(encoding="utf-8"))
    m = data["slurp"]
    print(f"  nodes {len(data['nodes'])}, links {len(data['links'])}, graph.json "
          f"{graph.stat().st_size / 1e6:.2f} MB, manifest {len(m['files'])} files + "
          f"{len(m['dirs'])} dirs ({len(json.dumps(m, indent=2)) / 1e3:.0f} KB)")
    # Age the tree out of the racy window, as a project at rest is.
    past = time.time() - 120
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirpath or ".bench-out" in dirpath:
            continue
        for name in filenames:
            os.utime(Path(dirpath) / name, (past, past), follow_symlinks=False)
    write_graph(graph, index_with_manifest(root, graph, root / ".slurpignore"))

    # -- per-call check, in process ----------------------------------------
    # Steady state, as between an agent's calls: entries touched just before
    # the build are verified once and trusted 2 s after their mtime.
    checker = ManifestChecker(Manifest.from_dict(
        json.loads(graph.read_text(encoding="utf-8"))["slurp"], graph))
    refresher = Refresher([GraphTracker(graph)])
    checker.check()
    refresher.prepare()
    time.sleep(2.5)
    checker.check()
    refresher.prepare()
    times = []
    for _ in range(200):
        t = time.perf_counter()
        assert not checker.check().any
        times.append((time.perf_counter() - t) * 1000)
    print(f"  check per call (ManifestChecker.check)        {stats(times)}")
    times = []
    for _ in range(200):
        t = time.perf_counter()
        assert refresher.prepare().status == "fresh"
        times.append((time.perf_counter() - t) * 1000)
    print(f"  check per call (Refresher.prepare, nothing)   {stats(times)}")

    # -- changes through the server and the CLI ----------------------------
    target = pick_file(root)
    print(f"  edited file: {target.relative_to(root)}")
    server = Server(graph, root)
    per_kind: dict[str, dict[str, list[float]]] = {}
    mismatches = 0
    for i in range(REPS):
        steps = scenarios(root, target, i)
        renamed_src: Path | None = None
        for kind, step in steps.items():
            outcome = step()
            if kind == "renamed file":
                renamed_src = outcome
                server.call("verify renamed")              # the file exists first…
                renamed_src.rename(renamed_src.with_name(f"verifyRenamedB{i}.py"))  # …then moves
                outcome = None
            text, meta, wall = server.call(outcome or "verify")
            assert meta["status"] == "refreshed", (kind, meta)
            row = per_kind.setdefault(kind, {"refresh": [], "wall": []})
            row["refresh"].append(meta.get("refresh_ms", 0.0))
            row["wall"].append(wall)
            if outcome and outcome not in text:
                print(f"  ! {kind}: answer lacks {outcome}")
                mismatches += 1
            on_disk = public(json.loads(graph.read_text(encoding="utf-8")))
            if on_disk != full_index(root):
                print(f"  ! {kind} #{i}: server graph differs from a full index")
                mismatches += 1
    fresh_wall = [server.call("verify")[2] for _ in range(20)]
    print(server.close() and "  ! stdout carried more than JSON-RPC" or
          "  stdout: JSON-RPC lines only")
    print("  server, per kind of change (refresh_ms from _meta; wall = whole call):")
    for kind, row in per_kind.items():
        print(f"    {kind:<14} refresh {stats(row['refresh'])}")
        print(f"    {'':<14} wall    {stats(row['wall'])}")
    print(f"    {'no change':<14} wall    {stats(fresh_wall)}")

    cli_mismatch = 0
    for i in range(REPS, REPS + 2):
        for kind, step in scenarios(root, target, i).items():
            outcome = step()
            if kind == "renamed file":
                subprocess.run([SLURP, "x", "--graph", str(graph), "--no-audit"],
                               cwd=root, capture_output=True)
                outcome.rename(outcome.with_name(f"verifyRenamedB{i}.py"))
                outcome = None
            res = subprocess.run([SLURP, outcome or "verify", "--graph", str(graph),
                                  "--no-audit", "--budget", "2000"],
                                 cwd=root, capture_output=True, text=True)
            if outcome and outcome not in res.stdout:
                cli_mismatch += 1
            if public(json.loads(graph.read_text(encoding="utf-8"))) != full_index(root):
                print(f"  ! CLI {kind} #{i}: graph differs from a full index")
                cli_mismatch += 1
    print(f"  equality with a full index: server {6 * REPS} changes, {mismatches} mismatches; "
          f"CLI {6 * 2} changes, {cli_mismatch} mismatches")

    # -- slurp index by hand while the server refreshes --------------------
    server = Server(graph, root)
    stop = threading.Event()
    reads = {"ok": 0, "bad": 0}

    def reader():
        while not stop.is_set():
            try:
                json.loads(graph.read_text(encoding="utf-8"))
                reads["ok"] += 1
            except (ValueError, OSError):
                reads["bad"] += 1

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    waits = 0
    for i in range(10):
        extra = f"\n\ndef verifyRace{i}():\n    return {i}\n"
        race_file = root / "verify_tmp" / "race.py"
        race_file.write_text((race_file.read_text(encoding="utf-8") if race_file.exists()
                              else "") + extra, encoding="utf-8")
        manual = subprocess.Popen([SLURP, "index", str(root), "-o", str(graph), "--ignore-file",
                                   str(root / ".slurpignore")], cwd=root, text=True,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        server.call(f"verifyRace{i}")
        _, err = manual.communicate(timeout=120)
        waits += "waiting for another slurp process" in err
    stop.set()
    thread.join()
    server.close()
    final_ok = public(json.loads(graph.read_text(encoding="utf-8"))) == full_index(root)
    print(f"  concurrent slurp index x10: {reads['ok']} reads parsed, {reads['bad']} half-written; "
          f"{waits} waited for the lock; final graph equals a full index: {final_ok}")

    shutil.rmtree(root / "verify_tmp", ignore_errors=True)


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        run(Path(arg).resolve())
