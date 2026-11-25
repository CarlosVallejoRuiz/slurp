"""Writing graph.json — one atomic writer for every command that produces a graph.

`slurp index` (full, --smart and --watch) and `slurp init` all write the graph
through :func:`write_graph`. A reader running at the same time — the MCP server
reloading after a re-index — sees the old graph or the new one, never a file
half written.
"""

import contextlib
import json
import os
import secrets
import stat
import time
from pathlib import Path

_IS_WINDOWS = os.name == "nt"

# Waits between attempts when Windows refuses the replace because another
# process holds the graph open: ~1.9 s in all before the error is raised.
_REPLACE_RETRY_DELAYS_S = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0)


def write_graph(path: Path, data: dict) -> None:
    """Write *data* to *path* as graph.json, replacing any graph there atomically.

    DECISION: the new graph goes to a temporary file in the destination's own
    directory, is flushed and fsynced, and only then takes the destination's
    place with os.replace — atomic within one filesystem. write_text truncated
    the live file first, so the MCP server could load a half-written graph and
    answer "could not be reloaded". A symlinked destination is written through,
    as write_text did, and an existing graph keeps its permission bits.

    Args:
        path: Destination, e.g. graphify-out/graph.json. Missing parent
            directories are created.
        data: The graph dict (`nodes`, `links`, `built_at_commit`).

    Raises:
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
        _replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


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
