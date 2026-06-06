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

_TOOL_DEFINITION = {
    "name": "slurp_query",
    "description": (
        "Select the most relevant nodes from a knowledge graph for a query "
        "within a token budget. Returns structured markdown with selected "
        "nodes, their relationships, and token usage statistics."
    ),
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
            }),
            out,
        )

    elif method == "ping":
        _write(_ok(msg_id, {}), out)

    elif method == "tools/list":
        _write(_ok(msg_id, {"tools": [_TOOL_DEFINITION]}), out)

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

        scores = score_nodes(G, query)
        subG, stats = select_subgraph(G, scores, budget=budget)
        text = format_subgraph(subG, stats, format="markdown", scores=scores, query=query)

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
