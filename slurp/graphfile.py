"""graph.json on disk — writing it safely, locking it, and telling whose it is.

`slurp index` (full, --smart and --watch), `slurp init` and the refresh of the
CLI and the MCP server all write the graph through :func:`write_graph`. A
reader running at the same time sees the old graph or the new one, never a
file half written; a writer that started from a graph someone else has since
replaced gives up instead of overwriting it (compare-and-swap).
"""

import contextlib
import json
import os
import secrets
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path

_IS_WINDOWS = os.name == "nt"

# Waits between attempts when Windows refuses the replace because another
# process holds the graph open: ~1.9 s in all before the error is raised.
_REPLACE_RETRY_DELAYS_S = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0)

# What a graph file is, for compare-and-swap: (st_mtime_ns, st_ino, st_size).
# A rewrite always changes one of them — os.replace brings a new inode.
GraphIdentity = tuple[int, ...]

#: The identity of a file that does not exist.
ABSENT: GraphIdentity = ()

_LOCK_POLL_S = 0.05


class GraphChangedError(OSError):
    """The graph on disk is no longer the one the writer started from."""


def graph_identity(path: Path) -> GraphIdentity:
    """(mtime_ns, inode, size) of the file *path* points at, or ABSENT."""
    try:
        st = os.stat(os.path.realpath(path))
    except FileNotFoundError:
        return ABSENT
    return (st.st_mtime_ns, st.st_ino, st.st_size)


