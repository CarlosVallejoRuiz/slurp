"""The MCP server's texts and protocol versions (v1.3, docs/design/mcp-autonomo.md §5.3, §6)."""

import io
import json
import re
import shutil
from pathlib import Path

import pytest

from slurp import __version__
from slurp.mcp import LEGACY_VERSIONS, MODERN_VERSIONS, GraphState, Session, _handle, serve

DESIGN = Path(__file__).resolve().parent.parent / "docs" / "design" / "mcp-autonomo.md"
FIXTURE = Path(__file__).parent / "fixtures" / "graphify_project"
PROTOCOL_META = "io.modelcontextprotocol/protocolVersion"


def _approved(title: str) -> str:
    """The text the author approved, from the design document."""
    doc = DESIGN.read_text(encoding="utf-8")
    start = doc.index(title)
    return re.compile(r"```\n(.*?)\n```", re.S).search(doc, start).group(1)


def _call(msg: dict, session: Session | None = None, state=None, G=None) -> dict | list | None:
    out = io.StringIO()
    _handle(msg, G if G is not None else (state.graph if state else None), out,
            session_log=False, state=state, session=session)
    text = out.getvalue().strip()
    return json.loads(text) if text else None


def _req(method: str, params: dict | None = None, msg_id: int = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def _modern(method: str, params: dict | None = None, version: str = "2026-07-28",
            msg_id: int = 1) -> dict:
    params = dict(params or {})
    params["_meta"] = {PROTOCOL_META: version,
                       "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"}}
    return _req(method, params, msg_id)


