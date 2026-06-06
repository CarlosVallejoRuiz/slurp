"""Append-only audit log for query history."""

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

_AUDIT_FILE = "audit.jsonl"


def log_query(
    query: str,
    graph_path: Path,
    stats: dict,
    top_nodes: list[str],
    audit_dir: Path = Path(".slurp"),
) -> None:
    """Appends a query record to audit.jsonl.

    Creates audit_dir (and any parents) if it does not exist.

    Args:
        query: The query string that was run.
        graph_path: Path to the graph file that was queried.
        stats: Stats dict returned by budget.select_subgraph.
        top_nodes: List of selected node IDs (ordered by score).
        audit_dir: Directory where audit.jsonl lives (default: .slurp/).
    """
    audit_dir.mkdir(parents=True, exist_ok=True)

    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "query": query,
        "budget": stats["tokens_budget"],
        "tokens_used": stats["tokens_used"],
        "nodes_selected": stats["nodes_selected"],
        "nodes_total": stats["nodes_total"],
        "graph_path": str(graph_path),
        "top_nodes": list(top_nodes),
    }

    with (audit_dir / _AUDIT_FILE).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def read_audit(audit_dir: Path = Path(".slurp")) -> list[dict]:
    """Reads and returns all entries from the audit log.

    Args:
        audit_dir: Directory containing audit.jsonl.

    Returns:
        List of entry dicts in the order they were logged.
        Returns an empty list if the file or directory does not exist.
    """
    audit_file = audit_dir / _AUDIT_FILE
    if not audit_file.exists():
        return []

    entries: list[dict] = []
    for raw in audit_file.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped:
            entries.append(json.loads(stripped))
    return entries


def top_nodes_from_audit(
    audit_dir: Path = Path(".slurp"),
    n: int = 10,
) -> list[tuple]:
    """Returns the n most frequently selected nodes across all audit entries.

    Args:
        audit_dir: Directory containing audit.jsonl.
        n: Maximum number of results to return.

    Returns:
        List of (node_id, count) tuples sorted by count descending.
        Returns an empty list if the audit log is empty or missing.
    """
    counter: Counter[str] = Counter()
    for entry in read_audit(audit_dir):
        counter.update(entry.get("top_nodes", []))
    return counter.most_common(n)
