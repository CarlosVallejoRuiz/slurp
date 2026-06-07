"""Extracts source code blocks from files and injects them into graph nodes."""

from __future__ import annotations

from pathlib import Path

import networkx as nx

# Maps file extension to a fenced-code-block language identifier.
_LANG_MAP: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
}

# Extensions that use brace-balanced extraction instead of indentation.
_BRACE_EXTS = frozenset({".ts", ".tsx", ".js", ".jsx", ".go"})


def _parse_line(source_location: str | int | None) -> int | None:
    """Parse a source location like 'L42', '42', or integer 42 into a 1-based line number."""
    if source_location is None:
        return None
    if isinstance(source_location, int):
        return source_location if source_location > 0 else None
    s = str(source_location).strip().lstrip("Ll")
    try:
        n = int(s)
        return n if n > 0 else None
    except ValueError:
        return None


def _extract_python(lines: list[str], start_line: int, max_lines: int) -> str | None:
    """Extract a Python function/class block starting at start_line (1-indexed).

    Collects the header line and all subsequent lines with strictly greater
    indentation. Stops at the first non-blank line whose indentation is ≤ the
    header's indentation.
    """
    idx = start_line - 1
    if idx < 0 or idx >= len(lines):
        return None

    header = lines[idx]
    if not header.strip():
        return None

    base_indent = len(header) - len(header.lstrip())
    result = [header.rstrip()]

    for line in lines[idx + 1 : idx + max_lines]:
        stripped = line.rstrip()
        if stripped == "":
            result.append("")
            continue
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= base_indent:
            break
        result.append(stripped)

    # Remove trailing blank lines from the collected block.
    while result and result[-1] == "":
        result.pop()

    return "\n".join(result) if result else None


def _extract_brace_balanced(lines: list[str], start_line: int, max_lines: int) -> str | None:
    """Extract a brace-delimited block starting at start_line (1-indexed).

    Counts '{' and '}' per line. Stops after a line that brings depth back to 0
    following the first opening brace. Checking at end-of-line (not per-character)
    means balanced braces within a signature line (e.g. destructuring params)
    don't trigger early termination.
    """
    idx = start_line - 1
    if idx < 0 or idx >= len(lines):
        return None

    result: list[str] = []
    depth = 0
    found_open = False

    for line in lines[idx : idx + max_lines]:
        result.append(line.rstrip())
        for ch in line:
            if ch == "{":
                depth += 1
                found_open = True
            elif ch == "}":
                depth -= 1
        if found_open and depth == 0:
            break

    return "\n".join(result) if result else None


def extract_code_block(
    file_path: Path,
    source_location: str | int | None,
    label: str = "",
    max_lines: int = 100,
) -> str | None:
    """Extract a code block from a source file at the given line.

    Args:
        file_path: Path to the source file.
        source_location: Line spec — 'L42', '42', or integer 42 (1-based).
        label: Node label (reserved for future fallback search; unused now).
        max_lines: Maximum lines to extract before truncating.

    Returns:
        Extracted code as a string, or None if file is missing or location invalid.
    """
    if not file_path.exists():
        return None

    line_num = _parse_line(source_location)
    if line_num is None:
        return None

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    lines = content.splitlines()
    ext = file_path.suffix.lower()

    if ext in _BRACE_EXTS:
        return _extract_brace_balanced(lines, line_num, max_lines)
    return _extract_python(lines, line_num, max_lines)


def inject_code(
    G: nx.DiGraph,
    project_root: Path,
    max_nodes: int = 30,
) -> nx.DiGraph:
    """Add ``code_block`` attributes to nodes that have source coordinates.

    For each node with both a ``source_file`` (or ``file_path``) attribute and
    a ``source_location`` attribute, the code block at that location is extracted
    and stored as ``code_block`` on the node.

    Only runs when the graph has at most *max_nodes* nodes — larger subgraphs
    would produce unmanageable context.

    Args:
        G: Subgraph whose nodes to annotate. Not mutated; a copy is returned.
        project_root: Root directory for resolving relative source_file paths.
        max_nodes: Maximum subgraph size. Returns G unchanged if exceeded.

    Returns:
        Copy of G with ``code_block`` set on nodes where extraction succeeded.
    """
    if G.number_of_nodes() > max_nodes:
        return G

    result = G.copy()
    for nid in result.nodes:
        attrs = result.nodes[nid]
        source_file = attrs.get("source_file") or attrs.get("file_path")
        source_location = attrs.get("source_location")

        if not source_file or source_location is None:
            continue

        file_path = project_root / source_file
        label = attrs.get("label", str(nid))
        code = extract_code_block(file_path, source_location, label)
        if code:
            result.nodes[nid]["code_block"] = code

    return result
