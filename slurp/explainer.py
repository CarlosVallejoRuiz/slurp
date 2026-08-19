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

CONFIG_DIR = ".slurp"
CONFIG_FILE = "config.json"

OLLAMA_DEFAULT_HOST = "http://localhost:11434"

PROVIDERS = ("anthropic", "openai", "ollama", "openai-compatible")

DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4o-mini",
    "ollama": "llama3.2",
    "openai-compatible": "local-model",
}

# Risk is measured in direct dependents: nodes that would have to change with it.
_RISK_HIGH_MIN = 6
_RISK_MEDIUM_MIN = 2

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
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


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

    api_key = saved.get("api_key")
    if chosen == "anthropic":
        api_key = api_key or environ.get("ANTHROPIC_API_KEY")
    elif chosen == "openai":
        api_key = api_key or environ.get("OPENAI_API_KEY")
    elif chosen == "openai-compatible":
        api_key = api_key or environ.get("SLURP_LLM_API_KEY")

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

def _label(G: nx.DiGraph, node_id: str) -> str:
    return G.nodes[node_id].get("label", node_id) if node_id in G else node_id


def _display(G: nx.DiGraph, node_id: str) -> str:
    """Render a node as `name()` for functions/methods, plain otherwise."""
    label = _label(G, node_id)
    node_type = G.nodes[node_id].get("type", "") if node_id in G else ""
    return f"{label}()" if node_type in {"function", "method"} else label


def find_node(G: nx.DiGraph, query: str, scores: dict[str, float] | None = None) -> str:
    """Resolve a query to the node it most likely names.

    An exact node id or label match wins outright — asking about a node by name
    should never be beaten by relevance scoring. Otherwise the highest-scoring
    node is used.

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
        for node_id, score in ranked:
            if node_id in G and score > 0:
                return node_id

    raise SlurpExplainError(
        f"No node matches {query!r}. Try `slurp \"{query}\"` to search the graph."
    )


def _callers(G: nx.DiGraph, node_id: str) -> list[str]:
    return sorted(G.predecessors(node_id)) if node_id in G else []


def _callees(G: nx.DiGraph, node_id: str) -> list[str]:
    return sorted(G.successors(node_id)) if node_id in G else []


def _risk_level(G: nx.DiGraph, node_id: str) -> str:
    """Rate how risky it is to change *node_id*, by direct dependent count.

    Args:
        G: The graph.
        node_id: Node to rate.

    Returns:
        "HIGH" (6+ dependents), "MEDIUM" (2–5), or "LOW" (0–1).
    """
    dependents = len(_callers(G, node_id))
    if dependents >= _RISK_HIGH_MIN:
        return "HIGH"
    if dependents >= _RISK_MEDIUM_MIN:
        return "MEDIUM"
    return "LOW"


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

    callers = _callers(G, node_id)
    lines.append("")
    lines.append(f"CALLED BY ({len(callers)}):")
    if callers:
        for caller in callers[:_MAX_LISTED_NEIGHBORS]:
            rel = G.edges[caller, node_id].get("relation", "references")
            lines.append(
                f"  - {_display(G, caller)} "
                f"[{G.nodes[caller].get('type', '?')}] --{rel}-->"
            )
        if len(callers) > _MAX_LISTED_NEIGHBORS:
            lines.append(f"  … and {len(callers) - _MAX_LISTED_NEIGHBORS} more")
    else:
        lines.append("  (nothing depends on this node)")

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
    lines.append(f"BLAST RADIUS: {len(callers)} direct dependents "
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
    callers = _callers(G, node_id)
    callees = _callees(G, node_id)

    sentences: list[str] = []

    opening = f"{name} is a {node_type}"
    source = attrs.get("source_file") or attrs.get("file_path")
    if source:
        opening += f" defined in {source}"
    sentences.append(opening + ".")

    if attrs.get("description"):
        sentences.append(str(attrs["description"]).rstrip(".") + ".")

    if callers:
        sentences.append(
            f"It is referenced by {len(callers)} "
            f"{'node' if len(callers) == 1 else 'nodes'}, including "
            f"{_join_display(G, callers)}."
        )
    else:
        sentences.append(
            "Nothing in the graph references it, so it is either an entry point "
            "or unused."
        )

    if callees:
        sentences.append(
            f"It depends on {_join_display(G, callees)}."
        )

    risk = _risk_level(G, node_id)
    if risk == "HIGH":
        sentences.append(
            "Because so much depends on it, changes here propagate widely — "
            "check every caller before altering its signature or behaviour."
        )
    elif risk == "MEDIUM":
        sentences.append("A change here affects a handful of callers directly.")
    else:
        sentences.append("It can be changed with little blast radius.")

    if scores and node_id in scores:
        sentences.append(f"Relevance to your query: {scores[node_id]:.3f}.")

    return " ".join(sentences)


# ---------------------------------------------------------------------------
# LLM providers
# ---------------------------------------------------------------------------

# DECISION: the model writes prose only. Callers, callees and blast radius are
# facts already in the graph and are rendered from it — asking the model to
# restate them would invite it to get them subtly wrong.
_PROMPT = """\
You are explaining one node of a codebase knowledge graph to a developer who \
is about to work on it.

Below is structured data about the node: its type, source file, what depends on \
it, and what it depends on.

{context}

Write 2-4 sentences explaining what this node does and why it exists, inferring \
from its name, type, description and relationships. Then write one more sentence \
on what would break if it changed, given its blast radius.

Rules:
- Plain prose. No markdown, no headers, no bullets, no preamble.
- Do not list the callers and callees back to me — they are shown separately.
- Do not invent function bodies, file contents, parameters, or behaviour the \
data above does not support. Say "likely" when you are inferring.\
"""


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
    # DECISION: Enable Anthropic Prompt Caching (`cache_control` + beta header)
    # to cache large graph context prompts, reducing repeat input token costs by
    # up to 90% and improving response latency.
    message = client.messages.create(
        model=config.model,
        max_tokens=_MAX_TOKENS_OUT,
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-15"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
    )
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
        callers=[_display(G, n) for n in _callers(G, node_id)],
        callees=[_display(G, n) for n in _callees(G, node_id)],
        provider_used="structural",
        context_tokens=count_tokens(context),
        node_type=attrs.get("type") or attrs.get("file_type") or "",
        source_file=attrs.get("source_file") or attrs.get("file_path") or "",
        score=round(scores.get(node_id, 0.0), 4),
    )

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


def format_explanation(result: ExplainResult, width: int = 69) -> str:
    """Render an ExplainResult as the plain-text body shown under the panel.

    Args:
        result: The explanation to render.
        width: Column width for the closing rule.

    Returns:
        Multi-line text without a trailing newline.
    """
    lines: list[str] = ["EXPLANATION", result.explanation, ""]

    lines.append("ARCHITECTURE")
    lines.extend(_wrap_list(result.callers, "→ Called by: "))
    lines.extend(_wrap_list(result.callees, "← Calls: "))
    lines.append("")

    noun = result.node_type or "node"
    count = len(result.callers)
    lines.append(f"RISK IF CHANGED: {result.risk_level}")
    lines.append(
        f"{count} node depends on this {noun} directly." if count == 1
        else f"{count} nodes depend on this {noun} directly." if count
        else f"Nothing depends on this {noun} directly."
    )

    lines.append("")
    lines.append("─" * width)
    provider = result.provider_used
    if result.model_used:
        provider += f" ({result.model_used})"
    lines.append(f"Provider: {provider} · Context: {result.context_tokens:,} tokens")
    if result.warning:
        lines.append(f"Note: {result.warning}")
    return "\n".join(lines)
