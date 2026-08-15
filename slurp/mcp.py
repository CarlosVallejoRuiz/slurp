"""MCP stdio server exposing the slurp_query tool.

Protocol: JSON-RPC 2.0 over newline-delimited stdin/stdout (MCP 2024-11-05).
No external dependencies — stdlib only for the transport layer.
"""

import json
import sys
from pathlib import Path

from slurp import __version__
from slurp.budget import select_subgraph
from slurp.formatter import format_subgraph
from slurp.loader import SlurpLoadError, load_graph
from slurp.scorer import score_nodes

_PROTOCOL_VERSION = "2024-11-05"

# DECISION: min_score mirrors the CLI default (see cli.py --min-score) so the
# same query returns the same subgraph through MCP and through the terminal.
# The library default is 0.0, which would select hundreds of irrelevant nodes.
_MIN_SCORE = 0.15

_INSTRUCTIONS = (
    "Slurp serves token-budget-aware slices of a codebase knowledge graph.\n\n"
    "Call slurp_query before reading files when you need to orient yourself in "
    "an unfamiliar codebase, locate the code responsible for a feature, or "
    "understand how components relate. It returns only the most relevant nodes "
    "that fit the token budget, so it is far cheaper than reading files "
    "speculatively.\n\n"
    "Pass a natural-language description of what you are looking for (for "
    "example 'user authentication flow'), not a filename or a bare keyword. "
    "Raise the budget when you need broader context; lower it when you only "
    "need a pointer to the right area."
)

_TOOL_DEFINITION = {
    "name": "slurp_query",
    "description": (
        "Select the most relevant nodes from a knowledge graph for a query "
        "within a token budget. Returns structured markdown with selected "
        "nodes, their relationships, and token usage statistics."
    ),
    # DECISION: annotations let clients auto-approve the call — slurp_query
    # only reads a graph loaded at startup, so it is read-only and idempotent.
    "annotations": {
        "title": "Slurp graph query",
        "readOnlyHint": True,
        "idempotentHint": True,
        "destructiveHint": False,
        "openWorldHint": False,
    },
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language query to find relevant nodes.",
            },
            "budget": {
                "type": "integer",
                "description": "Maximum token budget for the result.",
                "default": 4000,
            },
        },
        "required": ["query"],
    },
}


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------


def _ok(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_err(request_id, message: str) -> dict:
    """Build a tool-execution error.

    DECISION: MCP separates protocol errors (JSON-RPC "error") from tool
    failures (a normal result carrying isError). Tool failures are handed back
    to the model so it can retry with different arguments, whereas a JSON-RPC
    error is surfaced to the user and read by some clients as a broken server.
    """
    return _ok(request_id, {"content": [{"type": "text", "text": message}], "isError": True})


def _write(obj: dict, out) -> None:
    out.write(json.dumps(obj) + "\n")
    out.flush()


# ---------------------------------------------------------------------------
# Message dispatcher
# ---------------------------------------------------------------------------


def _handle(msg: dict, G, out) -> None:
    """Dispatch one JSON-RPC message and write the response to *out*.

    Args:
        msg: Parsed JSON-RPC message dict.
        G:   Pre-loaded nx.DiGraph, or None for non-query methods.
        out: File-like object to write responses to.

    Notifications (messages without an 'id' field) are silently ignored
    per the JSON-RPC 2.0 spec.
    """
    if "id" not in msg:
        return  # notification — no response required

    msg_id = msg["id"]

    if msg.get("jsonrpc") != "2.0":
        _write(_err(msg_id, -32600, "Invalid Request: 'jsonrpc' must be '2.0'"), out)
        return

    method = msg.get("method", "")
    raw_params = msg.get("params")
    params: dict = raw_params if isinstance(raw_params, dict) else {}

    if method == "initialize":
        _write(
            _ok(msg_id, {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "slurp", "version": __version__},
                "instructions": _INSTRUCTIONS,
            }),
            out,
        )

    elif method == "ping":
        _write(_ok(msg_id, {}), out)

    elif method == "tools/list":
        _write(_ok(msg_id, {"tools": [_TOOL_DEFINITION]}), out)

    # DECISION: slurp exposes no resources or prompts, so per spec these
    # methods could return -32601. Several clients probe them at startup
    # regardless of advertised capabilities and treat the error as a failing
    # server, so answer with empty lists instead.
    elif method == "resources/list":
        _write(_ok(msg_id, {"resources": []}), out)

    elif method == "resources/templates/list":
        _write(_ok(msg_id, {"resourceTemplates": []}), out)

    elif method == "prompts/list":
        _write(_ok(msg_id, {"prompts": []}), out)

    elif method == "tools/call":
        name = params.get("name")
        if name != "slurp_query":
            _write(_err(msg_id, -32602, f"Unknown tool: {name!r}"), out)
            return

        args: dict = params.get("arguments") or {}
        query = args.get("query")
        if not query or not isinstance(query, str):
            _write(_err(msg_id, -32602, "Missing required argument: 'query'"), out)
            return

        budget = args.get("budget", 4000)
        # JSON numbers may arrive as floats; coerce whole-number floats to int
        if isinstance(budget, float) and budget.is_integer():
            budget = int(budget)
        if not isinstance(budget, int) or budget <= 0:
            _write(_err(msg_id, -32602, "'budget' must be a positive integer"), out)
            return

        if G is None:
            _write(_err(msg_id, -32603, "Graph not loaded"), out)
            return

        # DECISION: any failure inside scoring/selection/formatting used to
        # propagate out of the read loop and kill the server, leaving the
        # in-flight request unanswered and the client staring at a dead pipe.
        # Report it as a tool error instead so the session survives.
        try:
            scores = score_nodes(G, query)
            subG, stats = select_subgraph(G, scores, budget=budget, min_score=_MIN_SCORE)
            text = format_subgraph(subG, stats, format="markdown", scores=scores, query=query)
        except Exception as exc:
            _write(_tool_err(msg_id, f"slurp_query failed: {type(exc).__name__}: {exc}"), out)
            return

        _write(
            _ok(msg_id, {"content": [{"type": "text", "text": text}]}),
            out,
        )

    else:
        _write(_err(msg_id, -32601, f"Method not found: {method!r}"), out)


# ---------------------------------------------------------------------------
# Server loop
# ---------------------------------------------------------------------------


def serve(graph_path: Path, inp=None, out=None) -> None:
    """Run the MCP stdio server.

    Loads the graph once at startup, then reads newline-delimited JSON-RPC
    messages from *inp* (defaults to sys.stdin) and writes responses to *out*
    (defaults to sys.stdout) until EOF.

    Args:
        graph_path: Path to the graph.json file.
        inp:        Readable file-like object (defaults to sys.stdin).
        out:        Writable file-like object (defaults to sys.stdout).
    """
    if inp is None:
        inp = sys.stdin
    if out is None:
        out = sys.stdout

    try:
        G = load_graph(graph_path)
    except SlurpLoadError as exc:
        print(f"slurp: error loading graph: {exc}", file=sys.stderr)
        sys.exit(1)

    for raw_line in inp:
        line = raw_line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32700, "message": f"Parse error: {exc}"}},
                out,
            )
            continue

        if not isinstance(msg, dict):
            _write(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32600, "message": "Request must be a JSON object"}},
                out,
            )
            continue

        _handle(msg, G, out)
