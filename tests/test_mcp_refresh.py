"""The MCP server keeps slurp's own graph current before each answer (v1.3, #76)."""

import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from slurp.cli import cli
from slurp.mcp import GraphState, _handle, serve
from slurp.refresh import META_KEY, GraphTracker

FIXTURE = Path(__file__).parent / "fixtures" / "graphify_project"


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
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    _put(root, "app.py", "from lib.billing import charge\n\n\ndef checkout():\n    return charge()\n")
    _put(root, "lib/billing.py", "def charge():\n    return 1\n")
    res = CliRunner().invoke(cli, ["index", str(root), "--ignore-file",
                                   str(root / ".slurpignore")])
    assert res.exit_code == 0, res.output
    _age(root)
    return root / "graphify-out" / "graph.json"


def _call(state: GraphState, name: str = "slurp_query", **arguments) -> dict:
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": name, "arguments": arguments or {"query": "refund payment"}}}
    out = io.StringIO()
    _handle(msg, state.graph, out, session_log=False, state=state)
    return json.loads(out.getvalue())["result"]


def _add_refund(graph: Path) -> None:
    _put(graph.parent.parent, "lib/billing.py",
         "def charge():\n    return 1\n\n\ndef refund_payment():\n    return -1\n")


class TestEveryToolAnswersFromTheCurrentGraph:
    @pytest.mark.parametrize("name,arguments", [
        ("slurp_query", {"query": "refund payment"}),
        ("slurp_explain", {"node_name": "refund_payment"}),
        ("slurp_impact", {"file_path": "lib/billing.py"}),
    ])
    def test_after_an_edit(self, project, name, arguments):
        state = GraphState(project)
        assert _call(state, name, **arguments)["_meta"][META_KEY]["status"] == "fresh"
        _add_refund(project)
        result = _call(state, name, **arguments)
        assert "refund_payment" in result["content"][0]["text"]
        meta = result["_meta"][META_KEY]
        assert meta["status"] == "refreshed"
        assert meta["changed_files"] == ["lib/billing.py"]
        assert meta["refresh_ms"] > 0

    def test_suggest_too(self, project):
        state = GraphState(project)
        _add_refund(project)
        result = _call(state, "slurp_suggest", query="charge")
        assert result["_meta"][META_KEY]["status"] == "refreshed"

    def test_new_deleted_and_renamed_files(self, project):
        state = GraphState(project)
        root = project.parent.parent
        _put(root, "lib/tax.py", "def compute_tax():\n    return 2\n")
        assert "compute_tax" in _call(state, query="compute tax")["content"][0]["text"]
        (root / "lib" / "tax.py").rename(root / "lib" / "taxes.py")
        _call(state, query="compute tax")
        assert "lib.taxes.compute_tax" in state.graph and "lib.tax.compute_tax" not in state.graph
        (root / "lib" / "taxes.py").unlink()
        _call(state, query="compute tax")
        assert "lib.taxes.compute_tax" not in state.graph

    def test_a_slurpignore_change(self, project):
        state = GraphState(project)
        root = project.parent.parent
        _put(root, ".slurpignore", "file:lib/*\n")
        _call(state, query="charge")
        assert "lib.billing.charge" not in state.graph


class TestPastTheTimeLimit:
    def test_the_warning_comes_first_and_the_state_is_in_meta(self, project, monkeypatch):
        real = GraphTracker.rebuild

        def slow(self, changes, should_stop=lambda: False):
            time.sleep(0.8)
            return real(self, changes, should_stop)

        monkeypatch.setattr(GraphTracker, "rebuild", slow)
        state = GraphState(project, timeout=0.1)
        _add_refund(project)
        result = _call(state)
        text = result["content"][0]["text"]
        assert text.startswith("⚠ Graph refresh still running — 1 file changed since this "
                               "graph was built (lib/billing.py). This answer may miss them; "
                               "call again in a few seconds.\n\n")
        assert result["_meta"][META_KEY]["status"] == "stale"
        state.refresher.finish()
        result = _call(state)
        assert result["_meta"][META_KEY]["status"] == "refreshed"
        assert "refund_payment" in result["content"][0]["text"]


