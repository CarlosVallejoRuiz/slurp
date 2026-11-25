"""Natural-language explanations of a node and its place in the graph.

`slurp QUERY` tells you *which* nodes matter; this tells you what one of them
does, who depends on it, and what breaks if you change it. The graph supplies
the structure — callers, callees, blast radius — and an LLM turns that into
prose. Without a provider configured the structural explanation stands on its
own, so the command never hard-depends on a network call.
"""

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

from slurp.budget import count_tokens
from slurp._graphutils import (
    _call_graph_gap,
    _caller_counts,
    _callees,
    _display,
    _files_of,
    _label,
    _left_out,
    _risk_band,
    _split_callers,
)

CONFIG_DIR = ".slurp"
CONFIG_FILE = "config.json"

OLLAMA_DEFAULT_HOST = "http://localhost:11434"

PROVIDERS = ("anthropic", "openai", "ollama", "openai-compatible")

# Environment variable each provider reads its API key from.
_ENV_KEY_VARS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-compatible": "SLURP_LLM_API_KEY",
}

# Providers whose call sends the API key to config.endpoint. See resolve_config.
_SENDS_KEY_TO_ENDPOINT = frozenset({"openai", "openai-compatible"})

DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4o-mini",
    "ollama": "llama3.2",
    "openai-compatible": "local-model",
}


_MAX_LISTED_NEIGHBORS = 12
_LLM_TIMEOUT_S = 60

# Generous for a few sentences of prose, deliberately. On current Claude models
# adaptive thinking is on by default and max_tokens caps thinking *plus* the
# reply, so a tight budget truncates the answer mid-sentence.
_MAX_TOKENS_OUT = 4096


class SlurpExplainError(Exception):
    """Raised when an explanation cannot be produced as requested."""


@dataclass
class LLMConfig:
    """Which model to call and how to reach it."""

    provider: str = "structural"
    model: str = ""
    api_key: str | None = None
    endpoint: str | None = None
    temperature: float = 0.3

    def __post_init__(self) -> None:
        if not self.model:
            self.model = DEFAULT_MODELS.get(self.provider, "")


@dataclass
class ExplainResult:
    """A finished explanation and the evidence behind it."""

    node_id: str
    label: str
    explanation: str
    risk_level: str
    callers: list[str] = field(default_factory=list)
    callees: list[str] = field(default_factory=list)
    provider_used: str = "structural"
    context_tokens: int = 0
    node_type: str = ""
    source_file: str = ""
    score: float = 0.0
    model_used: str = ""
    warning: str = ""
    # Structural facts rendered from the graph, never written by a model.
    role: str = ""
    part_of: str = ""
    used_by: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    test_callers: int = 0
    test_files: list[str] = field(default_factory=list)
    production_callers: int = 0
    dependents_pct: float = 0.0
    advice: str = ""
    explore: list[str] = field(default_factory=list)
    # Set when the node's language has no call graph: risk_level is "UNKNOWN".
    risk_reason: str = ""


# ---------------------------------------------------------------------------
# Persistent configuration
# ---------------------------------------------------------------------------

_CONFIG_KEYS = ("provider", "model", "endpoint", "api_key", "temperature")


def config_path(config_dir: Path | None = None) -> Path:
    return (config_dir or Path(CONFIG_DIR)) / CONFIG_FILE


