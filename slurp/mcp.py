"""MCP stdio server exposing the slurp_query tool.

Protocol: JSON-RPC 2.0 over newline-delimited stdin/stdout (MCP 2024-11-05).
No external dependencies — stdlib only for the transport layer.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

from slurp import __version__
from slurp.budget import clear_node_token_cache, select_subgraph
from slurp.formatter import format_subgraph
from slurp.loader import SlurpLoadError, load_graph
from slurp.scorer import clear_pagerank_cache, score_nodes

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


SESSION_LOG_FILE = "session.log"


class GraphState:
    """Owns the served graph and reloads it when the file changes on disk.

    The server loads the graph once at startup; without this, an edit to
    graph.json (a re-index, a `slurp index --watch` cycle) is invisible and the
    server keeps answering from the version it read at boot.
    """

    def __init__(self, graph_path: Path, graph) -> None:
        self.graph_path = graph_path
        self.graph = graph
        self.mtime = self._current_mtime()

    def _current_mtime(self) -> float | None:
        """mtime of the graph file, or None if it is unreadable right now."""
        try:
            return self.graph_path.stat().st_mtime
        except OSError:
            return None

    def check_reload(self) -> bool:
        """Reload the graph if the file changed since it was last read.

        DECISION: caches keyed on the graph object or on node ids must die with
        the old graph — a reload that kept them would serve fresh nodes with
        stale ranks and stale token counts.

        Returns:
            True if the graph was reloaded, False if it was already current.

        Raises:
            SlurpLoadError: If the changed file could not be loaded. The
                previously loaded graph is kept so the server stays usable.
        """
        current = self._current_mtime()
        if current is None or current == self.mtime:
            return False

        reloaded = load_graph(self.graph_path)  # may raise; old graph survives
        self.graph = reloaded
        self.mtime = current
        clear_pagerank_cache()
        clear_node_token_cache()
        return True

# DECISION: same 50-tokens-per-node baseline the `slurp audit` table uses, so
# savings_pct means the same thing everywhere in slurp.
_TOKENS_PER_NODE_BASELINE = 50


# ---------------------------------------------------------------------------
# Session log
# ---------------------------------------------------------------------------


def log_session(
    query: str,
    budget: int,
    stats: dict,
    log_dir: Path | None = None,
) -> bool:
    """Append one JSON line to .slurp/session.log, if that directory exists.

    DECISION: the directory is never created. Its presence is the opt-in signal —
    a user who has run a manual query already has .slurp/ from the audit log, so
    the session log appears for them and stays invisible for everyone else.

    Args:
        query: The query that was served.
        budget: Token budget requested by the caller.
        stats: Stats dict returned by budget.select_subgraph.
        log_dir: Directory holding session.log (default: ./.slurp).

    Returns:
        True if a line was written, False if the directory was absent or the
        write failed.
    """
    log_dir = Path(".slurp") if log_dir is None else log_dir
    if not log_dir.is_dir():
        return False

    used = stats.get("tokens_used", 0)
    baseline = stats.get("nodes_total", 0) * _TOKENS_PER_NODE_BASELINE
    savings_pct = (
        round(max(0, baseline - used) / baseline * 100, 1) if baseline > 0 else 0.0
    )

    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "query": query,
        "budget": budget,
        "nodes": stats.get("nodes_selected", 0),
        "tokens": used,
        "savings_pct": savings_pct,
    }

    try:
        with (log_dir / SESSION_LOG_FILE).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError as exc:
        # Never kill the server over an optional log, but never swallow it either:
        # stderr is safe on stdio transport, stdout would corrupt the protocol.
        print(f"slurp: session log write failed: {exc}", file=sys.stderr)
        return False
    return True


def read_session_log(log_dir: Path | None = None, last: int | None = None) -> list[dict]:
    """Read entries from the session log, oldest first.

    Args:
        log_dir: Directory holding session.log (default: ./.slurp).
        last: Return only the most recent N entries; None returns all.

    Returns:
        List of entry dicts. Empty if the file or directory does not exist.
        Malformed lines are skipped rather than raising.
    """
    log_dir = Path(".slurp") if log_dir is None else log_dir
    log_path = log_dir / SESSION_LOG_FILE
    if not log_path.exists():
        return []

    entries: list[dict] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)

    return entries[-last:] if last is not None and last > 0 else entries


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


def _handle(msg: dict, G, out, session_log: bool = True, state: "GraphState | None" = None) -> None:
    """Dispatch one JSON-RPC message and write the response to *out*.

    Args:
        msg: Parsed JSON-RPC message dict.
        G:   Pre-loaded nx.DiGraph, or None for non-query methods. Ignored when
             *state* is given, which owns the current graph.
        out: File-like object to write responses to.
        session_log: Whether to append successful queries to .slurp/session.log.
        state: Optional GraphState enabling on-disk staleness detection. When
            omitted, *G* is served as-is and no reload check runs.

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

        graph_reloaded = False
        if state is not None:
            try:
                graph_reloaded = state.check_reload()
            except Exception as exc:
                _write(_tool_err(
                    msg_id,
                    f"graph.json changed on disk but could not be reloaded: "
                    f"{type(exc).__name__}: {exc}. Still serving the previously "
                    f"loaded graph — fix the file and query again.",
                ), out)
                return
            G = state.graph

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

        if session_log:
            log_session(query, budget, stats)

        # DECISION: the flag is present only when a reload actually happened, so
        # it reads as an event rather than a status field on every response.
        result: dict = {"content": [{"type": "text", "text": text}]}
        if graph_reloaded:
            result["graph_reloaded"] = True

        _write(_ok(msg_id, result), out)

    else:
        _write(_err(msg_id, -32601, f"Method not found: {method!r}"), out)


# ---------------------------------------------------------------------------
# Server loop
# ---------------------------------------------------------------------------


def serve(graph_path: Path, inp=None, out=None, session_log: bool = True) -> None:
    """Run the MCP stdio server.

    Loads the graph once at startup, then reads newline-delimited JSON-RPC
    messages from *inp* (defaults to sys.stdin) and writes responses to *out*
    (defaults to sys.stdout) until EOF.

    Args:
        graph_path: Path to the graph.json file.
        inp:        Readable file-like object (defaults to sys.stdin).
        out:        Writable file-like object (defaults to sys.stdout).
        session_log: Whether to append served queries to .slurp/session.log.
    """
    if inp is None:
        inp = sys.stdin
    if out is None:
        out = sys.stdout

    try:
        state = GraphState(graph_path, load_graph(graph_path))
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

        _handle(msg, state.graph, out, session_log=session_log, state=state)
