#!/usr/bin/env python3
"""Re-run the commands behind every example output in README.md and USAGE.md.

Every example output in the docs sits right under a marker that names it:

    <!-- example: ID · slurp v1.2.0 · COMMAND -->

This script runs the command(s) behind each ID with the slurp of this checkout,
on fixed reference inputs, normalises what changes from run to run (temporary
paths, timestamps, durations) and checks that every line of the documented
block still appears, in order, in the fresh output. Lines that are only an
elision ("…", "- …", "...", "… text …", "... text ...") are skipped, so a block
may show an excerpt. Anything else that differs is reported as CHANGED.

Reference inputs — fixed, so that only the tool changes between releases:

- PrismaStats at commit 5d345dc with its graphify graph,
  graphify-out/graph.json (2,111 nodes): ``--prismastats PATH`` or
  ``$SLURP_PRISMASTATS``.
- slurp's own source at tag v1.2.0 (``git archive``), indexed by the slurp
  under test; v1.1.0, twice, for the ``slurp diff`` example.

Markers with ``historical`` in their text (``slurp benchmark`` on slurp's graph
at v1.1.0, ``slurp eval`` at v0.9.5) are listed, not re-run. Nothing here calls
an LLM: an example of LLM output cannot be re-run without a key and a bill, so
the docs describe that mode instead of showing it.

Usage, from the repository root, before a release::

    uv run python scripts/readme_examples.py --prismastats ~/code/prismastats
    uv run python scripts/readme_examples.py --prismastats ... --only impact --show
    uv run python scripts/readme_examples.py --prismastats ... --write /tmp/examples

``--write`` saves each fresh output, with paths already replaced by the
placeholders the docs use, ready to paste. Exit status is 1 when an example
changed or could not run, a marker names an unknown example, or an output block
in the docs carries no marker. Not part of the test suite: it needs PrismaStats,
the git tags, a virtualenv build and a few minutes.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parent.parent
DOCS = ("README.md", "USAGE.md")
SOURCE_REF = "v1.2.0"
DIFF_REF = "v1.1.0"
PRISMASTATS_COMMIT = "5d345dc"
PRISMASTATS_NODES = 2111

MARKER = re.compile(r"^<!-- example: ([a-z0-9-]+)(?: · (.*?))? -->$")
FENCE = re.compile(r"^(`{3,})(.*)$")
ELISION = re.compile(r"^(…|- …|\.\.\.|… .* …|\.\.\. .* \.\.\.)$")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
# What an output block looks like, to catch one that carries no marker.
OUTPUT_HINTS = ("╭", "┏", "┌", "✓ ", "💡", "→ File:", "Provider:", "Nodes:",
                "Federated:", "SUGGESTED QUERIES", "RISK", "EXPLANATION",
                "slurp-graph ·", "Selected ")
# Keys that would change what slurp prints (provider detection, token factor).
SCRUBBED_ENV = ("COLUMNS", "LINES", "SLURP_TOKEN_FACTOR", "ANTHROPIC_API_KEY",
                "OPENAI_API_KEY", "SLURP_LLM_ENDPOINT", "SLURP_LLM_API_KEY")


class SkipExample(Exception):
    """An example that cannot run here (a missing tool, a busy port)."""


@dataclass
class Example:
    """One documented output and how to produce it again."""

    run: Callable[["Ctx"], str]
    mode: str = "check"  # check | custom (a check function of its own)
    check: Callable[["Ctx", list[str]], list[str]] | None = None


@dataclass
class Block:
    """A marked fenced block in one of the docs."""

    doc: str
    line: int
    example_id: str
    note: str
    lines: list[str]


class Ctx:
    """Reference inputs, built once per run, and a way to call slurp on them."""

    def __init__(self, work: Path, prismastats: Path) -> None:
        self.work = work
        self.ps_root = prismastats
        self.ps_graph = prismastats / "graphify-out" / "graph.json"
        self.slurp_bin = Path(sys.executable).parent / "slurp"
        self.placeholders: list[tuple[str, str]] = []
        self._dirs = 0
        self.add_placeholder(prismastats, "/path/to/prismastats")

    # --- plumbing ---------------------------------------------------------

    def add_placeholder(self, real: Path, placeholder: str) -> None:
        for form in {str(real), str(real.resolve())}:
            self.placeholders.append((form, placeholder))
        self.placeholders.sort(key=lambda p: -len(p[0]))

    def placeholder_paths(self, text: str) -> str:
        for real, placeholder in self.placeholders:
            text = text.replace(real, placeholder)
        return text

    def run(self, args: list[str], cwd: Path, *, stdin: str | None = None,
            merge: bool = True, binary: Path | None = None) -> str:
        env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
        # Fixed hash seed: in v1.2.0 `--suggest` breaks a tie between two equally
        # depended-on frontier nodes by set order, so its risk suggestion varies
        # between processes. Remove once that tie breaks deterministically.
        env["PYTHONHASHSEED"] = "0"
        proc = subprocess.run(
            [str(binary or self.slurp_bin), *args], cwd=cwd, input=stdin,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge else subprocess.PIPE,
            text=True, env=env, timeout=900,
        )
        return proc.stdout

    def git(self, args: list[str], cwd: Path) -> None:
        subprocess.run(["git", "-c", "user.name=readme-examples", "-c",
                        "user.email=readme-examples@localhost", *args],
                       cwd=cwd, check=True, capture_output=True)

    def archive(self, ref: str, dest: Path) -> Path:
        dest.mkdir(parents=True)
        tar = subprocess.run(["git", "archive", ref], cwd=REPO, check=True,
                             capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", str(dest)], input=tar, check=True)
        return dest

    def rundir(self, name: str, graph: Path | None = None,
               link: str = "graph.json") -> Path:
        """A fresh working directory, with *graph* linked in as *link*."""
        self._dirs += 1
        d = self.work / "run" / f"{self._dirs:02d}-{name}"
        d.mkdir(parents=True)
        if graph is not None:
            (d / link).parent.mkdir(parents=True, exist_ok=True)
            (d / link).symlink_to(graph)
        return d

    def mcp(self, graph: Path, calls: list[tuple[str, int]], cwd: Path,
            log: bool) -> list[str]:
        """Send slurp_query calls to `slurp serve` over stdio; return the texts."""
        msgs: list[dict] = [
            {"jsonrpc": "2.0", "id": 0, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "readme-examples", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ]
        for i, (query, budget) in enumerate(calls, 1):
            msgs.append({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                         "params": {"name": "slurp_query",
                                    "arguments": {"query": query, "budget": budget}}})
        stdin = "".join(json.dumps(m) + "\n" for m in msgs)
        args = ["serve", "--graph", str(graph)] + ([] if log else ["--no-log"])
        out = self.run(args, cwd, stdin=stdin, merge=False)
        texts = []
        for line in out.splitlines():
            msg = json.loads(line)
            if msg.get("id"):
                texts.append(msg["result"]["content"][0]["text"])
        return texts

    # --- reference inputs -------------------------------------------------

    @functools.cached_property
    def slurp_src(self) -> Path:
        """slurp's own source at SOURCE_REF, indexed in place."""
        src = self.archive(SOURCE_REF, self.work / "src" / "slurp")
        self.run(["index", "."], src)
        self.add_placeholder(src, "/path/to/slurp")
        return src

    @functools.cached_property
    def slurp_graph(self) -> Path:
        return self.slurp_src / "graphify-out" / "graph.json"

    @functools.cached_property
    def ps_index(self) -> str:
        out = self.work / "ps-index" / "graph.json"
        text = self.run(["index", str(self.ps_root), "-o", str(out)], self.work)
        self.add_placeholder(out, "/path/to/prismastats/graphify-out/graph.json")
        return text

    # --- sessions shared by several examples ------------------------------

    @functools.cached_property
    def session_budget(self) -> dict[str, str]:
        """Five queries at 3000 on slurp's graph, then `budget selection heap`
        at four budgets: `slurp audit` after the first five, `slurp advisor`
        at the end."""
        d = self.rundir("session-budget", self.slurp_graph)
        for q in ("budget selection", "graph loader", "mcp server",
                  "token counting", "call graph extraction"):
            self.run([q, "--graph", "graph.json", "--budget", "3000"], d)
        audit = self.run(["audit"], d)
        for b in ("1500", "2500", "4000", "6000"):
            self.run(["budget selection heap", "--graph", "graph.json", "--budget", b], d)
        advisor = self.run(["advisor", "budget selection", "--graph", "graph.json"], d)
        return {"audit": audit, "advisor": advisor}

    @functools.cached_property
    def diff_output(self) -> str:
        """slurp diff over v1.1.0 and v1.1.0 with select_subgraph renamed."""
        base = self.work / "diff"
        old = self.archive(DIFF_REF, base / "old" / "slurp")
        new = self.archive(DIFF_REF, base / "new" / "slurp")
        budget = new / "slurp" / "budget.py"
        text = budget.read_text(encoding="utf-8")
        if text.count("\ndef select_subgraph(") != 1:
            raise SkipExample(f"select_subgraph not found once in {DIFF_REF} budget.py")
        budget.write_text(text.replace("\ndef select_subgraph(", "\ndef choose_subgraph("),
                          encoding="utf-8")
        self.run(["index", str(old), "-o", str(base / "old.json")], base)
        self.run(["index", str(new), "-o", str(base / "new.json")], base)
        return self.run(["diff", "old.json", "new.json"], base)

    @functools.cached_property
    def smart(self) -> dict[str, str]:
        """--smart on a git checkout of slurp: a body-only edit, then a new def."""
        repo = self.archive(SOURCE_REF, self.work / "smart" / "slurp")
        self.add_placeholder(repo, "/path/to/slurp")
        self.git(["init", "-q"], repo)
        self.git(["add", "-A"], repo)
        self.git(["commit", "-q", "-m", "base"], repo)
        self.run(["index", "."], repo)
        budget = repo / "slurp" / "budget.py"
        lines = budget.read_text(encoding="utf-8").split("\n")
        start = next(i for i, ln in enumerate(lines) if ln.startswith("def fit_to_budget("))
        quotes = [i for i in range(start, len(lines)) if lines[i].startswith('    """')]
        lines.insert(quotes[1] + 1, "    # a comment inside the function body")
        budget.write_text("\n".join(lines), encoding="utf-8")
        body = self.run(["index", ".", "--smart"], repo)
        with budget.open("a", encoding="utf-8") as f:
            f.write("\n\ndef added_helper() -> int:\n    return 1\n")
        full = self.run(["index", ".", "--smart"], repo)
        return {"body": body, "full": full}

    @functools.cached_property
    def fallback(self) -> str:
        """slurp index from this checkout installed with no tree-sitter grammar."""
        uv = shutil.which("uv")
        if uv is None:
            raise SkipExample("uv is not on PATH")
        venv = self.work / "fallback" / "venv"
        subprocess.run([uv, "venv", "-q", "--python", "3.12", str(venv)], check=True)
        py = venv / "bin" / "python"
        subprocess.run([uv, "pip", "install", "-q", "--python", str(py), str(REPO)],
                       check=True)
        probe = subprocess.run([str(py), "-c", "import tree_sitter"], capture_output=True)
        if probe.returncode == 0:
            raise SkipExample("tree_sitter is importable in the bare virtualenv")
        out = self.work / "fallback" / "graph.json"
        self.add_placeholder(out, "/path/to/prismastats/graphify-out/graph.json")
        return self.run(["index", str(self.ps_root), "-o", str(out)], self.work,
                        binary=venv / "bin" / "slurp")

    @functools.cached_property
    def usage_benchmark(self) -> str:
        d = self.rundir("usage-benchmark", self.ps_graph, "graphify-out/graph.json")
        return self.run(["benchmark", "--graph", "graphify-out/graph.json",
                         "--queries", "auth flow", "--queries", "prisma schema",
                         "--queries", "database pool",
                         "--budget", "2000", "--budget", "4000", "--budget", "8000"], d)