def _initialized(version: str | None) -> Session:
    session = Session()
    params = {"capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
    if version is not None:
        params["protocolVersion"] = version
    _call(_req("initialize", params), session)
    return session


@pytest.fixture
def own_state(sample_graph_json) -> GraphState:
    return GraphState(sample_graph_json)


@pytest.fixture
def graphify_state(tmp_path) -> GraphState:
    root = tmp_path / "g"
    shutil.copytree(FIXTURE, root)
    return GraphState(root / "graphify-out" / "graph.json")


@pytest.fixture
def slurp_state(tmp_path) -> GraphState:
    from click.testing import CliRunner

    from slurp.cli import cli

    root = tmp_path / "p"
    root.mkdir()
    (root / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    assert CliRunner().invoke(cli, ["index", str(root)]).exit_code == 0
    return GraphState(root / "graphify-out" / "graph.json")


# ---------------------------------------------------------------------------
# §5.3 — the approved texts
# ---------------------------------------------------------------------------

class TestInstructions:
    def test_slurps_own_graph_gets_the_approved_text(self, slurp_state):
        text = _call(_req("initialize", {}), Session(), slurp_state)["result"]["instructions"]
        assert text == _approved("**Server instructions**")

    def test_within_the_clients_limits(self, slurp_state):
        text = _call(_req("initialize", {}), Session(), slurp_state)["result"]["instructions"]
        assert len(text) <= 4096                      # Claude Code cuts beyond
        assert len(text.split("\n\n")[0]) <= 512      # Codex: the first 512 stand alone

    def test_graphifys_graph_says_slurp_does_not_rebuild_it(self, graphify_state):
        text = _call(_req("initialize", {}), Session(), graphify_state)["result"]["instructions"]
        first = text.split("\n\n")[0]
        assert first.endswith("This graph was built by graphify and slurp does not rebuild "
                              "it: answers can lag behind recent edits, and say so when files "
                              "they cover have changed.")
        assert "follows the working tree" not in text

    def test_server_discover_carries_them_too(self, graphify_state):
        result = _call(_modern("server/discover"), Session(), graphify_state)["result"]
        assert "built by graphify" in result["instructions"]


class TestBudgetGuidance:
    """#99: the budget study (docs/design/presupuesto-optimo/resultados/conclusiones.md)."""

    def test_query_description(self, slurp_state):
        tools = _call(_req("tools/list", {}), _initialized("2025-11-25"), slurp_state)
        desc = next(t for t in tools["result"]["tools"] if t["name"] == "slurp_query")[
            "description"]
        assert "8000–12000 for broad architecture" not in desc
        assert ("The default budget, 4000 tokens, answers architecture and orientation "
                "questions; to pin down the specific function to change in a large project, "
                "raise it to 16000–32000.") in desc

    def test_instructions(self, slurp_state):
        text = _call(_req("initialize", {}), Session(), slurp_state)["result"]["instructions"]
        assert ("The default budget answers architecture questions; to pin down one specific "
                "function in a large project, pass budget 16000–32000.") in text
        assert len(text) <= 4096 and len(text.split("\n\n")[0]) <= 512


class TestToolTexts:
    def _tools(self, state, session=None) -> dict:
        resp = _call(_req("tools/list", {}), session or _initialized("2025-11-25"), state)
        return {t["name"]: t for t in resp["result"]["tools"]}

    @pytest.mark.parametrize("name,title", [
        ("slurp_query", "**slurp_query** (lleva `alwaysLoad`)"),
        ("slurp_impact", "**slurp_impact**\n"),
        ("slurp_explain", "**slurp_explain**\n"),
        ("slurp_suggest", "**slurp_suggest**\n"),
        ("slurp_diff", "**slurp_diff**\n"),
    ])
    def test_descriptions_are_the_approved_ones(self, slurp_state, name, title):
        assert self._tools(slurp_state)[name]["description"] == _approved(title)

    def test_parameter_descriptions(self, slurp_state):
        tools = self._tools(slurp_state)
        props = {n: t["inputSchema"]["properties"] for n, t in tools.items()}
        assert props["slurp_query"]["query"]["description"] == (
            "Plain-language description of the behaviour or feature you are looking for.")
        assert props["slurp_query"]["budget"]["description"] == (
            "Token budget for the answer (default 4000, enough for architecture questions). "
            "Raise it to 16000–32000 to pin down a specific function in a large project.")
        assert props["slurp_query"]["include_tests"]["description"] == (
            "Also rank test code (left out by default).")
        assert props["slurp_query"]["include_imports"]["description"] == (
            "Also rank import statements (left out by default).")
        assert props["slurp_impact"]["file_path"]["description"] == (
            "Project-relative path (or unique suffix) of the file you are about to edit.")
        assert props["slurp_impact"]["hops"]["description"] == (
            "How many levels of indirect callers to follow (default 2).")
        assert props["slurp_explain"]["node_name"]["description"] == (
            "Function, class or module name, or a dotted node id.")
        assert props["slurp_explain"]["no_llm"]["description"] == (
            "Keep true (default) for the structural answer; false asks the configured LLM "
            "for prose.")

    def test_suggest_says_what_it_answers_when_there_is_nothing_to_suggest(self, slurp_state):
        # #90: with nothing to suggest the tool answers a sentence, not an empty text.
        import networkx as nx

        description = self._tools(slurp_state)["slurp_suggest"]["description"]
        assert "empty answer" not in description
        assert '"No follow-up suggested"' in description
        lone = nx.DiGraph()
        lone.add_node("auth", label="authenticate", type="function", source_file="a.py")
        resp = _call(_req("tools/call", {"name": "slurp_suggest",
                                         "arguments": {"query": "authenticate"}}),
                     _initialized("2025-11-25"), G=lone)
        assert resp["result"]["content"][0]["text"].startswith("No follow-up suggested")

    def test_always_load_only_on_slurp_query(self, slurp_state):
        tools = self._tools(slurp_state)
        assert tools["slurp_query"]["_meta"] == {"anthropic/alwaysLoad": True}
        assert [n for n, t in tools.items() if "_meta" in t] == ["slurp_query"]

    def test_the_graphify_variant_of_slurp_query(self, graphify_state):
        description = self._tools(graphify_state)["slurp_query"]["description"]
        assert description.endswith("Reflects graph.json as graphify last built it; answers "
                                    "flag files changed since.")
        assert "uncommitted edits included" not in description

    def test_every_tool_stays_read_only(self, slurp_state):
        for tool in self._tools(slurp_state).values():
            assert tool["annotations"]["readOnlyHint"] is True


# ---------------------------------------------------------------------------
# §6 — protocol versions
# ---------------------------------------------------------------------------

class TestLegacyNegotiation:
    @pytest.mark.parametrize("version", LEGACY_VERSIONS)
    def test_a_supported_version_is_echoed(self, version):
        session = Session()
        resp = _call(_req("initialize", {"protocolVersion": version}), session)
        assert resp["result"]["protocolVersion"] == version
        assert session.protocol == version

    @pytest.mark.parametrize("version", ["2099-01-01", "2026-07-28", None])
    def test_anything_else_gets_the_latest_legacy(self, version):
        params = {} if version is None else {"protocolVersion": version}
        resp = _call(_req("initialize", params), Session())
        assert resp["result"]["protocolVersion"] == "2025-11-25"

    def test_the_versions(self):
        assert MODERN_VERSIONS == ("2026-07-28",)
        assert LEGACY_VERSIONS == ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

    def test_ping_works_in_the_legacy_era(self):
        assert _call(_req("ping"), _initialized("2025-06-18"))["result"] == {}


class TestModernRequests:
    def test_server_discover(self, slurp_state):
        result = _call(_modern("server/discover"), Session(), slurp_state)["result"]
        assert result["supportedVersions"] == ["2026-07-28", "2025-11-25", "2025-06-18",
                                               "2025-03-26", "2024-11-05"]
        assert result["capabilities"] == {"tools": {}}
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"] == {
            "name": "slurp", "version": __version__}
        assert result["instructions"] == _approved("**Server instructions**")
        assert result["cacheScope"] == "private" and result["ttlMs"] > 0
        assert result["resultType"] == "complete"

    def test_a_tool_call_is_served_statelessly(self, own_state):
        resp = _call(_modern("tools/call", {"name": "slurp_query",
                                            "arguments": {"query": "auth"}}),
                     Session(), own_state)
        result = resp["result"]
        assert result["resultType"] == "complete"
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "slurp"
        assert "dev.imjuank.slurp/graph" in result["_meta"]

    def test_tools_list_can_be_cached(self, own_state):
        result = _call(_modern("tools/list"), Session(), own_state)["result"]
        assert result["cacheScope"] == "private" and result["ttlMs"] > 0
        assert result["resultType"] == "complete"

    def test_an_unsupported_version_is_refused_with_the_list(self):
        resp = _call(_modern("tools/list", version="2027-01-01"), Session())
        assert resp["error"]["code"] == -32022
        assert resp["error"]["message"] == "Unsupported protocol version"
        assert resp["error"]["data"] == {
            "supported": ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"],
            "requested": "2027-01-01"}

    def test_ping_was_removed_in_2026_07_28(self):
        assert _call(_modern("ping"), Session())["error"]["code"] == -32601


class TestBatches:
    def _batch(self, version: str) -> list | dict:
        session = _initialized(version)
        out = io.StringIO()
        inp = io.StringIO(json.dumps([_req("ping", msg_id=1),
                                      {"jsonrpc": "2.0", "method": "notifications/x"},
                                      _req("ping", msg_id=2)]) + "\n")
        serve_loop = __import__("slurp.mcp", fromlist=["_serve_lines"])._serve_lines
        serve_loop(inp, out, None, session_log=False, session=session)
        return json.loads(out.getvalue())

    def test_2025_03_26_answers_a_batch_with_an_array(self):
        resp = self._batch("2025-03-26")
        assert isinstance(resp, list) and [r["id"] for r in resp] == [1, 2]

    @pytest.mark.parametrize("version", ["2025-06-18", "2025-11-25", "2024-11-05"])
    def test_other_versions_refuse_it(self, version):
        assert self._batch(version)["error"]["code"] == -32600


class TestToolTitle:
    @pytest.mark.parametrize("version,has_title", [
        ("2024-11-05", False), ("2025-03-26", False), ("2025-06-18", True),
        ("2025-11-25", True)])
    def test_title_from_2025_06_18(self, own_state, version, has_title):
        tools = _call(_req("tools/list", {}), _initialized(version), own_state)["result"]["tools"]
        assert all(("title" in t) is has_title for t in tools)
        if has_title:
            assert tools[0]["title"] == tools[0]["annotations"]["title"]

    def test_modern_has_titles(self, own_state):
        tools = _call(_modern("tools/list"), Session(), own_state)["result"]["tools"]
        assert all("title" in t for t in tools)


class TestInvalidArgumentsByVersion:
    """2025-11-25 asks for input validation errors as tool errors (isError)."""

    BAD = {"name": "slurp_query", "arguments": {"query": "auth", "budget": -5}}

    @pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18"])
    def test_older_versions_keep_minus_32602(self, own_state, version):
        resp = _call(_req("tools/call", self.BAD), _initialized(version), own_state)
        assert resp["error"]["code"] == -32602

    def test_2025_11_25_returns_is_error(self, own_state):
        resp = _call(_req("tools/call", self.BAD), _initialized("2025-11-25"), own_state)
        assert resp["result"]["isError"] is True
        assert "'budget' must be a positive integer" in resp["result"]["content"][0]["text"]

    def test_modern_returns_is_error(self, own_state):
        resp = _call(_modern("tools/call", self.BAD), Session(), own_state)
        assert resp["result"]["isError"] is True

    def test_a_budget_too_small_is_a_tool_error_too(self, own_state):
        call = {"name": "slurp_query", "arguments": {"query": "auth", "budget": 1}}
        resp = _call(_req("tools/call", call), _initialized("2025-11-25"), own_state)
        assert resp["result"]["isError"] is True

    @pytest.mark.parametrize("version", ["2024-11-05", "2025-11-25"])
    def test_an_unknown_tool_stays_minus_32602(self, own_state, version):
        resp = _call(_req("tools/call", {"name": "slurp_nope", "arguments": {}}),
                     _initialized(version), own_state)
        assert resp["error"]["code"] == -32602

    def test_an_unknown_tool_stays_minus_32602_in_the_modern_era(self, own_state):
        resp = _call(_modern("tools/call", {"name": "slurp_nope", "arguments": {}}),
                     Session(), own_state)
        assert resp["error"]["code"] == -32602

    def test_no_initialize_keeps_minus_32602(self, own_state):
        resp = _call(_req("tools/call", self.BAD), Session(), own_state)
        assert resp["error"]["code"] == -32602


class TestServeNegotiatesOncePerProcess:
    def test_the_version_from_initialize_sticks(self, sample_graph_json):
        msgs = [_req("initialize", {"protocolVersion": "2025-11-25"}, msg_id=1),
                _req("tools/call", TestInvalidArgumentsByVersion.BAD, msg_id=2)]
        out = io.StringIO()
        serve(sample_graph_json, inp=io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)),
              out=out, session_log=False)
        second = json.loads(out.getvalue().splitlines()[1])
        assert second["result"]["isError"] is True
