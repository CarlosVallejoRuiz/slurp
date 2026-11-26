"""MCP stdio server exposing slurp_query, slurp_explain, slurp_diff, slurp_suggest
and slurp_impact.

Protocol: JSON-RPC 2.0 over newline-delimited stdin/stdout (MCP 2024-11-05).
No external dependencies — stdlib only for the transport layer.
"""

import json
import sys
import time
from dataclasses import dataclass, field
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
# read_session_log lives in slurp.session since v1.3; slurp.mcp still offers it.
from slurp.session import SESSION_LOG_FILE, SessionLog, call_entry
from slurp.session import read_session_log as read_session_log  # noqa: F401
from slurp.loader import SlurpLoadError, load_graph
from slurp.refresh import DEFAULT_TIMEOUT_S, Freshness, GraphTracker, Refresher, resolve_refresh
from slurp.impact import analyze_impact, format_impact
from slurp.suggester import format_suggestions, suggest_queries
from slurp.scorer import clear_pagerank_cache, score_nodes

# Protocol revisions this server speaks (docs/design/mcp-autonomo.md §6). A
# legacy client negotiates once, in `initialize`; a 2026-07-28 client names the
# revision in every request's _meta and is served statelessly.
MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
_ALL_VERSIONS = (*MODERN_VERSIONS, *LEGACY_VERSIONS)
#: What `initialize` answers when the client asks for a revision this server
#: does not speak: the latest legacy one, as the specification says.
_PROTOCOL_VERSION = LEGACY_VERSIONS[0]

_PROTOCOL_META = "io.modelcontextprotocol/protocolVersion"
_CLIENT_META = "io.modelcontextprotocol/clientInfo"
_SERVER_META = "io.modelcontextprotocol/serverInfo"
# How long a 2026-07-28 client may cache the instructions and the tool list.
# They change only when the served graph changes owner (graphify rewrites it).
_TTL_MS = 600_000

# The one default every entry point shares (see budget.DEFAULT_MIN_SCORE), so the
# same query returns the same subgraph through MCP and through the terminal.
_MIN_SCORE = DEFAULT_MIN_SCORE

# DECISION (v1.3, design §5.3): the server instructions and tool descriptions are
# the texts the author approved, written for the model: when to reach for slurp
# before grep, which tool for which moment, what it is not for. Claude Code cuts
# instructions past 4,096 characters and Codex wants the first 512 to stand on
# their own (1,260 and 470 here). #93: the lines for slurp_impact and
# slurp_explain name their argument — both tools are deferred, and in the agent
# measurement the model called slurp_impact before loading its schema. The last sentence of the first paragraph and of
# slurp_query's description say whether the graph follows the working tree, so
# they depend on whose graph is served.
_INSTRUCTIONS_HEAD = (
    "slurp answers structural questions about this codebase from its code graph: "
    "where a behaviour lives, what calls what, and what breaks if a file or "
    "function changes. Use it before grep or reading whole files when you orient "
    "yourself or are about to edit: one call returns the relevant functions, "
    "classes and modules, their relations and their files, within a token budget. "
)
_INSTRUCTIONS_REST = (
    "\n"
    "\n"
    "Which tool:\n"
    "- Starting a task, or unsure where something lives -> slurp_query, in plain "
    "language (\"how an uploaded stats file is validated\").\n"
    "- About to edit a file -> slurp_impact(file_path=...) with its path: which "
    "definitions have callers elsewhere, which only tests use, which are free to "
    "change.\n"
    "- About to change one function or class -> slurp_explain(node_name=...) with "
    "its name: its role, production callers vs tests, risk.\n"
    "- A slurp_query answer looks incomplete -> slurp_suggest with the same query.\n"
    "- Comparing two graph.json snapshots, e.g. two branches -> slurp_diff. Rarely "
    "needed.\n"
    "\n"
    "All five are read-only. Not for exact strings or regexes (use grep), a file "
    "you already know (read it), or non-code files. Answers are pointers: open the "
    "files they name before editing them."
)
_FRESHNESS_SENTENCE = {
    "own": "The graph follows the working tree: edits, new and deleted files are picked up before each answer.",
    "graphify": ("This graph was built by graphify and slurp does not rebuild it: answers "
                 "can lag behind recent edits, and say so when files they cover have "
                 "changed."),
    # Not in the design, which names two cases: a graph that is neither slurp's
    # nor graphify's (a generic or GraphML graph, a slurp 1.2 graph whose root
    # did not validate) must not claim to follow the working tree either.
    "readonly": ("This graph is served as it is on disk and slurp does not rebuild it: "
                 "answers can lag behind recent edits, and say so when files they cover "
                 "have changed."),
}

