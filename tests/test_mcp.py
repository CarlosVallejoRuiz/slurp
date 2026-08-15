"""Tests for slurp/mcp.py."""

import io
import json
from datetime import datetime
from pathlib import Path

import pytest

from slurp import __version__
from slurp.loader import load_graph
from slurp.mcp import (
    SESSION_LOG_FILE,
    _PROTOCOL_VERSION,
    _TOOL_DEFINITION,
    _handle,
    log_session,
    read_session_log,
    serve,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invoke(msg: dict, G=None) -> dict | None:
    """Call _handle once and return the parsed response, or None if no output."""
    out = io.StringIO()
    _handle(msg, G, out)
    text = out.getvalue().strip()
    return json.loads(text) if text else None


def _req(method: str, params=None, msg_id=1) -> dict:
    """Build a minimal valid JSON-RPC request."""
    msg: dict = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def _notif(method: str, params=None) -> dict:
    """Build a JSON-RPC notification (no id field)."""
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return msg


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


class TestInitialize:
    def test_protocol_version_matches_constant(self):
        resp = _invoke(_req("initialize", {}))
        assert resp["result"]["protocolVersion"] == _PROTOCOL_VERSION

    def test_server_name_is_slurp(self):
        resp = _invoke(_req("initialize", {}))
        assert resp["result"]["serverInfo"]["name"] == "slurp"

    def test_server_version_matches_package(self):
        resp = _invoke(_req("initialize", {}))
        assert resp["result"]["serverInfo"]["version"] == __version__

    def test_capabilities_include_tools(self):
        resp = _invoke(_req("initialize", {}))
        assert "tools" in resp["result"]["capabilities"]

    def test_id_echoed_back(self):
        resp = _invoke(_req("initialize", {}, msg_id=42))
        assert resp["id"] == 42

    def test_jsonrpc_field_is_2_0(self):
        resp = _invoke(_req("initialize", {}))
        assert resp["jsonrpc"] == "2.0"

    def test_no_error_field_in_success(self):
        resp = _invoke(_req("initialize", {}))
        assert "error" not in resp

    def test_client_info_in_params_accepted(self):
        resp = _invoke(_req("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"},
        }))
        assert "error" not in resp

    def test_empty_params_accepted(self):
        resp = _invoke(_req("initialize", {}))
        assert "result" in resp


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------


class TestPing:
    def test_returns_empty_result(self):
        resp = _invoke(_req("ping"))
        assert resp["result"] == {}

    def test_id_echoed(self):
        resp = _invoke(_req("ping", msg_id=7))
        assert resp["id"] == 7

    def test_no_error_field(self):
        resp = _invoke(_req("ping"))
        assert "error" not in resp


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------


class TestToolsList:
    @pytest.fixture(autouse=True)
    def _resp(self):
        self.resp = _invoke(_req("tools/list", {}))

    def test_result_has_tools_key(self):
        assert "tools" in self.resp["result"]

    def test_exactly_one_tool_registered(self):
        assert len(self.resp["result"]["tools"]) == 1

    def test_tool_name_is_slurp_query(self):
        assert self.resp["result"]["tools"][0]["name"] == "slurp_query"

    def test_tool_has_nonempty_description(self):
        assert self.resp["result"]["tools"][0]["description"]

    def test_tool_has_input_schema(self):
        assert "inputSchema" in self.resp["result"]["tools"][0]

    def test_input_schema_type_is_object(self):
        schema = self.resp["result"]["tools"][0]["inputSchema"]
        assert schema["type"] == "object"

    def test_query_is_in_required(self):
        schema = self.resp["result"]["tools"][0]["inputSchema"]
        assert "query" in schema["required"]

    def test_budget_is_not_required(self):
        schema = self.resp["result"]["tools"][0]["inputSchema"]
        assert "budget" not in schema.get("required", [])

    def test_query_property_type_is_string(self):
        props = self.resp["result"]["tools"][0]["inputSchema"]["properties"]
        assert props["query"]["type"] == "string"

    def test_budget_property_type_is_integer(self):
        props = self.resp["result"]["tools"][0]["inputSchema"]["properties"]
        assert props["budget"]["type"] == "integer"

    def test_tool_definition_matches_module_constant(self):
        assert self.resp["result"]["tools"][0] == _TOOL_DEFINITION