# --- the examples ----------------------------------------------------------

def _q(*args: str) -> Callable[[Ctx], str]:
    """`slurp ARGS` on PrismaStats's graphify graph, linked as graph.json."""
    return lambda c: c.run(list(args), c.rundir(args[0].replace(" ", "-"), c.ps_graph))


def _explain_fallback(c: Ctx) -> str:
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", 1234)) == 0:
            raise SkipExample("something listens on localhost:1234")
    return c.run(["explain", "recalcularPlayerStats", "--graph", "graph.json",
                  "--provider", "openai-compatible", "--endpoint",
                  "http://localhost:1234/v1"], c.rundir("explain-fallback", c.ps_graph))


def _federation(c: Ctx) -> str:
    return c.run(["auth flow", "--graph", str(c.ps_graph), "--graph", str(c.slurp_graph)],
                 c.rundir("federation"))


def _config(c: Ctx) -> str:
    d = c.rundir("config")
    for key, value in (("provider", "openai-compatible"), ("model", "qwen2.5-coder"),
                       ("endpoint", "http://localhost:1234/v1"), ("api_key", "sk-example")):
        c.run(["config", "set", key, value], d)
    return c.run(["config", "show"], d)


def _advisor_auth(c: Ctx) -> str:
    d = c.rundir("advisor-auth", c.ps_graph)
    for suffix in ("", " callback", " cookie", " guard", " jwt", " login", " logout",
                   " middleware", " redirect", " session", " signup", " token"):
        c.run(["auth flow" + suffix, "--graph", "graph.json", "--budget", "3000"], d)
    return c.run(["advisor", "auth flow", "--graph", "graph.json"], d)