_QUERY_HEAD = (
    "Find where something lives in this codebase. Returns the functions, classes "
    "and modules most relevant to a plain-language description, with their files "
    "and the call/import relations between them, trimmed to a token budget. Call it "
    "first when starting a task or when you don't know which files are involved — "
    "it is cheaper than grepping and reading files speculatively. Describe "
    "behaviour ('where uploaded stats files are validated'), not a bare identifier: "
    "for one known symbol use slurp_explain; for exact text use grep. Default "
    "budget 4000 tokens; 8000–12000 for broad architecture questions. Tests and "
    "import nodes are left out unless include_tests / include_imports. "
)
_QUERY_FRESHNESS = {
    "own": "Reflects the current working tree, uncommitted edits included.",
    "graphify": "Reflects graph.json as graphify last built it; answers flag files changed since.",
    "readonly": "Reflects graph.json as it is on disk; answers flag files changed since.",
}


def _instructions(variant: str) -> str:
    return _INSTRUCTIONS_HEAD + _FRESHNESS_SENTENCE[variant] + _INSTRUCTIONS_REST


# Tests and import nodes do not compete for a place by default (see
# _graphutils._left_out); these let each kind back in, as the CLI flags do.
_INCLUDE_PROPERTIES = {
    "include_tests": {
        "type": "boolean",
        "default": False,
        "description": "Also rank test code (left out by default).",
    },
    "include_imports": {
        "type": "boolean",
        "default": False,
        "description": "Also rank import statements (left out by default).",
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


# DECISION: every tool is read-only and idempotent, and says so. The hints
# approve nothing in Claude Code: there readOnlyHint lets a call run in parallel
# with other read-only tools, and readOnlyHint/idempotentHint let it be retried
# after a reconnection; approval takes a permission rule or "don't ask again".
# Codex does count readOnlyHint for approval: with default_tools_approval_mode =
# "writes" it only asks about tools not marked read-only. The MCP spec calls
# annotations untrusted hints, so no client has to act on them.
# DECISION (v1.3, design §5.1): readOnlyHint stays although a call may now
# refresh graph.json. What it rewrites is a cache derived from the code, never
# the code — as the server already wrote session.log, and as Claude Code counts
# `git status` read-only though it refreshes .git/index. Dropping the hint
# would lose parallel calls in Claude Code and gain nothing.
_READ_ONLY = {"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False,
               "openWorldHint": False}

_TOOL_DEFINITION = {
    "name": "slurp_query",
    "description": _QUERY_HEAD + _QUERY_FRESHNESS["own"],
    # DECISION (design §5.1, decision 5): with tool search, on by default in
    # Claude Code, only tool names are visible at first; the entry-point tool
    # loads its description from the start. A few hundred tokens a session.
    "_meta": {"anthropic/alwaysLoad": True},
    "annotations": {"title": "Slurp graph query", **_READ_ONLY},
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Plain-language description of the behaviour or feature you "
                               "are looking for.",
            },
            "budget": {
                "type": "integer",
                "description": "Token budget for the answer (default 4000). Raise it for "
                               "broad questions.",
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
    "Explain one function, class or module by name: the role it plays, who calls it "
    "(production callers and tests counted apart), what it calls, and how risky "
    "changing it is. Call it once you know which symbol you will change, or when a "
    "name from slurp_query needs context. Accepts a plain name "
    "('createAdminClient') or a dotted id. An unknown name falls back to the "
    "closest match — check the name in the answer. Structural by default, no API "
    "cost; no_llm=false adds prose only if the user configured an LLM provider."
),
    "annotations": {"title": "Slurp node explanation", **_READ_ONLY},
    "inputSchema": {
        "type": "object",
        "properties": {
            "node_name": {
                "type": "string",
                "description": "Function, class or module name, or a dotted node id.",
            },
            "no_llm": {
                "type": "boolean",
                "default": True,
                "description": "Keep true (default) for the structural answer; false asks "
                               "the configured LLM for prose.",
            },
        },
        "required": ["node_name"],
    },
}

_DIFF_TOOL_DEFINITION = {
    "name": "slurp_diff",
    "description": (
    "Compare two graph.json snapshots — e.g. built on two branches, or before and "
    "after a refactor — and report added, removed and modified nodes plus the "
    "production callers put at risk. Needs paths to two graph files; it does not "
    "read git or the working tree. For the effect of editing a file now, use "
    "slurp_impact."
),
    # Reads two files named by the caller rather than the graph being served.
    # openWorldHint stays false: local disk, no network — debatable.
    "annotations": {"title": "Slurp change impact", **_READ_ONLY},
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
    # #90: the approved text said "An empty answer means…"; the tool answers a sentence.
    "description": (
        "Follow-up queries after a slurp_query answer that looks incomplete. Reads the "
        "nodes that fell just outside the budget and proposes two or three queries that "
        "reach them: a drill-down into the top result, a related cluster, or a "
        "heavily-used neighbour. Pass the same query and budget you used. When the "
        "budget already covered the neighbourhood, the answer says \"No follow-up "
        "suggested\" instead of proposing queries."
    ),
    "annotations": {"title": "Slurp follow-up queries", **_READ_ONLY},
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
    "Blast radius of editing a file, before you edit it. For each definition in the "
    "file: callers in other files (direct and transitive), affected files, risk "
    "level; plus which definitions only tests call and which nothing calls. Call it "
    "before modifying, deleting, renaming or changing the signature of code in a "
    "file. Pass the path relative to the project root; a unique suffix such as "
    "'auth/session.ts' works. Risk is UNKNOWN, not LOW, for languages without a "
    "call graph — don't read that as safe."
),
    "annotations": {"title": "Slurp edit impact", **_READ_ONLY},
    "inputSchema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Project-relative path (or unique suffix) of the file you are "
                               "about to edit.",
            },
            "hops": {
                "type": "integer",
                "default": 2,
                "description": "How many levels of indirect callers to follow (default 2).",
            },
        },
        "required": ["file_path"],
    },
}

