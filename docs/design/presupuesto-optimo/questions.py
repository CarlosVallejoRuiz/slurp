"""Retrieval questions from a repository's git history, with no hand labels. Not part of slurp.

Design: docs/design/presupuesto-optimo.md §2. For a commit C with parent P:

    question       = C's subject line, cleaned
    relevant_nodes = the definitions (functions, methods, classes...) whose lines C
                     changes, as nodes of the graph `slurp index` builds on P

Run with the slurp checkout's interpreter (it imports slurp):

    PY=~/Desktop/Slurp/slurp/.venv/bin/python
    $PY -I questions.py count REPO...            # stage A only: how many commits pass
    $PY -I questions.py sample REPO N [SEED]     # stage A + B until N questions pass

REPO names a clone in ~/slurp-bench/repos/ ($SLURP_BENCH overrides the root).
Frozen for the study: a change here means re-running the gate (§2.5).
"""

from __future__ import annotations

import ast
import json
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import networkx as nx

from slurp._graphutils import _is_test
from slurp.indexer import (_INDEXED_EXTENSIONS, _go_body_span, _should_skip, _TSLanguage,
                           _TSParser, _tsjava, _tsrust, _tsts)
from slurp.injector import _brace_block, _python_block
from slurp.loader import load_graph
from slurp.scorer import _tokenize

BENCH = Path(os.environ.get("SLURP_BENCH", Path.home() / "slurp-bench"))
SLURP = Path(sys.executable).parent / "slurp"     # the checkout's slurp, beside its python
SEED = 12
SINCE = "2023-11-26"          # history window: the last three years
MIN_WINDOW_COMMITS = 400      # the whole history below this

# --- §2.2 filters ---------------------------------------------------------------
MAX_SOURCE_FILES = 5          # non-test source files touched (any status)
MAX_RENAMES = 2               # renamed or copied source files
MAX_CHURN = 300               # added + deleted lines in non-test source
MAX_FILES = 20                # files of any kind
MAX_RELEVANT = 8              # definitions changed
MIN_CONTENT_WORDS = 3

SKIP_TYPES = {"docs", "doc", "chore", "ci", "build", "style", "test", "tests", "release",
              "revert", "deps", "bump", "lint", "format", "typo", "wip"}
KNOWN_TYPES = {"feat", "fix", "perf", "refactor", "feature"}
SKIP_WORDS = re.compile(
    r"(?i)\b(bump(ed|s)?|release[sd]?|changelog|readme|docs?|documentation|typos?|spelling|"
    r"lint(ing|er)?|format(ting|ted)?|prettier|black|ruff|gofmt|rustfmt|clippy|whitespace|"
    r"license|copyright|dependabot|renovate|deps|dependenc(y|ies)|revert(s|ed)?|merge[sd]?|"
    r"backport|cherry[- ]pick|tests?|testing|mypy|pyright|flake8|pylint|eslint|tslint|"
    r"coverage|benchmarks?)\b")
BOT = re.compile(r"(?i)(\[bot\]|dependabot|renovate|github-actions|pre-commit-ci|mergify)")
VAGUE = {"fix", "fixes", "fixed", "fixing", "update", "updates", "updated", "minor", "cleanup",
         "clean", "up", "small", "some", "stuff", "wip", "typo", "tweak", "tweaks", "misc",
         "change", "changes", "changed", "improve", "improves", "improved", "improvement",
         "improvements", "refactor", "refactored", "refactoring", "code", "bug", "bugs",
         "issue", "issues", "the", "a", "an", "to", "of", "in", "for", "and", "on", "with",
         "more", "few", "various", "it", "this", "that", "is", "be", "by", "from", "as", "at"}
# Build and tooling configuration: never the code a question is about.
TOOLING = {"setup.py", "noxfile.py", "tasks.py", "fabfile.py", "conftest.py", "build.rs",
           "gruntfile.js", "gulpfile.js"}
TOOLING_PREFIX = ("webpack.config.", "rollup.config.", "vite.config.", "vitest.config.",
                  "jest.config.", "eslint.config.", "babel.config.", "tsup.config.")