def _session(c: Ctx) -> str:
    d = c.rundir("session", c.slurp_graph)
    (d / ".slurp").mkdir()
    c.mcp(Path("graph.json"), [("auth flow", 4000), ("mcp server", 2000)], d, log=True)
    return c.run(["session", "--last", "2"], d)


def _usage_audit(c: Ctx) -> str:
    d = c.rundir("usage-audit", c.ps_graph, "graphify-out/graph.json")
    for q in ("auth flow", "prisma schema validation", "database pool connection"):
        c.run([q, "--budget", "4000"], d)
    return c.run(["audit"], d)


def _usage_diagram(c: Ctx) -> str:
    return c.mcp(c.ps_graph, [("auth flow", 4000)], c.rundir("usage-diagram"), log=False)[0]


def _check_diagram(c: Ctx, block: list[str]) -> list[str]:
    """The diagram is a sketch, but every figure in it must be a real one."""
    text = _usage_diagram(c)
    m = re.search(r"Selected (\d+)/(\d+) nodes · ([\d,]+)/4,000 tokens", text)
    if not m:
        return ["the slurp_query answer has no 'Selected …' header"]
    labels = re.findall(r"^### (.+?) · score", text, flags=re.M)[:4]
    row = next((ln for ln in c.usage_benchmark.splitlines()
                if "auth flow" in ln and "4,000" in ln), "")
    saving = re.search(r"(\d+\.\d)%", row)
    wanted = [f"scores all {m.group(2)} graph nodes",
              f"selects the {m.group(1)} most relevant",
              f"in {m.group(3)} tokens",
              f"({saving.group(1) if saving else '?'}% fewer tokens"] + labels
    body = "\n".join(block)
    return [f"diagram lacks {w!r}" for w in wanted if w not in body]