def write_graph(path: Path, data: dict, expected: GraphIdentity | None = None) -> GraphIdentity:
    """Write *data* to *path* as graph.json, replacing any graph there atomically.

    DECISION: the new graph goes to a temporary file in the destination's own
    directory, is flushed and fsynced, and only then takes the destination's
    place with os.replace — atomic within one filesystem. write_text truncated
    the live file first, so the MCP server could load a half-written graph and
    answer "could not be reloaded". A symlinked destination is written through,
    as write_text did, and an existing graph keeps its permission bits.

    DECISION (v1.3): with *expected*, the destination must still be that file
    just before the replace — compare-and-swap. A refresh that took a second
    must not overwrite what graphify or another slurp wrote meanwhile; it is
    discarded and the caller reloads what is on disk. A window of microseconds
    remains between the check and the replace against writers that do not
    take slurp's lock.

    Args:
        path: Destination, e.g. graphify-out/graph.json. Missing parent
            directories are created.
        data: The graph dict (`nodes`, `links`, `built_at_commit`, `slurp`).
        expected: The identity the destination must still have (graph_identity
            when the writer read it; ABSENT for "must not exist"). None skips
            the check.

    Returns:
        The identity of the file written.

    Raises:
        GraphChangedError: The destination changed; nothing was written.
        OSError: The graph could not be written. Whatever was at *path* before
            is left as it was, and no temporary file stays behind.
    """
    text = json.dumps(data, indent=2)
    target = Path(os.path.realpath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    # 0o666 lets the umask decide, as for any file open() creates; tempfile's
    # 0600 would hide the graph from other tools reading it.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        with contextlib.suppress(FileNotFoundError):
            os.chmod(tmp, stat.S_IMODE(target.stat().st_mode))
        if expected is not None and graph_identity(target) != expected:
            raise GraphChangedError(f"{target} changed while the new graph was being built")
        _replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return graph_identity(target)


def _replace(src: Path, dst: Path) -> None:
    """os.replace, retried with growing waits while Windows reports the file busy.

    Elsewhere a PermissionError is a real permission problem and is raised at once.
    """
    for delay in (*_REPLACE_RETRY_DELAYS_S, None):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not _IS_WINDOWS or delay is None:
                raise
            time.sleep(delay)


class GraphLock:
    """Advisory lock `.graph.json.lock` beside a graph, held while one is written.

    DECISION: the lock only saves duplicate work — two slurp processes
    re-indexing the same project at once. Correctness never depends on it: the
    atomic replace, a manifest true to its graph and the compare-and-swap
    guarantee that. So a directory where the lock cannot be created (read-only,
    an exotic filesystem) is no error: the writer goes on without it. The file
    is never deleted; deleting lock files is how two processes end up holding
    "the" lock on two different inodes.
    """

    def __init__(self, graph_path: Path) -> None:
        target = Path(os.path.realpath(graph_path))
        self.path = target.with_name(f".{target.name}.lock")
        self._fd: int | None = None
        #: True while this process holds the lock (False when going without).
        self.held = False

    def acquire(self, timeout: float | None = None,
                on_wait: Callable[[], None] | None = None) -> bool:
        """Take the lock, waiting up to *timeout* seconds (None: as long as it takes).

        Args:
            timeout: Seconds to wait for a holder to finish; 0 tries once.
            on_wait: Called once, the first time the lock is found busy.

        Returns:
            True when the caller may write: the lock is held, or it cannot be
            used here at all. False when another process kept it past *timeout*.
        """
        if self._fd is None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
            except OSError:
                return True   # no lock possible here: go on without one
        deadline = None if timeout is None else time.monotonic() + timeout
        waited = False
        while True:
            try:
                _lock_fd(self._fd)
                self.held = True
                return True
            except BlockingIOError:
                pass
            except OSError:
                return True   # locking unsupported on this filesystem
            if deadline is not None and time.monotonic() >= deadline:
                return False
            if not waited:
                waited = True
                if on_wait is not None:
                    on_wait()
            time.sleep(_LOCK_POLL_S)

    def release(self) -> None:
        """Let go of the lock (and the file descriptor). Safe to call twice."""
        if self._fd is None:
            return
        if self.held:
            with contextlib.suppress(OSError):
                _unlock_fd(self._fd)
        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = None
        self.held = False

    def __enter__(self) -> "GraphLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _lock_fd(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError(str(exc)) from exc

    def _unlock_fd(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Whose graph is it
# ---------------------------------------------------------------------------

# The top-level keys slurp writes. graphify's graph.json is networkx node-link
# data and adds directed/multigraph/graph/hyperedges — it writes
# built_at_commit as well, so that key alone cannot tell them apart.
# "slurp" is the manifest (slurp.manifest), written since v1.3 and recognised
# since v1.2.1, so that version re-indexes a v1.3 graph as its own.
_SLURP_GRAPH_KEYS = frozenset({"nodes", "links", "built_at_commit", "slurp"})
_LEGACY_GRAPH_KEYS = frozenset({"nodes", "links", "built_at_commit"})
_NODE_LINK_KEYS = frozenset({"directed", "multigraph", "graph", "hyperedges"})


def graph_kind(data: object) -> str:
    """Who wrote this parsed graph.json, as far as the refresh is concerned.

    DECISION: positive detection. Only a graph carrying slurp's manifest is
    refreshed; one with exactly the keys slurp ≤ 1.2 wrote may be adopted
    (slurp.refresh validates its root first); everything else is served as it
    is and never written — graphify's graph holds relations slurp cannot
    regenerate, and a generic {nodes, links} graph could be anyone's.

    Returns:
        "own" (has the manifest), "legacy" (slurp ≤ 1.2), "graphify"
        (networkx node-link data) or "foreign".
    """
    if not isinstance(data, dict):
        return "foreign"
    manifest = data.get("slurp")
    if isinstance(manifest, dict) and "format" in manifest:
        return "own"
    keys = set(data)
    if keys == _LEGACY_GRAPH_KEYS:
        return "legacy"
    if keys & _NODE_LINK_KEYS:
        return "graphify"
    return "foreign"


def is_foreign_graph(path: Path) -> bool:
    """Whether *path* holds a file that `slurp index` did not write.

    DECISION: slurp's default output, graphify-out/graph.json, is also where
    graphify writes its own graph, and that graph carries LLM-extracted
    relations slurp cannot regenerate. Anything that is not recognisably
    slurp's output counts as foreign — including a file that does not parse,
    which may be another tool's half-written graph. A bare {nodes, links}
    graph is indistinguishable from slurp's and is treated as slurp's: this
    answers "may `slurp index` overwrite it after asking nobody", which the
    user decides by running it; the refresh asks graph_kind instead.

    Args:
        path: Output path about to be written.

    Returns:
        True if the file exists and was not written by slurp.
    """
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not isinstance(data, dict) or not set(data) <= _SLURP_GRAPH_KEYS
