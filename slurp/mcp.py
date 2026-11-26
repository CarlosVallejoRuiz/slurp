"""MCP stdio server exposing slurp_query, slurp_explain, slurp_diff, slurp_suggest
and slurp_impact.

Protocol: JSON-RPC 2.0 over newline-delimited stdin/stdout (MCP 2024-11-05).
No external dependencies — stdlib only for the transport layer.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

from slurp import __version__
from slurp._graphutils import _left_out, clear_left_out_cache
from slurp.budget import (
    DEFAULT_MIN_SCORE,
    BudgetTooSmallError,
    build_context,
    clear_node_token_cache,
    resolve_token_factor,
    select_subgraph,
)
from slurp.diff import diff_graphs, format_diff
from slurp.explainer import (
    SlurpExplainError,
    explain_node,
    find_node,
    format_explanation,
    resolve_config,
)
from slurp.graphfile import ABSENT, graph_identity
from slurp.loader import SlurpLoadError, load_graph
from slurp.refresh import DEFAULT_TIMEOUT_S, Freshness, GraphTracker, Refresher, resolve_refresh
from slurp.impact import analyze_impact, format_impact
from slurp.suggester import format_suggestions, suggest_queries
from slurp.scorer import clear_pagerank_cache, score_nodes

_PROTOCOL_VERSION = "2024-11-05"

# The one default every entry point shares (see budget.DEFAULT_MIN_SCORE), so the
# same query returns the same subgraph through MCP and through the terminal.
_MIN_SCORE = DEFAULT_MIN_SCORE

_INSTRUCTIONS = (
    "Slurp serves token-budget-aware slices of a codebase knowledge graph. "
    "Five tools, for five different questions.\n\n"
    "slurp_query — 'where is the code that does X?'. Call it before reading "
    "files when you need to orient yourself in an unfamiliar codebase, locate "
    "the code responsible for a feature, or understand how components relate. "
    "It returns only the most relevant nodes that fit the token budget, so it "
    "is far cheaper than reading files speculatively. Pass a natural-language "
    "description (for example 'user authentication flow'), not a filename or a "
    "bare keyword. Raise the budget when you need broader context; lower it "
    "when you only need a pointer to the right area.\n\n"
    "slurp_explain — 'what is this one thing, and what breaks if I change "
    "it?'. Call it once slurp_query has pointed you at a function, class or "
    "module and you are about to modify it. It names the part that node plays, "
    "who depends on it, and how risky a change is. Answers from the graph "
    "alone by default, with no API cost.\n\n"
    "slurp_diff — 'what does this change affect?'. Call it with two graph "
    "snapshots, typically before merging, to see which parts of the codebase "
    "sit in the blast radius of what changed.\n\n"
    "slurp_suggest — 'what else should I look at?'. Call it after a "
    "slurp_query whose answer felt incomplete. It reads the nodes that sat "
    "just outside the budget and proposes the two or three queries that would "
    "reach them.\n\n"
    "slurp_impact — 'what breaks if I edit this file?'. Call it before "
    "editing any file. Unlike slurp_diff it needs no second snapshot: it "
    "reads the current graph and reports which definitions in that file carry "
    "dependants, how far they reach, and which are free to touch."
)

# Tests and import nodes do not compete for a place by default (see
# _graphutils._left_out); these let each kind back in, as the CLI flags do.
_INCLUDE_PROPERTIES = {
    "include_tests": {
        "type": "boolean",
        "default": False,
        "description": "Let test code compete for a place (left out by default).",
    },
    "include_imports": {
        "type": "boolean",
        "default": False,
        "description": "Let import nodes compete for a place (left out by default).",
    },
}


def _include_flags(args: dict) -> tuple[bool, bool] | str:
    """(include_tests, include_imports) from tool arguments, or the error to report."""
    flags = []
    for name in ("include_tests", "include_imports"):
        value = args.get(name, False)
        if not isinstance(value, bool):
            return f"'{name}' must be a boolean"
        flags.append(value)
    return flags[0], flags[1]


_TOOL_DEFINITION = {
    "name": "slurp_query",
    "description": (
        "Select the most relevant nodes from a knowledge graph for a query "
        "within a token budget. Returns structured markdown with selected "
        "nodes, their relationships, and token usage statistics."
    ),
    # DECISION: slurp_query only reads a graph loaded at startup, so it is
    # read-only and idempotent, and says so. The hints approve nothing in Claude
    # Code: there readOnlyHint lets a call run in parallel with other read-only
    # tools, and readOnlyHint/idempotentHint let it be retried after a
    # reconnection; approval takes a permission rule or "don't ask again".
    # Codex does count readOnlyHint for approval: with
    # default_tools_approval_mode = "writes" it only asks about tools not marked
    # read-only. The MCP spec calls annotations untrusted hints, so no client
    # has to act on them.
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
            **_INCLUDE_PROPERTIES,
        },
        "required": ["query"],
    },
}


_EXPLAIN_TOOL_DEFINITION = {
    "name": "slurp_explain",
    "description": (
        "Explains a specific function or node in natural language — what it "
        "does, who calls it, and the risk of changing it. Use this when you "
        "need to understand a specific part of the codebase before modifying "
        "it."
    ),
    "annotations": {
        "title": "Slurp node explanation",
        "readOnlyHint": True,
        "idempotentHint": True,
        "destructiveHint": False,
        "openWorldHint": False,
    },
    "inputSchema": {
        "type": "object",
        "properties": {
            "node_name": {
                "type": "string",
                "description": "Name of the function, class or module to explain",
            },
            "no_llm": {
                "type": "boolean",
                "default": True,
                "description": "Use structural analysis only (faster, no API cost)",
            },
        },
        "required": ["node_name"],
    },
}

_DIFF_TOOL_DEFINITION = {
    "name": "slurp_diff",
    "description": (
        "Shows the blast radius of changes between two graph snapshots. Use "
        "this before merging a PR to see what parts of the codebase are "
        "affected."
    ),
    # Reads two files named by the caller rather than the graph loaded at
    # startup, so unlike the other two this one does reach outside the server.
    "annotations": {
        "title": "Slurp change impact",
        "readOnlyHint": True,
        "idempotentHint": True,
        "destructiveHint": False,
        "openWorldHint": False,
    },
    "inputSchema": {
        "type": "object",
        "properties": {
            "old_graph": {
                "type": "string",
                "description": "Path to the old graph.json",
            },
            "new_graph": {
                "type": "string",
                "description": "Path to the new graph.json",
            },
            "hops": {
                "type": "integer",
                "default": 2,
                "description": "Depth of impact neighborhood",
            },
        },
        "required": ["old_graph", "new_graph"],
    },
}

_SUGGEST_TOOL_DEFINITION = {
    "name": "slurp_suggest",
    "description": (
        "Suggests related queries to explore after a slurp_query call. Use "
        "this when you want to understand the broader context around what you "
        "just found."
    ),
    "annotations": {
        "title": "Slurp follow-up queries",
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
                "description": "The query whose neighbourhood should be explored.",
            },
            "budget": {
                "type": "integer",
                "description": "Token budget the original query used.",
                "default": 4000,
            },
            **_INCLUDE_PROPERTIES,
        },
        "required": ["query"],
    },
}

_IMPACT_TOOL_DEFINITION = {
    "name": "slurp_impact",
    "description": (
        "Analyzes the blast radius of editing a specific file before making "
        "changes. Use this before editing any file to understand what might "
        "break."
    ),
    "annotations": {
        "title": "Slurp edit impact",
        "readOnlyHint": True,
        "idempotentHint": True,
        "destructiveHint": False,
        "openWorldHint": False,
    },
    "inputSchema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Relative path to the file about to be edited",
            },
            "hops": {
                "type": "integer",
                "default": 2,
                "description": "How far to follow dependants outward.",
            },
        },
        "required": ["file_path"],
    },
}

_TOOLS = [_TOOL_DEFINITION, _EXPLAIN_TOOL_DEFINITION, _DIFF_TOOL_DEFINITION,
          _SUGGEST_TOOL_DEFINITION, _IMPACT_TOOL_DEFINITION]
_TOOL_NAMES = frozenset(t["name"] for t in _TOOLS)


SESSION_LOG_FILE = "session.log"


class GraphState:
    """Owns the served graph and keeps it current (docs/design/mcp-autonomo.md §2).

    Before each call, :meth:`prepare` checks slurp's own graph against the
    working tree through its manifest and refreshes it when something changed
    (slurp.refresh), reloads it when another process rewrote the file, and
    leaves graphify's graphs as they are. The swap to a new graph happens here,
    on the server's thread, and drops the caches keyed on the old one.
    """

    #: How many source graphs this state serves. Federated states override it.
    graphs_count = 1

    def __init__(self, graph_path: Path, graph=None, *, refresh: bool = True,
                 timeout: float = DEFAULT_TIMEOUT_S) -> None:
        del graph  # loaded by the tracker, from the file, with what it is made of
        self.graph_path = graph_path
        self.trackers = [GraphTracker(graph_path)]
        self.refresher = Refresher(self.trackers, timeout, refresh, on_swap=self._swapped)
        self.graph = self._current()

    @property
    def identity(self):
        """What the served file was when last read or written: (mtime_ns, inode, size)."""
        return self.trackers[0].identity

    def _current(self):
        return self.trackers[0].graph

    def _swapped(self) -> None:
        """A tracker serves a new graph: point at it and drop what the old one cached.

        DECISION: caches keyed on the graph object or on node ids must die with
        the old graph — keeping them would serve fresh nodes with stale ranks
        and stale token counts.
        """
        self.graph = self._current()
        clear_pagerank_cache()
        clear_node_token_cache()
        clear_left_out_cache()

    def start(self) -> None:
        """Check and refresh in the background; `initialize` does not wait for it."""
        self.refresher.start()

    def prepare(self) -> Freshness:
        """Make the graph as current as the time limit allows, and say how current."""
        return self.refresher.prepare()

    def check_reload(self) -> bool:
        """Reload the graph if another process replaced the file (no re-index).

        A missing file is not a reload: the graph in memory keeps being served.

        Returns:
            True if the graph was reloaded, False if it was already current.

        Raises:
            SlurpLoadError: If the changed file could not be loaded. The
                previously loaded graph is kept so the server stays usable.
        """
        changed = False
        for tracker in self.trackers:
            if graph_identity(tracker.path) == ABSENT or not tracker.disk_changed():
                continue
            tracker.load()  # may raise; the old graph survives
            changed = True
        if changed:
            self._swapped()
        return changed


class FederatedGraphState(GraphState):
    """Serves several graphs merged into one, each member kept current on its own.

    Node ids are namespaced by project, so a query can cross project boundaries
    without ids colliding (slurp.federation). Only slurp's own members are
    refreshed, in order, sharing one time limit; the merge is redone whenever
    any member changes.
    """

    def __init__(self, graph_paths: list[Path], labels: list[str] | None = None, *,
                 refresh: bool = True, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        from slurp.federation import derive_labels

        self.graph_paths = list(graph_paths)
        self.labels = list(labels) if labels else derive_labels(self.graph_paths)
        self.graph_path = self.graph_paths[0]
        self.trackers = [GraphTracker(p) for p in self.graph_paths]
        for tracker in self.trackers:
            if tracker.data is None:
                raise ValueError(f"{tracker.path}: a federation reads graph.json files only")
        self.refresher = Refresher(self.trackers, timeout, refresh, on_swap=self._swapped)
        self.graph = self._current()

    @property
    def graphs_count(self) -> int:  # type: ignore[override]
        return len(self.graph_paths)

    def _current(self):
        from slurp.federation import merge_graphs
        from slurp.loader import build_graph

        return build_graph(merge_graphs([t.data for t in self.trackers], self.labels))


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

    # DECISION: no savings figure. Savings mean what `slurp benchmark` measures:
    # the whole graph printed the same way and counted. That costs 70-700 ms a
    # call against 4-7 ms for the query itself, so the server does not pay it,
    # and without it there is nothing honest to write. Until v1.1.0 this logged
    # savings against a fixed per-node guess.
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "query": query,
        "budget": budget,
        "nodes": stats.get("nodes_selected", 0),
        "tokens": stats.get("tokens_used", 0),
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
            # INTENTIONAL: the session log is append-only JSONL written by a
            # long-running server, so a truncated final line is expected after
            # a crash. One unreadable line must not hide the rest of the log.
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


class _Invalid(Exception):
    """Arguments the call cannot work with (JSON-RPC -32602)."""


class _Failed(Exception):
    """The tool ran and failed: a result with isError and a readable message."""


def _positive_int(args: dict, name: str, default: int) -> int:
    value = args.get(name, default)
    # JSON numbers may arrive as floats; whole-number floats are accepted.
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _Invalid(f"'{name}' must be a positive integer")
    return value


def _required_text(args: dict, name: str) -> str:
    value = args.get(name)
    if not value or not isinstance(value, str):
        raise _Invalid(f"Missing required argument: {name!r}")
    return value


def _tool_query(args: dict, G) -> tuple[str, dict]:
    """slurp_query: the subgraph for a query, within a token budget."""
    query = _required_text(args, "query")
    budget = _positive_int(args, "budget", 4000)
    flags = _include_flags(args)
    if isinstance(flags, str):
        raise _Invalid(flags)
    include_tests, include_imports = flags
    # DECISION: any failure inside scoring/selection/formatting used to
    # propagate out of the read loop and kill the server, leaving the
    # in-flight request unanswered and the client staring at a dead pipe.
    # Report it as a tool error instead so the session survives.
    try:
        scores = score_nodes(G, query)
        # Read per call, like the LLM config: export SLURP_TOKEN_FACTOR in the
        # server's environment (.mcp.json "env") to budget in Claude tokens.
        text, _subG, stats = build_context(G, scores, budget=budget, min_score=_MIN_SCORE,
                                           query=query, token_factor=resolve_token_factor(),
                                           include_tests=include_tests,
                                           include_imports=include_imports)
    except BudgetTooSmallError as exc:
        # The caller's budget argument cannot work: an invalid parameter.
        raise _Invalid(str(exc)) from None
    except Exception as exc:
        raise _Failed(f"slurp_query failed: {type(exc).__name__}: {exc}") from None
    return text, {"query": query, "budget": budget, "stats": stats}


def _tool_explain(args: dict, G) -> tuple[str, dict]:
    """slurp_explain: one node's role, callers, and risk.

    Every failure past argument validation becomes a tool error rather than an
    exception: a missing node or an unreachable provider is the caller's
    problem to read, not a reason to take the session down.
    """
    node_name = _required_text(args, "node_name")
    no_llm = args.get("no_llm", True)
    if not isinstance(no_llm, bool):
        raise _Invalid("'no_llm' must be a boolean")

    try:
        scores = score_nodes(G, node_name)
        node_id = find_node(G, node_name, scores)
    except SlurpExplainError as exc:
        raise _Failed(f"slurp_explain failed: {exc}") from None
    except Exception as exc:
        raise _Failed(f"slurp_explain failed: {type(exc).__name__}: {exc}") from None

    # DECISION: the config is resolved per call rather than at startup. A user
    # who exports an API key mid-session gets prose without restarting the
    # server, and no_llm stays a per-call decision rather than a launch flag.
    config = None
    refused = ""
    if not no_llm:
        try:
            config = resolve_config()
        except SlurpExplainError as exc:
            # A refusal (e.g. a project endpoint paired with an env key) is
            # reported on the answer so the structural fallback is explained.
            refused = str(exc)
        except Exception:
            config = None      # fall through to the structural explanation

    try:
        result = explain_node(G, node_id, scores=scores, config=config)
        if refused and not result.warning:
            result.warning = f"LLM not used: {refused}"
        text = format_explanation(result)
    except Exception as exc:
        raise _Failed(f"slurp_explain failed: {type(exc).__name__}: {exc}") from None
    attrs = G.nodes.get(node_id, {})
    return text, {"node_name": node_name, "node": node_id,
                  "file": str(attrs.get("source_file") or attrs.get("file_path") or "")}


def _tool_impact(args: dict, G) -> tuple[str, dict]:
    """slurp_impact: what editing a file could break, from the graph already loaded."""
    file_path = _required_text(args, "file_path")
    hops = _positive_int(args, "hops", 2)
    try:
        result = analyze_impact(G, file_path, hops=hops)
        text = format_impact(result, G)
    except Exception as exc:
        raise _Failed(f"slurp_impact failed: {type(exc).__name__}: {exc}") from None
    return text, {"file_path": file_path, "hops": hops, "risk": result.risk_level}


def _tool_suggest(args: dict, G) -> tuple[str, dict]:
    """slurp_suggest: re-run the cut and read its edge."""
    query = _required_text(args, "query")
    budget = _positive_int(args, "budget", 4000)
    flags = _include_flags(args)
    if isinstance(flags, str):
        raise _Invalid(flags)
    include_tests, include_imports = flags
    try:
        scores = score_nodes(G, query)
        subG, _stats = select_subgraph(G, scores, budget=budget, min_score=_MIN_SCORE,
                                       query=query, token_factor=resolve_token_factor(),
                                       include_tests=include_tests,
                                       include_imports=include_imports)
        text = format_suggestions(suggest_queries(
            G, query, scores, subG, exclude=_left_out(G, include_tests, include_imports)))
    except BudgetTooSmallError as exc:
        raise _Invalid(str(exc)) from None
    except Exception as exc:
        raise _Failed(f"slurp_suggest failed: {type(exc).__name__}: {exc}") from None
    if not text:
        text = ("No follow-up suggested: the budget already covers this "
                "neighbourhood.")
    return text, {"query": query, "budget": budget}


def _tool_diff(args: dict, G=None) -> tuple[str, dict]:
    """slurp_diff: compare two graph files on disk."""
    old_path = _required_text(args, "old_graph")
    new_path = _required_text(args, "new_graph")
    hops = _positive_int(args, "hops", 2)
    try:
        old_graph = load_graph(Path(old_path))
        new_graph = load_graph(Path(new_path))
    except SlurpLoadError as exc:
        raise _Failed(f"slurp_diff could not load a graph: {exc}") from None
    except OSError as exc:
        raise _Failed(f"slurp_diff could not read a graph: {exc}") from None
    except Exception as exc:
        raise _Failed(f"slurp_diff failed: {type(exc).__name__}: {exc}") from None
    try:
        text = format_diff(diff_graphs(old_graph, new_graph), new_graph, hops=hops)
    except Exception as exc:
        raise _Failed(f"slurp_diff failed: {type(exc).__name__}: {exc}") from None
    return text, {"old_graph": old_path, "new_graph": new_path}


_HANDLERS = {
    "slurp_query": _tool_query,
    "slurp_explain": _tool_explain,
    "slurp_impact": _tool_impact,
    "slurp_suggest": _tool_suggest,
    "slurp_diff": _tool_diff,
}


def _call_tool(msg_id, params: dict, G, out, session_log: bool,
               state: "GraphState | None") -> None:
    """Answer one tools/call: freshness first, then the tool, then the envelope."""
    name = params.get("name")
    if name not in _TOOL_NAMES:
        _write(_err(msg_id, -32602, f"Unknown tool: {name!r}"), out)
        return
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        _write(_err(msg_id, -32602, "'arguments' must be an object"), out)
        return

    fresh: Freshness | None = None
    if name != "slurp_diff":   # diff reads two files named by the caller
        if state is not None:
            fresh = state.prepare()
            G = state.graph
        if G is None:
            _write(_err(msg_id, -32603, "Graph not loaded"), out)
            return

    try:
        text, info = _HANDLERS[name](args, G)
    except _Invalid as exc:
        _write(_err(msg_id, -32602, str(exc)), out)
        return
    except _Failed as exc:
        result: dict = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    else:
        if name == "slurp_query" and session_log:
            log_session(info["query"], info["budget"], info["stats"])
        # DECISION: the model reads only `content`, so a warning about the graph
        # goes first in the text; the machine-readable state goes in _meta.
        if fresh is not None and fresh.warnings:
            text = "\n".join(fresh.warnings) + "\n\n" + text
        result = {"content": [{"type": "text", "text": text}]}
    if fresh is not None:
        result["_meta"] = fresh.meta()
    # Only present when federating, so single-graph responses are unchanged.
    if state is not None and state.graphs_count > 1 and not result.get("isError"):
        result["graphs_count"] = state.graphs_count
    _write(_ok(msg_id, result), out)


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
        _write(_ok(msg_id, {"tools": _TOOLS}), out)

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
        _call_tool(msg_id, params, G, out, session_log, state)

    else:
        _write(_err(msg_id, -32601, f"Method not found: {method!r}"), out)


# ---------------------------------------------------------------------------
# Server loop
# ---------------------------------------------------------------------------


def serve(
    graph_path: "Path | list[Path]",
    inp: TextIO | None = None,
    out: TextIO | None = None,
    session_log: bool = True,
    labels: list[str] | None = None,
    refresh: bool | None = None,
    refresh_timeout: float | None = None,
) -> None:
    """Run the MCP stdio server.

    Loads the graph once at startup, then reads newline-delimited JSON-RPC
    messages from *inp* (defaults to sys.stdin) and writes responses to *out*
    (defaults to sys.stdout) until EOF.

    Args:
        graph_path: Path to the graph.json file, or a list of paths to serve
            several projects as one federated graph.
        inp:        Readable file-like object (defaults to sys.stdin).
        out:        Writable file-like object (defaults to sys.stdout).
        session_log: Whether to append served queries to .slurp/session.log.
        labels:     Project labels for a federated graph, one per path.
        refresh:    Keep slurp's own graph current before each call (default:
            SLURP_REFRESH, else on).
        refresh_timeout: Seconds a call waits for a refresh (default:
            SLURP_REFRESH_TIMEOUT, else 2).
    """
    if inp is None:
        inp = sys.stdin
    if out is None:
        out = sys.stdout

    paths = [graph_path] if isinstance(graph_path, Path) else list(graph_path)

    try:
        enabled, seconds = resolve_refresh(refresh is False, refresh_timeout)
    except ValueError as exc:
        print(f"slurp: {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        if len(paths) == 1:
            state: GraphState = GraphState(paths[0], refresh=enabled, timeout=seconds)
        else:
            state = FederatedGraphState(paths, labels, refresh=enabled, timeout=seconds)
    except SlurpLoadError as exc:
        print(f"slurp: error loading graph: {exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"slurp: error federating graphs: {exc}", file=sys.stderr)
        sys.exit(1)
    for tracker in state.trackers:
        if tracker.note:
            print(f"slurp: {tracker.path} is served read-only: {tracker.note}",
                  file=sys.stderr)
    # The startup check runs in the background: `initialize` never waits on it.
    state.start()

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
