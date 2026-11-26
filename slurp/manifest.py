"""The manifest: what a graph was built from, kept inside graph.json (v1.3, #76).

Every graph slurp writes carries a top-level ``"slurp"`` key recording the
files the index walked (not the ``source_file`` of its nodes), each with its
stat and a content hash, the mtime of every directory walked, the ignore file
and the parsers used. Telling whether the graph is still current is then a
matter of stat calls — 0.4 ms on PrismaStats (185 files, 161 directories) —
and a file is read only when its stat no longer matches.

Stdlib only. The walk and its skip rules are the indexer's
(slurp.indexer.walk_source_tree), so the manifest and the index agree on what
the project holds.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from slurp import __version__
from slurp.indexer import list_source_dir, parser_for_extension, walk_source_tree

MANIFEST_KEY = "slurp"
FORMAT = 1

# DECISION: a file whose mtime falls within 2 s of the build's start is always
# verified by hash. mtime granularity is 1 s on HFS+ and 2 s on FAT, so an edit
# in the same tick as the stat the manifest recorded leaves the stat unchanged
# — git calls such entries "racily clean" and does the same.
RACY_WINDOW_NS = 2_000_000_000

# DECISION: a full walk at least every 30 s, for filesystems that do not update
# a directory's mtime when an entry is added (some network and Docker mounts).
# Elsewhere it changes nothing; there, it bounds how late a new file is seen.
FULL_WALK_INTERVAL_NS = 30_000_000_000

_HASH_BYTES = 16
_READ_CHUNK = 1 << 20


class ManifestError(ValueError):
    """A manifest this version of slurp cannot read."""


def content_hash(path: Path) -> str:
    """blake2b-128 of a file's bytes, in hex."""
    digest = hashlib.blake2b(digest_size=_HASH_BYTES)
    with open(path, "rb") as fh:
        while chunk := fh.read(_READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def file_entry(path: Path) -> list:
    """[mtime_ns, size, inode, hash] — stat taken before the file is read.

    DECISION: stat first, hash second, parse last. A file edited between the
    stat and the read is recorded with an older stat than its content, so the
    next check re-hashes it; one edited after the read has a hash that no
    longer matches. Either way the change surfaces at the next check, never
    lost.
    """
    st = os.stat(path)
    return [st.st_mtime_ns, st.st_size, st.st_ino, content_hash(path)]


def ignore_sha(path: Path) -> str | None:
    """Hash of the ignore file, or None when there is none to read."""
    try:
        return content_hash(path) if path.is_file() else None
    except OSError:
        return None


def _parent(key: str) -> str:
    return key.rsplit("/", 1)[0] if "/" in key else ""


def _child(rel: str, name: str) -> str:
    return f"{rel}/{name}" if rel else name


def _under(key: str, rel: str) -> bool:
    return not rel or key == rel or key.startswith(rel + "/")


@dataclass
class Manifest:
    """What one graph was built from. Paths in memory are absolute."""

    root: Path
    started_ns: int
    slurp_version: str
    ignore_path: Path
    ignore_sha: str | None
    parsers: dict[str, str] = field(default_factory=dict)
    dirs: dict[str, int] = field(default_factory=dict)
    files: dict[str, list] = field(default_factory=dict)

    def to_dict(self, graph_path: Path) -> dict:
        """The ``"slurp"`` value written into graph.json at *graph_path*.

        DECISION: the root is stored relative to the graph's own directory, so
        a graph works whatever `-o` it was written to and whatever directory a
        client launches the server from. A root on another Windows volume
        cannot be made relative and is stored absolute. The ignore file is
        stored relative to the root when it lives inside it.
        """
        graph_dir = Path(os.path.realpath(graph_path)).parent
        try:
            root = Path(os.path.relpath(self.root, graph_dir)).as_posix()
        except ValueError:
            root = str(self.root)
        try:
            ignore = self.ignore_path.relative_to(self.root).as_posix()
        except ValueError:
            ignore = str(self.ignore_path)
        return {
            "format": FORMAT,
            "slurp_version": self.slurp_version,
            "root": root,
            "started_ns": self.started_ns,
            "ignore": {"path": ignore, "sha": self.ignore_sha},
            "parsers": dict(sorted(self.parsers.items())),
            "dirs": dict(sorted(self.dirs.items())),
            "files": {k: list(v) for k, v in sorted(self.files.items())},
        }

    @classmethod
    def from_dict(cls, data: object, graph_path: Path) -> "Manifest":
        """Read the ``"slurp"`` value of the graph at *graph_path*.

        Raises:
            ManifestError: Another format, or a field missing or malformed.
        """
        if not isinstance(data, dict):
            raise ManifestError("the manifest is not an object")
        if data.get("format") != FORMAT:
            raise ManifestError(f"manifest format {data.get('format')!r} "
                                f"(this slurp reads format {FORMAT})")
        try:
            graph_dir = Path(os.path.realpath(graph_path)).parent
            root = (graph_dir / data["root"]).resolve()
            ignore = data["ignore"]
            ignore_path = Path(ignore["path"])
            if not ignore_path.is_absolute():
                ignore_path = root / ignore_path
            return cls(
                root=root,
                started_ns=int(data["started_ns"]),
                slurp_version=str(data["slurp_version"]),
                ignore_path=ignore_path,
                ignore_sha=ignore["sha"],
                parsers={str(k): str(v) for k, v in data["parsers"].items()},
                dirs={str(k): int(v) for k, v in data["dirs"].items()},
                files={str(k): list(v) for k, v in data["files"].items()},
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ManifestError(f"malformed manifest: {exc}") from None

    def copy(self) -> "Manifest":
        """An independent copy, entries included."""
        return Manifest(self.root, self.started_ns, self.slurp_version, self.ignore_path,
                        self.ignore_sha, dict(self.parsers), dict(self.dirs),
                        {k: list(v) for k, v in self.files.items()})


def parsers_for(keys: object) -> dict[str, str]:
    """{extension without the dot: parser} for the extensions among file keys."""
    exts = {Path(str(k)).suffix.lower() for k in keys}  # type: ignore[union-attr]
    return {ext[1:]: parser_for_extension(ext) for ext in sorted(exts) if ext}


def build_manifest(
    root: Path,
    ignore_path: Path,
    started_ns: int | None = None,
    walk: tuple[dict[str, int], list[Path]] | None = None,
) -> Manifest:
    """Walk *root*, stat and hash every indexable file, and record it all.

    Args:
        root: Project root.
        ignore_path: The ignore file the graph is (about to be) built with.
        started_ns: When the build started (time.time_ns()); default now.
        walk: An existing walk_source_tree(root) result, so the index and the
            manifest cover the same files.

    Returns:
        The manifest. Hashing comes before the index parses anything, as
        file_entry explains.
    """
    started = time.time_ns() if started_ns is None else started_ns
    root = root.resolve()
    dirs, files = walk if walk is not None else walk_source_tree(root)
    entries: dict[str, list] = {}
    for path in files:
        try:
            entries[path.relative_to(root).as_posix()] = file_entry(path)
        except OSError:
            continue  # vanished mid-walk: the next check sees the directory change
    ignore_path = Path(os.path.abspath(ignore_path))
    return Manifest(root=root, started_ns=started, slurp_version=__version__,
                    ignore_path=ignore_path, ignore_sha=ignore_sha(ignore_path),
                    parsers=parsers_for(entries), dirs=dict(dirs), files=entries)


@dataclass
class Changes:
    """What differs between a manifest and the disk."""

    modified: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    #: Set when only a full index will do (ignore file, version, parsers).
    reason: str = ""
    #: (mtime_ns, size) of each changed file when it was seen, for signature().
    stamps: dict[str, tuple] = field(default_factory=dict)

    @property
    def any(self) -> bool:
        return bool(self.reason or self.modified or self.added or self.deleted)

    @property
    def needs_full(self) -> bool:
        """A file added or removed, or a full-index reason: the fast path cannot run."""
        return bool(self.reason or self.added or self.deleted)

    @property
    def files(self) -> list[str]:
        return sorted({*self.modified, *self.added, *self.deleted})

    def signature(self) -> tuple:
        """Identifies this state of the disk: a failed refresh is not retried until it moves."""
        return (self.reason, tuple((f, self.stamps.get(f)) for f in self.files))

    def describe(self, limit: int = 2) -> str:
        """`3 files changed since this graph was built (lib/a.ts, app/b.tsx, +1)`."""
        files = self.files
        if not files:
            return self.reason
        shown = ", ".join(files[:limit]) + (f", +{len(files) - limit}" if len(files) > limit else "")
        text = (f"{len(files)} file{'s' if len(files) != 1 else ''} changed since this "
                f"graph was built ({shown})")
        return f"{self.reason}; {text}" if self.reason else text


class ManifestChecker:
    """Compares a manifest with the disk, cheaply, as often as asked.

    The checker owns its manifest in memory: an entry whose stat moved while
    its content did not (a touch, a checkout there and back) is updated so it
    is not hashed again, and a directory whose mtime moved without changing
    the set of indexable files (a new node_modules/) is updated so it is not
    listed again. Neither is a reason to rewrite the graph.
    """

    def __init__(self, manifest: Manifest) -> None:
        self.manifest = manifest
        self._ignore_stat: tuple | None = None
        self._last_walk_ns = manifest.started_ns
        # DECISION: when each entry was last verified (hashed, or listed). An
        # entry is in the racy window while its mtime is within 2 s of that
        # moment — the build's start until it is verified again. Without this, a
        # directory touched just before a build (the graph's own graphify-out/,
        # the project root) was listed on every call until the next build: 2.7
        # ms a call instead of 0.5 on PrismaStats.
        self._seen_dirs: dict[str, int] = {}
        self._seen_files: dict[str, int] = {}

    def check(self) -> Changes:
        """What changed on disk since the manifest was written (or last checked clean)."""
        m = self.manifest
        if m.slurp_version != __version__:
            return Changes(reason=f"the graph was built by slurp {m.slurp_version}")
        if not m.root.is_dir():
            return Changes(reason=f"the project root {m.root} is gone")
        for ext, parser in sorted(m.parsers.items()):
            now = parser_for_extension(f".{ext}")
            if now and now != parser:
                return Changes(reason=f"the .{ext} parser changed ({parser} → {now})")
        if self._ignore_changed():
            return Changes(reason=f"{m.ignore_path.name} changed, which can add or remove "
                                  "nodes in any file")

        now_ns = time.time_ns()
        if now_ns - self._last_walk_ns >= FULL_WALK_INTERVAL_NS:
            added, deleted = self._full_walk()
            self._last_walk_ns = now_ns
        else:
            added, deleted = self._rescan_dirs()

        changes = Changes(added=sorted(added), deleted=sorted(deleted))
        # Plain strings, not Path objects: building ~200 Paths a call cost three
        # times the stat calls themselves on PrismaStats.
        base = str(m.root) + os.sep
        for key, entry in m.files.items():
            if key in deleted:
                continue
            path = base + key
            seen = time.time_ns()
            try:
                st = os.stat(path)
            except OSError:
                changes.deleted.append(key)
                continue
            stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
            if stamp == tuple(entry[:3]) and \
                    entry[0] < self._seen_files.get(key, m.started_ns) - RACY_WINDOW_NS:
                continue
            try:
                digest = content_hash(path)
            except OSError:
                changes.deleted.append(key)
                continue
            if digest != entry[3]:
                changes.modified.append(key)
                changes.stamps[key] = stamp[:2]
            else:
                entry[0:3] = list(stamp)
                self._seen_files[key] = seen
        changes.modified.sort()
        changes.deleted.sort()
        for key in changes.added:
            try:
                st = os.stat(m.root / key)
                changes.stamps[key] = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass
        return changes

    # --- pieces ----------------------------------------------------------

    def _ignore_changed(self) -> bool:
        m = self.manifest
        try:
            st = os.stat(m.ignore_path)
            stamp: tuple = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            stamp = ("missing",)
        if stamp == self._ignore_stat:
            return False
        if ignore_sha(m.ignore_path) != m.ignore_sha:
            return True
        self._ignore_stat = stamp
        return False

    def _full_walk(self) -> tuple[set[str], set[str]]:
        m = self.manifest
        dirs, files = walk_source_tree(m.root)
        found = {p.relative_to(m.root).as_posix() for p in files}
        added, deleted = found - set(m.files), set(m.files) - found
        if not added and not deleted:
            m.dirs = dict(dirs)
        return added, deleted

    def _rescan_dirs(self) -> tuple[set[str], set[str]]:
        """List again only the directories whose mtime moved (or sits in the racy window).

        DECISION: the listing decides. A name in the manifest that the listing
        does not show is deleted, even if stat() of it still succeeds — that is
        a case-only rename on a case-insensitive filesystem.
        """
        m = self.manifest
        added: set[str] = set()
        deleted: set[str] = set()
        removed: list[str] = []
        by_parent: dict[str, set[str]] | None = None
        root = str(m.root)
        for rel in sorted(m.dirs):
            if removed and any(_under(rel, gone) for gone in removed):
                continue
            recorded = m.dirs[rel]
            try:
                now = os.stat(root + os.sep + rel if rel else root).st_mtime_ns
            except OSError:
                now = None
            if now == recorded and \
                    recorded < self._seen_dirs.get(rel, m.started_ns) - RACY_WINDOW_NS:
                continue
            seen = time.time_ns()
            listed = list_source_dir(m.root, rel)
            if listed is None:
                removed.append(rel)
                continue
            mtime, subdirs, files = listed
            if by_parent is None:
                by_parent = {}
                for key in m.files:
                    by_parent.setdefault(_parent(key), set()).add(key)
            here = {p.relative_to(m.root).as_posix() for p in files}
            known = by_parent.get(rel, set())
            new_here, gone_here = here - known, known - here
            new_dirs: dict[str, int] = {}
            for name in subdirs:
                child = _child(rel, name)
                if child in m.dirs:
                    continue
                sub_dirs, sub_files = walk_source_tree(m.root, child)
                new_dirs.update(sub_dirs)
                new_here |= {p.relative_to(m.root).as_posix() for p in sub_files}
            listed_children = {_child(rel, name) for name in subdirs}
            for child in m.dirs:
                if child and _parent(child) == rel and child not in listed_children:
                    removed.append(child)
            added |= new_here
            deleted |= gone_here
            if not new_here and not gone_here:
                m.dirs[rel] = mtime
                m.dirs.update(new_dirs)
                self._seen_dirs[rel] = seen
        for gone in removed:
            under = {k for k in m.files if _under(k, gone)}
            deleted |= under
            if not under:
                for d in [d for d in m.dirs if _under(d, gone)]:
                    del m.dirs[d]
        return added, deleted
