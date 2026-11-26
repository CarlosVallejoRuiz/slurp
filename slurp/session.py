"""The MCP session log: what an agent asked slurp, and what it edited (v1.3, design §7).

`.slurp/session.log` (JSON lines, written only if `.slurp/` exists) gets, per
server process:

- a ``start`` event: the graph served, whose it is, the slurp version;
- one ``call`` line per call to any of the five tools: client, protocol, tool,
  the arguments in short, how long it took, the graph's state (fresh,
  refreshed, stale, failed, readonly), the refresh time and the files that
  changed, nodes and tokens when there are any, the error if it failed;
- an ``end`` event at EOF: the files that changed while the session ran — in a
  measured task, the agent's edits.

Lines written before v1.3 (one per slurp_query, no event) are still read.
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from slurp import __version__

SESSION_LOG_FILE = "session.log"

# How many changed file names a call line keeps; the end event keeps more.
_CALL_FILES = 20
_END_FILES = 500


def read_session_log(log_dir: Path | None = None, last: int | None = None) -> list[dict]:
    """Read entries from the session log, oldest first.

    Args:
        log_dir: Directory holding session.log (default: ./.slurp).
        last: Return only the most recent N entries; None returns all.

    Returns:
        List of entry dicts. Empty if the file or directory does not exist.
        Malformed lines are skipped rather than raising.
    """
    log_dir = Path(".slurp") if log_dir is None else log_dir
    log_path = log_dir / SESSION_LOG_FILE
    if not log_path.exists():
        return []

    entries: list[dict] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            # INTENTIONAL: the session log is append-only JSONL written by a
            # long-running server, so a truncated final line is expected after
            # a crash. One unreadable line must not hide the rest of the log.
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)

    return entries[-last:] if last is not None and last > 0 else entries


class SessionLog:
    """The log of one server process. Opt-in: nothing is written unless `.slurp/` exists.

    DECISION: the directory is never created. Its presence is the opt-in signal —
    a user who has run a manual query already has .slurp/ from the audit log, so
    the session log appears for them and stays invisible for everyone else.
    """

    def __init__(self, log_dir: Path | None = None) -> None:
        self.dir = Path(".slurp") if log_dir is None else log_dir
        self.started = datetime.now()
        self.id = f"{os.getpid()}-{self.started.strftime('%Y%m%dT%H%M%S')}"
        self.calls = 0
        self._snapshots: list = []

    @property
    def enabled(self) -> bool:
        return self.dir.is_dir()

    def write(self, entry: dict) -> bool:
        """Append one line (timestamp and session added). Never raises."""
        if not self.enabled:
            return False
        line = {"ts": datetime.now().isoformat(timespec="seconds"), "session": self.id, **entry}
        try:
            with (self.dir / SESSION_LOG_FILE).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")
        except OSError as exc:
            # Never kill the server over an optional log, but never swallow it
            # either: stderr is safe on stdio transport, stdout is the protocol.
            print(f"slurp: session log write failed: {exc}", file=sys.stderr)
            return False
        return True

    def start(self, trackers: list) -> None:
        """The start event, and a snapshot of each project to diff at the end.

        The snapshot (stat and hash of every indexable file, ~50 ms on
        PrismaStats) is taken before the first message is read, so no edit made
        during the session can slip into it. Only when the log is on.
        """
        if not self.enabled:
            return
        from slurp.manifest import build_manifest

        for tracker in trackers:
            root = tracker.root or tracker._candidate_root()
            if root is not None and Path(root).is_dir():
                ignore = tracker.ignore_path or Path(root) / ".slurpignore"
                self._snapshots.append(build_manifest(Path(root), ignore))
        first = trackers[0]
        self.write({"event": "start", "graph": str(first.path),
                    "owner": "own" if first.refreshable else first.kind,
                    "graphs": len(trackers), "slurp_version": __version__})

    def call(self, entry: dict) -> None:
        self.calls += 1
        self.write({"event": "call", **entry})

    def end(self) -> None:
        """The end event: every file that changed since the session started."""
        if not self.enabled:
            return
        from slurp.manifest import ManifestChecker

        changed: list[str] = []
        many = len(self._snapshots) > 1
        for snapshot in self._snapshots:
            checker = ManifestChecker(snapshot)
            checker._last_walk_ns = 0          # a full walk: nothing may be missed here
            changes = checker.check()
            prefix = f"{snapshot.root.name}/" if many else ""
            changed.extend(prefix + f for f in changes.files)
        changed.sort()
        self.write({"event": "end", "calls": self.calls,
                    "changed": {"count": len(changed), "files": changed[:_END_FILES]}})


def call_entry(*, client: dict | None, protocol: str | None, tool: str, args: dict,
               ms: float, fresh=None, info: dict | None = None,
               error: str = "") -> dict:
    """One call line's fields (design §7)."""
    entry: dict = {"client": client, "protocol": protocol, "tool": tool, "args": args,
                   "ms": round(ms, 1)}
    if fresh is not None:
        entry["graph"] = fresh.status
        if fresh.refresh_ms is not None:
            entry["refresh_ms"] = round(fresh.refresh_ms, 1)
        if fresh.changed_files:
            entry["changed"] = {"count": len(fresh.changed_files),
                                "files": fresh.changed_files[:_CALL_FILES]}
        if fresh.warnings:
            entry["warned"] = True
    stats = (info or {}).get("stats")
    if stats:
        entry["nodes"] = stats.get("nodes_selected", 0)
        entry["tokens"] = stats.get("tokens_used", 0)
    if tool in ("slurp_query", "slurp_suggest") and "query" in args:
        # Kept at the top level, as v1 lines had it, for `slurp session`.
        entry["query"] = args["query"]
        entry["budget"] = args.get("budget")
    if error:
        entry["error"] = error
    return entry


