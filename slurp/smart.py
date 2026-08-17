"""Incremental re-indexing driven by git — the engine behind `slurp index --smart`.

A full index re-parses every source file in the project. Most edits touch a
handful of files, so this module asks git what actually changed, widens that set
to the files that depend on them, and rewrites only the affected slice of the
graph.
"""

import json
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

from slurp.ignore import SlurpIgnore
from slurp.indexer import _INDEXED_EXTENSIONS, _should_skip, iter_source_files

# Edge relations that mean "the source node depends on the target node".
_DEPENDENCY_RELATIONS: frozenset[str] = frozenset({"imports_from", "contains"})

_GIT_TIMEOUT_S = 30


@dataclass
class SmartIndexResult:
    """Outcome of one incremental re-index.

    Attributes:
        changed_files: Files git reported as modified, staged, or untracked.
        expanded_files: changed_files plus their dependents — the set actually
            re-parsed. Deleted files appear in changed_files but not here.
        updated_nodes: Nodes written into the graph for the re-parsed files.
        removed_nodes: Nodes dropped because they belonged to a re-parsed or
            deleted file. A node that is removed and rewritten counts in both.
        elapsed_ms: Wall-clock duration of the whole incremental pass.
        full_index_ms_estimate: Extrapolated cost of a full index, from the
            measured per-file parse cost times the project's file count.
        updated_edges: Edges written for the re-parsed files.
        is_git_repo: False when *root* is not inside a git work tree, in which
            case nothing was re-indexed and the caller must fall back.
        graph_found: False when no existing graph was present to update, which
            likewise forces a full index.
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
    dry_run: bool = False

    @property
    def needs_full_index(self) -> bool:
        """True when the incremental path could not run at all."""
        return not self.is_git_repo or not self.graph_found

    @property
    def dependents_added(self) -> int:
        """How many files dependency expansion contributed beyond the changed set."""
        return max(0, len(self.expanded_files) - len(self._changed_and_present()))

    @property
    def speedup(self) -> float:
        """Estimated ratio of a full index to this run. 0.0 when unmeasurable."""
        if self.elapsed_ms <= 0 or self.full_index_ms_estimate <= 0:
            return 0.0
        return self.full_index_ms_estimate / self.elapsed_ms

    def _changed_and_present(self) -> list[Path]:
        return [p for p in self.changed_files if p in set(self.expanded_files)]


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


def get_changed_files(root: Path) -> list[Path]:
    """List indexable source files that differ from the last commit.

    Combines unstaged edits, staged edits, and untracked files. Untracked files
    are included because a newly created file is the most common thing to want
    indexed, and `git diff` alone never reports one.

    Args:
        root: Project root, inside a git work tree.

    Returns:
        Absolute paths, sorted and deduplicated, restricted to extensions slurp
        can index. Deleted files are included so their nodes can be dropped.
    """
    seen: set[Path] = set()
    for args in (
        ("diff", "--name-only", "--relative", "HEAD"),
        ("diff", "--name-only", "--relative", "--cached"),
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
            if path.suffix.lower() in _INDEXED_EXTENSIONS and not _should_skip(path):
                seen.add(path)
    return sorted(seen)


# ---------------------------------------------------------------------------
# dependency expansion
# ---------------------------------------------------------------------------

def _module_id_for_label(label: str, module_files: dict[str, str]) -> str | None:
    """Resolve an import label to the module node it refers to, if any.

    ``slurp.ignore.load_ignore`` resolves to the module ``slurp.ignore``. The
    longest matching prefix wins, so a nested package beats its parent.

    Args:
        label: The import node's label.
        module_files: Mapping of module node id to its source file.

    Returns:
        The matching module node id, or None.
    """
    parts = str(label).split(".")
    for cut in range(len(parts), 0, -1):
        candidate = ".".join(parts[:cut])
        if candidate in module_files:
            return candidate
    return None


def _build_referrer_index(graph: dict) -> dict[str, set[str]]:
    """Map each source file to the set of files that reference it.

    Two mechanisms feed this, because they cover different graph shapes:

    1. Graph edges in _DEPENDENCY_RELATIONS, read in reverse.
    2. Import nodes whose label resolves to another file's module node.

    Mechanism 2 exists because slurp's own indexers emit file-local import
    nodes (``a.import_b``) rather than edges that cross files — on a graph built
    by `slurp index`, mechanism 1 alone finds nothing.

    Args:
        graph: Graph dict with 'nodes' and 'links'.

    Returns:
        Mapping of source file (as stored in the graph) to referencing files.
    """
    nodes = graph.get("nodes") or []
    links = graph.get("links") or []

    file_of_node: dict[str, str] = {}
    module_files: dict[str, str] = {}
    for n in nodes:
        source_file = n.get("source_file")
        if not source_file:
            continue
        file_of_node[n["id"]] = source_file
        if n.get("type") == "module":
            module_files[n["id"]] = source_file

    referrers: dict[str, set[str]] = defaultdict(set)

    for e in links:
        if e.get("relation") not in _DEPENDENCY_RELATIONS:
            continue
        src_file = file_of_node.get(e.get("source", ""))
        tgt_file = file_of_node.get(e.get("target", ""))
        if src_file and tgt_file and src_file != tgt_file:
            referrers[tgt_file].add(src_file)

    for n in nodes:
        if n.get("type") != "import":
            continue
        src_file = n.get("source_file")
        if not src_file:
            continue
        module_id = _module_id_for_label(n.get("label", ""), module_files)
        if module_id is None:
            continue
        target_file = module_files[module_id]
        if target_file != src_file:
            referrers[target_file].add(src_file)

    return referrers


def expand_dependencies(
    changed: list[Path],
    graph: dict,
    root: Path,
    hops: int = 2,
) -> list[Path]:
    """Widen a changed-file set to include the files that depend on it.

    Args:
        changed: Absolute paths of changed files.
        graph: The existing graph dict being updated.
        root: Project root, used to relate absolute paths to graph entries.
        hops: Maximum expansion depth. Bounded so that a change to a widely
            imported module does not drag in the whole project.

    Returns:
        Absolute paths of files to re-index — the changed files that still
        exist, plus their dependents — sorted and deduplicated.
    """
    present = {p for p in changed if p.is_file()}
    if not present or hops <= 0:
        return sorted(present)

    referrers = _build_referrer_index(graph)
    if not referrers:
        return sorted(present)

    def _rel_key(path: Path) -> str:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    by_key = {_rel_key(p): p for p in present}
    frontier = set(by_key)
    collected = set(by_key)

    for _ in range(hops):
        nxt: set[str] = set()
        for key in frontier:
            nxt |= referrers.get(key, set())
        nxt -= collected
        if not nxt:
            break
        collected |= nxt
        frontier = nxt

    result = set(present)
    for key in collected - set(by_key):
        candidate = (root / key).resolve()
        if candidate.is_file() and candidate.suffix.lower() in _INDEXED_EXTENSIONS:
            result.add(candidate)
    return sorted(result)


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
        except Exception:
            # Matches index_project: an unparseable file is skipped, not fatal.
            continue
        nodes.extend(file_nodes)
        edges.extend(file_edges)
    return nodes, edges, (perf_counter() - start) * 1000


def smart_reindex(
    root: Path,
    graph_path: Path,
    ignore: SlurpIgnore | None = None,
    hops: int = 2,
    dry_run: bool = False,
) -> SmartIndexResult:
    """Update an existing graph in place, re-parsing only what changed.

    Args:
        root: Project root to index.
        graph_path: Path of the graph to read and rewrite.
        ignore: Optional SlurpIgnore rules applied to newly produced nodes.
        hops: Dependency expansion depth.
        dry_run: Compute everything but leave the graph on disk untouched.

    Returns:
        A SmartIndexResult. Check `needs_full_index` first: when there is no git
        repo or no existing graph, no work was done and the caller should run a
        full index instead.
    """
    start = perf_counter()

    if not is_git_repo(root):
        return SmartIndexResult(is_git_repo=False, dry_run=dry_run)

    if not graph_path.is_file():
        return SmartIndexResult(graph_found=False, dry_run=dry_run)
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SmartIndexResult(graph_found=False, dry_run=dry_run)

    changed = get_changed_files(root)
    if not changed:
        return SmartIndexResult(
            elapsed_ms=(perf_counter() - start) * 1000, dry_run=dry_run,
        )

    expanded = expand_dependencies(changed, graph, root, hops=hops)

    new_nodes, new_edges, parse_ms = _reindex_files(expanded, root)
    if ignore is not None:
        new_nodes = [n for n in new_nodes if not ignore.should_ignore(n, n["id"])]

    # A file is "touched" if it was re-parsed or deleted; either way its old
    # nodes must go. Keys match the graph's stored relative-POSIX form.
    root_resolved = root.resolve()

    def _rel_key(path: Path) -> str:
        try:
            return path.resolve().relative_to(root_resolved).as_posix()
        except ValueError:
            return path.as_posix()

    touched = {_rel_key(p) for p in expanded} | {
        _rel_key(p) for p in changed if not p.is_file()
    }

    old_nodes = graph.get("nodes") or []
    kept_nodes = [n for n in old_nodes if n.get("source_file") not in touched]
    removed_ids = {
        n["id"] for n in old_nodes if n.get("source_file") in touched
    }
    removed_nodes = len(old_nodes) - len(kept_nodes)

    # Deduplicate incoming nodes, first occurrence wins (as index_project does).
    merged_nodes = list(kept_nodes)
    seen_ids = {n["id"] for n in kept_nodes}
    for n in new_nodes:
        if n["id"] not in seen_ids:
            seen_ids.add(n["id"])
            merged_nodes.append(n)

    # DECISION: drop only edges *originating* from a removed node. Those get
    # regenerated by the re-parse. An incoming cross-file edge whose target id
    # survives the rewrite is still valid, and dropping it would silently erode
    # the graph one incremental run at a time.
    kept_edges = [
        e for e in (graph.get("links") or [])
        if e.get("source") not in removed_ids
    ]

    merged_edges: list[dict] = []
    seen_edges: set[tuple[str, str]] = set()
    for e in [*kept_edges, *new_edges]:
        src, tgt = e.get("source", ""), e.get("target", "")
        if src not in seen_ids or tgt not in seen_ids:
            continue
        key = (src, tgt)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        merged_edges.append(e)

    graph["nodes"] = merged_nodes
    graph["links"] = merged_edges

    if not dry_run:
        graph_path.parent.mkdir(parents=True, exist_ok=True)
        graph_path.write_text(json.dumps(graph, indent=2), encoding="utf-8")

    elapsed_ms = (perf_counter() - start) * 1000
    total_files = sum(1 for _ in iter_source_files(root))
    per_file_ms = parse_ms / len(expanded) if expanded else 0.0

    return SmartIndexResult(
        changed_files=changed,
        expanded_files=expanded,
        updated_nodes=len(new_nodes),
        removed_nodes=removed_nodes,
        elapsed_ms=elapsed_ms,
        full_index_ms_estimate=per_file_ms * total_files,
        updated_edges=len(new_edges),
        dry_run=dry_run,
    )