class TestGraphifyGraphsOverMcp:
    def test_not_one_byte_changes_after_many_calls(self, tmp_path):
        root = tmp_path / "graphify_project"
        shutil.copytree(FIXTURE, root)
        _age(root)
        graph = root / "graphify-out" / "graph.json"
        before = graph.read_bytes()
        state = GraphState(graph)
        state.start()
        for i in range(5):
            _put(root, "lib/util.py", f"def helper():\n    return {i}\n")
            _put(root, f"lib/new_{i}.py", "def new():\n    pass\n")
            for name, args in (("slurp_query", {"query": "helper"}),
                               ("slurp_impact", {"file_path": "lib/util.py"}),
                               ("slurp_explain", {"node_name": "helper"})):
                result = _call(state, name, **args)
                assert result["_meta"][META_KEY]["status"] == "readonly"
        assert result["content"][0]["text"].startswith("1 file this graph covers changed") or \
            "rerun graphify." in result["content"][0]["text"]
        assert graph.read_bytes() == before
        assert sorted(p.name for p in graph.parent.iterdir()) == ["graph.json"]


class TestStdoutDuringARefresh:
    def test_stdout_stays_empty_while_the_server_refreshes(self, project, capfd, monkeypatch):
        # A file whose parse fails makes the indexer warn — on stderr.
        root = project.parent.parent
        real = GraphTracker.rebuild
        running = []

        def slow(self, changes, should_stop=lambda: False):
            running.append(True)
            time.sleep(0.5)
            return real(self, changes, should_stop)

        monkeypatch.setattr(GraphTracker, "rebuild", slow)
        out = io.StringIO()
        observed: list[str] = []

        def messages():
            yield json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "t", "version": "1"}}})
            _put(root, "lib/broken.ts", "export function (((\n")
            _add_refund(project)
            yield json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "slurp_query",
                                         "arguments": {"query": "refund"}}})
            # The refresh is now running in its thread: nothing may reach stdout.
            written = out.getvalue()
            for _ in range(20):
                time.sleep(0.05)
                observed.append(capfd.readouterr().out)
                assert out.getvalue() == written
            yield json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                              "params": {"name": "slurp_query",
                                         "arguments": {"query": "refund"}}})

        monkeypatch.setenv("SLURP_REFRESH_TIMEOUT", "0")
        serve(project, inp=messages(), out=out, session_log=False)
        assert running
        assert "".join(observed) == ""
        lines = out.getvalue().splitlines()
        assert [json.loads(line)["id"] for line in lines] == [0, 1, 2]

    def test_a_real_server_writes_only_json_rpc_lines(self, project):
        root = project.parent.parent
        proc = subprocess.Popen(
            [sys.executable, "-c", "from slurp.cli import cli; cli()", "serve", "--graph",
             str(project), "--no-log"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=root)

        def send(msg: dict) -> None:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()

        send({"jsonrpc": "2.0", "id": 0, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "t", "version": "1"}}})
        first = json.loads(proc.stdout.readline())
        assert first["id"] == 0
        _put(root, "lib/broken.ts", "export function (((\n")
        _add_refund(project)
        send({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "slurp_query", "arguments": {"query": "refund payment"}}})
        second = json.loads(proc.stdout.readline())
        out, _err = proc.communicate(timeout=30)
        assert out == ""
        assert second["id"] == 1
        assert "refund_payment" in second["result"]["content"][0]["text"]


class TestServeOptions:
    def test_no_refresh_serves_the_graph_as_built_with_a_warning(self, project):
        before = project.read_bytes()
        _add_refund(project)
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "slurp_query", "arguments": {"query": "refund payment"}}},
        ]
        res = CliRunner().invoke(cli, ["serve", "--graph", str(project), "--no-log",
                                       "--no-refresh"],
                                 input="".join(json.dumps(m) + "\n" for m in msgs))
        assert res.exit_code == 0, res.output
        answer = json.loads(res.stdout.splitlines()[-1])["result"]
        assert answer["_meta"][META_KEY]["status"] == "stale"
        assert "not refreshed" in answer["content"][0]["text"].splitlines()[0]
        assert project.read_bytes() == before

    def test_a_bad_timeout_stops_the_server_before_it_starts(self, project):
        res = CliRunner().invoke(cli, ["serve", "--graph", str(project),
                                       "--refresh-timeout", "-2"], input="")
        assert res.exit_code != 0
        assert "--refresh-timeout" in res.output
