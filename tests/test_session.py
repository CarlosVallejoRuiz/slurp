"""The MCP session log, v2: every tool call, start and end, summaries (v1.3, design §7)."""

import io
import json
import os
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from slurp.cli import cli
from slurp.mcp import serve
from slurp.session import SESSION_LOG_FILE, read_session_log, summarize_sessions


def _put(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _age(root: Path, seconds: float = 60) -> None:
    past = time.time() - seconds
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + dirnames:
            os.utime(Path(dirpath) / name, (past, past), follow_symlinks=False)
        os.utime(dirpath, (past, past))


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "proj"
    _put(root, "app.py", "from lib.billing import charge\n\n\ndef checkout():\n    return charge()\n")
    _put(root, "lib/billing.py", "def charge():\n    return 1\n")
    res = CliRunner().invoke(cli, ["index", str(root), "--ignore-file",
                                   str(root / ".slurpignore")])
    assert res.exit_code == 0, res.output
    _age(root)
    (root / ".slurp").mkdir()
    monkeypatch.chdir(root)
    return root


def _rpc(msg_id, method, params=None) -> str:
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


def _tool(msg_id, name, **arguments) -> str:
    return _rpc(msg_id, "tools/call", {"name": name, "arguments": arguments})


def _session(project: Path, lines: list) -> list[dict]:
    """Serve *lines* (strings, or callables run between messages); return the log."""
    def messages():
        for line in lines:
            if callable(line):
                line()
            else:
                yield line

    serve(project / "graphify-out" / "graph.json", inp=messages(), out=io.StringIO())
    return read_session_log(project / ".slurp")


INIT = _rpc(0, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                              "clientInfo": {"name": "claude-code", "version": "2.1"}})


class TestOneLinePerCall:
    def test_every_tool_is_logged_with_the_designs_fields(self, project):
        old = project / "old.json"
        old.write_bytes((project / "graphify-out" / "graph.json").read_bytes())
        log = _session(project, [
            INIT,
            _tool(1, "slurp_query", query="charge", budget=2000),
            _tool(2, "slurp_explain", node_name="charge"),
            _tool(3, "slurp_impact", file_path="lib/billing.py"),
            _tool(4, "slurp_suggest", query="charge"),
            _tool(5, "slurp_diff", old_graph=str(old),
                  new_graph=str(project / "graphify-out" / "graph.json")),
        ])
        calls = [e for e in log if e.get("event") == "call"]
        assert [c["tool"] for c in calls] == ["slurp_query", "slurp_explain", "slurp_impact",
                                              "slurp_suggest", "slurp_diff"]
        for call in calls:
            assert call["session"] and call["ts"]
            assert call["client"] == {"name": "claude-code", "version": "2.1"}
            assert call["protocol"] == "2025-11-25"
            assert call["ms"] >= 0
        query = calls[0]
        assert query["args"] == {"query": "charge", "budget": 2000}
        assert query["graph"] == "fresh" and query["nodes"] > 0 and query["tokens"] > 0
        assert calls[1]["args"]["node_name"] == "charge"
        assert calls[1]["args"]["file"] == "lib/billing.py"
        assert calls[2]["args"] == {"file_path": "lib/billing.py", "hops": 2}
        assert "graph" not in calls[4]          # diff does not read the served graph

    def test_an_aliased_argument_is_logged_under_its_real_name(self, project):
        # #93: the analysis reads args.file_path; an alias must not hide the file.
        log = _session(project, [INIT, _tool(1, "slurp_impact", target="lib/billing.py")])
        [call] = [e for e in log if e.get("event") == "call"]
        assert call["args"] == {"file_path": "lib/billing.py", "hops": 2}
        assert "error" not in call

    def test_a_refresh_is_recorded_with_its_files(self, project):
        log = _session(project, [
            INIT,
            lambda: _put(project, "lib/billing.py",
                         "def charge():\n    return 1\n\n\ndef refund():\n    return -1\n"),
            _tool(1, "slurp_query", query="refund"),
        ])
        [call] = [e for e in log if e.get("event") == "call"]
        assert call["graph"] == "refreshed" and call["refresh_ms"] > 0
        assert call["changed"] == {"count": 1, "files": ["lib/billing.py"]}

    def test_a_failed_call_is_logged_with_its_error(self, project):
        log = _session(project, [INIT, _tool(1, "slurp_query", query="x", budget=-1)])
        [call] = [e for e in log if e.get("event") == "call"]
        assert "positive integer" in call["error"]

    def test_a_modern_client_is_named_by_its_requests(self, project):
        params = {"name": "slurp_query", "arguments": {"query": "charge"},
                  "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientInfo": {"name": "dual",
                                                                   "version": "9"}}}
        log = _session(project, [_rpc(1, "tools/call", params)])
        [call] = [e for e in log if e.get("event") == "call"]
        assert call["client"] == {"name": "dual", "version": "9"}
        assert call["protocol"] == "2026-07-28"

    def test_nothing_is_written_without_a_slurp_directory(self, project):
        (project / ".slurp").rmdir()
        _session(project, [INIT, _tool(1, "slurp_query", query="charge")])
        assert not (project / ".slurp").exists()


