"""Keeping a graph current: building it with its manifest, and refreshing it on demand.

The engine behind the freshness of `slurp serve`, of the query commands and of
`slurp index --watch` (docs/design/mcp-autonomo.md §2–§4). This module owns the
full build; slurp.smart owns the incremental path, slurp.manifest the record
and the change detection, slurp.graphfile the write, the lock and ownership.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
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
        #: Signature of the changes whose refresh last failed (not retried),
        #: and what it failed with.
        self.failed_signature: tuple | None = None
        self.failed_message = ""
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


# ---------------------------------------------------------------------------
# Freshness on demand: one refresh thread at most, a time limit per call
# ---------------------------------------------------------------------------

#: _meta key of the graph's state in every MCP answer (reverse-DNS, as the
#: MCP specification asks of custom keys).
META_KEY = "dev.imjuank.slurp/graph"

DEFAULT_TIMEOUT_S = 2.0
_OFF_VALUES = frozenset({"0", "false", "no", "off"})

# A federation reports its worst member.
_SEVERITY = {"fresh": 0, "readonly": 1, "refreshed": 2, "stale": 3, "failed": 4}

_RELOAD_FAILED = (
    "⚠ {path} changed on disk but could not be reloaded: {exc}. Still serving the "
    "previously loaded graph — fix the file and try again."
)


def resolve_refresh(no_refresh: bool = False, timeout: float | None = None) -> tuple[bool, float]:
    """(enabled, seconds to wait) from the flags, else SLURP_REFRESH / SLURP_REFRESH_TIMEOUT.

    Raises:
        ValueError: A timeout that is negative or not a number.
    """
    enabled = not no_refresh and os.environ.get("SLURP_REFRESH", "1").strip().lower() \
        not in _OFF_VALUES
    if timeout is None:
        raw = os.environ.get("SLURP_REFRESH_TIMEOUT", "").strip()
        source = "SLURP_REFRESH_TIMEOUT"
        try:
            timeout = float(raw) if raw else DEFAULT_TIMEOUT_S
        except ValueError:
            raise ValueError(f"{source}={raw!r} is not a number of seconds") from None
    else:
        source = "--refresh-timeout"
    if not timeout >= 0 or timeout == float("inf"):
        raise ValueError(f"{source} must be a number of seconds ≥ 0, got {timeout!r}")
    return enabled, float(timeout)


@dataclass
class Freshness:
    """The state of the graph an answer is served from."""

    status: str = "fresh"
    refresh_ms: float | None = None
    changed_files: list[str] = field(default_factory=list)
    #: Lines to put first in an MCP answer (the model reads only content),
    #: or on stderr in the CLI.
    warnings: list[str] = field(default_factory=list)

    def worsen(self, status: str) -> None:
        if _SEVERITY[status] > _SEVERITY[self.status]:
            self.status = status

    def warn(self, line: str) -> None:
        if line not in self.warnings:
            self.warnings.append(line)

    def add_files(self, files: list[str]) -> None:
        self.changed_files.extend(f for f in files if f not in self.changed_files)

    def meta(self) -> dict:
        """The _meta entry: {META_KEY: {status, refresh_ms, changed_files}}."""
        info: dict = {"status": self.status}
        if self.refresh_ms is not None:
            info["refresh_ms"] = round(self.refresh_ms, 1)
        if self.changed_files:
            info["changed_files"] = self.changed_files[:20]
        return {META_KEY: info}


class Refresher:
    """Keeps a set of graphs current on demand for one process (server or command).

    DECISION (docs/design/mcp-autonomo.md §2.3–2.4): a call checks the
    manifests (stat only) and, when something changed, starts the one refresh
    thread and waits for it until *timeout* after it started; past that it is
    answered from the previous graph, with a warning, and the refresh goes on
    — a thread cannot be cancelled safely and its work is wanted. A call that
    finds a refresh already running waits for that one instead of starting
    another. The swap to the new graph always happens here, on the caller's
    thread, so no answer sees a half-updated state and the thread never
    touches the caches.
    """

    def __init__(
        self,
        trackers: list[GraphTracker],
        timeout: float = DEFAULT_TIMEOUT_S,
        enabled: bool = True,
        disabled_reason: str = "auto-refresh is off (--no-refresh or SLURP_REFRESH=0)",
        on_swap: Callable[[], None] | None = None,
    ) -> None:
        self.trackers = list(trackers)
        self.timeout = timeout
        self.enabled = enabled
        self.disabled_reason = disabled_reason
        self.on_swap = on_swap
        self._thread: threading.Thread | None = None
        self._started = 0.0
        self._work: list[tuple[GraphTracker, Changes | None]] = []
        self._found: dict[int, Changes] = {}
        self._results: list[tuple[GraphTracker, Changes, Rebuilt | None, str]] | None = None
        self._cancel = threading.Event()
        self._warned_memory: set[int] = set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # --- the thread ------------------------------------------------------

    def start(self) -> None:
        """Check and refresh in the background, without waiting (server startup)."""
        if self.enabled and self._thread is None:
            work = [(t, None) for t in self.trackers if t.refreshable]
            if work:
                self._launch(work)

    def _launch(self, work: list[tuple[GraphTracker, Changes | None]]) -> None:
        self._work, self._found, self._results = work, {}, None
        self._cancel.clear()
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._job, args=(work,), name="slurp-refresh",
                                        daemon=True)
        self._thread.start()

    def _job(self, work: list[tuple[GraphTracker, Changes | None]]) -> None:
        results: list[tuple[GraphTracker, Changes, Rebuilt | None, str]] = []
        for tracker, changes in work:
            try:
                if changes is None:
                    changes = tracker.check()
                    self._found[id(tracker)] = changes
                    if not changes.any or tracker.failed_signature == changes.signature():
                        results.append((tracker, changes, None, ""))
                        continue
                results.append((tracker, changes, tracker.rebuild(changes, self._cancel.is_set),
                                ""))
            except Exception as exc:
                # The detail goes to stderr — never stdout, the MCP transport.
                traceback.print_exc(file=sys.stderr)
                results.append((tracker, changes or Changes(), None,
                                f"{type(exc).__name__}: {exc}"))
        self._results = results

    # --- per call --------------------------------------------------------

    def prepare(self) -> Freshness:
        """Make the graphs as current as the time limit allows, and say how current."""
        fresh = Freshness()
        if self._thread is None:
            self._reload_rewritten(fresh)
        deadline: float | None = None
        for _round in range(3):
            if self._thread is not None:
                if deadline is None:
                    deadline = self._started + self.timeout
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._thread.join(remaining)
                if self._thread.is_alive():
                    fresh.worsen("stale")
                    for changes in self._pending_changes():
                        fresh.add_files(changes.files)
                    fresh.warn(self._stale_warning(waited=remaining > 0))
                    break
                self._collect(fresh)
                continue
            work = self._check(fresh)
            if not work:
                break
            self._launch(work)
        for tracker in self.trackers:
            if tracker.readonly:
                fresh.worsen("readonly")
                if hint := tracker.readonly_warning():
                    fresh.warn(hint)
        return fresh

    def finish(self, on_wait: Callable[[], None] | None = None) -> None:
        """Let a running refresh complete before the process exits (the CLI's way).

        Ctrl+C while waiting cancels it before it writes; the graph stays as it was.
        """
        if not self.running:
            return
        if on_wait is not None:
            on_wait()
        try:
            assert self._thread is not None
            self._thread.join()
        except KeyboardInterrupt:
            self._cancel.set()

    # --- pieces ----------------------------------------------------------

    def _reload_rewritten(self, fresh: Freshness) -> None:
        swapped = False
        for tracker in self.trackers:
            if not tracker.disk_changed():
                continue
            try:
                tracker.load()
            except Exception as exc:  # the old graph keeps being served
                fresh.worsen("failed")
                fresh.warn(_RELOAD_FAILED.format(
                    path=tracker.path, exc=f"{type(exc).__name__}: {exc}"))
                continue
            fresh.worsen("refreshed")
            swapped = True
        if swapped and self.on_swap is not None:
            self.on_swap()

    def _check(self, fresh: Freshness) -> list[tuple[GraphTracker, Changes | None]]:
        work: list[tuple[GraphTracker, Changes | None]] = []
        for tracker in self.trackers:
            if not tracker.refreshable:
                continue
            changes = tracker.check()
            if not changes.any:
                continue
            fresh.add_files(changes.files)
            if not self.enabled:
                fresh.worsen("stale")
                fresh.warn(f"⚠ {changes.describe()} — not refreshed: "
                                      f"{self.disabled_reason}.")
            elif tracker.failed_signature == changes.signature():
                fresh.worsen("failed")
                fresh.warn(f"⚠ Graph refresh failed: {tracker.failed_message}")
            else:
                work.append((tracker, changes))
        return work

    def _collect(self, fresh: Freshness) -> None:
        results = self._results or []
        self._thread, self._results, self._work = None, None, []
        swapped = False
        for tracker, changes, rebuilt, error in results:
            if error:
                tracker.failed_signature = changes.signature()
                tracker.failed_message = error
                fresh.worsen("failed")
                fresh.add_files(changes.files)
                fresh.warn(f"⚠ Graph refresh failed: {error}")
            elif rebuilt is None or rebuilt.cancelled:
                if changes.any and tracker.failed_signature == changes.signature():
                    fresh.worsen("failed")
                    fresh.warn(f"⚠ Graph refresh failed: {tracker.failed_message}")
            elif rebuilt.reloaded:
                try:
                    tracker.load()
                except Exception as exc:
                    fresh.worsen("failed")
                    fresh.warn(_RELOAD_FAILED.format(
                        path=tracker.path, exc=f"{type(exc).__name__}: {exc}"))
                    continue
                fresh.worsen("refreshed")
                swapped = True
            else:
                tracker.apply(rebuilt)
                swapped = True
                fresh.worsen("refreshed")
                fresh.add_files(changes.files)
                fresh.refresh_ms = (fresh.refresh_ms or 0.0) + rebuilt.ms
                if not rebuilt.written and id(tracker) not in self._warned_memory:
                    self._warned_memory.add(id(tracker))
                    fresh.warn(f"⚠ Graph refreshed in memory only: {tracker.path} "
                                          f"cannot be written ({rebuilt.write_error}).")
        if swapped and self.on_swap is not None:
            self.on_swap()

    def _pending_changes(self) -> list[Changes]:
        found = []
        for tracker, changes in self._work:
            changes = changes if changes is not None else self._found.get(id(tracker))
            if changes is not None and changes.any:
                found.append(changes)
        return found

    def _stale_warning(self, waited: bool) -> str:
        what = "; ".join(c.describe() for c in self._pending_changes()) \
            or "files changed since this graph was built"
        tail = "This answer may miss them; call again in a few seconds."
        if waited:
            return f"⚠ Graph refresh still running — {what}. {tail}"
        ago = int(time.monotonic() - self._started)
        return f"⚠ Graph refresh in progress (started {ago}s ago) — {what}. {tail}"