# ---------------------------------------------------------------------------
# tools/call — success cases
# ---------------------------------------------------------------------------


class TestToolsCallSuccess:
    @pytest.fixture(autouse=True)
    def _load(self, sample_graph_json):
        self.G = load_graph(sample_graph_json)

    def _call(self, arguments: dict, msg_id: int = 1) -> dict:
        return _invoke(
            _req("tools/call", {"name": "slurp_query", "arguments": arguments}, msg_id=msg_id),
            self.G,
        )

    def test_result_has_content_key(self):
        resp = self._call({"query": "auth flow"})
        assert "content" in resp["result"]

    def test_content_is_a_list(self):
        resp = self._call({"query": "auth flow"})
        assert isinstance(resp["result"]["content"], list)

    def test_content_has_one_item(self):
        resp = self._call({"query": "auth flow"})
        assert len(resp["result"]["content"]) == 1

    def test_content_item_type_is_text(self):
        resp = self._call({"query": "auth flow"})
        assert resp["result"]["content"][0]["type"] == "text"

    def test_content_text_is_nonempty(self):
        resp = self._call({"query": "auth flow"})
        assert resp["result"]["content"][0]["text"].strip()

    def test_content_text_is_markdown(self):
        resp = self._call({"query": "auth"})
        assert "## Relevant Nodes" in resp["result"]["content"][0]["text"]

    def test_query_string_appears_in_header(self):
        resp = self._call({"query": "authentication jwt"})
        assert "authentication jwt" in resp["result"]["content"][0]["text"]

    def test_no_error_without_budget(self):
        resp = self._call({"query": "auth"})
        assert "error" not in resp

    def test_custom_budget_accepted(self):
        resp = self._call({"query": "auth", "budget": 500})
        assert "error" not in resp

    def test_float_whole_number_budget_accepted(self):
        resp = self._call({"query": "auth", "budget": 4000.0})
        assert "error" not in resp

    def test_large_budget_selects_more_nodes_than_small(self):
        import re
        resp_small = self._call({"query": "auth", "budget": 60})
        resp_large = self._call({"query": "auth", "budget": 10_000})
        # Extract "Selected N/M" from both headers
        def selected(text):
            m = re.search(r"Selected (\d+)/\d+", text)
            return int(m.group(1)) if m else -1
        small_count = selected(resp_small["result"]["content"][0]["text"])
        large_count = selected(resp_large["result"]["content"][0]["text"])
        assert small_count <= large_count

    def test_id_echoed_back(self):
        resp = self._call({"query": "auth"}, msg_id=99)
        assert resp["id"] == 99

    def test_jsonrpc_version_in_response(self):
        resp = self._call({"query": "auth"})
        assert resp["jsonrpc"] == "2.0"


# ---------------------------------------------------------------------------
# tools/call — error cases
# ---------------------------------------------------------------------------