_TOOLS = [_TOOL_DEFINITION, _EXPLAIN_TOOL_DEFINITION, _DIFF_TOOL_DEFINITION,
          _SUGGEST_TOOL_DEFINITION, _IMPACT_TOOL_DEFINITION]
_TOOL_NAMES = frozenset(t["name"] for t in _TOOLS)


def _tools_for(variant: str, version: str | None) -> list[dict]:
    """The tool list as this client should see it.

    slurp_query's last sentence follows the graph's owner; a top-level `title`
    (from annotations.title) exists from revision 2025-06-18 on.
    """
    tools = []
    for tool in _TOOLS:
        tool = dict(tool)
        if tool["name"] == "slurp_query":
            tool["description"] = _QUERY_HEAD + _QUERY_FRESHNESS[variant]
        if version is not None and version >= "2025-06-18":
            tool = {"name": tool["name"], "title": tool["annotations"]["title"],
                    **{k: v for k, v in tool.items() if k != "name"}}
        tools.append(tool)
    return tools





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


# DECISION (#93): slurp_impact is deferred (only slurp_query has alwaysLoad), and
# in the agent measurement each first call guessed the argument before loading the
# schema: `target`, `path`. Those names are accepted too; the schema still asks
# for file_path, which wins when both are given, and the log records file_path.
_IMPACT_ALIASES = ("target", "path")


def _tool_impact(args: dict, G) -> tuple[str, dict]:
    """slurp_impact: what editing a file could break, from the graph already loaded."""
    if not args.get("file_path"):
        alias = next((a for a in _IMPACT_ALIASES if args.get(a)), None)
        if alias is not None:
            args = {**args, "file_path": args[alias]}
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


@dataclass
class _Outcome:
    """One tools/call, answered: the result or the JSON-RPC error, and what to log."""

    name: str = ""
    args: dict = field(default_factory=dict)
    result: dict | None = None
    error: tuple[int, str] | None = None
    fresh: Freshness | None = None
    info: dict = field(default_factory=dict)
    failure: str = ""


def _logged_args(name: str, args: dict, info: dict) -> dict:
    """The arguments of a call, in short, for the session log."""
    keys = {"slurp_query": ("query", "budget"), "slurp_suggest": ("query", "budget"),
            "slurp_impact": ("file_path", "hops"), "slurp_explain": ("node_name",),
            "slurp_diff": ("old_graph", "new_graph")}.get(name, ())
    out = {k: info[k] if k in info else args.get(k) for k in keys
           if k in info or k in args}
    if name == "slurp_explain" and info.get("file"):
        out["file"] = info["file"]   # the file of the node it resolved to
    return out


