"""Helpers shared across the modules that read a graph.

Each of these was a private in whichever module needed it first, imported by
underscore from the others as they appeared. Six modules deep that stops being
a shortcut: scorer owned tokenisation, explainer owned node naming and the
caller split, formatter owned the box, and everyone reached across for them.

The names keep their underscore. They are still internal to slurp — the point
is that they now have one home rather than being borrowed from wherever they
happened to be defined.
"""

from __future__ import annotations

import re

import networkx as nx

# Risk is measured in direct dependents: nodes that would have to change with
# it. Shared so a file and its nodes never disagree about the same code —
# impact.py used to carry its own copy of this number.
_RISK_HIGH_MIN = 6
_RISK_MEDIUM_MIN = 2

def _risk_band(dependents: int) -> str:
    """HIGH / MEDIUM / LOW for a count of direct dependents.

    The one place the bands are decided: explain, impact and diff all call it,
    and it reads the constants when called, so changing them moves every band.
    """
    if dependents >= _RISK_HIGH_MIN:
        return "HIGH"
    if dependents >= _RISK_MEDIUM_MIN:
        return "MEDIUM"
    return "LOW"


_TEST_MARKERS = ("tests/", "test_", "_test.", ".test.", ".spec.", "spec/")

# Display name of every language `slurp index` reads, by file extension.
_LANGUAGE_BY_EXT: dict[str, str] = {
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".go": "Go",
    ".java": "Java",
    ".rs": "Rust",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".scala": "Scala",
    ".swift": "Swift",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".hpp": "C++",
    ".hh": "C++",
    ".hxx": "C++",
    ".lua": "Lua",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".ps1": "PowerShell",
    ".psm1": "PowerShell",
}

# DECISION: the extensions slurp's own indexer extracts `calls` edges for. The
# other eleven languages get `contains` and imports only, so an empty caller
# list there means "unknown", never "unused" — reporting it as SAFE TO EDIT
# would turn a gap in the graph into a reassurance.
_CALL_GRAPH_EXTENSIONS = frozenset({".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".rs"})


def _tokenize(text: str) -> list[str]:
    """Splits text into tokens handling snake_case, camelCase, and PascalCase.

    Pipeline:
    1. Insert spaces at camelCase/PascalCase boundaries (before lowercasing so
       boundary info is preserved): "recalcularPlayerStats" → "recalcular Player Stats".
    2. Lowercase and split on every non-alphanumeric character (covers snake_case,
       hyphens, dots, spaces, etc.).
    3. Append any original (pre-split) tokens that are not already in the result,
       so full compound identifiers remain searchable alongside their parts.
    """
    # Step 1: split camelCase/PascalCase before losing case info.
    #   "XMLParser"         → "XML Parser"    (uppercase run before capitalized word)
    #   "recalcularPlayer"  → "recalcular Player"  (lowercase-to-uppercase boundary)
    split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", split)

    # Step 2: lowercase + extract alphanumeric runs (covers _ . - spaces …).
    primary = re.findall(r"[a-z0-9]+", split.lower())

    # Step 3: also extract original tokens so compound identifiers are searchable.
    original = re.findall(r"[a-z0-9]+", text.lower())
    seen = set(primary)
    extras = [t for t in original if t not in seen]

    return primary + extras


def _label(G: nx.DiGraph, node_id: str) -> str:
    return G.nodes[node_id].get("label", node_id) if node_id in G else node_id


def _display(G: nx.DiGraph, node_id: str) -> str:
    """Render a node as `name()` for functions/methods, plain otherwise."""
    label = _label(G, node_id)
    node_type = G.nodes[node_id].get("type", "") if node_id in G else ""
    return f"{label}()" if node_type in {"function", "method"} else label


def _callers(G: nx.DiGraph, node_id: str) -> list[str]:
    return sorted(G.predecessors(node_id)) if node_id in G else []


def _callees(G: nx.DiGraph, node_id: str) -> list[str]:
    return sorted(G.successors(node_id)) if node_id in G else []


def _is_test(G: nx.DiGraph, node_id: str) -> bool:
    """Whether a node lives in test code, judged by its file path."""
    source = str(G.nodes.get(node_id, {}).get("source_file", "")).replace("\\", "/")
    label = node_id.rsplit(".", 1)[-1]
    return any(m in source for m in _TEST_MARKERS) or label.startswith("test_")


def _split_callers(G: nx.DiGraph, node_id: str) -> tuple[list[str], list[str]]:
    """Callers split into production and test, in that order.

    A node called by sixty tests and six modules is not depended on by
    sixty-six things in any sense a reader cares about, and reporting one
    number overstates the blast radius by an order of magnitude.
    """
    callers = [
        c for c in _callers(G, node_id)
        if G.edges[c, node_id].get("relation") != "contains"
    ]
    tests = [c for c in callers if _is_test(G, c)]
    return [c for c in callers if c not in set(tests)], tests


def _box(title: str, subtitle: str) -> str:
    """Renders a Unicode border box with a title line and a subtitle line.

    Example:
        ╭─ Slurp — (budget: 100 tokens) ─╮
        │ Selected 3/7 nodes · 58/100 ... │
        ╰──────────────────────────────────╯
    """
    # total_w must accommodate both content lines with their border decoration.
    #   title line:    "╭─ " + title + " " + "─" * fill + "╮"  = len(title) + 5 + fill
    #   subtitle line: "│ " + subtitle + spaces + " │"          = len(subtitle) + 4 + spaces
    total_w = max(len(title) + 5, len(subtitle) + 4)
    title_fill = total_w - len(title) - 5
    sub_spaces = total_w - len(subtitle) - 4

    top = f"╭─ {title} {'─' * title_fill}╮"
    mid = f"│ {subtitle}{' ' * sub_spaces} │"
    bot = "╰" + "─" * (total_w - 2) + "╯"
    return "\n".join([top, mid, bot])


def _extension(G: nx.DiGraph, node_id: str) -> str:
    attrs = G.nodes.get(node_id, {})
    source = str(attrs.get("source_file") or attrs.get("file_path") or "")
    name = source.replace("\\", "/").rsplit("/", 1)[-1]
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _extensions_with_calls(G: nx.DiGraph) -> set[str]:
    """Extensions of every file that takes part in at least one `calls` edge."""
    found: set[str] = set()
    for u, v, relation in G.edges(data="relation"):
        if relation == "calls":
            found.update(e for e in (_extension(G, u), _extension(G, v)) if e)
    return found


def _call_graph_gap(G: nx.DiGraph, node_id: str,
                    with_calls: set[str] | None = None) -> str:
    """Why this graph cannot say who calls *node_id*, or "" when it can.

    A node is covered when slurp extracts calls for its language, or when the
    graph holds calls for that language anyway — graphify extracts them with
    an LLM for any language. Nodes without a source file are left alone.

    Args:
        G: The graph.
        node_id: The node in question.
        with_calls: Precomputed _extensions_with_calls(G), for callers that ask
            about many nodes of the same graph.

    Returns:
        A sentence fragment explaining the gap, or "".
    """
    ext = _extension(G, node_id)
    if not ext or ext in _CALL_GRAPH_EXTENSIONS:
        return ""
    if ext in (_extensions_with_calls(G) if with_calls is None else with_calls):
        return ""
    language = _LANGUAGE_BY_EXT.get(ext, f"{ext} files")
    return (f"the graph has no call edges for {language} — slurp extracts calls only "
            "for Python, TypeScript/JavaScript, Go, Java and Rust — so who calls "
            "this code is unknown")