EXAMPLES: dict[str, Example] = {
    # README
    "quickstart": Example(_q("player stats", "--graph", "graph.json", "--budget", "4000")),
    "inject-code": Example(lambda c: c.run(
        ["recalcularPlayerStats", "--graph", "graph.json", "--budget", "2000",
         "--inject-code", "--min-score", "0.8", "--project-root", str(c.ps_root)],
        c.rundir("inject-code", c.ps_graph))),
    "federation": Example(_federation),
    "stats": Example(_q("stats", "--graph", "graph.json")),
    "audit": Example(lambda c: c.session_budget["audit"]),
    "advisor-budget": Example(lambda c: c.session_budget["advisor"]),
    "advisor-auth": Example(_advisor_auth),
    "explain-structural": Example(_q("explain", "recalcularPlayerStats", "--graph",
                                     "graph.json", "--no-llm")),
    "explain-fallback": Example(_explain_fallback),
    "config": Example(_config),
    "diff": Example(lambda c: c.diff_output),
    "diff-summary": Example(lambda c: c.diff_output),
    "impact": Example(_q("impact", "lib/supabase/admin.ts", "--graph", "graph.json")),
    "suggest": Example(_q("player stats", "--graph", "graph.json", "--budget", "4000",
                          "--suggest")),
    "session": Example(_session),
    "index": Example(lambda c: c.ps_index),
    "index-fallback": Example(lambda c: c.fallback),
    "smart-body": Example(lambda c: c.smart["body"]),
    "smart-full": Example(lambda c: c.smart["full"]),
    # USAGE
    "usage-diagram": Example(_usage_diagram, mode="custom", check=_check_diagram),
    "usage-explain": Example(lambda c: c.run(
        ["auth flow", "--explain"],
        c.rundir("usage-explain", c.ps_graph, "graphify-out/graph.json"))),
    "usage-stats": Example(lambda c: c.run(
        ["stats", "--graph", "graphify-out/graph.json"],
        c.rundir("usage-stats", c.ps_graph, "graphify-out/graph.json"))),
    "usage-audit": Example(_usage_audit),
    "usage-export": Example(lambda c: c.run(
        ["export", "auth flow", "--format", "claudemd"],
        c.rundir("usage-export", c.ps_graph, "graphify-out/graph.json"))),
    "usage-benchmark": Example(lambda c: c.usage_benchmark),
}


