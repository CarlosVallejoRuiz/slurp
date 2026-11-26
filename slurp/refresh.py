"""Keeping a graph current: building it with its manifest, and refreshing it on demand.

The engine behind the freshness of `slurp serve`, of the query commands and of
`slurp index --watch` (docs/design/mcp-autonomo.md §2–§4). This module owns the
full build; slurp.smart owns the incremental path, slurp.manifest the record
and the change detection, slurp.graphfile the write, the lock and ownership.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from slurp import indexer
from slurp.ignore import load_ignore
from slurp.manifest import MANIFEST_KEY, build_manifest


def index_with_manifest(
    root: Path,
    graph_path: Path,
    ignore_path: Path,
    on_file: Callable[[Path], None] | None = None,
) -> dict:
    """A full index of *root* with its manifest, ready for write_graph(graph_path, …).

    The tree is walked once: the manifest records that walk (directories with
    their mtimes, files with stat and hash) and the index parses exactly those
    files, after they were hashed.

    Args:
        root: Project root.
        graph_path: Where the graph will be written (the manifest's root is
            stored relative to its directory).
        ignore_path: The ignore rules to build with, recorded in the manifest.
        on_file: Progress callback, once per file parsed.

    Returns:
        The graph dict: nodes, links, built_at_commit and the manifest.
    """
    from slurp.smart import head_commit

    started = time.time_ns()
    root = root.resolve()
    walk = indexer.walk_source_tree(root)
    manifest = build_manifest(root, ignore_path, started_ns=started, walk=walk)
    graph = indexer.index_project(root, load_ignore(ignore_path), on_file=on_file,
                                  files=walk[1])
    # Still written for slurp ≤ 1.2 and for --smart on graphs without a manifest.
    graph["built_at_commit"] = head_commit(root)
    graph[MANIFEST_KEY] = manifest.to_dict(graph_path)
    return graph