class TestToolsCallErrors:
    @pytest.fixture(autouse=True)
    def _load(self, sample_graph_json):
        self.G = load_graph(sample_graph_json)

    def test_missing_query_returns_32602(self):
        resp = _invoke(
            _req("tools/call", {"name": "slurp_query", "arguments": {}}),
            self.G,
        )
        assert resp["error"]["code"] == -32602

    def test_empty_string_query_returns_32602(self):
        resp = _invoke(
            _req("tools/call", {"name": "slurp_query", "arguments": {"query": ""}}),
            self.G,
        )
        assert resp["error"]["code"] == -32602

    def test_non_string_query_returns_error(self):
        resp = _invoke(
            _req("tools/call", {"name": "slurp_query", "arguments": {"query": 42}}),
            self.G,
        )
        assert "error" in resp

    def test_zero_budget_returns_32602(self):
        resp = _invoke(
            _req("tools/call", {"name": "slurp_query", "arguments": {"query": "auth", "budget": 0}}),
            self.G,
        )
        assert resp["error"]["code"] == -32602

    def test_negative_budget_returns_32602(self):
        resp = _invoke(
            _req("tools/call", {"name": "slurp_query", "arguments": {"query": "auth", "budget": -10}}),
            self.G,
        )
        assert resp["error"]["code"] == -32602

    def test_float_fractional_budget_returns_error(self):
        resp = _invoke(
            _req("tools/call", {"name": "slurp_query", "arguments": {"query": "auth", "budget": 100.5}}),
            self.G,
        )
        assert "error" in resp

    def test_unknown_tool_name_returns_32602(self):
        resp = _invoke(
            _req("tools/call", {"name": "does_not_exist", "arguments": {"query": "x"}}),
            self.G,
        )
        assert resp["error"]["code"] == -32602

    def test_none_tool_name_returns_32602(self):
        resp = _invoke(
            _req("tools/call", {"arguments": {"query": "x"}}),
            self.G,
        )
        assert resp["error"]["code"] == -32602

    def test_no_graph_returns_32603(self):
        resp = _invoke(
            _req("tools/call", {"name": "slurp_query", "arguments": {"query": "auth"}}),
            G=None,
        )
        assert resp["error"]["code"] == -32603


# ---------------------------------------------------------------------------
# Protocol error cases
# ---------------------------------------------------------------------------


class TestProtocolErrors:
    def test_unknown_method_returns_32601(self):
        resp = _invoke(_req("no/such/method"))
        assert resp["error"]["code"] == -32601

    def test_error_message_is_nonempty(self):
        resp = _invoke(_req("no/such/method"))
        assert resp["error"]["message"]

    def test_invalid_jsonrpc_version_returns_32600(self):
        resp = _invoke({"jsonrpc": "1.0", "id": 1, "method": "initialize"})
        assert resp["error"]["code"] == -32600

    def test_missing_jsonrpc_field_returns_32600(self):
        resp = _invoke({"id": 1, "method": "initialize"})
        assert resp["error"]["code"] == -32600

    def test_error_response_echoes_id(self):
        resp = _invoke(_req("nonexistent", msg_id=77))
        assert resp["id"] == 77

    def test_non_dict_params_treated_as_empty(self):
        # Passing params as a string should not crash — treated as {}
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": "bad"}
        resp = _invoke(msg)
        assert "tools" in resp["result"]


# ---------------------------------------------------------------------------
# Notifications — must produce no output
# ---------------------------------------------------------------------------


class TestNotifications:
    def test_initialized_notification_silent(self):
        assert _invoke(_notif("notifications/initialized")) is None

    def test_cancelled_notification_silent(self):
        assert _invoke(_notif("notifications/cancelled")) is None

    def test_arbitrary_notification_silent(self):
        assert _invoke(_notif("anything/happened")) is None

    def test_notification_with_params_silent(self):
        assert _invoke(_notif("notifications/initialized", {"meta": {}})) is None


# ---------------------------------------------------------------------------
# serve() — full server loop
# ---------------------------------------------------------------------------