# ---------------------------------------------------------------------------
# Summaries: slurp session --sessions
# ---------------------------------------------------------------------------

@dataclass
class SessionSummary:
    """What one server process saw, for `slurp session --sessions`."""

    session: str
    started: str
    client: str = ""
    duration_s: float = 0.0
    calls: Counter = field(default_factory=Counter)
    first_call_s: float | None = None
    changed_before_first_call: int = 0
    edited: int = 0
    edited_after_impact_or_explain: int = 0
    warned: int = 0
    p95_refresh_ms: float | None = None


def _ts(entry: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(entry.get("ts", "")))
    except ValueError:
        return None


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").removeprefix("./").strip("/")


def _names_file(call: dict, path: str) -> bool:
    """Whether a slurp_impact or slurp_explain call was about *path*."""
    args = call.get("args") or {}
    target = _norm(path)
    if call.get("tool") == "slurp_impact":
        asked = _norm(args.get("file_path", ""))
        return bool(asked) and (target == asked or target.endswith("/" + asked))
    if call.get("tool") == "slurp_explain":
        return _norm(args.get("file", "")) == target
    return False


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def summarize_sessions(entries: list[dict]) -> list[SessionSummary]:
    """One summary per server process, oldest first. Lines without a session are skipped.

    DECISION: "edited" is the end event's list — the files that differ from
    the snapshot taken when the session started. A call's changed files only
    date an edit (it happened since the previous call); they may also hold
    changes made before the session, which are no edits of the agent's. An
    edit counts as preceded by slurp_impact or slurp_explain when a call about
    that file came before the call whose answer first saw it changed.
    """
    sessions: dict[str, list[dict]] = {}
    for entry in entries:
        if entry.get("session"):
            sessions.setdefault(str(entry["session"]), []).append(entry)

    summaries = []
    for session_id, items in sessions.items():
        start = next((e for e in items if e.get("event") == "start"), None)
        end = next((e for e in items if e.get("event") == "end"), None)
        calls = [e for e in items if e.get("event") == "call"]
        first_ts = _ts(start or items[0])
        summary = SessionSummary(session=session_id,
                                 started=str((start or items[0]).get("ts", "")))
        client = next((c.get("client") for c in calls if c.get("client")), None)
        if isinstance(client, dict):
            summary.client = " ".join(str(client.get(k, "")) for k in ("name", "version")).strip()
        last = _ts(end or items[-1])
        if first_ts and last:
            summary.duration_s = (last - first_ts).total_seconds()
        summary.calls = Counter(str(c.get("tool")) for c in calls)
        if calls:
            first_call = _ts(calls[0])
            if first_ts and first_call:
                summary.first_call_s = (first_call - first_ts).total_seconds()
            summary.changed_before_first_call = (calls[0].get("changed") or {}).get("count", 0)
        summary.warned = sum(1 for c in calls if c.get("warned"))
        summary.p95_refresh_ms = _p95([float(c["refresh_ms"]) for c in calls
                                       if isinstance(c.get("refresh_ms"), (int, float))])
        edited = ((end or {}).get("changed") or {}).get("files") or []
        summary.edited = len(edited)
        for path in edited:
            seen_at = next((i for i, c in enumerate(calls)
                            if path in ((c.get("changed") or {}).get("files") or [])),
                           len(calls))
            if any(_names_file(c, path) for c in calls[:seen_at]):
                summary.edited_after_impact_or_explain += 1
        summaries.append(summary)
    return summaries
