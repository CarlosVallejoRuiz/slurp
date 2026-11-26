"""slurp index --watch: a poll over the manifest, debounced, atomic (v1.3, design §4)."""

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from slurp.cli import cli

REPO = Path(__file__).resolve().parent.parent


def _put(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    _put(root, "app.py", "from lib.util import helper\n\n\ndef main():\n    return helper()\n")
    _put(root, "lib/util.py", "def helper():\n    return 1\n")
    return root


def _watch(project: Path, steps: list, monkeypatch, *extra: str):
    """Run `slurp index --watch`; each poll runs the next step, then Ctrl+C."""
    out = project / "graphify-out" / "graph.json"
    queue = iter(steps + [None])
    polls = []

    def poll(seconds):
        polls.append(seconds)
        step = next(queue)
        if step is None:
            raise KeyboardInterrupt
        step()

    monkeypatch.setattr("slurp.cli._watch_sleep", poll)
    res = CliRunner().invoke(cli, ["index", str(project), "--watch", "--ignore-file",
                                   str(project / ".slurpignore"), *extra])
    assert res.exit_code == 0, res.output
    return res, out, polls


def _ids(out: Path) -> set[str]:
    return {n["id"] for n in json.loads(out.read_text(encoding="utf-8"))["nodes"]}


def _refresh_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("↻")]


QUIET = lambda: None  # noqa: E731 — a poll in which nothing happens


class TestDebounce:
    def test_polls_every_second(self, project, monkeypatch):
        _res, _out, polls = _watch(project, [QUIET, QUIET], monkeypatch)
        assert polls and set(polls) == {1.0}

    def test_refreshes_after_one_quiet_second(self, project, monkeypatch):
        steps = [lambda: _put(project, "lib/util.py",
                              "def helper():\n    return 1\n\n\ndef extra():\n    return 2\n"),
                 QUIET]
        res, out, _ = _watch(project, steps, monkeypatch)
        assert len(_refresh_lines(res.output)) == 1
        assert "lib.util.extra" in _ids(out)

    def test_waits_while_a_file_keeps_changing(self, project, monkeypatch):
        def edits(start: int) -> list:
            return [lambda i=i: _put(project, "lib/util.py", f"def helper():\n    return {i}\n")
                    for i in range(start, start + 4)]

        res, _out, _ = _watch(project, edits(10), monkeypatch)
        assert _refresh_lines(res.output) == []      # never a quiet second
        res, _out, _ = _watch(project, [*edits(20), QUIET], monkeypatch)
        assert len(_refresh_lines(res.output)) == 1

    def test_a_checkout_of_many_files_is_one_refresh(self, project, monkeypatch):
        def checkout():
            for i in range(30):
                _put(project, f"pkg/m{i}.py", f"def f{i}():\n    return {i}\n")
        res, out, _ = _watch(project, [checkout, QUIET], monkeypatch)
        lines = _refresh_lines(res.output)
        assert len(lines) == 1 and "30 files" in lines[0]
        assert "pkg.m29.f29" in _ids(out)


class TestEveryKindOfChange:
    def test_new_deleted_renamed_and_slurpignore(self, project, monkeypatch):
        steps = [
            lambda: _put(project, "lib/new.py", "def new():\n    return 1\n"), QUIET,
            lambda: (project / "lib" / "new.py").rename(project / "lib" / "renamed.py"), QUIET,
            lambda: (project / "lib" / "renamed.py").unlink(), QUIET,
            lambda: _put(project, ".slurpignore", "file:lib/*\n"), QUIET,
        ]
        res, out, _ = _watch(project, steps, monkeypatch)
        lines = _refresh_lines(res.output)
        assert len(lines) == 4, res.output
        assert "full index" in lines[3] and ".slurpignore" in lines[3]
        assert not any(i.startswith("lib.") for i in _ids(out))

    def test_a_body_edit_takes_the_fast_path(self, project, monkeypatch):
        steps = [lambda: _put(project, "lib/util.py", "def helper():\n    return 42\n"), QUIET]
        res, _out, _ = _watch(project, steps, monkeypatch)
        [line] = _refresh_lines(res.output)
        assert re.match(r"↻ 1 file changed \(lib/util\.py\) — smart update in \d+\.\ds$", line)

    def test_skipped_directories_never_trigger(self, project, monkeypatch):
        steps = [lambda: _put(project, "node_modules/pkg/index.js", "function x() {}\n"),
                 QUIET,
                 lambda: _put(project, ".next/server/page.js", "function y() {}\n"),
                 QUIET]
        res, _out, _ = _watch(project, steps, monkeypatch)
        assert _refresh_lines(res.output) == []

    def test_stops_on_ctrl_c(self, project, monkeypatch):
        res, _out, _ = _watch(project, [], monkeypatch)
        assert "Watching" in res.output and "Stopped." in res.output


class TestNoWatchdog:
    def test_slurp_never_imports_watchdog(self):
        offenders = [p.name for p in (REPO / "slurp").glob("*.py")
                     if re.search(r"^\s*(import|from)\s+watchdog", p.read_text(encoding="utf-8"),
                                  re.M)]
        assert offenders == []

    def test_watchdog_is_not_a_dependency(self):
        import tomllib

        project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        deps = project["project"]["dependencies"]
        extras = [d for group in project["project"].get("optional-dependencies", {}).values()
                  for d in group]
        assert not [d for d in deps + extras if d.lower().startswith("watchdog")]
        assert 'name = "watchdog"' not in (REPO / "uv.lock").read_text(encoding="utf-8")