def _call_tool(params: dict, G, state: "GraphState | None",
               invalid_as_tool_error: bool) -> _Outcome:
    """Answer one tools/call: freshness first, then the tool, then the envelope."""
    name = params.get("name")
    outcome = _Outcome(name=str(name))
    if name not in _TOOL_NAMES:
        # An unknown tool is a protocol error in every revision.
        outcome.error = (-32602, f"Unknown tool: {name!r}")
        return outcome
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        outcome.error = (-32602, "'arguments' must be an object")
        return outcome
    outcome.args = args

    if name != "slurp_diff":   # diff reads two files named by the caller
        if state is not None:
            outcome.fresh = state.prepare()
            G = state.graph
        if G is None:
            outcome.error = (-32603, "Graph not loaded")
            return outcome
    fresh = outcome.fresh

    try:
        text, outcome.info = _HANDLERS[name](args, G)
    except _Invalid as exc:
        outcome.failure = str(exc)
        # DECISION (v1.3, design §6, decision 10): revision 2025-11-25 asks for
        # input validation errors as tool errors, so the model reads the message
        # and corrects the call; earlier revisions keep JSON-RPC -32602.
        if not invalid_as_tool_error:
            outcome.error = (-32602, str(exc))
            return outcome
        result: dict = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    except _Failed as exc:
        outcome.failure = str(exc)
        result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    else:
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
    outcome.result = result
    return outcome


class Session:
    """What one stdio connection negotiated — stdio means one client per process."""

    def __init__(self, log: SessionLog | None = None) -> None:
        #: The legacy revision fixed by `initialize`, or None before it.
        self.protocol: str | None = None
        #: clientInfo from `initialize`, or from the latest 2026-07-28 request.
        self.client: dict | None = None
        #: The session log of this process (written only if .slurp/ exists).
        self.log = log


def _variant(state: "GraphState | None") -> str:
    """Which freshness wording the served graphs deserve: own, graphify or readonly."""
    if state is None or all(t.refreshable for t in state.trackers):
        return "own"
    return "graphify" if any(t.kind == "graphify" for t in state.trackers) else "readonly"


def _server_info() -> dict:
    return {"name": "slurp", "version": __version__}