class TestStartAndEnd:
    def test_start_names_the_graph_its_owner_and_the_version(self, project):
        from slurp import __version__

        log = _session(project, [INIT])
        start = log[0]
        assert start["event"] == "start"
        assert start["graph"] == str(project / "graphify-out" / "graph.json")
        assert start["owner"] == "own" and start["slurp_version"] == __version__

    def test_end_lists_the_files_changed_during_the_session(self, project):
        log = _session(project, [
            INIT,
            _tool(1, "slurp_query", query="charge"),
            lambda: _put(project, "lib/tax.py", "def tax():\n    return 2\n"),
            lambda: _put(project, "app.py", "def checkout():\n    return 0\n"),
        ])
        end = log[-1]
        assert end["event"] == "end"
        assert end["changed"] == {"count": 2, "files": ["app.py", "lib/tax.py"]}
        assert end["calls"] == 1

    def test_every_line_of_one_server_shares_the_session(self, project):
        log = _session(project, [INIT, _tool(1, "slurp_query", query="charge")])
        assert len({e["session"] for e in log}) == 1


class TestOldLogsStillRead:
    def test_v1_lines_and_v2_lines_side_by_side(self, project):
        path = project / ".slurp" / SESSION_LOG_FILE
        path.write_text(json.dumps({"ts": "2026-11-01T10:00:00", "query": "old", "budget": 4000,
                                    "nodes": 3, "tokens": 100}) + "\n", encoding="utf-8")
        _session(project, [INIT, _tool(1, "slurp_impact", file_path="app.py")])
        res = CliRunner().invoke(cli, ["session"])
        assert res.exit_code == 0, res.output
        assert "old" in res.output and "slurp_impact" in res.output and "app.py" in res.output


def _e(event, ts, **kw):
    return {"event": event, "ts": ts, "session": "1-a", **kw}


class TestSummaries:
    LOG = [
        _e("start", "2026-11-26T10:00:00", graph="g.json", owner="own", slurp_version="1.3.0"),
        _e("call", "2026-11-26T10:00:04", tool="slurp_query", client={"name": "cc", "version": "1"},
           graph="fresh", args={"query": "q"}),
        _e("call", "2026-11-26T10:01:00", tool="slurp_impact", graph="refreshed", refresh_ms=300,
           changed={"count": 1, "files": ["lib/a.py"]}, args={"file_path": "lib/b.py"}),
        _e("call", "2026-11-26T10:02:00", tool="slurp_query", graph="stale", warned=True,
           refresh_ms=None, changed={"count": 1, "files": ["lib/b.py"]}, args={"query": "q2"}),
        _e("call", "2026-11-26T10:03:00", tool="slurp_explain", graph="refreshed", refresh_ms=500,
           args={"node_name": "f", "file": "lib/c.py"}),
        _e("end", "2026-11-26T10:05:00", calls=4,
           changed={"count": 3, "files": ["lib/a.py", "lib/b.py", "lib/c.py"]}),
    ]

    def test_one_summary_per_session(self):
        [s] = summarize_sessions(self.LOG)
        assert s.client == "cc 1"
        assert s.duration_s == 300
        assert s.calls == {"slurp_query": 2, "slurp_impact": 1, "slurp_explain": 1}
        assert s.first_call_s == 4
        assert s.changed_before_first_call == 0
        assert s.warned == 1
        assert s.p95_refresh_ms == 500

    def test_edits_with_and_without_a_prior_impact_or_explain(self):
        [s] = summarize_sessions(self.LOG)
        # a.py: edited before the impact on b.py, no impact or explain on it before → without.
        # b.py: slurp_impact on it came before the edit → with.
        # c.py: edited after the last call; slurp_explain named a node in it → with.
        assert s.edited == 3
        assert s.edited_after_impact_or_explain == 2

    def test_old_entries_without_a_session_are_left_out(self):
        assert summarize_sessions([{"query": "old", "ts": "2026-11-01T10:00:00"}]) == []

    def test_the_cli_prints_them(self, project):
        path = project / ".slurp" / SESSION_LOG_FILE
        path.write_text("".join(json.dumps(e) + "\n" for e in self.LOG), encoding="utf-8")
        res = CliRunner().invoke(cli, ["session", "--sessions"])
        assert res.exit_code == 0, res.output
        for text in ("cc 1", "5m 0s", "+4s", "query 2", "2 of 3", "500 ms"):
            assert text in res.output, text


class TestEndEventWhenTheClientStopsTheServer:
    def test_sigterm_still_writes_the_end_event(self, project):
        # Found while preparing the agent measurement: Claude Code stops its MCP
        # servers with a signal, not by closing stdin, so the end event never came.
        import signal
        import subprocess
        import sys

        proc = subprocess.Popen(
            [sys.executable, "-c", "from slurp.cli import cli; cli()", "serve", "--graph",
             str(project / "graphify-out" / "graph.json")],
            cwd=project, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
        proc.stdin.write(INIT + "\n")
        proc.stdin.flush()
        assert json.loads(proc.stdout.readline())["id"] == 0
        _put(project, "lib/edited.py", "def edited():\n    return 1\n")
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
        log = read_session_log(project / ".slurp")
        assert log[-1]["event"] == "end"
        assert log[-1]["changed"]["files"] == ["lib/edited.py"]
