"""Tests for slurp.graphfile — every graph slurp writes is replaced atomically (#84)."""

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from slurp import graphfile
from slurp.cli import cli
from slurp.graphfile import write_graph

_OLD = {"nodes": [{"id": "old", "label": "old"}], "links": [], "built_at_commit": None}
_NEW = {"nodes": [{"id": "new", "label": "néw"}], "links": [], "built_at_commit": "abc"}


def _leftovers(directory: Path, keep: str = "graph.json") -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name != keep)


class TestWriteGraph:
    def test_writes_the_same_bytes_as_before(self, tmp_path):
        # Same serialisation as the write_text it replaces: indent=2, ASCII escapes.
        out = tmp_path / "graph.json"
        write_graph(out, _NEW)
        assert out.read_text(encoding="utf-8") == json.dumps(_NEW, indent=2)

    def test_creates_missing_parent_directories(self, tmp_path):
        out = tmp_path / "graphify-out" / "nested" / "graph.json"
        write_graph(out, _NEW)
        assert json.loads(out.read_text(encoding="utf-8")) == _NEW

    def test_destination_holds_the_old_graph_until_the_replace(self, tmp_path, monkeypatch):
        # A concurrent reader (the MCP server) must see the old graph or the new
        # one, never a half-written file: nothing touches the destination before
        # the new file is complete and on disk.
        out = tmp_path / "graph.json"
        out.write_text(json.dumps(_OLD, indent=2), encoding="utf-8")
        seen: list[str] = []
        real_fsync = os.fsync

        def fsync(fd: int) -> None:
            seen.append(out.read_text(encoding="utf-8"))
            real_fsync(fd)

        monkeypatch.setattr(graphfile.os, "fsync", fsync)
        write_graph(out, _NEW)
        assert seen == [json.dumps(_OLD, indent=2)]
        assert json.loads(out.read_text(encoding="utf-8")) == _NEW

    def test_temporary_file_sits_next_to_the_graph(self, tmp_path, monkeypatch):
        # os.replace is only atomic within one filesystem.
        out = tmp_path / "graph.json"
        calls: list[tuple[str, str]] = []
        real_replace = os.replace

        def replace(src, dst):
            calls.append((str(src), str(dst)))
            real_replace(src, dst)

        monkeypatch.setattr(graphfile.os, "replace", replace)
        write_graph(out, _NEW)
        [(src, dst)] = calls
        assert Path(src).parent == tmp_path
        assert Path(dst) == out
        assert _leftovers(tmp_path) == []

    def test_a_failed_replace_keeps_the_old_graph_and_cleans_up(self, tmp_path, monkeypatch):
        out = tmp_path / "graph.json"
        out.write_text(json.dumps(_OLD, indent=2), encoding="utf-8")

        def replace(src, dst):
            raise OSError("disk on fire")

        monkeypatch.setattr(graphfile.os, "replace", replace)
        with pytest.raises(OSError, match="disk on fire"):
            write_graph(out, _NEW)
        assert json.loads(out.read_text(encoding="utf-8")) == _OLD
        assert _leftovers(tmp_path) == []

    def test_a_failed_write_keeps_the_old_graph_and_cleans_up(self, tmp_path, monkeypatch):
        out = tmp_path / "graph.json"
        out.write_text(json.dumps(_OLD, indent=2), encoding="utf-8")

        def fsync(fd: int) -> None:
            raise OSError("no space left on device")

        monkeypatch.setattr(graphfile.os, "fsync", fsync)
        with pytest.raises(OSError, match="no space"):
            write_graph(out, _NEW)
        assert json.loads(out.read_text(encoding="utf-8")) == _OLD
        assert _leftovers(tmp_path) == []

    def test_writes_through_a_symlink(self, tmp_path):
        # write_text followed a symlinked graph.json into its target; replacing
        # the link with a plain file would cut it off from the real graph.
        real = tmp_path / "real" / "graph.json"
        real.parent.mkdir()
        real.write_text(json.dumps(_OLD), encoding="utf-8")
        link = tmp_path / "graph.json"
        link.symlink_to(real)
        write_graph(link, _NEW)
        assert link.is_symlink()
        assert json.loads(real.read_text(encoding="utf-8")) == _NEW

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_keeps_the_permissions_of_the_graph_it_replaces(self, tmp_path):
        out = tmp_path / "graph.json"
        out.write_text("{}", encoding="utf-8")
        out.chmod(0o640)
        write_graph(out, _NEW)
        assert stat.S_IMODE(out.stat().st_mode) == 0o640

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_a_new_graph_gets_the_permissions_write_text_gave_it(self, tmp_path):
        # A temporary file from tempfile is 0600; the graph is shared with other
        # tools, so it keeps the umask's default like any file slurp writes.
        probe = tmp_path / "probe.json"
        probe.write_text("{}", encoding="utf-8")
        out = tmp_path / "graph.json"
        write_graph(out, _NEW)
        assert stat.S_IMODE(out.stat().st_mode) == stat.S_IMODE(probe.stat().st_mode)


