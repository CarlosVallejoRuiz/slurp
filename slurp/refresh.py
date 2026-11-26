"""Keeping a graph current: building it with its manifest, and refreshing it on demand.

The engine behind the freshness of `slurp serve`, of the query commands and of
`slurp index --watch` (docs/design/mcp-autonomo.md §2–§4). This module owns the
full build; slurp.smart owns the incremental path, slurp.manifest the record
and the change detection, slurp.graphfile the write, the lock and ownership.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from slurp import indexer
from slurp.graphfile import (
    ABSENT,
    GraphChangedError,
    GraphIdentity,
    GraphLock,
    graph_identity,
    graph_kind,
    write_graph,
)
from slurp.ignore import load_ignore
from slurp.loader import build_graph, load_graph, read_graph_json
from slurp.manifest import (
    MANIFEST_KEY,
    Changes,
    Manifest,
    ManifestChecker,
    ManifestError,
    build_manifest,
)


def index_with_manifest(
    root: Path,
    graph_path: Path,
    ignore_path: Path,
    on_file: Callable[[Path], None] | None = None,
) -> dict:
    """A full index of *root* with its manifest, ready for write_graph(graph_path, …).

    The tree is walked once: the manifest records that walk (directories with
    their mtimes, files with stat and hash) and the index parses exactly those
    files, after they were hashed.

    Args:
        root: Project root.
        graph_path: Where the graph will be written (the manifest's root is
            stored relative to its directory).
        ignore_path: The ignore rules to build with, recorded in the manifest.
        on_file: Progress callback, once per file parsed.

    Returns:
        The graph dict: nodes, links, built_at_commit and the manifest.
    """
    from slurp.smart import head_commit

    started = time.time_ns()
    root = root.resolve()
    walk = indexer.walk_source_tree(root)
    manifest = build_manifest(root, ignore_path, started_ns=started, walk=walk)
    graph = indexer.index_project(root, load_ignore(ignore_path), on_file=on_file,
                                  files=walk[1])
    # Still written for slurp ≤ 1.2 and for --smart on graphs without a manifest.
    graph["built_at_commit"] = head_commit(root)
    graph[MANIFEST_KEY] = manifest.to_dict(graph_path)
    return graph


# ---------------------------------------------------------------------------
# One graph file: whose it is, and what it was built from
# ---------------------------------------------------------------------------

# DECISION: a graph from slurp ≤ 1.2 carries no manifest and no root, so the
# root is guessed — the parent of graphify-out/, where `slurp index` writes by
# default, or else the graph's own directory — and only trusted when nine in
# ten of the graph's source files exist under it. Guessing wrong would
# rebuild one project's graph from another's tree.
_ADOPTION_MIN_SHARE = 0.9
_GRAPHIFY_OUT = "graphify-out"


@dataclass
class Rebuilt:
    """What a refresh produced, handed from the refresh thread to the main one."""

    graph: dict | None = None
    manifest: Manifest | None = None
    identity: GraphIdentity = ABSENT
    way: str = ""              # "smart" or "full"
    reason: str = ""           # why a full index, when it was one
    written: bool = False
    write_error: str = ""
    reloaded: bool = False     # another writer got there first: reload from disk
    cancelled: bool = False
    ms: float = 0.0


class GraphTracker:
    """One graph file: loaded, classified, and — when it is slurp's — kept current.

    Owns the parsed dict as well as the graph, so a refresh never reads the
    disk again, and the manifest checker, so the per-call check only stats.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.kind = "foreign"
        self.data: dict | None = None
        self.graph: nx.DiGraph = nx.DiGraph()
        self.identity: GraphIdentity = ABSENT
        self.root: Path | None = None
        self.ignore_path: Path | None = None
        self.checker: ManifestChecker | None = None
        self.adoptable = False
        #: Why a graph that looks like slurp's is served read-only.
        self.note = ""
        #: Signature of the changes whose refresh last failed (not retried).
        self.failed_signature: tuple | None = None
        self._covered: list[Path] = []
        self._built_ns = 0
        self.load()

    @property
    def refreshable(self) -> bool:
        return self.checker is not None or self.adoptable

    @property
    def readonly(self) -> bool:
        return not self.refreshable

    # --- loading ---------------------------------------------------------

    def load(self) -> None:
        """(Re)read the file and decide again whose graph it is.

        Raises:
            SlurpLoadError: The file cannot be read as a graph. Whatever was
                loaded before is kept.
        """
        identity = graph_identity(self.path)
        if self.path.suffix.lower() in (".graphml", ".csv"):
            data, graph = None, load_graph(self.path)
        else:
            data = read_graph_json(self.path)
            graph = build_graph(data)
        self.data, self.graph, self.identity = data, graph, identity
        self._classify()

    def _classify(self) -> None:
        self.kind = graph_kind(self.data) if self.data is not None else "foreign"
        self.root = self.ignore_path = self.checker = None
        self.adoptable = False
        self.note = ""
        if self.kind == "own":
            try:
                manifest = Manifest.from_dict(self.data[MANIFEST_KEY], self.path)
            except ManifestError as exc:
                self.note = f"its {exc}"
            else:
                if manifest.root.is_dir():
                    self.root, self.ignore_path = manifest.root, manifest.ignore_path
                    self.checker = ManifestChecker(manifest)
                    return
                self.note = f"its project root {manifest.root} does not exist here"
        elif self.kind == "legacy":
            root = self._candidate_root()
            if self._validates(root):
                self.root, self.ignore_path, self.adoptable = root, root / ".slurpignore", True
                return
            self.note = "run `slurp index` once to enable auto-refresh"
        self._remember_covered()

    def _candidate_root(self) -> Path:
        here = Path(os.path.realpath(self.path)).parent
        return here.parent if here.name == _GRAPHIFY_OUT else here

    def _sources(self) -> list[str]:
        nodes = (self.data or {}).get("nodes") or []
        return sorted({str(n.get("source_file") or n.get("file_path") or "")
                       for n in nodes if isinstance(n, dict)} - {""})

    def _validates(self, root: Path) -> bool:
        sources = self._sources()
        if not sources:
            return False
        found = sum(1 for s in sources if (root / s).is_file())
        return found / len(sources) >= _ADOPTION_MIN_SHARE

    def _remember_covered(self) -> None:
        root = self._candidate_root()
        self._covered = [p for p in (root / s for s in self._sources()) if p.is_file()]
        try:
            self._built_ns = os.stat(os.path.realpath(self.path)).st_mtime_ns
        except OSError:
            self._built_ns = 0

    def disk_changed(self) -> bool:
        """Whether another process replaced the file since it was loaded or written."""
        return graph_identity(self.path) != self.identity

    # --- read-only graphs ------------------------------------------------

    def readonly_changed(self) -> int:
        """How many files this read-only graph covers changed after it was built.

        Cheap (a stat per covered file) and partial: a file the graph never
        knew about is not seen.
        """
        changed = 0
        for path in self._covered:
            try:
                if os.stat(path).st_mtime_ns > self._built_ns:
                    changed += 1
            except OSError:
                changed += 1
        return changed

    def readonly_warning(self) -> str:
        """The line a read-only graph adds to an answer, or "" when nothing changed."""
        changed = self.readonly_changed()
        if not changed:
            return ""
        files = f"{changed} file{'s' if changed != 1 else ''} this graph covers changed after it was built"
        if self.kind == "graphify":
            return f"{files}; slurp doesn't rebuild graphify graphs — rerun graphify."
        return f"{files}; {self.note or 'slurp does not rebuild this graph'}."

    # --- slurp's own graphs ----------------------------------------------

    def check(self) -> Changes:
        """What changed on disk since this graph was built."""
        if self.adoptable:
            return Changes(reason="adopting a graph written by slurp ≤ 1.2: one full index "
                                  "records its manifest")
        if self.checker is None:
            return Changes()
        return self.checker.check()

    def rebuild(self, changes: Changes, should_stop: Callable[[], bool] = lambda: False,
                ) -> Rebuilt:
        """Bring the graph up to date with *changes* and write it. Runs off the main thread.

        DECISION: this never prints to stdout, reads stdin or calls into the
        CLI — on the MCP transport stdout is the protocol. It takes the lock,
        and writes with compare-and-swap against the identity it was loaded
        with: if any other writer got there first, the result is dropped and
        the caller reloads what is on disk.

        Raises:
            RuntimeError: The graph is not slurp's to rebuild.
        """
        if not self.refreshable or self.root is None or self.ignore_path is None:
            raise RuntimeError(f"{self.path} is not a graph slurp refreshes")
        from slurp.smart import head_commit, manifest_after_rebuild, rebuild_modified

        t0 = time.perf_counter()
        started = time.time_ns()
        lock = GraphLock(self.path)
        lock.acquire()
        try:
            if self.disk_changed():
                return Rebuilt(reloaded=True, ms=(time.perf_counter() - t0) * 1000)
            graph: dict | None = None
            manifest: Manifest | None = None
            reason = changes.reason or (
                f"{len(changes.added)} added and {len(changes.deleted)} removed or renamed files"
                if changes.added or changes.deleted else "")
            if not changes.needs_full and self.checker is not None and self.data is not None:
                manifest = manifest_after_rebuild(self.checker.manifest, changes.modified,
                                                  started)
                outcome = rebuild_modified(self.data, changes.modified, self.root,
                                           load_ignore(self.ignore_path))
                if outcome.graph is not None:
                    graph = outcome.graph
                    graph["built_at_commit"] = head_commit(self.root)
                    graph[MANIFEST_KEY] = manifest.to_dict(self.path)
                else:
                    reason = outcome.fallback_reason
            way = "smart" if graph is not None else "full"
            if graph is None:
                graph = index_with_manifest(self.root, self.path, self.ignore_path)
                manifest = Manifest.from_dict(graph[MANIFEST_KEY], self.path)
            if should_stop():
                return Rebuilt(cancelled=True)
            result = Rebuilt(graph=graph, manifest=manifest, way=way, reason=reason)
            try:
                result.identity = write_graph(self.path, graph, expected=self.identity)
                result.written = True
            except GraphChangedError:
                return Rebuilt(reloaded=True, ms=(time.perf_counter() - t0) * 1000)
            except OSError as exc:
                result.identity = self.identity
                result.write_error = f"{type(exc).__name__}: {exc}"
            result.ms = (time.perf_counter() - t0) * 1000
            return result
        finally:
            lock.release()

    def apply(self, rebuilt: Rebuilt) -> None:
        """Serve what a refresh produced. Main thread only: builds the graph from the dict."""
        assert rebuilt.graph is not None and rebuilt.manifest is not None
        self.data = rebuilt.graph
        self.graph = build_graph(rebuilt.graph)
        self.identity = rebuilt.identity
        self.kind = "own"
        self.adoptable = False
        self.note = ""
        self.root, self.ignore_path = rebuilt.manifest.root, rebuilt.manifest.ignore_path
        self.checker = ManifestChecker(rebuilt.manifest)
        self.failed_signature = None
