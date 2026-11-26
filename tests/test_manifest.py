"""Tests for slurp.manifest — the record of what a graph was built from (v1.3, #76)."""

import json
import os
import time
from pathlib import Path

import pytest

from slurp import __version__
from slurp.manifest import (
    Changes,
    Manifest,
    ManifestChecker,
    ManifestError,
    build_manifest,
    content_hash,
)


def _put(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    _put(root, "app.py", "def main():\n    return 1\n")
    _put(root, "lib/util.py", "def helper():\n    return 2\n")
    _put(root, "web/page.ts", "export function page(): number { return 1 }\n")
    _put(root, "node_modules/pkg/index.js", "function x() {}\n")
    _put(root, "README.md", "# proj\n")
    return root


def _age(root: Path, seconds: float = 60) -> None:
    """Push every mtime back, out of the racy window of a manifest built now."""
    past = time.time() - seconds
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + dirnames:
            os.utime(Path(dirpath) / name, (past, past), follow_symlinks=False)
        os.utime(dirpath, (past, past))


def _manifest(root: Path, ignore: Path | None = None) -> Manifest:
    return build_manifest(root, ignore or root / ".slurpignore")


def _checker(root: Path, ignore: Path | None = None) -> ManifestChecker:
    _age(root)
    return ManifestChecker(_manifest(root, ignore))


class TestBuild:
    def test_records_the_discovered_files_and_walked_dirs(self, project):
        m = _manifest(project)
        assert set(m.files) == {"app.py", "lib/util.py", "web/page.ts"}
        assert set(m.dirs) == {"", "lib", "web"}

    def test_a_file_entry_is_stat_then_content_hash(self, project):
        m = _manifest(project)
        st = os.stat(project / "app.py")
        assert m.files["app.py"] == [st.st_mtime_ns, st.st_size, st.st_ino,
                                     content_hash(project / "app.py")]

    def test_a_file_slurpignore_excludes_whole_is_still_recorded(self, project):
        _put(project, ".slurpignore", "file:lib/util.py\n")
        assert "lib/util.py" in _manifest(project).files

    def test_records_the_ignore_file_even_when_missing(self, project):
        m = _manifest(project)
        assert m.ignore_path == (project / ".slurpignore").resolve()
        assert m.ignore_sha is None

    def test_records_the_parser_of_each_extension(self, project):
        m = _manifest(project)
        assert m.parsers["py"] == "ast"
        assert m.parsers["ts"] in ("tree-sitter", "regex fallback")

    def test_round_trips_through_the_graph_relative_to_its_directory(self, project):
        graph = project / "graphify-out" / "graph.json"
        m = _manifest(project)
        data = m.to_dict(graph)
        assert data["format"] == 1
        assert data["slurp_version"] == __version__
        assert data["root"] == ".."
        assert data["ignore"] == {"path": ".slurpignore", "sha": None}
        back = Manifest.from_dict(json.loads(json.dumps(data)), graph)
        assert back.root == project.resolve()
        assert back.files == m.files and back.dirs == m.dirs
        assert back.started_ns == m.started_ns

    def test_an_ignore_file_outside_the_root_is_kept_absolute(self, project, tmp_path):
        rules = _put(tmp_path, "rules/ignore", "")
        data = _manifest(project, rules).to_dict(project / "graph.json")
        assert data["ignore"]["path"] == str(rules.resolve())

    def test_a_newer_format_is_refused(self, project):
        data = _manifest(project).to_dict(project / "graph.json")
        data["format"] = 2
        with pytest.raises(ManifestError):
            Manifest.from_dict(data, project / "graph.json")


class TestCheckFindsEveryKindOfChange:
    def test_nothing_changed(self, project):
        assert not _checker(project).check().any

    def test_an_edit(self, project):
        checker = _checker(project)
        _put(project, "app.py", "def main():\n    return 2\n")
        assert checker.check().modified == ["app.py"]

    def test_a_touch_is_not_a_change(self, project):
        checker = _checker(project)
        os.utime(project / "app.py")
        assert not checker.check().any

    def test_same_content_rewritten_is_not_a_change(self, project):
        checker = _checker(project)
        _put(project, "lib/util.py", "def helper():\n    return 2\n")
        assert not checker.check().any

    def test_a_verified_touch_is_not_hashed_again(self, project, monkeypatch):
        from slurp import manifest

        checker = _checker(project)
        past = time.time() - 30
        os.utime(project / "app.py", (past, past))
        checker.check()
        hashed: list[Path] = []
        monkeypatch.setattr(manifest, "content_hash",
                            lambda p: (hashed.append(p), "x")[1])
        assert not checker.check().any
        assert hashed == []

    def test_an_edit_inside_the_same_mtime_is_caught_by_the_racy_window(self, project):
        # Same size, mtime put back where it was: only the hash can tell.
        m = _manifest(project)  # built now: every file is inside the window
        checker = ManifestChecker(m)
        path = project / "app.py"
        st = os.stat(path)
        path.write_text("def main():\n    return 9\n", encoding="utf-8")
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        assert os.stat(path).st_size == st.st_size
        assert checker.check().modified == ["app.py"]

    def test_outside_the_racy_window_an_unchanged_stat_is_trusted(self, project, monkeypatch):
        from slurp import manifest

        checker = _checker(project)
        hashed: list[Path] = []
        monkeypatch.setattr(manifest, "content_hash",
                            lambda p: (hashed.append(p), "x")[1])
        assert not checker.check().any
        assert hashed == []

    def test_a_new_file(self, project):
        checker = _checker(project)
        _put(project, "lib/extra.py", "def extra():\n    pass\n")
        assert checker.check().added == ["lib/extra.py"]

    def test_a_new_directory(self, project):
        checker = _checker(project)
        _put(project, "api/v1/routes.py", "def route():\n    pass\n")
        assert checker.check().added == ["api/v1/routes.py"]

    def test_a_deleted_file(self, project):
        checker = _checker(project)
        (project / "lib" / "util.py").unlink()
        assert checker.check().deleted == ["lib/util.py"]

    def test_a_deleted_directory(self, project):
        import shutil

        checker = _checker(project)
        shutil.rmtree(project / "web")
        assert checker.check().deleted == ["web/page.ts"]

    def test_a_rename(self, project):
        checker = _checker(project)
        (project / "lib" / "util.py").rename(project / "lib" / "helpers.py")
        changes = checker.check()
        assert (changes.added, changes.deleted) == (["lib/helpers.py"], ["lib/util.py"])

    def test_a_case_only_rename(self, project):
        # On a case-insensitive filesystem stat("lib/util.py") still succeeds
        # after the rename: the listing decides.
        checker = _checker(project)
        tmp = project / "lib" / "tmp.py"
        (project / "lib" / "util.py").rename(tmp)
        tmp.rename(project / "lib" / "Util.py")
        changes = checker.check()
        assert (changes.added, changes.deleted) == (["lib/Util.py"], ["lib/util.py"])

    def test_a_file_in_a_skipped_directory_is_never_a_change(self, project):
        checker = _checker(project)
        _put(project, "node_modules/pkg/new.js", "function y() {}\n")
        _put(project, ".next/server/page.js", "function z() {}\n")
        assert not checker.check().any

    def test_a_new_skipped_directory_costs_a_listing_not_a_change(self, project):
        checker = _checker(project)
        _put(project, "dist/bundle.js", "function b() {}\n")   # bumps the root's mtime
        assert not checker.check().any
        assert not checker.check().any

    def test_a_non_indexable_file_is_not_a_change(self, project):
        checker = _checker(project)
        _put(project, "notes.md", "hello\n")
        assert not checker.check().any

    def test_a_file_slurpignore_excludes_is_never_added_again(self, project):
        _put(project, ".slurpignore", "file:lib/util.py\n")
        checker = _checker(project)
        assert not checker.check().any


class TestCheckAsksForAFullIndex:
    def test_when_slurpignore_changes(self, project):
        _put(project, ".slurpignore", "file:a.py\n")
        checker = _checker(project)
        _put(project, ".slurpignore", "file:lib/*\n")
        assert ".slurpignore" in checker.check().reason

    def test_when_slurpignore_appears(self, project):
        checker = _checker(project)
        _put(project, ".slurpignore", "type:document\n")
        assert checker.check().reason

    def test_when_slurpignore_disappears(self, project):
        _put(project, ".slurpignore", "type:document\n")
        checker = _checker(project)
        (project / ".slurpignore").unlink()
        assert checker.check().reason

    def test_when_the_ignore_file_is_only_touched_nothing_happens(self, project):
        _put(project, ".slurpignore", "type:document\n")
        checker = _checker(project)
        os.utime(project / ".slurpignore")
        assert not checker.check().any

    def test_when_another_slurp_version_built_it(self, project):
        m = _manifest(project)
        m.slurp_version = "1.2.9"
        assert "1.2.9" in ManifestChecker(m).check().reason

    def test_when_a_parser_became_available(self, project, monkeypatch):
        from slurp import indexer

        m = _manifest(project)
        m.parsers["ts"] = "regex fallback"
        monkeypatch.setattr(indexer, "_TREE_SITTER_AVAILABLE", True)
        assert "ts" in ManifestChecker(m).check().reason

    def test_full_index_reasons_beat_file_lists(self, project):
        checker = _checker(project)
        _put(project, ".slurpignore", "type:x\n")
        assert Changes(reason="r").needs_full
        assert checker.check().needs_full


class TestSafetyNet:
    def test_a_filesystem_that_never_updates_dir_mtimes_is_caught_within_30_s(
            self, project, monkeypatch):
        from slurp import manifest

        checker = _checker(project)
        before = os.stat(project / "lib").st_mtime_ns
        _put(project, "lib/sneaky.py", "def s():\n    pass\n")
        os.utime(project / "lib", ns=(before, before))   # as if mtime never moved
        assert not checker.check().any
        now = time.time_ns()
        monkeypatch.setattr(manifest.time, "time_ns", lambda: now + 31_000_000_000)
        assert checker.check().added == ["lib/sneaky.py"]

    def test_a_manifest_older_than_30_s_gets_a_full_walk_on_its_first_check(self, project):
        m = _manifest(project)
        m.started_ns -= 60_000_000_000
        _age(project)
        before = os.stat(project / "lib").st_mtime_ns
        _put(project, "lib/sneaky.py", "def s():\n    pass\n")
        os.utime(project / "lib", ns=(before, before))
        assert ManifestChecker(m).check().added == ["lib/sneaky.py"]


class TestDescribe:
    def test_names_two_files_and_counts_the_rest(self):
        changes = Changes(modified=["lib/a.ts"], added=["app/b.tsx", "z.py"])
        assert changes.describe() == ("3 files changed since this graph was built "
                                      "(app/b.tsx, lib/a.ts, +1)")

    def test_one_file(self):
        assert Changes(deleted=["a.py"]).describe() == (
            "1 file changed since this graph was built (a.py)")

    def test_a_reason_alone(self):
        assert Changes(reason=".slurpignore changed").describe() == ".slurpignore changed"

    def test_the_signature_tells_two_states_apart(self):
        assert Changes(modified=["a"]).signature() != Changes(modified=["b"]).signature()


class TestSteadyStateCost:
    """Recently touched files and directories are verified once, not on every call."""

    def test_a_racy_file_is_hashed_until_it_is_two_seconds_old_then_trusted(
            self, project, monkeypatch):
        from slurp import manifest

        m = _manifest(project)              # built now: every file sits in the window
        checker = ManifestChecker(m)
        hashed: list = []
        real = manifest.content_hash
        monkeypatch.setattr(manifest, "content_hash",
                            lambda p: (hashed.append(p), real(p))[1])
        checker.check()
        assert hashed                       # in the window: verified by hash
        later = time.time_ns() + 5_000_000_000
        monkeypatch.setattr(manifest.time, "time_ns", lambda: later)
        checker.check()                     # verified again, now well past the mtime…
        hashed.clear()
        checker.check()
        assert hashed == []                 # …so the stat is trusted from here on

    def test_a_racy_directory_is_listed_until_it_is_two_seconds_old(self, project, monkeypatch):
        from slurp import manifest

        m = _manifest(project)
        checker = ManifestChecker(m)
        listed: list = []
        real = manifest.list_source_dir
        monkeypatch.setattr(manifest, "list_source_dir",
                            lambda root, rel: (listed.append(rel), real(root, rel))[1])
        checker.check()
        assert listed
        later = time.time_ns() + 5_000_000_000
        monkeypatch.setattr(manifest.time, "time_ns", lambda: later)
        checker.check()
        listed.clear()
        checker.check()
        assert listed == []

    def test_an_edit_after_the_trust_is_still_seen(self, project, monkeypatch):
        from slurp import manifest

        m = _manifest(project)
        checker = ManifestChecker(m)
        later = time.time_ns() + 5_000_000_000
        monkeypatch.setattr(manifest.time, "time_ns", lambda: later)
        checker.check()
        checker.check()
        _put(project, "lib/util.py", "def helper():\n    return 3\n")
        _put(project, "lib/new.py", "def new():\n    pass\n")
        changes = checker.check()
        assert changes.modified == ["lib/util.py"] and changes.added == ["lib/new.py"]