class TestReplaceRetriesOnWindows:
    """os.replace fails while another process holds the graph open on Windows."""

    @staticmethod
    def _flaky_replace(failures: int):
        real_replace = os.replace
        state = {"calls": 0}

        def replace(src, dst):
            state["calls"] += 1
            if state["calls"] <= failures:
                raise PermissionError(13, "The process cannot access the file")
            real_replace(src, dst)

        return replace, state

    def test_retries_with_growing_waits(self, tmp_path, monkeypatch):
        replace, state = self._flaky_replace(failures=3)
        sleeps: list[float] = []
        monkeypatch.setattr(graphfile, "_IS_WINDOWS", True)
        monkeypatch.setattr(graphfile.os, "replace", replace)
        monkeypatch.setattr(graphfile.time, "sleep", sleeps.append)
        out = tmp_path / "graph.json"
        write_graph(out, _NEW)
        assert state["calls"] == 4
        assert len(sleeps) == 3
        assert sleeps == sorted(sleeps) and sleeps[0] < sleeps[-1]
        assert json.loads(out.read_text(encoding="utf-8")) == _NEW

    def test_gives_up_with_the_error_and_cleans_up(self, tmp_path, monkeypatch):
        replace, _ = self._flaky_replace(failures=10_000)
        sleeps: list[float] = []
        monkeypatch.setattr(graphfile, "_IS_WINDOWS", True)
        monkeypatch.setattr(graphfile.os, "replace", replace)
        monkeypatch.setattr(graphfile.time, "sleep", sleeps.append)
        out = tmp_path / "graph.json"
        with pytest.raises(PermissionError):
            write_graph(out, _NEW)
        assert 0 < sum(sleeps) <= 5.0
        assert _leftovers(tmp_path, keep="") == []

    def test_no_retry_outside_windows(self, tmp_path, monkeypatch):
        # Elsewhere a PermissionError is a real permission problem: report it now.
        replace, state = self._flaky_replace(failures=1)
        monkeypatch.setattr(graphfile, "_IS_WINDOWS", False)
        monkeypatch.setattr(graphfile.os, "replace", replace)
        with pytest.raises(PermissionError):
            write_graph(tmp_path / "graph.json", _NEW)
        assert state["calls"] == 1


# ---------------------------------------------------------------------------
# Every writer of the graph goes through write_graph
# ---------------------------------------------------------------------------

@pytest.fixture
def replaced(monkeypatch) -> list[Path]:
    """Destinations os.replace was called with, in order."""
    calls: list[Path] = []
    real_replace = os.replace

    def replace(src, dst):
        calls.append(Path(dst))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    return calls


def _project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    return root


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


class TestEveryWriterIsAtomic:
    def test_index(self, tmp_path, replaced):
        root = _project(tmp_path / "p")
        out = tmp_path / "graph.json"
        res = CliRunner().invoke(cli, ["index", str(root), "-o", str(out),
                                       "--ignore-file", "/dev/null"])
        assert res.exit_code == 0, res.output
        assert replaced == [out.resolve()]

    def test_init(self, tmp_path, monkeypatch, replaced):
        root = _project(tmp_path / "p")
        monkeypatch.chdir(root)
        res = CliRunner().invoke(cli, ["init", "--yes"])
        assert res.exit_code == 0, res.output
        assert (root / "graphify-out" / "graph.json").resolve() in replaced

    def test_smart_fast_path(self, tmp_path, replaced):
        root = _project(tmp_path / "p")
        _git(root, "init", "-q")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "init")
        out = tmp_path / "graph.json"
        args = ["index", str(root), "-o", str(out), "--ignore-file", "/dev/null"]
        assert CliRunner().invoke(cli, args).exit_code == 0
        replaced.clear()
        (root / "app.py").write_text("def main():\n    return 2\n", encoding="utf-8")
        res = CliRunner().invoke(cli, [*args, "--smart"])
        assert res.exit_code == 0, res.output
        assert "Full index needed" not in res.output
        assert replaced == [out.resolve()]

    def test_watch(self, tmp_path, monkeypatch, replaced):
        # --watch re-indexes on every change event; each re-index is a write.
        import watchdog.observers

        root = _project(tmp_path / "p")
        out = tmp_path / "graph.json"

        class _Event:
            is_directory = False
            src_path = str(root / "app.py")

        class _FakeObserver:
            def schedule(self, handler, path, recursive):
                self.handler = handler

            def start(self):
                self.handler.on_modified(_Event())

            def stop(self):
                pass

            def join(self):
                pass

        def interrupt(_seconds):
            raise KeyboardInterrupt

        monkeypatch.setattr(watchdog.observers, "Observer", _FakeObserver)
        monkeypatch.setattr(time, "sleep", interrupt)
        res = CliRunner().invoke(cli, ["index", str(root), "-o", str(out), "--watch",
                                       "--ignore-file", "/dev/null"])
        assert res.exit_code == 0, res.output
        assert "re-indexing" in res.output
        assert replaced == [out.resolve(), out.resolve()]
