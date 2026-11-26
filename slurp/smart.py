"""Incremental re-indexing — the engine behind `slurp index --smart`.

A full index re-parses every source file in the project. Most edits touch a
handful of files, so this module finds what changed since the graph was built,
re-parses only those files and rewrites their slice of the graph.

Invariant: the result is the graph a full index would build. Cross-file call
resolution reads symbols from every file, so an edit that adds, removes or
renames a file or a symbol can change how *other* files resolve. Those edits
fall back to a full index; edits that keep every symbol of the edited files
intact — the common case, a change inside function bodies — stay incremental.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

from slurp.graphfile import graph_identity, write_graph
from slurp.ignore import SlurpIgnore
from slurp.indexer import (
    _INDEXED_EXTENSIONS,
    _assemble_graph,
    _should_skip,
    iter_source_files,
)
from slurp.manifest import MANIFEST_KEY, build_manifest

_GIT_TIMEOUT_S = 30

# Node attributes that may differ while the symbol itself is unchanged. An edit
# inside a function moves every later definition to a new line.
_POSITIONAL_ATTRS = frozenset({"source_location"})


@dataclass
class SmartIndexResult:
    """Outcome of one incremental re-index.

    Attributes:
        changed_files: Files that differ from the graph: edited since it was
            built (per git or mtime), added, or deleted.
        expanded_files: The files actually re-parsed — the changed files that
            still exist. Kept under this name for the CLI report.
        updated_nodes: Nodes written into the graph for the re-parsed files.
        removed_nodes: Nodes dropped because they belonged to a re-parsed file.
        elapsed_ms: Wall-clock duration of the whole incremental pass.
        full_index_ms_estimate: Extrapolated cost of a full index, from the
            measured per-file parse cost times the project's file count.
        updated_edges: Edges written for the re-parsed files.
        is_git_repo: False when *root* is not inside a git work tree, in which
            case nothing was re-indexed and the caller must fall back.
        graph_found: False when no existing graph was present to update, which
            likewise forces a full index.
        fallback_reason: Non-empty when the change could alter cross-file
            resolution, or the graph records no usable build commit: nothing
            was written and the caller must run a full index.
        dry_run: True when the graph was left untouched on disk.
    """

    changed_files: list[Path] = field(default_factory=list)
    expanded_files: list[Path] = field(default_factory=list)
    updated_nodes: int = 0
    removed_nodes: int = 0
    elapsed_ms: float = 0.0
    full_index_ms_estimate: float = 0.0
    updated_edges: int = 0
    is_git_repo: bool = True
    graph_found: bool = True
    fallback_reason: str = ""
    dry_run: bool = False

    @property
    def needs_full_index(self) -> bool:
        """True when the incremental path could not or must not run."""
        return not self.is_git_repo or not self.graph_found or bool(self.fallback_reason)

    @property
    def dependents_added(self) -> int:
        """Always 0: re-parsing is limited to the changed files themselves.

        Dependents can only resolve differently when a symbol changes, and that
        case runs a full index instead.
        """
        return 0

    @property
    def speedup(self) -> float:
        """Estimated ratio of a full index to this run. 0.0 when unmeasurable."""
        if self.elapsed_ms <= 0 or self.full_index_ms_estimate <= 0:
            return 0.0
        return self.full_index_ms_estimate / self.elapsed_ms


# ---------------------------------------------------------------------------
# git interrogation
# ---------------------------------------------------------------------------

def _git(root: Path, *args: str) -> str | None:
    """Run a git command in *root*, returning stdout or None if it failed.

    Args:
        root: Directory to run git in.
        *args: Arguments after `git`.

    Returns:
        Captured stdout, or None when git is missing, times out, or exits
        non-zero (e.g. `diff HEAD` in a repo with no commits yet).
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def is_git_repo(root: Path) -> bool:
    """Return True if *root* is inside a git work tree."""
    out = _git(root, "rev-parse", "--is-inside-work-tree")
    return out is not None and out.strip() == "true"


def head_commit(root: Path) -> str | None:
    """The commit HEAD points at, or None outside a repo or before a first commit.

    `slurp index` stores it as the graph's `built_at_commit`, so --smart can
    later ask git what changed since the graph was built rather than since the
    latest commit.
    """
    out = _git(root, "rev-parse", "--verify", "--quiet", "HEAD")
    return out.strip() if out else None


def _commit_exists(root: Path, commit: str) -> bool:
    return _git(root, "cat-file", "-e", f"{commit}^{{commit}}") is not None