DEFINITION_TYPES = {"function", "method", "class", "interface", "struct", "enum", "trait",
                    "type", "constructor", "impl", "record", "object", "protocol"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True, errors="replace").stdout


def is_source(path: str) -> bool:
    """A file `slurp index` reads, outside skipped directories and build configuration."""
    p = Path(path)
    name = p.name.lower()
    if name in TOOLING or name.startswith(TOOLING_PREFIX):
        return False
    return p.suffix.lower() in _INDEXED_EXTENSIONS and not _should_skip(p)


def is_test_file(path: str) -> bool:
    g = nx.DiGraph()
    g.add_node("n", source_file=path)
    return _is_test(g, "n")


def clean_subject(subject: str) -> tuple[str | None, str]:
    """(question, "") or (None, the reason it was rejected)."""
    s = subject.strip()
    m = re.match(r"^\s*(\w+)(\(([^)]*)\))?!?:\s*(.+)$", s)
    if m and m.group(1).lower() in SKIP_TYPES:
        return None, f"type:{m.group(1).lower()}"
    if m and len(m.group(1)) <= 10 and m.group(1).isalpha():
        scope = (m.group(3) or "").strip()
        rest = m.group(4)
        # `area: text` (ripgrep, tokio, Go) keeps the area; `feat: text` drops the type.
        if scope:
            s = f"{scope}: {rest}"
        elif m.group(1).lower() in KNOWN_TYPES:
            s = rest
    s = re.sub(r"^\s*(\[[^\]]{1,20}\]\s*)+", "", s)                 # [3.x] [WIP] ...
    s = re.sub(r"\(?\b(fix(es|ed)?|close[sd]?|resolve[sd]?|refs?|see)\s+#\d+\)?", "", s,
               flags=re.I)
    s = re.sub(r"\(#\d+\)|#\d+|gh-\d+|GH-\d+", "", s)                # issue / PR refs
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"\s+", " ", s).strip(" .,:;-")
    if not s:
        return None, "empty"
    if SKIP_WORDS.search(s):
        return None, "keyword"
    words = [w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", s)]
    if len([w for w in words if w not in VAGUE]) < MIN_CONTENT_WORDS:
        return None, "vague"
    return s, ""


@dataclass
class Commit:
    sha: str
    parent: str
    author: str
    subject: str
    files: dict[str, str] = field(default_factory=dict)     # path -> status (A/M/D/Rxx)
    churn: dict[str, int] = field(default_factory=dict)     # path -> added + deleted
    old_path: dict[str, str] = field(default_factory=dict)  # new -> old, for renames


def log_commits(repo: Path, since: str | None) -> list[Commit]:
    rng = [f"--since={since}"] if since else []
    fmt = "--format=%x1e%H%x1f%P%x1f%an %ae%x1f%s"
    commits: dict[str, Commit] = {}
    order: list[str] = []
    for block in git(repo, "log", "--no-merges", "-M", "--name-status", fmt, *rng).split("\x1e"):
        if not block.strip():
            continue
        head, *lines = block.strip("\n").split("\n")
        sha, parents, author, subject = head.split("\x1f")
        c = Commit(sha, parents.split()[0] if parents else "", author, subject)
        for ln in lines:
            parts = ln.split("\t")
            if len(parts) == 2:
                c.files[parts[1]] = parts[0]
            elif len(parts) == 3:                      # R100 old new
                c.files[parts[2]] = parts[0]
                c.old_path[parts[2]] = parts[1]
        commits[sha] = c
        order.append(sha)
    for block in git(repo, "log", "--no-merges", "--no-renames", "--numstat", "--format=%x1e%H",
                     *rng).split("\x1e"):
        if not block.strip():
            continue
        head, *lines = block.strip("\n").split("\n")
        c = commits.get(head.strip())
        for ln in lines:
            parts = ln.split("\t")
            if c is not None and len(parts) == 3:
                a, d = (int(x) if x.isdigit() else 0 for x in parts[:2])
                c.churn[parts[2]] = c.churn.get(parts[2], 0) + a + d
    return [commits[s] for s in order]


def _source_files(c: Commit) -> dict[str, str]:
    return {p: s for p, s in c.files.items() if is_source(p) and not is_test_file(p)}


def stage_a(c: Commit) -> tuple[str | None, str]:
    """(question, "") or (None, reason), from the message and the file list alone."""
    if not c.parent:
        return None, "root commit"
    if BOT.search(c.author):
        return None, "bot"
    if len(c.files) > MAX_FILES:
        return None, "too many files"
    src = _source_files(c)
    if not any(s == "M" or s.startswith("R") for s in src.values()):
        return None, "no modified source file"
    if len(src) > MAX_SOURCE_FILES:
        return None, "too many source files"
    if sum(1 for s in src.values() if s[0] in "RC") > MAX_RENAMES:
        return None, "mass rename"
    if sum(c.churn.get(p, 0) for p in src) > MAX_CHURN:
        return None, "too many changed lines"
    return clean_subject(c.subject)


# --- §2.3 from the diff to the definitions --------------------------------------

def parse_diff(diff: str) -> dict[str, set]:
    """Old-side lines each file's change touches, from `git diff -U0`.

    A changed or deleted line is an int; an insertion between old lines a and
    a+1 is the pair (a, a+1): it counts only for a definition holding both.
    New files have no old side and are left out.
    """
    lines: dict[str, set] = {}
    current = None
    for ln in diff.splitlines():
        if ln.startswith("--- "):
            current = ln[6:] if ln.startswith("--- a/") else None
        elif ln.startswith("@@") and current:
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", ln)
            start, count = int(m.group(1)), int(m.group(2) or "1")
            touched = range(start, start + count) if count else [(start, start + 1)]
            lines.setdefault(current, set()).update(touched)
    return {p: s for p, s in lines.items() if s}


def changed_old_lines(repo: Path, c: Commit, paths: list[str]) -> dict[str, set]:
    """parse_diff of the commit's change to *paths*, whitespace ignored."""
    return parse_diff(git(repo, "diff", "-U0", "-w", "-M", "--no-color", c.parent, c.sha, "--",
                          *paths, *[c.old_path[p] for p in paths if p in c.old_path]))


def _ts_tree(path: str, text: str):
    ext = Path(path).suffix.lower()
    if ext in (".ts", ".js", ".mjs", ".cjs") and _tsts:
        lang = _tsts.language_typescript()
    elif ext in (".tsx", ".jsx") and _tsts:
        lang = _tsts.language_tsx()
    elif ext == ".java" and _tsjava:
        lang = _tsjava.language()
    elif ext == ".rs" and _tsrust:
        lang = _tsrust.language()
    else:
        return None
    return _TSParser(_TSLanguage(lang)).parse(text.encode("utf-8", "replace"))


def parser_ends(path: str, lines: list[str]) -> dict[int, int]:
    """1-based start line -> last line of the outermost construct starting there.

    Python from `ast`; TS/JS/Java/Rust from tree-sitter (an erroneous tree gives
    nothing, as slurp's _tree_is_broken); Go from the parameter list, then the
    body (`_go_body_span`). Lines with no entry fall back to the block extractors.
    """
    ends: dict[int, int] = {}
    ext = Path(path).suffix.lower()
    src = "\n".join(lines)
    if ext == ".py":
        try:
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    ends[node.lineno] = node.end_lineno or node.lineno
        except SyntaxError:
            pass
        return ends
    if ext == ".go":
        offsets = [0]
        for ln in lines:
            offsets.append(offsets[-1] + len(ln) + 1)
        for i, ln in enumerate(lines, 1):
            m = re.match(r"\s*func\s*(\([^)]*\)\s*)?\w+\s*(\[[^\]]*\])?\(", ln)
            if m:
                span = _go_body_span(src, offsets[i - 1] + m.end())
                if span:
                    ends[i] = src.count("\n", 0, span[1]) + 1
        return ends
    tree = _ts_tree(path, src)
    if tree is None or tree.root_node.has_error:
        return ends
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.parent is None:
            continue
        start, end = node.start_point[0] + 1, node.end_point[0] + 1
        line = lines[start - 1] if start - 1 < len(lines) else ""
        # Only a construct that opens its line: `export const f = (...) => {`.
        if node.start_point[1] == len(line) - len(line.lstrip()):
            ends[start] = max(ends.get(start, start), end)
    return ends


def spans(G: nx.DiGraph, path: str, lines: list[str]) -> list[tuple[int, int, str]]:
    """(first line, last line, node id) of every definition of *path* in *G*."""
    defs = []
    for nid, d in G.nodes(data=True):
        if d.get("source_file") != path or str(d.get("type")) not in DEFINITION_TYPES:
            continue
        m = re.match(r"L(\d+)", str(d.get("source_location", "")))
        if m:
            defs.append((int(m.group(1)), nid))
    defs.sort()
    ext = Path(path).suffix.lower()
    ends = parser_ends(path, lines)
    out = []
    for i, (start, nid) in enumerate(defs):
        if start in ends:
            end = ends[start]
        else:
            block, _ = (_python_block if ext == ".py" else _brace_block)(lines, start, 100_000)
            end = start + (block.count("\n") if block else 0)
        # Never past the next definition that is not nested in this one.
        later = [s for s, other in defs[i + 1:] if not other.startswith(nid + ".")]
        if later:
            end = min(end, later[0] - 1)
        out.append((start, max(start, end), nid))
    return out


def relevant_from(G: nx.DiGraph, touched: dict[str, set],
                  read_lines: Callable[[str], list[str]]) -> list[str]:
    """The innermost definition holding each touched line, tests left out, in order."""
    relevant: list[str] = []
    for path, items in touched.items():
        sp = spans(G, path, read_lines(path))
        for item in sorted(items, key=lambda x: x if isinstance(x, int) else x[0]):
            a, b = (item, item) if isinstance(item, int) else item
            holders = [(s, e, n) for s, e, n in sp if s <= a and b <= e]
            if holders:
                inner = max(holders, key=lambda h: (h[0], -h[1]))[2]
                if inner not in relevant and not _is_test(G, inner):
                    relevant.append(inner)
    return relevant


def _strip_docstrings(tree):
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    return tree


def code_signature(path: str, text: str) -> str | None:
    """The file's code with comments and docstrings gone, or None when unknown."""
    ext = Path(path).suffix.lower()
    if ext == ".py":
        try:
            return ast.dump(_strip_docstrings(ast.parse(text)))
        except SyntaxError:
            return None
    if ext == ".go":
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        text = re.sub(r"//[^\n]*", " ", text)
        return " ".join(text.split())
    tree = _ts_tree(path, text)
    if tree is None or tree.root_node.has_error:
        return None
    leaves, stack = [], [tree.root_node]
    while stack:
        node = stack.pop()
        if "comment" in node.type:
            continue
        if node.child_count == 0:
            leaves.append(node.text.decode("utf-8", "replace"))
        stack.extend(reversed(node.children))
    return " ".join(leaves)


def comment_only(repo: Path, c: Commit, paths: list[str]) -> bool:
    for p in paths:
        old = git(repo, "show", f"{c.parent}:{c.old_path.get(p, p)}")
        new = git(repo, "show", f"{c.sha}:{p}")
        a, b = code_signature(p, old), code_signature(p, new)
        if a is None or b is None or a != b:
            return False
    return True


def index_parent(work: Path, parent: str, graph_dir: Path) -> tuple[Path, float]:
    """The graph of *parent*, built once and kept gzipped beside an unpacked copy."""
    gpath = graph_dir / f"{parent[:12]}.json"
    gz = gpath.with_suffix(".json.gz")
    t0 = time.perf_counter()
    if gz.exists() and not gpath.exists():
        subprocess.run(["gunzip", "-kf", str(gz)], check=True)
    if not gpath.exists():
        git(work, "checkout", "-q", "--detach", "-f", parent)
        git(work, "clean", "-qfdx")
        gpath.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([str(SLURP), "index", str(work), "-o", str(gpath), "--force"],
                       capture_output=True, check=True)
    return gpath, time.perf_counter() - t0


def stage_b(repo: Path, c: Commit, work: Path, graph_dir: Path) -> tuple[list[str], dict, str]:
    """(relevant node ids, graph info, "") or ([], info, reason)."""
    src = [p for p, s in _source_files(c).items() if s == "M" or s.startswith("R")]
    touched = changed_old_lines(repo, c, src)
    if not touched:
        return [], {}, "whitespace-only"
    if comment_only(repo, c, src):
        return [], {}, "comments or docstrings only"
    gpath, index_s = index_parent(work, c.parent, graph_dir)
    G = load_graph(gpath)

    def read_lines(path: str) -> list[str]:
        return git(repo, "show", f"{c.parent}:{path}").splitlines()

    relevant = relevant_from(G, touched, read_lines)
    info = {"graph": str(gpath), "graph_nodes": G.number_of_nodes(),
            "index_s": round(index_s, 2)}
    if not relevant:
        return [], info, "no definition changed"
    if len(relevant) > MAX_RELEVANT:
        return [], info, "too many definitions"
    return relevant, info, ""


def names_a_symbol(question: str, labels: list[str]) -> bool:
    """Whether the question names one of the changed definitions."""
    q = set(_tokenize(question))
    return any(lbl and (lbl.lower() in question.lower() or set(_tokenize(lbl)) <= q)
               for lbl in labels)


def seeded_order(items: list, project: str, seed: int = SEED) -> list:
    out = list(items)
    random.Random(f"{seed}:{project}").shuffle(out)
    return out


def window(repo: Path) -> list[Commit]:
    commits = log_commits(repo, SINCE)
    return commits if len(commits) >= MIN_WINDOW_COMMITS else log_commits(repo, None)


def candidates(repo: Path) -> tuple[list[tuple[Commit, str]], dict[str, int]]:
    """Commits passing stage A, subject deduplicated, newest first; and why the rest fell."""
    out, seen, reasons = [], set(), {}
    for c in window(repo):
        q, why = stage_a(c)
        if q and q.lower() in seen:
            q, why = None, "duplicate subject"
        reasons[why or "pass"] = reasons.get(why or "pass", 0) + 1
        if q:
            seen.add(q.lower())
            out.append((c, q))
    return out, reasons


def generate(project: str, n: int, seed: int = SEED, bench: Path = BENCH):
    """Yield (question dict, stage B rejection counts) until *n* questions pass."""
    repo = bench / "repos" / project
    work = bench / "work" / project
    if not work.exists():
        work.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "add", "-q", "--detach", str(work), "HEAD")
    cands, _ = candidates(repo)
    rejected: dict[str, int] = {}
    accepted = 0
    for c, q in seeded_order(cands, project, seed):
        rel, info, why = stage_b(repo, c, work, bench / "graphs" / "q" / project)
        if why:
            rejected[why] = rejected.get(why, 0) + 1
            continue
        G = load_graph(Path(info["graph"]))
        labels = [str(G.nodes[r].get("label", "")).rstrip("()") for r in rel]
        accepted += 1
        yield ({"id": f"{project}-{c.sha[:8]}", "question": q, "relevant_nodes": rel,
                "ground_truth": "-", "commit": c.sha, "parent": c.parent, "subject": c.subject,
                "names_symbol": names_a_symbol(q, labels), **info}, dict(rejected))
        if accepted >= n:
            return


if __name__ == "__main__":
    cmd, *rest = sys.argv[1:]
    if cmd == "count":
        for name in rest:
            cands, reasons = candidates(BENCH / "repos" / name)
            print(json.dumps({"name": name, "pass_a": len(cands), "reasons": reasons}))
    elif cmd == "sample":
        name, n, *s = rest
        qs, rej = [], {}
        for q, rej in generate(name, int(n), int(s[0]) if s else SEED):
            qs.append(q)
        dest = BENCH / "questions" / f"{name}-sample.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps({"questions": qs}, indent=2, ensure_ascii=False))
        print(json.dumps({"name": name, "accepted": len(qs), "rejected": rej}), file=sys.stderr)
        print(dest)