class TestServe:
    def _run(self, messages: list, graph_path) -> list[dict]:
        """Send messages through serve() and return all parsed responses."""
        inp = io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n")
        out = io.StringIO()
        serve(graph_path, inp=inp, out=out)
        return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]

    def test_single_initialize(self, sample_graph_json):
        resps = self._run([_req("initialize", {})], sample_graph_json)
        assert len(resps) == 1
        assert resps[0]["result"]["serverInfo"]["name"] == "slurp"

    def test_multiple_requests_all_answered(self, sample_graph_json):
        msgs = [
            _req("initialize", {}, msg_id=1),
            _req("tools/list", {}, msg_id=2),
            _req("ping", msg_id=3),
        ]
        resps = self._run(msgs, sample_graph_json)
        assert len(resps) == 3
        assert [r["id"] for r in resps] == [1, 2, 3]

    def test_notification_skipped_no_extra_response(self, sample_graph_json):
        msgs = [
            _req("initialize", {}, msg_id=1),
            _notif("notifications/initialized"),
            _req("ping", msg_id=2),
        ]
        resps = self._run(msgs, sample_graph_json)
        assert len(resps) == 2
        assert [r["id"] for r in resps] == [1, 2]

    def test_parse_error_returns_32700(self, sample_graph_json):
        inp = io.StringIO("{ not valid json }\n")
        out = io.StringIO()
        serve(sample_graph_json, inp=inp, out=out)
        resps = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        assert len(resps) == 1
        assert resps[0]["error"]["code"] == -32700

    def test_non_object_json_returns_32600(self, sample_graph_json):
        inp = io.StringIO("[1, 2, 3]\n")
        out = io.StringIO()
        serve(sample_graph_json, inp=inp, out=out)
        resps = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        assert len(resps) == 1
        assert resps[0]["error"]["code"] == -32600

    def test_empty_lines_ignored(self, sample_graph_json):
        inp = io.StringIO("\n\n" + json.dumps(_req("ping")) + "\n\n")
        out = io.StringIO()
        serve(sample_graph_json, inp=inp, out=out)
        resps = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        assert len(resps) == 1

    def test_mixed_valid_and_invalid_messages(self, sample_graph_json):
        inp = io.StringIO(
            json.dumps(_req("ping", msg_id=1)) + "\n"
            + "{ bad json }\n"
            + json.dumps(_req("ping", msg_id=2)) + "\n"
        )
        out = io.StringIO()
        serve(sample_graph_json, inp=inp, out=out)
        resps = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        assert len(resps) == 3  # ping ok + parse error + ping ok
        assert resps[0]["id"] == 1
        assert resps[1]["error"]["code"] == -32700
        assert resps[2]["id"] == 2

    def test_full_mcp_handshake_with_query(self, sample_graph_json):
        msgs = [
            _req("initialize", {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0"},
            }, msg_id=1),
            _notif("notifications/initialized"),
            _req("tools/list", {}, msg_id=2),
            _req("tools/call", {
                "name": "slurp_query",
                "arguments": {"query": "authentication jwt", "budget": 10_000},
            }, msg_id=3),
        ]
        resps = self._run(msgs, sample_graph_json)
        # notification skipped → 3 responses
        assert len(resps) == 3
        assert resps[0]["result"]["protocolVersion"] == _PROTOCOL_VERSION
        assert resps[1]["result"]["tools"][0]["name"] == "slurp_query"
        call_resp = resps[2]
        assert "error" not in call_resp
        text = call_resp["result"]["content"][0]["text"]
        assert "## Relevant Nodes" in text

    def test_graph_loaded_once_responses_consistent(self, sample_graph_json):
        msgs = [
            _req("tools/call", {
                "name": "slurp_query",
                "arguments": {"query": "auth", "budget": 10_000},
            }, msg_id=i)
            for i in range(1, 4)
        ]
        resps = self._run(msgs, sample_graph_json)
        assert len(resps) == 3
        texts = [r["result"]["content"][0]["text"] for r in resps]
        # Same query → same output every time
        assert texts[0] == texts[1] == texts[2]


# ---------------------------------------------------------------------------
# Session log
# ---------------------------------------------------------------------------