def read_config(config_dir: Path | None = None) -> dict:
    """Read .slurp/config.json, or an empty dict if absent or unreadable."""
    path = config_path(config_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_config(key: str, value: str, config_dir: Path | None = None) -> dict:
    """Set one key in .slurp/config.json, creating the file if needed.

    Args:
        key: One of provider, model, endpoint, api_key, temperature.
        value: Value to store.
        config_dir: Directory holding config.json (default: .slurp).

    Returns:
        The full config dict after the update.

    Raises:
        SlurpExplainError: If the key or a provider value is not recognised.
    """
    if key not in _CONFIG_KEYS:
        raise SlurpExplainError(
            f"Unknown config key {key!r}. Known keys: {', '.join(_CONFIG_KEYS)}"
        )
    if key == "provider" and value not in PROVIDERS:
        raise SlurpExplainError(
            f"Unknown provider {value!r}. Known providers: {', '.join(PROVIDERS)}"
        )

    config = read_config(config_dir)
    config[key] = value
    path = config_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_owner_only(path, json.dumps(config, indent=2))
    return config


def _write_owner_only(path: Path, text: str) -> None:
    """Write *text* to *path* with permissions 0600.

    DECISION: config.json can hold an API key, so it is readable by its owner
    only. The mode passed to os.open applies to a new file; an existing file
    keeps its old bits, so it is tightened before the secret is written rather
    than after.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, "fchmod"):  # not available on Windows
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _ollama_running(host: str = OLLAMA_DEFAULT_HOST) -> bool:
    """True if an Ollama server answers on *host* right now."""
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=1.0) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def detect_provider(env: dict | None = None, check_ollama: bool = True) -> str:
    """Pick a provider from the environment.

    Order is cheapest-to-verify first, with the two hosted SDKs ahead of the
    local server so an explicit API key always wins over a stray daemon.

    Args:
        env: Environment mapping (defaults to os.environ).
        check_ollama: Whether to probe localhost for Ollama. Disabled in tests
            and whenever a network probe would be inappropriate.

    Returns:
        A provider name, or "structural" when nothing is configured.
    """
    environ = os.environ if env is None else env
    if environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if environ.get("OPENAI_API_KEY"):
        return "openai"
    if check_ollama and _ollama_running(environ.get("OLLAMA_HOST") or OLLAMA_DEFAULT_HOST):
        return "ollama"
    if environ.get("SLURP_LLM_ENDPOINT"):
        return "openai-compatible"
    return "structural"


def resolve_config(
    provider: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
    config_dir: Path | None = None,
    env: dict | None = None,
    check_ollama: bool = True,
) -> LLMConfig:
    """Combine CLI flags, .slurp/config.json, and the environment.

    Precedence is flags, then the saved config, then auto-detection — an
    explicit flag must never be silently overridden by a stored preference.

    Args:
        provider: Provider from --provider, if given.
        model: Model from --model, if given.
        endpoint: Endpoint from --endpoint, if given.
        config_dir: Directory holding config.json.
        env: Environment mapping (defaults to os.environ).
        check_ollama: Whether auto-detection may probe localhost.

    Returns:
        A fully resolved LLMConfig.

    Raises:
        SlurpExplainError: If the endpoint comes from the project config while
            the API key comes from the environment, for a provider that would
            send that key to that endpoint.
    """
    environ = os.environ if env is None else env
    saved = read_config(config_dir)

    chosen = provider or saved.get("provider") or detect_provider(environ, check_ollama)
    chosen_model = model or saved.get("model") or DEFAULT_MODELS.get(chosen, "")
    chosen_endpoint = (
        endpoint
        or saved.get("endpoint")
        or environ.get("SLURP_LLM_ENDPOINT")
    )
    endpoint_from_project = not endpoint and bool(saved.get("endpoint"))

    api_key = saved.get("api_key")
    env_key_var = _ENV_KEY_VARS.get(chosen)
    key_from_env = False
    if not api_key and env_key_var and environ.get(env_key_var):
        api_key = environ[env_key_var]
        key_from_env = True

    # DECISION: a key from the user's environment is never sent to an endpoint
    # read from the project config. .slurp/config.json can be committed by any
    # repository the user clones, so its endpoint is not something the user
    # chose; pairing it with OPENAI_API_KEY would hand that key to whoever wrote
    # the file. The user opts in by naming the endpoint themselves (--endpoint,
    # SLURP_LLM_ENDPOINT) or by storing a key in the same config. Anthropic is
    # not in the set only because _call_anthropic ignores `endpoint` today —
    # wiring a base_url there must add it.
    if endpoint_from_project and key_from_env and chosen in _SENDS_KEY_TO_ENDPOINT:
        raise SlurpExplainError(
            f"Refusing to send {env_key_var} from your environment to the endpoint "
            f"{chosen_endpoint} set in {config_path(config_dir)}. A project config "
            "can come from any repository you clone. If you trust this endpoint, "
            "pass --endpoint or set SLURP_LLM_ENDPOINT; or store a key for it "
            "with `slurp config set api_key <key>`."
        )

    temperature = saved.get("temperature", 0.3)
    try:
        temperature = float(temperature)
    except (TypeError, ValueError):
        temperature = 0.3

    return LLMConfig(
        provider=chosen,
        model=chosen_model,
        api_key=api_key,
        endpoint=chosen_endpoint,
        temperature=temperature,
    )


# ---------------------------------------------------------------------------
# Graph reading
# ---------------------------------------------------------------------------

def find_node(G: nx.DiGraph, query: str, scores: dict[str, float] | None = None) -> str:
    """Resolve a query to the node it most likely names.

    An exact node id or label match wins outright — asking about a node by name
    should never be beaten by relevance scoring, even when it is a test.
    Otherwise the highest-scoring node is used, skipping the tests and import
    nodes a query leaves out (see _graphutils._left_out) unless nothing else
    scored.

    Args:
        G: The graph.
        query: Node id, label, or free-text query.
        scores: Optional relevance scores for the fallback.

    Returns:
        The chosen node id.

    Raises:
        SlurpExplainError: If the graph is empty or nothing matches.
    """
    if G.number_of_nodes() == 0:
        raise SlurpExplainError("Graph has no nodes.")

    if query in G:
        return query

    normalized = query.strip().rstrip("()").lower()
    exact = [n for n in G.nodes if str(_label(G, n)).lower() == normalized]
    if exact:
        return sorted(exact, key=lambda n: (-(scores or {}).get(n, 0.0), n))[0]

    suffix = [n for n in G.nodes if str(n).lower().endswith(f".{normalized}")]
    if suffix:
        return sorted(suffix, key=lambda n: (-(scores or {}).get(n, 0.0), n))[0]

    if scores:
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        for skip in (_left_out(G), frozenset()):
            for node_id, score in ranked:
                if node_id in G and score > 0 and node_id not in skip:
                    return node_id

    raise SlurpExplainError(
        f"No node matches {query!r}. Try `slurp \"{query}\"` to search the graph."
    )


_ROLE_HUB_CALLERS = 10
_ROLE_ORCHESTRATOR_MIN = 5


def _by_importance(G: nx.DiGraph, nodes: list[str], limit: int = 3) -> list[str]:
    """The *limit* most-depended-on nodes, most central first."""
    return sorted(nodes, key=lambda n: (-G.in_degree(n), n))[:limit]


def _parent_module(G: nx.DiGraph, node_id: str) -> str:
    """Node that `contains` this one, if any."""
    for pred in G.predecessors(node_id):
        if G.edges[pred, node_id].get("relation") == "contains":
            return pred
    return ""


def _architectural_role(G: nx.DiGraph, node_id: str) -> str:
    """One phrase naming the part this node plays in the codebase.

    Derived from its position rather than its name: how many things call it,
    how many it calls, and whether it holds other definitions.
    """
    attrs = G.nodes[node_id]
    ntype = attrs.get("type", "")
    production, _tests = _split_callers(G, node_id)
    callees = _callees(G, node_id)

    # Every role below is read off calls. Without a call graph, "isolated" or
    # "entry point" would be a claim the graph cannot back.
    if ntype != "module" and _call_graph_gap(G, node_id):
        return "Role unknown — no call graph for this language"

    if ntype == "module":
        held = sum(1 for _, t in G.out_edges(node_id)
                   if G.edges[node_id, t].get("relation") == "contains")
        importers = sum(1 for sid, _ in G.in_edges(node_id)
                        if G.edges[sid, node_id].get("relation") == "imports_from")
        return (f"Core module — {held} definition{'s' if held != 1 else ''}, "
                f"imported by {importers} file{'s' if importers != 1 else ''}")

    if len(production) > _ROLE_HUB_CALLERS:
        return "Central utility — called across the entire codebase"
    if len(production) >= _ROLE_ORCHESTRATOR_MIN and len(callees) >= _ROLE_ORCHESTRATOR_MIN:
        return "Orchestrator — coordinates several components"
    if len(production) >= _ROLE_ORCHESTRATOR_MIN:
        return "Shared utility — many callers, few dependencies of its own"
    if not production and len(callees) >= _ROLE_ORCHESTRATOR_MIN:
        return "Entry point — top-level, nothing in the project calls it"
    if not production and not callees:
        return "Isolated — nothing calls it and it calls nothing"
    return "Leaf — focused, with a small surface"


def _dependents_share(G: nx.DiGraph, node_id: str) -> float:
    """Share of the project's nodes that depend on this one, as a percentage."""
    total = G.number_of_nodes()
    if not total:
        return 0.0
    production, _tests = _split_callers(G, node_id)
    return round(len(production) / total * 100, 1)


def _risk_advice(G: nx.DiGraph, node_id: str, risk: str) -> str:
    """What to actually do about the risk, in one line."""
    if risk == "UNKNOWN":
        return ("Search the code for its usages before changing it — the graph "
                "cannot show who calls it.")
    if risk == "HIGH":
        return "Add tests covering the current behaviour before modifying."
    if risk == "MEDIUM":
        return "Check each caller before changing the signature."
    production, tests = _split_callers(G, node_id)
    if tests and not production:
        return ""   # the risk line already says what to do: update the tests
    return "Safe to refactor — little depends on it."


def _explore_further(G: nx.DiGraph, node_id: str, risk: str) -> list[str]:
    """Two or three commands that continue from here."""
    label = _display(G, node_id).rstrip("()")
    out: list[str] = []
    parent = _parent_module(G, node_id)
    if parent:
        out.append(f'slurp "{label} {_display(G, parent).rstrip("()")}"'
                   "  — the surrounding context")
    production, _tests = _split_callers(G, node_id)
    top = _by_importance(G, production, limit=1)
    if top:
        out.append(f"slurp explain '{_display(G, top[0]).rstrip('()')}'"
                   "  — its most connected caller")
    if risk == "HIGH":
        out.append("slurp diff old.json new.json  — impact before you ship")
    elif len(out) < 2:
        out.append(f'slurp "{label}" --inject-code  — read the body')
    return out[:3]


def _risk_level(G: nx.DiGraph, node_id: str) -> str:
    """Rate how risky it is to change *node_id*, by direct dependent count.

    Args:
        G: The graph.
        node_id: Node to rate.

    A containing module is not a dependant — it owns the node rather than
    calling it — so every function used to start one dependant ahead of its
    real count, pushing a single-caller helper up to MEDIUM.

    DECISION: only production callers count, as in `slurp diff` and `slurp
    impact`. Until v1.2 explain added the tests in, so a helper with one caller
    and sixty tests was HIGH here and low in diff. The tests are shown apart.

    Returns:
        "HIGH" (6+ production callers), "MEDIUM" (2–5), "LOW" (0–1), or
        "UNKNOWN" when the node's language has no call graph and its callers
        cannot be known.
    """
    if _call_graph_gap(G, node_id):
        return "UNKNOWN"
    production, _tests = _split_callers(G, node_id)
    return _risk_band(len(production))


def _neighbors_within(G: nx.DiGraph, node_id: str, hops: int) -> list[tuple[str, int]]:
    """Nodes reachable within *hops* edges, ignoring direction, with distance."""
    if node_id not in G or hops <= 0:
        return []
    undirected = G.to_undirected(as_view=True)
    lengths = nx.single_source_shortest_path_length(undirected, node_id, cutoff=hops)
    return sorted(
        ((n, d) for n, d in lengths.items() if n != node_id),
        key=lambda pair: (pair[1], pair[0]),
    )


def _relation(G: nx.DiGraph, a: str, b: str) -> str:
    """Relation label of whichever edge connects *a* and *b*."""
    if G.has_edge(a, b):
        return G.edges[a, b].get("relation", "related")
    if G.has_edge(b, a):
        return G.edges[b, a].get("relation", "related")
    return "related"


def _build_context(
    G: nx.DiGraph,
    node_id: str,
    scores: dict[str, float] | None = None,
    hops: int = 2,
) -> str:
    """Serialise a node and its neighbourhood as prompt context.

    Args:
        G: The graph.
        node_id: The node being explained.
        scores: Relevance scores, used to report the node's own score.
        hops: Neighbourhood radius.

    Returns:
        Structured plain text — node facts, callers, callees, and the wider
        neighbourhood — suitable for an LLM prompt or for reading directly.
    """
    if node_id not in G:
        raise SlurpExplainError(f"Node not in graph: {node_id}")

    attrs = G.nodes[node_id]
    scores = scores or {}
    lines: list[str] = [
        f"NODE: {_display(G, node_id)}",
        f"  id: {node_id}",
        f"  type: {attrs.get('type') or attrs.get('file_type') or 'unknown'}",
    ]
    source = attrs.get("source_file") or attrs.get("file_path")
    if source:
        lines.append(f"  source file: {source}")
    if attrs.get("source_location"):
        lines.append(f"  location: {attrs['source_location']}")
    if attrs.get("description"):
        lines.append(f"  description: {attrs['description']}")
    if node_id in scores:
        lines.append(f"  relevance score: {scores[node_id]:.3f}")
    if attrs.get("_project"):
        lines.append(f"  project: {attrs['_project']}")

    # The same rule the rendered facts use: a containing module owns the node,
    # it does not depend on it. Feeding the model the larger number had it
    # writing "4 direct dependents" above a RISK line that said 3.
    production, tests = _split_callers(G, node_id)
    if gap := _call_graph_gap(G, node_id):
        lines.append("")
        lines.append(f"NOTE: {gap}. The caller and callee lists below are unknown, "
                     "not empty — do not describe this code as unused.")
    lines.append("")
    lines.append(f"CALLED BY ({len(production)}):")
    if production:
        for caller in production[:_MAX_LISTED_NEIGHBORS]:
            rel = G.edges[caller, node_id].get("relation", "references")
            lines.append(
                f"  - {_display(G, caller)} "
                f"[{G.nodes[caller].get('type', '?')}] --{rel}-->"
            )
        if len(production) > _MAX_LISTED_NEIGHBORS:
            lines.append(f"  … and {len(production) - _MAX_LISTED_NEIGHBORS} more")
    else:
        lines.append("  (no production code depends on this node)" if tests
                     else "  (nothing depends on this node)")
    if tests:
        files = _files_of(G, tests)
        lines.append("")
        lines.append(f"TESTED BY ({len(tests)} test{'' if len(tests) == 1 else 's'} in "
                     f"{len(files)} file{'' if len(files) == 1 else 's'}):")
        for test in tests[:_MAX_LISTED_NEIGHBORS]:
            lines.append(f"  - {_display(G, test)} [{G.nodes[test].get('type', '?')}]")
        if len(tests) > _MAX_LISTED_NEIGHBORS:
            lines.append(f"  … and {len(tests) - _MAX_LISTED_NEIGHBORS} more")
    callers = production + tests

    callees = _callees(G, node_id)
    lines.append("")
    lines.append(f"CALLS ({len(callees)}):")
    if callees:
        for callee in callees[:_MAX_LISTED_NEIGHBORS]:
            rel = G.edges[node_id, callee].get("relation", "references")
            lines.append(
                f"  - --{rel}--> {_display(G, callee)} "
                f"[{G.nodes[callee].get('type', '?')}]"
            )
        if len(callees) > _MAX_LISTED_NEIGHBORS:
            lines.append(f"  … and {len(callees) - _MAX_LISTED_NEIGHBORS} more")
    else:
        lines.append("  (this node calls nothing)")

    direct = set(callers) | set(callees)
    wider = [
        (n, d) for n, d in _neighbors_within(G, node_id, hops)
        if n not in direct
    ]
    if wider:
        lines.append("")
        lines.append(f"NEIGHBOURHOOD (within {hops} hops):")
        for neighbor, distance in wider[:_MAX_LISTED_NEIGHBORS * 2]:
            n_attrs = G.nodes[neighbor]
            lines.append(
                f"  - {_display(G, neighbor)} "
                f"[{n_attrs.get('type', '?')}] · {distance} hops"
            )
        if len(wider) > _MAX_LISTED_NEIGHBORS * 2:
            lines.append(f"  … and {len(wider) - _MAX_LISTED_NEIGHBORS * 2} more")

    lines.append("")
    lines.append(f"BLAST RADIUS: {_caller_counts(len(production), len(tests))} "
                 f"(risk {_risk_level(G, node_id)})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Structural explanation (no LLM)
# ---------------------------------------------------------------------------

def _join_display(G: nx.DiGraph, node_ids: list[str], limit: int = 3) -> str:
    shown = [_display(G, n) for n in node_ids[:limit]]
    text = ", ".join(shown)
    extra = len(node_ids) - len(shown)
    return f"{text} and {extra} more" if extra > 0 else text


def _explain_structural(
    G: nx.DiGraph,
    node_id: str,
    scores: dict[str, float] | None = None,
) -> str:
    """Describe a node from graph structure alone, with no LLM involved.

    This is the fallback whenever no provider is configured or a call fails,
    and it is what makes `slurp explain` work offline.

    Args:
        G: The graph.
        node_id: The node to describe.
        scores: Optional relevance scores.

    Returns:
        A short prose description.
    """
    if node_id not in G:
        raise SlurpExplainError(f"Node not in graph: {node_id}")

    attrs = G.nodes[node_id]
    name = _display(G, node_id)
    node_type = attrs.get("type") or attrs.get("file_type") or "node"
    if node_type in ("code", "document", "markdown"):
        node_type += " node"       # "is a code" does not read as English
    callees = _callees(G, node_id)

    sentences: list[str] = []

    opening = f"{name} is a {node_type}"
    source = attrs.get("source_file") or attrs.get("file_path")
    if source:
        location = str(attrs.get("source_location", "")).lstrip("L")
        opening += f" defined in {source}"
        if location.isdigit():
            opening += f":{location}"
    sentences.append(opening + ".")
    sentences.append(_architectural_role(G, node_id) + ".")

    if attrs.get("description"):
        sentences.append(str(attrs["description"]).rstrip(".") + ".")

    production, tests = _split_callers(G, node_id)
    if production:
        shown = _by_importance(G, production, limit=3)
        sentences.append(
            f"It is called by {len(production)} "
            f"{'place' if len(production) == 1 else 'places'} in the project"
            + (f" and {len(tests)} in tests" if tests else "")
            + f", chiefly {_join_display(G, shown)}."
        )
    elif tests:
        sentences.append(
            f"Only tests call it ({len(tests)}), so nothing in the shipped "
            "code depends on it yet."
        )
    elif _call_graph_gap(G, node_id):
        pass  # the role sentence says it; RISK IF CHANGED gives the reason
    else:
        sentences.append(
            "Nothing in the graph references it, so it is either an entry point "
            "or unused."
        )

    if callees:
        sentences.append(
            f"It depends on {_join_display(G, _by_importance(G, callees, limit=3))}."
        )

    if scores and node_id in scores:
        sentences.append(f"Relevance to your query: {scores[node_id]:.3f}.")

    return " ".join(sentences)


# ---------------------------------------------------------------------------
# LLM providers
# ---------------------------------------------------------------------------

# DECISION: the model writes prose only. Callers, callees and blast radius are
# facts already in the graph and are rendered from it — asking the model to
# restate them would invite it to get them subtly wrong.
# DECISION: the instructions are a separate constant from the context so the
# Anthropic provider can mark them as a cacheable prefix. Caching is a prefix
# match — the stable half has to come first and on its own. The other three
# providers still receive one concatenated string, which _PROMPT rebuilds.
_SYSTEM_PROMPT = """\
You are explaining one node of a codebase knowledge graph to a developer who \
is about to work on it.

The user message contains structured data about the node: its type, source \
file, what depends on it, and what it depends on.

Write 2-4 sentences explaining what this node does and why it exists, inferring \
from its name, type, description and relationships. Then write one more sentence \
on what would break if it changed, given its blast radius.

Rules:
- Plain prose. No markdown, no headers, no bullets, no preamble.
- Do not list the callers and callees back to me — they are shown separately.
- Do not invent function bodies, file contents, parameters, or behaviour the \
data above does not support. Say "likely" when you are inferring.\
"""

_PROMPT = _SYSTEM_PROMPT + "\n\n{context}"


# Last call's cache counters, so a caller can verify caching actually happened
# rather than assume it. Zero reads across repeated calls means the prefix is
# below the model's minimum or something is invalidating it.
_LAST_CACHE_USAGE: dict[str, int] = {}


def _record_cache_usage(usage) -> None:
    """Record the cache counters the API reported for the last call."""
    _LAST_CACHE_USAGE.clear()
    if usage is None:
        return
    for counter in ("cache_creation_input_tokens", "cache_read_input_tokens",
                    "input_tokens", "output_tokens"):
        value = getattr(usage, counter, None)
        if isinstance(value, int):
            _LAST_CACHE_USAGE[counter] = value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SlurpExplainError(message)


def _call_anthropic(prompt: str, config: LLMConfig) -> str:
    try:
        import anthropic
    except ImportError:
        raise SlurpExplainError(
            "The anthropic provider requires the anthropic package.\n"
            "Install it with: pip install anthropic"
        ) from None
    _require(
        bool(config.api_key),
        "No Anthropic API key found. Set ANTHROPIC_API_KEY, or run "
        "`slurp config set api_key <key>`.",
    )
    client = anthropic.Anthropic(api_key=config.api_key)
    # DECISION: no `temperature`. Current Claude models (Sonnet 5, Opus 5, Opus
    # 4.8/4.7, Fable 5) removed the sampling parameters and reject the request
    # with a 400 if one is sent. `temperature` still applies to the other
    # providers, which is why it stays on LLMConfig.
    #
    # DECISION: the instructions go in `system` with a cache breakpoint and the
    # node context goes in the user message. Caching is a prefix match, so the
    # stable half must come first; marking the whole prompt would cache a
    # prefix that changes with every node and never hit.
    #
    # No beta header: prompt caching is GA. Sending the retired
    # `prompt-caching-2024-07-15` header is the same class of mistake that the
    # sampling parameters above already cost us — a 400 on a request that used
    # to work.
    context = prompt.removeprefix(_SYSTEM_PROMPT).lstrip("\n")
    message = client.messages.create(
        model=config.model,
        max_tokens=_MAX_TOKENS_OUT,
        system=[{
            "type": "text",
            "text": _SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": context}],
    )
    _record_cache_usage(getattr(message, "usage", None))
    return "".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    ).strip()


def _call_openai(prompt: str, config: LLMConfig) -> str:
    try:
        import openai
    except ImportError:
        raise SlurpExplainError(
            "The openai provider requires the openai package.\n"
            "Install it with: pip install openai"
        ) from None
    _require(
        bool(config.api_key),
        "No OpenAI API key found. Set OPENAI_API_KEY, or run "
        "`slurp config set api_key <key>`.",
    )
    client = openai.OpenAI(api_key=config.api_key, base_url=config.endpoint or None)
    response = client.chat.completions.create(
        model=config.model,
        temperature=config.temperature,
        max_tokens=_MAX_TOKENS_OUT,
        messages=[{"role": "user", "content": prompt}],
    )
    return (response.choices[0].message.content or "").strip()


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    """POST JSON and return the parsed response."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_LLM_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise SlurpExplainError(
            f"{url} returned HTTP {exc.code}: {detail}"
        ) from None
    except urllib.error.URLError as exc:
        raise SlurpExplainError(f"Could not reach {url}: {exc.reason}") from None
    except (TimeoutError, ValueError) as exc:
        raise SlurpExplainError(f"Bad response from {url}: {exc}") from None


def _call_ollama(prompt: str, config: LLMConfig) -> str:
    host = (config.endpoint or OLLAMA_DEFAULT_HOST).rstrip("/")
    # Accept either a bare host or a full /api/generate URL.
    url = host if host.endswith("/api/generate") else f"{host}/api/generate"
    data = _post_json(url, {
        "model": config.model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": config.temperature},
    }, {})
    text = str(data.get("response", "")).strip()
    _require(bool(text), f"Ollama returned an empty response for model {config.model!r}.")
    return text


def _call_openai_compatible(prompt: str, config: LLMConfig) -> str:
    _require(
        bool(config.endpoint),
        "The openai-compatible provider needs an endpoint. Pass --endpoint, set "
        "SLURP_LLM_ENDPOINT, or run `slurp config set endpoint <url>`.",
    )
    base = str(config.endpoint).rstrip("/")
    url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    data = _post_json(url, {
        "model": config.model,
        "temperature": config.temperature,
        "max_tokens": _MAX_TOKENS_OUT,
        "messages": [{"role": "user", "content": prompt}],
    }, headers)
    try:
        text = str(data["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError):
        raise SlurpExplainError(
            f"Unexpected response shape from {url}: {json.dumps(data)[:200]}"
        ) from None
    _require(bool(text), f"{url} returned an empty completion.")
    return text


_DISPATCH = {
    "anthropic": _call_anthropic,
    "openai": _call_openai,
    "ollama": _call_ollama,
    "openai-compatible": _call_openai_compatible,
}


def _call_llm(prompt: str, config: LLMConfig) -> str:
    """Send *prompt* to the configured provider and return its text.

    Args:
        prompt: The full prompt.
        config: Provider, model, credentials, endpoint.

    Returns:
        The model's reply, stripped.

    Raises:
        SlurpExplainError: If the provider is unknown, its SDK is missing, its
            credentials are absent, or the call fails.
    """
    handler = _DISPATCH.get(config.provider)
    if handler is None:
        raise SlurpExplainError(
            f"Unknown provider {config.provider!r}. "
            f"Known providers: {', '.join(PROVIDERS)}"
        )
    return handler(prompt, config)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def explain_node(
    G: nx.DiGraph,
    node_id: str,
    scores: dict[str, float] | None = None,
    config: LLMConfig | None = None,
    hops: int = 2,
) -> ExplainResult:
    """Explain one node, using an LLM when one is available.

    A provider failure is never fatal: the structural explanation is produced
    first and stands in whenever the model cannot be reached, with the reason
    reported on the result rather than raised.

    Args:
        G: The graph.
        node_id: Node to explain.
        scores: Relevance scores.
        config: Provider configuration. None or provider "structural" skips
            the LLM entirely.
        hops: Neighbourhood radius for context.

    Returns:
        An ExplainResult; `provider_used` says which path actually produced it.

    Raises:
        SlurpExplainError: If the node is not in the graph.
    """
    if node_id not in G:
        raise SlurpExplainError(f"Node not in graph: {node_id}")

    scores = scores or {}
    attrs = G.nodes[node_id]
    context = _build_context(G, node_id, scores, hops=hops)

    result = ExplainResult(
        node_id=node_id,
        label=_display(G, node_id),
        explanation=_explain_structural(G, node_id, scores),
        risk_level=_risk_level(G, node_id),
        callers=[],   # filled below, excluding the containing module

        callees=[_display(G, n) for n in _callees(G, node_id)],
        provider_used="structural",
        context_tokens=count_tokens(context),
        node_type=attrs.get("type") or attrs.get("file_type") or "",
        source_file=attrs.get("source_file") or attrs.get("file_path") or "",
        score=round(scores.get(node_id, 0.0), 4),
    )
    production, tests = _split_callers(G, node_id)
    result.callers = [_display(G, n) for n in production]
    result.test_files = _files_of(G, tests)
    parent = _parent_module(G, node_id)
    result.role = _architectural_role(G, node_id)
    result.part_of = _display(G, parent) if parent else ""
    result.used_by = [_display(G, n) for n in _by_importance(G, production)]
    result.depends_on = [_display(G, n) for n in _by_importance(G, _callees(G, node_id))]
    result.test_callers = len(tests)
    result.production_callers = len(production)
    result.dependents_pct = _dependents_share(G, node_id)
    result.risk_reason = _call_graph_gap(G, node_id)
    result.advice = _risk_advice(G, node_id, result.risk_level)
    result.explore = _explore_further(G, node_id, result.risk_level)

    if config is None or config.provider in ("structural", ""):
        return result

    try:
        text = _call_llm(_PROMPT.format(context=context), config)
    except SlurpExplainError as exc:
        result.warning = f"{config.provider} unavailable ({exc}); showed structural analysis"
        return result
    except Exception as exc:
        result.warning = (
            f"{config.provider} call failed ({type(exc).__name__}: {exc}); "
            "showed structural analysis"
        )
        return result

    # A blank or whitespace-only reply is a failed call, not an explanation —
    # keep the structural text rather than replacing it with nothing.
    if text and text.strip():
        result.explanation = text.strip()
        result.provider_used = config.provider
        result.model_used = config.model
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_ARCH_INDENT = " " * len("→ Called by: ")


def _wrap_list(items: list[str], prefix: str, width: int = 66) -> list[str]:
    """Lay out a comma-separated list under *prefix*, wrapping aligned."""
    if not items:
        return [f"{prefix}nothing"]
    lines: list[str] = []
    current = prefix
    indent = " " * len(prefix)
    for index, item in enumerate(items):
        piece = item + ("," if index < len(items) - 1 else "")
        candidate = f"{current}{piece} " if current.strip() else f"{indent}{piece} "
        if len(candidate.rstrip()) > width and current.strip() != prefix.strip():
            lines.append(current.rstrip())
            current = f"{indent}{piece} "
        else:
            current = candidate
    if current.strip():
        lines.append(current.rstrip().rstrip(","))
    return lines


_ARCH_LIST_MAX = 6


def format_explanation(result: ExplainResult, width: int = 69) -> str:
    """Render an ExplainResult as the plain-text body shown under the panel.

    Args:
        result: The explanation to render.
        width: Column width for the closing rule.

    Returns:
        Multi-line text without a trailing newline.
    """
    lines: list[str] = ["EXPLANATION", result.explanation, ""]

    if result.part_of or result.used_by or result.depends_on:
        lines.append("CONTEXT")
        if result.part_of:
            lines.append(f"Part of:     {result.part_of}")
        if result.used_by:
            lines.append(f"Used by:     {', '.join(result.used_by)}")
        if result.depends_on:
            lines.append(f"Depends on:  {', '.join(result.depends_on)}")
        lines.append("")

    # DECISION: the full caller list is capped. select_subgraph has eighty,
    # and printing them turned a explanation into ninety lines of test names
    # that buried every other section.
    lines.append("ARCHITECTURE")
    if result.risk_reason:
        # Empty lists here are a gap in the graph, not an absence of calls.
        lines.append("→ Called by: unknown — no call graph for this language")
        lines.append("← Calls: unknown")
    else:
        shown = result.callers[:_ARCH_LIST_MAX]
        extra = len(result.callers) - len(shown)
        lines.extend(_wrap_list(
            shown + ([f"+{extra} more"] if extra > 0 else []), "→ Called by: "))
        if result.test_callers:
            files = len(result.test_files)
            lines.append(f"→ Tested by: {result.test_callers} "
                         f"test{'' if result.test_callers == 1 else 's'}"
                         + (f" in {files} file{'' if files == 1 else 's'}" if files else ""))
        lines.extend(_wrap_list(result.callees[:_ARCH_LIST_MAX], "← Calls: "))
    lines.append("")

    noun = result.node_type or "node"
    # Fall back to the caller list when the count was not populated, so a
    # result built by hand still renders an accurate line. `callers` holds
    # production callers only.
    count = result.production_callers or len(result.callers)
    tests = result.test_callers
    lines.append(f"RISK IF CHANGED: {result.risk_level}")
    if result.risk_reason:
        summary = f"Callers unknown: {result.risk_reason}"
    elif count:
        summary = _caller_counts(count, tests)
    elif tests:
        # DECISION: never "safe" while tests call it: nothing shipped breaks,
        # but its tests do. SAFE is for code with no caller of either kind.
        summary = (f"No production callers · {tests} test{'' if tests == 1 else 's'} "
                   f"call{'s' if tests == 1 else ''} it — update them if you change "
                   "its behaviour")
    else:
        summary = f"Nothing in the project depends on this {noun}"
    if result.dependents_pct:
        summary += f" — {result.dependents_pct}% of the project"
    lines.append(summary + ".")
    if result.advice:
        lines.append(result.advice)

    if result.explore:
        lines.append("")
        lines.append("EXPLORE FURTHER")
        lines.extend(f"  {cmd}" for cmd in result.explore)

    lines.append("")
    lines.append("─" * width)
    provider = result.provider_used
    if result.model_used:
        provider += f" ({result.model_used})"
    lines.append(f"Provider: {provider} · Context: {result.context_tokens:,} tokens")
    if result.warning:
        lines.append(f"Note: {result.warning}")
    return "\n".join(lines)