def _handle(msg: dict, G, out, session_log: bool = True, state: "GraphState | None" = None,
            session: Session | None = None) -> None:
    """Dispatch one JSON-RPC message and write the response to *out*.

    Args:
        msg: Parsed JSON-RPC message dict.
        G:   Pre-loaded nx.DiGraph, or None for non-query methods. Ignored when
             *state* is given, which owns the current graph.
        out: File-like object to write responses to.
        session_log: Whether to append successful queries to .slurp/session.log.
        state: Optional GraphState that keeps the graph current. When omitted,
            *G* is served as-is.
        session: What this connection negotiated. A fresh one when omitted —
            no `initialize` yet, so the oldest revision's rules.

    Notifications (messages without an 'id' field) are silently ignored
    per the JSON-RPC 2.0 spec.
    """
    session = Session() if session is None else session
    if "id" not in msg:
        return  # notification — no response required

    msg_id = msg["id"]

    if msg.get("jsonrpc") != "2.0":
        _write(_err(msg_id, -32600, "Invalid Request: 'jsonrpc' must be '2.0'"), out)
        return

    method = msg.get("method", "")
    raw_params = msg.get("params")
    params: dict = raw_params if isinstance(raw_params, dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}

    # A 2026-07-28 request names its revision; a legacy one relies on initialize.
    requested = meta.get(_PROTOCOL_META)
    modern = False
    if requested is None:
        version = session.protocol
    elif requested in MODERN_VERSIONS:
        version, modern = requested, True
    elif requested in LEGACY_VERSIONS:
        version = requested
    else:
        _write({"jsonrpc": "2.0", "id": msg_id, "error": {
            "code": -32022, "message": "Unsupported protocol version",
            "data": {"supported": list(_ALL_VERSIONS), "requested": requested}}}, out)
        return
    if isinstance(meta.get(_CLIENT_META), dict):
        session.client = meta[_CLIENT_META]
    variant = _variant(state)

    def reply(result: dict) -> None:
        if modern:
            # Served statelessly: every result says it is complete and names the server.
            result = {**result, "resultType": "complete",
                      "_meta": {**result.get("_meta", {}), _SERVER_META: _server_info()}}
        _write(_ok(msg_id, result), out)

    if method == "initialize":
        asked = params.get("protocolVersion")
        # DECISION (design §6, decision 7): echo a revision this server speaks,
        # else answer the latest legacy one. Fixed for the whole process.
        session.protocol = asked if asked in LEGACY_VERSIONS else _PROTOCOL_VERSION
        if isinstance(params.get("clientInfo"), dict):
            session.client = params["clientInfo"]
        reply({
            "protocolVersion": session.protocol,
            "capabilities": {"tools": {}},
            "serverInfo": _server_info(),
            "instructions": _instructions(variant),
        })

    elif method == "server/discover":
        _write(_ok(msg_id, {
            "supportedVersions": list(_ALL_VERSIONS),
            "capabilities": {"tools": {}},
            "instructions": _instructions(variant),
            # The instructions depend on the local graph: never shared.
            "ttlMs": _TTL_MS,
            "cacheScope": "private",
            "resultType": "complete",
            "_meta": {_SERVER_META: _server_info()},
        }), out)

    elif method == "ping":
        if modern:   # removed in 2026-07-28
            _write(_err(msg_id, -32601, f"Method not found: {method!r}"), out)
        else:
            reply({})

    elif method == "tools/list":
        result: dict = {"tools": _tools_for(variant, version)}
        if modern:
            result.update(ttlMs=_TTL_MS, cacheScope="private")
        reply(result)

    # DECISION: slurp exposes no resources or prompts, so per spec these
    # methods could return -32601. Several clients probe them at startup
    # regardless of advertised capabilities and treat the error as a failing
    # server, so answer with empty lists instead.
    elif method == "resources/list":
        reply({"resources": []})

    elif method == "resources/templates/list":
        reply({"resourceTemplates": []})

    elif method == "prompts/list":
        reply({"prompts": []})

    elif method == "tools/call":
        tool_errors = modern or (version is not None and version >= "2025-11-25")
        started = time.perf_counter()
        outcome = _call_tool(params, G, state, tool_errors)
        if outcome.error is not None:
            _write(_err(msg_id, *outcome.error), out)
        else:
            assert outcome.result is not None
            reply(outcome.result)
        if session_log and outcome.name in _TOOL_NAMES:
            if session.log is None:
                session.log = SessionLog()
            session.log.call(call_entry(
                client=session.client, protocol=version, tool=outcome.name,
                args=_logged_args(outcome.name, outcome.args, outcome.info),
                ms=(time.perf_counter() - started) * 1000, fresh=outcome.fresh,
                info=outcome.info, error=outcome.failure))

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
    log = SessionLog() if session_log else None
    if log is not None:
        log.start(state.trackers)
    # The startup check runs in the background: `initialize` never waits on it.
    state.start()
    restore = _stop_on_signals()
    try:
        _serve_lines(inp, out, state, session_log=session_log, session=Session(log))
    finally:
        restore()
        if log is not None:
            log.end()


def _stop_on_signals():
    """End the read loop on SIGTERM/SIGHUP as on EOF, so the session log gets its end event.

    DECISION: Claude Code stops its MCP servers with a signal rather than by
    closing stdin (found while preparing the agent measurement): without this
    the process died mid-read and the end event — the files edited during the
    session — was never written. Returns a function restoring the previous
    handlers. Outside the main thread signals cannot be set, and nothing changes.
    """
    import signal

    def stop(signum, frame):
        raise SystemExit(0)

    previous = {}
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, stop)
        except ValueError:     # not the main thread
            pass

    def restore() -> None:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return restore


class _Lines:
    """A stand-in for stdout that keeps what one batch's requests answer."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        self.lines.extend(line for line in text.splitlines() if line.strip())

    def flush(self) -> None:
        pass


def _serve_lines(inp: TextIO, out: TextIO, state: "GraphState | None", *,
                 session_log: bool, session: Session) -> None:
    """Read newline-delimited JSON-RPC from *inp* until EOF, answering on *out*."""
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

        # DECISION (design §6): revision 2025-03-26 is the only one with JSON-RPC
        # batches — each message answered, notifications omitted, one array back.
        if isinstance(msg, list) and msg and session.protocol == "2025-03-26":
            collected = _Lines()
            for item in msg:
                if isinstance(item, dict):
                    _handle(item, state.graph if state else None, collected,
                            session_log=session_log, state=state, session=session)
                else:
                    _write(_err(None, -32600, "Request must be a JSON object"), collected)
            if collected.lines:
                out.write(json.dumps([json.loads(r) for r in collected.lines]) + "\n")
                out.flush()
            continue

        if not isinstance(msg, dict):
            _write(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32600, "message": "Request must be a JSON object"}},
                out,
            )
            continue

        _handle(msg, state.graph if state else None, out, session_log=session_log,
                state=state, session=session)