class TestLogSession:
    """Tests for slurp.mcp.log_session."""

    STATS = {
        "nodes_selected": 28,
        "nodes_total": 100,
        "tokens_used": 260,
        "tokens_budget": 4000,
        "coverage_pct": 28.0,
    }

    def test_returns_false_when_dir_missing(self, tmp_path):
        assert log_session("q", 4000, self.STATS, log_dir=tmp_path / "nope") is False

    def test_does_not_create_the_dir(self, tmp_path):
        missing = tmp_path / "nope"
        log_session("q", 4000, self.STATS, log_dir=missing)
        assert not missing.exists()

    def test_does_not_raise_when_dir_missing(self, tmp_path):
        log_session("q", 4000, self.STATS, log_dir=tmp_path / "nope")  # must not raise

    def test_returns_true_when_dir_exists(self, tmp_path):
        assert log_session("q", 4000, self.STATS, log_dir=tmp_path) is True

    def test_creates_the_log_file(self, tmp_path):
        log_session("q", 4000, self.STATS, log_dir=tmp_path)
        assert (tmp_path / SESSION_LOG_FILE).exists()

    def test_writes_one_json_line(self, tmp_path):
        log_session("auth flow", 4000, self.STATS, log_dir=tmp_path)
        lines = (tmp_path / SESSION_LOG_FILE).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])

    def test_entry_has_all_required_keys(self, tmp_path):
        log_session("auth flow", 4000, self.STATS, log_dir=tmp_path)
        entry = json.loads((tmp_path / SESSION_LOG_FILE).read_text(encoding="utf-8"))
        for key in ("ts", "query", "budget", "nodes", "tokens", "savings_pct"):
            assert key in entry

    def test_entry_records_query_and_budget(self, tmp_path):
        log_session("auth flow", 4000, self.STATS, log_dir=tmp_path)
        entry = json.loads((tmp_path / SESSION_LOG_FILE).read_text(encoding="utf-8"))
        assert entry["query"] == "auth flow"
        assert entry["budget"] == 4000

    def test_entry_records_nodes_and_tokens_from_stats(self, tmp_path):
        log_session("q", 4000, self.STATS, log_dir=tmp_path)
        entry = json.loads((tmp_path / SESSION_LOG_FILE).read_text(encoding="utf-8"))
        assert entry["nodes"] == 28
        assert entry["tokens"] == 260

    def test_savings_pct_uses_the_50_token_baseline(self, tmp_path):
        # baseline = 100 nodes * 50 = 5000; used 260 -> 94.8% saved
        log_session("q", 4000, self.STATS, log_dir=tmp_path)
        entry = json.loads((tmp_path / SESSION_LOG_FILE).read_text(encoding="utf-8"))
        assert entry["savings_pct"] == pytest.approx(94.8)

    def test_savings_pct_is_zero_for_empty_graph(self, tmp_path):
        log_session("q", 4000, {"nodes_total": 0, "tokens_used": 0,
                                "nodes_selected": 0}, log_dir=tmp_path)
        entry = json.loads((tmp_path / SESSION_LOG_FILE).read_text(encoding="utf-8"))
        assert entry["savings_pct"] == 0.0

    def test_savings_pct_never_negative(self, tmp_path):
        log_session("q", 4000, {"nodes_total": 1, "tokens_used": 9999,
                                "nodes_selected": 1}, log_dir=tmp_path)
        entry = json.loads((tmp_path / SESSION_LOG_FILE).read_text(encoding="utf-8"))
        assert entry["savings_pct"] == 0.0

    def test_timestamp_is_iso_seconds(self, tmp_path):
        log_session("q", 4000, self.STATS, log_dir=tmp_path)
        entry = json.loads((tmp_path / SESSION_LOG_FILE).read_text(encoding="utf-8"))
        datetime.fromisoformat(entry["ts"])  # must parse

    def test_appends_rather_than_overwrites(self, tmp_path):
        log_session("first", 1000, self.STATS, log_dir=tmp_path)
        log_session("second", 2000, self.STATS, log_dir=tmp_path)
        lines = (tmp_path / SESSION_LOG_FILE).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[1])["query"] == "second"

    def test_write_failure_returns_false(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(Path, "open", boom)
        assert log_session("q", 4000, self.STATS, log_dir=tmp_path) is False

    def test_write_failure_reports_to_stderr(self, tmp_path, monkeypatch, capsys):
        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(Path, "open", boom)
        log_session("q", 4000, self.STATS, log_dir=tmp_path)
        assert "session log write failed" in capsys.readouterr().err


class TestReadSessionLog:
    def test_missing_dir_returns_empty(self, tmp_path):
        assert read_session_log(tmp_path / "nope") == []

    def test_missing_file_returns_empty(self, tmp_path):
        assert read_session_log(tmp_path) == []

    def test_reads_entries_oldest_first(self, tmp_path):
        for q in ("one", "two", "three"):
            log_session(q, 1000, TestLogSession.STATS, log_dir=tmp_path)
        entries = read_session_log(tmp_path)
        assert [e["query"] for e in entries] == ["one", "two", "three"]

    def test_last_returns_most_recent(self, tmp_path):
        for q in ("one", "two", "three"):
            log_session(q, 1000, TestLogSession.STATS, log_dir=tmp_path)
        entries = read_session_log(tmp_path, last=2)
        assert [e["query"] for e in entries] == ["two", "three"]

    def test_last_larger_than_log_returns_all(self, tmp_path):
        log_session("only", 1000, TestLogSession.STATS, log_dir=tmp_path)
        assert len(read_session_log(tmp_path, last=99)) == 1

    def test_skips_malformed_lines(self, tmp_path):
        log_session("good", 1000, TestLogSession.STATS, log_dir=tmp_path)
        with (tmp_path / SESSION_LOG_FILE).open("a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        entries = read_session_log(tmp_path)
        assert len(entries) == 1
        assert entries[0]["query"] == "good"

    def test_skips_blank_lines(self, tmp_path):
        (tmp_path / SESSION_LOG_FILE).write_text('\n\n{"query": "x"}\n\n', encoding="utf-8")
        assert len(read_session_log(tmp_path)) == 1

    def test_skips_non_dict_json(self, tmp_path):
        (tmp_path / SESSION_LOG_FILE).write_text('[1,2]\n{"query": "x"}\n', encoding="utf-8")
        assert len(read_session_log(tmp_path)) == 1


class TestHandlerSessionLog:
    """The tools/call path writes to the log only when enabled."""

    @staticmethod
    def _call(G, out, session_log, log_dir, monkeypatch):
        monkeypatch.chdir(log_dir)
        msg = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "slurp_query",
                       "arguments": {"query": "auth", "budget": 2000}},
        }
        _handle(msg, G, out, session_log=session_log)

    def test_successful_query_is_logged(self, sample_graph, tmp_path, monkeypatch):
        (tmp_path / ".slurp").mkdir()
        self._call(sample_graph, io.StringIO(), True, tmp_path, monkeypatch)
        assert len(read_session_log(tmp_path / ".slurp")) == 1

    def test_logged_entry_records_the_query(self, sample_graph, tmp_path, monkeypatch):
        (tmp_path / ".slurp").mkdir()
        self._call(sample_graph, io.StringIO(), True, tmp_path, monkeypatch)
        assert read_session_log(tmp_path / ".slurp")[0]["query"] == "auth"

    def test_disabled_writes_nothing(self, sample_graph, tmp_path, monkeypatch):
        (tmp_path / ".slurp").mkdir()
        self._call(sample_graph, io.StringIO(), False, tmp_path, monkeypatch)
        assert read_session_log(tmp_path / ".slurp") == []

    def test_no_slurp_dir_writes_nothing_and_succeeds(self, sample_graph, tmp_path,
                                                      monkeypatch):
        out = io.StringIO()
        self._call(sample_graph, out, True, tmp_path, monkeypatch)
        assert not (tmp_path / ".slurp").exists()
        assert "result" in json.loads(out.getvalue())

    def test_failed_query_is_not_logged(self, sample_graph, tmp_path, monkeypatch):
        (tmp_path / ".slurp").mkdir()
        monkeypatch.chdir(tmp_path)
        msg = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "slurp_query", "arguments": {}},  # missing query
        }
        _handle(msg, sample_graph, io.StringIO(), session_log=True)
        assert read_session_log(tmp_path / ".slurp") == []

    def test_logging_does_not_alter_the_response(self, sample_graph, tmp_path, monkeypatch):
        (tmp_path / ".slurp").mkdir()
        out = io.StringIO()
        self._call(sample_graph, out, True, tmp_path, monkeypatch)
        resp = json.loads(out.getvalue())
        assert resp["result"]["content"][0]["type"] == "text"
