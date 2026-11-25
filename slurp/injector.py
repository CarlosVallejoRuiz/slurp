"""Extracts source code blocks from files and injects them into graph nodes."""

from __future__ import annotations

import sys
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

# The longest block injected for one node.
_MAX_BLOCK_LINES = 100


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
    return _python_block(lines, start_line, max_lines)[0]


def _python_block(lines: list[str], start_line: int,
                  max_lines: int) -> tuple[str | None, int | None]:
    """The block _extract_python returns, and the line it was cut at, if it was.

    Returns:
        (text, cut_at): cut_at is the 1-based line where the block goes on past
        *max_lines*, or None when the whole block fits.
    """
    idx = start_line - 1
    if idx < 0 or idx >= len(lines):
        return None, None

    header = lines[idx]
    if not header.strip():
        return None, None

    base_indent = len(header) - len(header.lstrip())
    result = [header.rstrip()]
    ended = False

    for line in lines[idx + 1 : idx + max_lines]:
        stripped = line.rstrip()
        if stripped == "":
            result.append("")
            continue
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= base_indent:
            ended = True
            break
        result.append(stripped)

    # Remove trailing blank lines from the collected block.
    while result and result[-1] == "":
        result.pop()

    cut_at = None
    if not ended:
        # The window ran out: the block goes on if the next line with content
        # past it is still indented under the header.
        rest = (ln for ln in lines[idx + max_lines:] if ln.strip())
        following = next(rest, None)
        if following is not None and len(following) - len(following.lstrip()) > base_indent:
            cut_at = idx + max_lines + 1

    return ("\n".join(result) if result else None), cut_at


def _extract_brace_balanced(lines: list[str], start_line: int, max_lines: int) -> str | None:
    """Extract a brace-delimited block starting at start_line (1-indexed).

    Counts '{' and '}' per line. Stops after a line that brings depth back to 0
    following the first opening brace. Checking at end-of-line (not per-character)
    means balanced braces within a signature line (e.g. destructuring params)
    don't trigger early termination.
    """
    return _brace_block(lines, start_line, max_lines)[0]


def _brace_block(lines: list[str], start_line: int,
                 max_lines: int) -> tuple[str | None, int | None]:
    """The block _extract_brace_balanced returns, and the line it was cut at, if it was.

    Returns:
        (text, cut_at): cut_at is the 1-based line where the block goes on past
        *max_lines*, or None when it closed (or the file ended) within them.
    """
    idx = start_line - 1
    if idx < 0 or idx >= len(lines):
        return None, None

    result: list[str] = []
    depth = 0
    found_open = False
    closed = False

    for line in lines[idx : idx + max_lines]:
        result.append(line.rstrip())
        for ch in line:
            if ch == "{":
                depth += 1
                found_open = True
            elif ch == "}":
                depth -= 1
        if found_open and depth == 0:
            closed = True
            break

    cut_at = idx + max_lines + 1 if not closed and idx + max_lines < len(lines) else None
    return ("\n".join(result) if result else None), cut_at


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
    return _code_block(file_path, source_location, max_lines)[0]


def _code_block(
    file_path: Path,
    source_location: str | int | None,
    max_lines: int,
) -> tuple[str | None, int | None]:
    """extract_code_block's text, and the 1-based line it was cut at (None if whole)."""
    if not file_path.exists():
        return None, None

    line_num = _parse_line(source_location)
    if line_num is None:
        return None, None

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None

    lines = content.splitlines()
    ext = file_path.suffix.lower()

    if ext in _BRACE_EXTS:
        return _brace_block(lines, line_num, max_lines)
    return _python_block(lines, line_num, max_lines)


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
            A source_file that resolves outside it (``../``, an absolute path,
            a symlink) is skipped with a warning on stderr.
        max_nodes: Maximum subgraph size. Returns G unchanged if exceeded.

    Returns:
        Copy of G with ``code_block`` set on nodes where extraction succeeded,
        and ``code_block_note`` on those whose block was cut at 100 lines.
    """
    if G.number_of_nodes() > max_nodes:
        return G

    root = project_root.resolve()
    result = G.copy()
    for nid in result.nodes:
        attrs = result.nodes[nid]
        source_file = attrs.get("source_file") or attrs.get("file_path")
        source_location = attrs.get("source_location")

        if not source_file or source_location is None:
            continue

        # DECISION: source_file comes from graph.json, which may have been
        # written by someone else (a graphify-out/ committed to a repo). A
        # `../` path, an absolute path or a symlink must not pull a file from
        # outside the project into the LLM context. resolve() follows symlinks,
        # so containment is judged on the file that would actually be read.
        file_path = (project_root / source_file).resolve()
        if not file_path.is_relative_to(root):
            print(
                f"slurp: --inject-code skipped node {nid!r}: source_file "
                f"{source_file!r} resolves outside the project root {root}",
                file=sys.stderr,
            )
            continue
        code, cut_at = _code_block(file_path, source_location, _MAX_BLOCK_LINES)
        if code:
            result.nodes[nid]["code_block"] = code
            if cut_at is not None:
                # DECISION (#80): a cut block says so, and where it goes on.
                # Until v1.2.0 it just stopped at line 100, mid-function, and
                # the reader (an LLM) could take it for the whole body.
                result.nodes[nid]["code_block_note"] = (
                    f"… cut at {_MAX_BLOCK_LINES} lines; the block continues at "
                    f"line {cut_at} of {source_file}")

    return result
