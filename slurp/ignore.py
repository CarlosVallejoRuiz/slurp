"""Node filtering based on .slurpignore rules."""

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx


@dataclass
class SlurpIgnore:
    """Compiled set of ignore rules loaded from a .slurpignore file."""

    type_rules: list[str] = field(default_factory=list)
    file_rules: list[str] = field(default_factory=list)
    id_rules: list[str] = field(default_factory=list)

    def should_ignore(self, node_attrs: dict, node_id: str = "") -> bool:
        """Returns True if the node matches any ignore rule.

        Args:
            node_attrs: Node attribute dict from the graph.
            node_id: Node identifier string (used for id: rules).
        """
        node_type = node_attrs.get("type") or node_attrs.get("file_type") or ""
        for rule in self.type_rules:
            if node_type == rule:
                return True

        file_path = node_attrs.get("file_path") or node_attrs.get("source_file") or ""
        if file_path:
            fname = Path(file_path).name
            for pattern in self.file_rules:
                if fnmatch.fnmatch(file_path, pattern) or fnmatch.fnmatch(fname, pattern):
                    return True

        for pattern in self.id_rules:
            if fnmatch.fnmatch(node_id, pattern):
                return True

        return False


def load_ignore(path: Path) -> SlurpIgnore:
    """Parses a .slurpignore file and returns a compiled SlurpIgnore.

    Supported rule prefixes:
      type:VALUE   — ignore nodes whose type or file_type equals VALUE
      file:PATTERN — ignore nodes whose file path matches the glob PATTERN
                     (matched against both the full path and the filename)
      id:PATTERN   — ignore nodes whose ID matches the glob PATTERN

    Lines starting with '#' and blank lines are skipped.

    Args:
        path: Path to the .slurpignore file. Missing files are silently accepted.

    Returns:
        Compiled SlurpIgnore. If the file does not exist, returns an empty
        instance that matches nothing.
    """
    ignore = SlurpIgnore()
    if not path.exists():
        return ignore

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("type:"):
            ignore.type_rules.append(line[5:].strip())
        elif line.startswith("file:"):
            ignore.file_rules.append(line[5:].strip())
        elif line.startswith("id:"):
            ignore.id_rules.append(line[3:].strip())

    return ignore


def apply_ignore(G: nx.DiGraph, ignore: SlurpIgnore) -> nx.DiGraph:
    """Returns a copy of G with all ignored nodes (and their incident edges) removed.

    Args:
        G: Source directed graph.
        ignore: Compiled ignore rules.

    Returns:
        New DiGraph containing only nodes that pass all ignore rules.
    """
    kept = [n for n in G.nodes if not ignore.should_ignore(dict(G.nodes[n]), n)]
    return G.subgraph(kept).copy()