def get_changed_files(root: Path, base: str = "HEAD") -> list[Path]:
    """List indexable source files that differ from *base*.

    Combines the working tree and the index compared with *base*, plus
    untracked files. Untracked files are included because a newly created file
    is the most common thing to want indexed, and `git diff` never reports one.

    Args:
        root: Project root, inside a git work tree.
        base: Commit to compare against. --smart passes the graph's
            built_at_commit, so edits committed since the build still count.

    Returns:
        Absolute paths, sorted and deduplicated, restricted to extensions slurp
        can index. Deleted files are included so their nodes can be dropped.
    """
    seen: set[Path] = set()
    for args in (
        ("diff", "--name-only", "--relative", base),
        ("diff", "--name-only", "--relative", "--cached", base),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        out = _git(root, *args)
        if out is None:
            continue
        for line in out.splitlines():
            rel = line.strip()
            if not rel:
                continue
            path = (root / rel).resolve()
            # Skip rules apply to the root-relative path, not the folders above it.
            if path.suffix.lower() in _INDEXED_EXTENSIONS and not _should_skip(Path(rel), root):
                seen.add(path)
    return sorted(seen)


# ---------------------------------------------------------------------------
# incremental re-index
# ---------------------------------------------------------------------------

def _reindex_files(
    files: list[Path],
    root: Path,
) -> tuple[list[dict], list[dict], float]:
    """Parse *files* and return their nodes, edges, and total parse time in ms."""
    nodes: list[dict] = []
    edges: list[dict] = []
    start = perf_counter()
    for path in files:
        indexer = _INDEXED_EXTENSIONS.get(path.suffix.lower())
        if indexer is None:
            continue
        try:
            file_nodes, file_edges = indexer(path, root)  # type: ignore[operator]
        except Exception as exc:
            # An unparseable file is skipped rather than fatal, exactly as in
            # index_project() — and, as there, it is reported. A silent skip
            # hands back a quietly incomplete graph, which is worse than a
            # slow one: nothing downstream can tell the file was dropped.
            print(f"  Warning: skipping {path}: {exc}", file=sys.stderr)
            continue
        nodes.extend(file_nodes)
        edges.extend(file_edges)
    return nodes, edges, (perf_counter() - start) * 1000


def _files_with_nodes(
    keys: list[str],
    on_disk: dict[str, Path],
    root: Path,
    ignore: SlurpIgnore | None,
) -> list[str]:
    """The files among *keys* that a full index would give at least one node.

    DECISION (#83): a file on disk that no node of the graph comes from is new
    only if it would have nodes. One that a `.slurpignore` rule excludes whole,
    that is empty or that does not parse has none in a full index either, and
    counting it as added made every --smart run a full index. The graph does
    not record the files its build saw (the v1.3 manifest will), so this asks
    the parser again: a `file:` rule settles it without parsing; any other file
    is parsed, which costs only the files the graph holds no node for.

    Args:
        keys: Root-relative paths of files on disk with no node in the graph.
        on_disk: Every indexable file, by root-relative path.
        root: Project root.
        ignore: The rules the graph was built with, if any.

    Returns:
        The keys, in order, of the files with at least one node to give.
    """
    found: list[str] = []
    for key in keys:
        if ignore is not None and ignore.excludes_file(key):
            continue
        nodes, _, _ = _reindex_files([on_disk[key]], root)
        if any(ignore is None or not ignore.should_ignore(n, n["id"]) for n in nodes):
            found.append(key)
    return found


def _symbols(nodes: list[dict]) -> list[str]:
    """Every node, minus positional and private attributes, comparably.

    Private keys are resolution metadata a fresh parse carries and the written
    graph does not; they only steer the resolution of their own file, which
    the incremental path redoes anyway.
    """
    return sorted(
        json.dumps({k: v for k, v in n.items()
                    if k not in _POSITIONAL_ATTRS and not k.startswith("_")},
                   sort_keys=True, default=str)
        for n in nodes
    )


def smart_reindex(
    root: Path,
    graph_path: Path,
    ignore: SlurpIgnore | None = None,
    dry_run: bool = False,
    ignore_path: Path | None = None,
) -> SmartIndexResult:
    """Update an existing graph in place, re-parsing only what changed.

    Args:
        root: Project root to index.
        graph_path: Path of the graph to read and rewrite.
        ignore: Optional SlurpIgnore rules applied to newly produced nodes.
        dry_run: Compute everything but leave the graph on disk untouched.
        ignore_path: The file *ignore* was read from, recorded in the graph's
            manifest. Default: <root>/.slurpignore.

    Returns:
        A SmartIndexResult. Check `needs_full_index` first: when there is no git
        repo, no existing graph, or a change that could alter cross-file
        resolution, nothing was written and the caller should run a full index.
    """
    start = perf_counter()

    if not is_git_repo(root):
        return SmartIndexResult(is_git_repo=False, dry_run=dry_run)

    if not graph_path.is_file():
        return SmartIndexResult(graph_found=False, dry_run=dry_run)
    try:
        identity = graph_identity(graph_path)
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        graph_mtime = graph_path.stat().st_mtime_ns
    except (OSError, ValueError):
        return SmartIndexResult(graph_found=False, dry_run=dry_run)
    ignore_path = root / ".slurpignore" if ignore_path is None else ignore_path
    # The manifest the rewritten graph will carry: taken before git is asked
    # and before anything is parsed, so an edit made meanwhile shows up as a
    # stale entry at the next check rather than vanishing (see manifest.file_entry).
    snapshot = build_manifest(root, ignore_path)

    # DECISION: changes are measured against the commit the graph was built
    # from, not against HEAD. Comparing with HEAD made a commit between the
    # build and the re-index invisible: --smart answered "up to date" over a
    # stale graph. A graph without a recorded commit (written before v1.0.7)
    # gives no base to compare with, so it is rebuilt once.
    base = graph.get("built_at_commit")
    if not base:
        return SmartIndexResult(
            dry_run=dry_run,
            fallback_reason="the graph records no build commit (written before v1.0.7)")
    if not _commit_exists(root, base):
        return SmartIndexResult(
            dry_run=dry_run,
            fallback_reason=f"its build commit {str(base)[:12]} is not in this repository")

    root_resolved = root.resolve()

    def _rel_key(path: Path) -> str:
        try:
            return path.resolve().relative_to(root_resolved).as_posix()
        except ValueError:
            return path.as_posix()

    on_disk = {_rel_key(p): p for p in iter_source_files(root)}
    old_nodes = graph.get("nodes") or []
    in_graph = {n.get("source_file") for n in old_nodes if n.get("source_file")}

    # Git sees committed, staged and unstaged edits relative to the build
    # commit. mtime catches what git cannot: a file that was dirty at build
    # time and has since been reverted, or an indexed file git ignores.
    changed = {_rel_key(p) for p in get_changed_files(root, base)}
    changed |= {key for key, path in on_disk.items()
                if os.stat(path).st_mtime_ns >= graph_mtime}
    absent = sorted(set(on_disk) - in_graph)
    added = _files_with_nodes(absent, on_disk, root, ignore)
    deleted = sorted(in_graph - set(on_disk))
    changed |= set(added) | set(deleted)
    # A file with no node in the graph and none to give leaves the graph as it
    # is, however often it is edited.
    changed -= set(absent) - set(added)
    changed &= set(on_disk) | in_graph
    changed_paths = sorted(root / key for key in changed)

    if not changed:
        return SmartIndexResult(
            elapsed_ms=(perf_counter() - start) * 1000, dry_run=dry_run,
        )

    if added or deleted:
        return SmartIndexResult(
            changed_files=changed_paths, dry_run=dry_run,
            fallback_reason=f"{len(added)} added and {len(deleted)} removed or renamed "
                            "files change what other files can call")

    modified = sorted(changed)
    new_nodes, new_edges, parse_ms = _reindex_files([on_disk[k] for k in modified], root)
    # A single parse can emit one id twice (two imports of the same module);
    # the assembled graph keeps the first, so compare against that.
    seen: set[str] = set()
    new_nodes = [n for n in new_nodes if not (n["id"] in seen or seen.add(n["id"]))]
    if ignore is not None:
        new_nodes = [n for n in new_nodes if not ignore.should_ignore(n, n["id"])]

    touched = set(modified)
    previous = [n for n in old_nodes if n.get("source_file") in touched]
    if _symbols(previous) != _symbols(new_nodes):
        return SmartIndexResult(
            changed_files=changed_paths, dry_run=dry_run,
            fallback_reason="definitions or imports changed, which can change how "
                            "other files resolve their calls")

    # Same symbols, so every symbol index is unchanged and no other file can
    # resolve differently: keep their nodes and edges, replace the edited files'
    # slice, and let the shared assembly resolve the new pending calls exactly
    # as a full index would.
    removed_ids = {n["id"] for n in previous}
    kept_nodes = [n for n in old_nodes if n.get("source_file") not in touched]
    kept_edges = [e for e in (graph.get("links") or []) if e.get("source") not in removed_ids]
    rebuilt = _assemble_graph(kept_nodes + new_nodes, kept_edges + new_edges, ignore)
    rebuilt["built_at_commit"] = head_commit(root)
    rebuilt[MANIFEST_KEY] = snapshot.to_dict(graph_path)

    if not dry_run:
        write_graph(graph_path, rebuilt, expected=identity)

    elapsed_ms = (perf_counter() - start) * 1000
    per_file_ms = parse_ms / len(modified) if modified else 0.0
    new_ids = {n["id"] for n in new_nodes}

    return SmartIndexResult(
        changed_files=changed_paths,
        expanded_files=[on_disk[k] for k in modified],
        updated_nodes=len(new_nodes),
        removed_nodes=len(previous),
        elapsed_ms=elapsed_ms,
        full_index_ms_estimate=per_file_ms * len(on_disk),
        updated_edges=sum(1 for e in rebuilt["links"] if e.get("source") in new_ids),
        dry_run=dry_run,
    )