# --- docs ------------------------------------------------------------------

def parse_docs() -> tuple[list[Block], list[str]]:
    """Marked blocks, and output-looking blocks that carry no marker."""
    blocks: list[Block] = []
    unmarked: list[str] = []
    for doc in DOCS:
        lines = (REPO / doc).read_text(encoding="utf-8").split("\n")
        pending: tuple[int, str, str] | None = None
        i = 0
        while i < len(lines):
            stripped = lines[i].strip()
            marker = MARKER.match(stripped)
            if marker:
                pending = (i + 1, marker.group(1), marker.group(2) or "")
                i += 1
                continue
            fence = FENCE.match(lines[i])
            if fence:
                ticks, lang = fence.group(1), fence.group(2).strip()
                end = next((k for k in range(i + 1, len(lines))
                            if lines[k].rstrip() == ticks), len(lines))
                body = lines[i + 1:end]
                if pending is not None:
                    blocks.append(Block(doc, pending[0], pending[1], pending[2], body))
                elif _looks_like_output(lang, body):
                    unmarked.append(f"{doc}:{i + 1}")
                pending = None
                i = end + 1
                continue
            if stripped:
                pending = None
            i += 1
    return blocks, unmarked


def _looks_like_output(lang: str, body: list[str]) -> bool:
    if lang in ("bash", "sh", "bat", "powershell"):
        return any(re.match(r"^#\s*(→|✓)", line) for line in body)
    if lang in ("", "text", "console", "markdown"):
        return any(hint in line for line in body for hint in OUTPUT_HINTS)
    return False


def normalise(text: str) -> str:
    text = TIMESTAMP.sub("<time>", text)
    text = re.sub(r"(?<=in )\d+(?:\.\d+)?s\b", "<t>s", text)
    text = re.sub(r"~\d+(?:\.\d+)?s\b", "~<t>s", text)
    text = re.sub(r"\(\d+(?:\.\d+)?× faster\)", "(<n>× faster)", text)
    return text


def compare(block: list[str], output: str) -> list[str]:
    """Documented lines missing from the fresh output, in order."""
    fresh = [line.rstrip() for line in normalise(output).split("\n") if line.strip()]
    doc = [normalise(line).rstrip() for line in block]
    missing, pos = [], 0
    for line in doc:
        if not line.strip() or ELISION.match(line.strip()):
            continue
        try:
            pos = fresh.index(line, pos) + 1
        except ValueError:
            missing.append(line)
    return missing


def _closest(line: str, fresh: list[str]) -> str | None:
    import difflib
    match = difflib.get_close_matches(line, fresh, n=1, cutoff=0.5)
    return match[0] if match else None


# --- main ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--prismastats", type=Path,
                        default=os.environ.get("SLURP_PRISMASTATS"),
                        help="PrismaStats checkout at 5d345dc (or $SLURP_PRISMASTATS)")
    parser.add_argument("--only", action="append", default=[], metavar="ID",
                        help="run only these examples (repeatable)")
    parser.add_argument("--show", action="store_true", help="print each fresh output")
    parser.add_argument("--write", type=Path, metavar="DIR",
                        help="save each fresh output to DIR/ID.txt")
    parser.add_argument("--keep", action="store_true", help="keep the working directory")
    args = parser.parse_args()

    if args.prismastats is None:
        parser.error("pass --prismastats PATH or set $SLURP_PRISMASTATS")
    ps = Path(args.prismastats).expanduser().resolve()
    graph = ps / "graphify-out" / "graph.json"
    if not graph.is_file():
        parser.error(f"{graph} does not exist")
    nodes = len(json.loads(graph.read_text(encoding="utf-8"))["nodes"])
    if nodes != PRISMASTATS_NODES:
        print(f"warning: PrismaStats graph has {nodes} nodes, the docs use "
              f"{PRISMASTATS_NODES}", file=sys.stderr)
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ps,
                          capture_output=True, text=True).stdout.strip()
    if head and not head.startswith(PRISMASTATS_COMMIT):
        print(f"warning: PrismaStats is at {head}, the docs use {PRISMASTATS_COMMIT}",
              file=sys.stderr)

    blocks, unmarked = parse_docs()
    unknown = sorted({b.example_id for b in blocks} - set(EXAMPLES))
    ids = args.only or list(EXAMPLES)
    work = Path(tempfile.mkdtemp(prefix="slurp-readme-examples-"))
    ctx = Ctx(work, ps)
    if args.write:
        args.write.mkdir(parents=True, exist_ok=True)

    failures = 0
    try:
        for example_id in ids:
            example = EXAMPLES[example_id]
            mine = [b for b in blocks if b.example_id == example_id]
            try:
                output = ctx.placeholder_paths(example.run(ctx))
            except SkipExample as exc:
                print(f"SKIPPED   {example_id}: {exc}")
                failures += 1
                continue
            if args.write:
                (args.write / f"{example_id}.txt").write_text(output, encoding="utf-8")
            if args.show:
                print(f"----- {example_id}\n{output}")
            if not mine:
                print(f"NO BLOCK  {example_id}: no marker in {', '.join(DOCS)}")
                continue
            for block in mine:
                where = f"{block.doc}:{block.line}"
                if "historical" in block.note:
                    print(f"HISTORIC  {example_id} ({where}): not re-run")
                    continue
                if example.mode == "custom" and example.check is not None:
                    problems = example.check(ctx, block.lines)
                else:
                    problems = compare(block.lines, output)
                if not problems:
                    print(f"OK        {example_id} ({where})")
                    continue
                failures += 1
                print(f"CHANGED   {example_id} ({where})")
                fresh = [ln.rstrip() for ln in normalise(output).split("\n") if ln.strip()]
                for line in problems[:8]:
                    print(f"    docs : {line}")
                    near = _closest(line, fresh) if example.mode != "custom" else None
                    if near is not None:
                        print(f"    now  : {near}")
                if len(problems) > 8:
                    print(f"    … and {len(problems) - 8} more lines")
        for block in blocks:
            if "historical" in block.note and block.example_id not in EXAMPLES:
                print(f"HISTORIC  {block.example_id} ({block.doc}:{block.line}): not re-run")
        for example_id in unknown:
            if not any("historical" in b.note for b in blocks if b.example_id == example_id):
                print(f"UNKNOWN   marker '{example_id}' names no example in this script")
                failures += 1
        for where in unmarked:
            print(f"UNMARKED  {where}: looks like command output but has no marker")
            failures += 1
    finally:
        if args.keep:
            print(f"working directory kept: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
