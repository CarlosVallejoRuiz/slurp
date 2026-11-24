"""Tests for slurp.smart — git-driven incremental re-indexing."""

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from slurp.cli import cli
from slurp.indexer import index_project
from slurp.smart import (
    get_changed_files,
    head_commit,
    is_git_repo,
    smart_reindex,
)

GIT_AVAILABLE = subprocess.run(
    ["git", "--version"], capture_output=True, check=False
).returncode == 0

requires_git = pytest.mark.skipif(not GIT_AVAILABLE, reason="git not installed")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")


def _commit_all(root: Path, message: str = "wip") -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A committed two-module Python project where `app` imports `helpers`."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "helpers.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8"
    )
    (root / "app.py").write_text(
        "from helpers import helper\n\n\ndef main():\n    return helper()\n",
        encoding="utf-8",
    )
    (root / "unrelated.py").write_text("def solo():\n    pass\n", encoding="utf-8")
    _init_repo(root)
    _commit_all(root)
    return root


@pytest.fixture
def graph_path(project: Path) -> Path:
    """A freshly built graph for the fixture project."""
    path = project / "graphify-out" / "graph.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    graph = index_project(project)
    # What `slurp index` records since v1.0.7: the commit the graph reflects.
    graph["built_at_commit"] = head_commit(project)
    path.write_text(json.dumps(graph, indent=2), encoding="utf-8")
    return path


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _node_ids(path: Path) -> set[str]:
    return {n["id"] for n in _load(path)["nodes"]}


def _smart(project: Path, graph_path: Path):
    """Run `slurp index --smart` the way a user does, full-index fallback included."""
    res = CliRunner().invoke(cli, ["index", str(project), "--smart", "-o", str(graph_path)])
    assert res.exit_code == 0, res.output
    return res


def _files_in(path: Path) -> set[str]:
    return {n["source_file"] for n in _load(path)["nodes"] if n.get("source_file")}


class TestNoGitRepo:
    def test_is_git_repo_false_outside_repo(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert is_git_repo(plain) is False

    def test_smart_reindex_reports_no_repo(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        result = smart_reindex(plain, plain / "graph.json")
        assert result.is_git_repo is False
        assert result.needs_full_index is True

    @requires_git
    def test_cli_falls_back_to_full_index(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "mod.py").write_text("def f():\n    pass\n", encoding="utf-8")
        out = plain / "graph.json"
        res = CliRunner().invoke(
            cli, ["index", str(plain), "--smart", "-o", str(out)]
        )
        assert res.exit_code == 0, res.output
        assert "No git repository found — running full index" in res.output
        # The fallback really ran: a full graph exists.
        assert out.is_file()
        assert "mod" in "".join(_node_ids(out))


class TestNoChanges:
    @requires_git
    def test_reports_up_to_date(self, project, graph_path):
        result = smart_reindex(project, graph_path)
        assert result.changed_files == []
        assert result.updated_nodes == 0

    @requires_git
    def test_cli_message(self, project, graph_path):
        res = CliRunner().invoke(
            cli, ["index", str(project), "--smart", "-o", str(graph_path)]
        )
        assert res.exit_code == 0, res.output
        assert "No changes since the graph was built — index is up to date" in res.output

    @requires_git
    def test_graph_untouched(self, project, graph_path):
        before = graph_path.read_text(encoding="utf-8")
        smart_reindex(project, graph_path)
        assert graph_path.read_text(encoding="utf-8") == before


class TestChangeDetection:
    @requires_git
    def test_detects_unstaged_edit(self, project):
        (project / "helpers.py").write_text(
            "def helper():\n    return 2\n", encoding="utf-8"
        )
        assert (project / "helpers.py").resolve() in get_changed_files(project)

    @requires_git
    def test_detects_staged_edit(self, project):
        (project / "helpers.py").write_text("def helper():\n    return 3\n", encoding="utf-8")
        _git(project, "add", "helpers.py")
        assert (project / "helpers.py").resolve() in get_changed_files(project)

    @requires_git
    def test_detects_untracked_file(self, project):
        (project / "brand_new.py").write_text("def fresh():\n    pass\n", encoding="utf-8")
        assert (project / "brand_new.py").resolve() in get_changed_files(project)

    @requires_git
    def test_ignores_non_indexable_extensions(self, project):
        (project / "notes.txt").write_text("hello", encoding="utf-8")
        assert all(p.suffix != ".txt" for p in get_changed_files(project))

    @requires_git
    def test_unchanged_files_absent(self, project):
        (project / "helpers.py").write_text("def helper():\n    return 4\n", encoding="utf-8")
        changed = get_changed_files(project)
        assert (project / "unrelated.py").resolve() not in changed

    @requires_git
    def test_detects_deleted_file(self, project):
        (project / "unrelated.py").unlink()
        assert (project / "unrelated.py").resolve() in get_changed_files(project)


class TestReparseScope:
    """Which files --smart re-parses, and when it hands over to a full index."""

    @requires_git
    def test_body_edit_reparses_only_that_file(self, project, graph_path):
        # app.py imports helpers.py, but an edit inside helper() keeps every
        # symbol, so app.py cannot resolve differently and is not re-parsed.
        (project / "helpers.py").write_text("def helper():\n    return 2\n", encoding="utf-8")
        result = smart_reindex(project, graph_path)
        assert not result.fallback_reason
        assert result.expanded_files == [project / "helpers.py"]

    @requires_git
    def test_new_symbol_falls_back_with_a_reason(self, project, graph_path):
        before = graph_path.read_bytes()
        (project / "helpers.py").write_text(
            "def helper():\n    return 1\n\n\ndef extra():\n    return 2\n", encoding="utf-8")
        result = smart_reindex(project, graph_path)
        assert result.needs_full_index
        assert "definitions" in result.fallback_reason
        assert graph_path.read_bytes() == before  # nothing written

    @requires_git
    def test_added_file_falls_back(self, project, graph_path):
        (project / "brand_new.py").write_text("def fresh():\n    pass\n", encoding="utf-8")
        assert "added" in smart_reindex(project, graph_path).fallback_reason

    @requires_git
    def test_deleted_file_falls_back(self, project, graph_path):
        (project / "unrelated.py").unlink()
        assert "removed" in smart_reindex(project, graph_path).fallback_reason

    @requires_git
    def test_unknown_build_commit_falls_back(self, project, graph_path):
        graph = _load(graph_path)
        graph["built_at_commit"] = "0" * 40
        graph_path.write_text(json.dumps(graph), encoding="utf-8")
        assert "not in this repository" in smart_reindex(project, graph_path).fallback_reason

    @requires_git
    def test_reverted_edit_is_still_detected(self, project, tmp_path):
        # Built while helpers.py was dirty, then reverted: git sees no change
        # against the build commit, but the graph holds the dirty version.
        original = (project / "helpers.py").read_text(encoding="utf-8")
        (project / "helpers.py").write_text("def helper():\n    return 99\n", encoding="utf-8")
        graph = tmp_path / "graph.json"
        _cli_index(project, graph)
        (project / "helpers.py").write_text(original, encoding="utf-8")
        result = smart_reindex(project, graph)
        assert (project / "helpers.py") in result.changed_files


class TestNodeReplacement:
    @requires_git
    def test_old_nodes_removed(self, project, graph_path):
        assert "helpers.helper" in _node_ids(graph_path)
        (project / "helpers.py").write_text(
            "def renamed():\n    return 1\n", encoding="utf-8"
        )
        _smart(project, graph_path)
        assert "helpers.helper" not in _node_ids(graph_path)

    @requires_git
    def test_new_nodes_added(self, project, graph_path):
        (project / "helpers.py").write_text(
            "def renamed():\n    return 1\n", encoding="utf-8"
        )
        _smart(project, graph_path)
        assert "helpers.renamed" in _node_ids(graph_path)

    @requires_git
    def test_untouched_files_survive(self, project, graph_path):
        (project / "helpers.py").write_text("def renamed():\n    pass\n", encoding="utf-8")
        _smart(project, graph_path)
        assert "unrelated.solo" in _node_ids(graph_path)

    @requires_git
    def test_new_file_nodes_appear(self, project, graph_path):
        (project / "extra.py").write_text("def added_fn():\n    pass\n", encoding="utf-8")
        _smart(project, graph_path)
        assert "extra.added_fn" in _node_ids(graph_path)

    @requires_git
    def test_deleted_file_nodes_dropped(self, project, graph_path):
        assert "unrelated.py" in _files_in(graph_path)
        (project / "unrelated.py").unlink()
        _smart(project, graph_path)
        assert "unrelated.py" not in _files_in(graph_path)

    @requires_git
    def test_result_counts_are_reported(self, project, graph_path):
        (project / "helpers.py").write_text("def helper():\n    return 7\n", encoding="utf-8")
        result = smart_reindex(project, graph_path)
        assert result.updated_nodes > 0
        assert result.removed_nodes > 0
        assert result.elapsed_ms > 0
        assert result.full_index_ms_estimate > 0

    @requires_git
    def test_no_dangling_edges(self, project, graph_path):
        (project / "helpers.py").write_text("def renamed():\n    pass\n", encoding="utf-8")
        _smart(project, graph_path)
        graph = _load(graph_path)
        ids = {n["id"] for n in graph["nodes"]}
        for e in graph["links"]:
            assert e["source"] in ids and e["target"] in ids

    @requires_git
    def test_result_matches_full_index_node_set(self, project, graph_path):
        """Incremental and full indexing must agree on the resulting graph."""
        (project / "helpers.py").write_text(
            "def renamed():\n    return 9\n", encoding="utf-8"
        )
        _smart(project, graph_path)
        full = {n["id"] for n in index_project(project)["nodes"]}
        assert _node_ids(graph_path) == full

    @requires_git
    def test_missing_graph_requires_full_index(self, project):
        result = smart_reindex(project, project / "nope" / "graph.json")
        assert result.graph_found is False
        assert result.needs_full_index is True

    @requires_git
    def test_corrupt_graph_requires_full_index(self, project):
        bad = project / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert smart_reindex(project, bad).graph_found is False


class TestDryRun:
    @requires_git
    def test_graph_not_modified(self, project, graph_path):
        (project / "helpers.py").write_text("def renamed():\n    pass\n", encoding="utf-8")
        before = graph_path.read_text(encoding="utf-8")
        smart_reindex(project, graph_path, dry_run=True)
        assert graph_path.read_text(encoding="utf-8") == before

    @requires_git
    def test_still_reports_what_would_change(self, project, graph_path):
        (project / "helpers.py").write_text("def helper():\n    return 7\n", encoding="utf-8")
        result = smart_reindex(project, graph_path, dry_run=True)
        assert result.dry_run is True
        assert result.updated_nodes > 0
        assert (project / "helpers.py").resolve() in result.expanded_files

    @requires_git
    def test_cli_lists_files_without_writing(self, project, graph_path):
        (project / "helpers.py").write_text("def renamed():\n    pass\n", encoding="utf-8")
        before = graph_path.read_text(encoding="utf-8")
        res = CliRunner().invoke(
            cli,
            ["index", str(project), "--smart", "--dry-run", "-o", str(graph_path)],
        )
        assert res.exit_code == 0, res.output
        assert "helpers.py" in res.output
        assert "No changes written (--dry-run)." in res.output
        assert graph_path.read_text(encoding="utf-8") == before

    def test_dry_run_without_smart_errors(self, tmp_path):
        res = CliRunner().invoke(cli, ["index", str(tmp_path), "--dry-run"])
        assert res.exit_code != 0
        assert "--dry-run only applies with --smart" in res.output


class TestCliOutput:
    @requires_git
    def test_smart_mode_banner(self, project, graph_path):
        (project / "helpers.py").write_text("def renamed():\n    pass\n", encoding="utf-8")
        res = CliRunner().invoke(
            cli, ["index", str(project), "--smart", "-o", str(graph_path)]
        )
        assert res.exit_code == 0, res.output
        assert "(smart mode) ..." in res.output

    @requires_git
    def test_reports_detection_and_update(self, project, graph_path):
        (project / "helpers.py").write_text("def helper():\n    return 7\n", encoding="utf-8")
        res = CliRunner().invoke(
            cli, ["index", str(project), "--smart", "-o", str(graph_path)]
        )
        assert "changed file" in res.output
        assert "since the graph was built" in res.output
        assert "✓ Updated" in res.output
        assert "nodes ·" in res.output
        assert f"Saved: {graph_path}" in res.output
        assert 'Next: slurp "your query"' in res.output

    @requires_git
    def test_reports_why_a_full_index_runs(self, project, graph_path):
        (project / "helpers.py").write_text("def renamed():\n    pass\n", encoding="utf-8")
        res = _smart(project, graph_path)
        assert "Full index needed: definitions or imports changed" in res.output
        assert f"Saved: {graph_path}" in res.output

    @requires_git
    def test_no_graph_falls_back_and_builds_one(self, project):
        out = project / "fresh" / "graph.json"
        res = CliRunner().invoke(
            cli, ["index", str(project), "--smart", "-o", str(out)]
        )
        assert res.exit_code == 0, res.output
        assert "running full index" in res.output
        assert out.is_file()

    def test_smart_flag_in_help(self):
        res = CliRunner().invoke(cli, ["index", "--help"])
        assert "--smart" in res.output
        assert "--dry-run" in res.output


# ---------------------------------------------------------------------------
# --smart must produce the same graph a full index would
# ---------------------------------------------------------------------------

def _cli_index(root: Path, out: Path, *extra: str):
    res = CliRunner().invoke(cli, ["index", str(root), "-o", str(out),
                                   "--ignore-file", "/dev/null", *extra])
    assert res.exit_code == 0, res.output
    return res


def _public(graph: dict) -> tuple[dict, list[str], object]:
    """Nodes by id and edges as a sorted multiset, public attributes only."""
    nodes = {n["id"]: {k: v for k, v in n.items() if not k.startswith("_")}
             for n in graph["nodes"]}
    edges = sorted(
        json.dumps({k: v for k, v in e.items() if not k.startswith("_")}, sort_keys=True)
        for e in graph["links"]
    )
    return nodes, edges, graph.get("built_at_commit")


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def mixed_project(tmp_path: Path) -> Path:
    """Python and TypeScript files calling each other across files."""
    root = tmp_path / "mixed"
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/util.py", "def helper():\n    return 1\n\n\ndef other():\n    return 2\n")
    _write(root, "pkg/app.py",
           "from pkg.util import helper, missing\n\n\ndef main():\n    return helper()\n\n\n"
           "def later():\n    return missing()\n")
    _write(root, "pkg/side.py", "def side():\n    return 3\n")
    _write(root, "web/client.ts", "export function send(): number { return 1 }\n")
    _write(root, "web/use.ts",
           'import { send, pending } from "./client"\n'
           "export function boot(): number { return send() }\n"
           "export function later(): number { return pending() }\n")
    _init_repo(root)
    _commit_all(root, "init")
    return root


def _mv(root: Path, old: str, new: str) -> None:
    (root / new).parent.mkdir(parents=True, exist_ok=True)
    (root / old).rename(root / new)


_SCENARIOS = {
    "file_added": lambda r: _write(
        r, "pkg/extra.py", "from pkg.util import helper\n\n\ndef extra():\n    return helper()\n"),
    "file_modified": lambda r: _write(
        r, "pkg/app.py",
        "from pkg.util import helper, other\n\n\ndef main():\n    return helper()\n\n\n"
        "def more():\n    return other()\n"),
    "file_deleted": lambda r: (r / "pkg/side.py").unlink(),
    "callee_file_deleted": lambda r: (r / "pkg/util.py").unlink(),
    "file_renamed": lambda r: _mv(r, "pkg/side.py", "pkg/side2.py"),
    "callee_renamed_and_importer_updated": lambda r: (
        _mv(r, "pkg/util.py", "pkg/utils.py"),
        _write(r, "pkg/app.py", "from pkg.utils import helper\n\n\ndef main():\n    return helper()\n")),
    # Only the callee changes: app.later() already calls missing(), which now exists.
    "only_callee_gains_symbol": lambda r: _write(
        r, "pkg/util.py", "def helper():\n    return 1\n\n\ndef missing():\n    return 4\n"),
    # Only the callee changes: app.main() keeps calling helper(), which is gone.
    "only_callee_loses_symbol": lambda r: _write(r, "pkg/util.py", "def other():\n    return 2\n"),
    "only_ts_callee_gains_symbol": lambda r: _write(
        r, "web/client.ts",
        "export function send(): number { return 1 }\n"
        "export function pending(): number { return 2 }\n"),
    # Body-only edits keep every symbol: these take the incremental path, and the
    # new cross-file calls must resolve exactly as a full index resolves them.
    "caller_body_adds_cross_file_call": lambda r: _write(
        r, "pkg/app.py",
        "from pkg.util import helper, missing\n\n\ndef main():\n    return helper()\n\n\n"
        "def later():\n    x = helper()\n    return missing() + x\n"),
    "callee_body_edit": lambda r: _write(
        r, "pkg/util.py", "def helper():\n    value = 41\n    return value + 1\n\n\n"
                          "def other():\n    return helper()\n"),
    "ts_caller_body_adds_cross_file_call": lambda r: _write(
        r, "web/use.ts",
        'import { send, pending } from "./client"\n'
        "export function boot(): number { return send() }\n"
        "export function later(): number { return pending() + send() }\n"),
    "only_ts_caller_changes": lambda r: _write(
        r, "web/use.ts",
        'import { send } from "./client"\n'
        "export function boot(): number { return send() + send() }\n"
        "export function again(): number { return send() }\n"),
}


# Scenarios whose edits keep every symbol of the edited files.
_INCREMENTAL = {"caller_body_adds_cross_file_call", "callee_body_edit",
                "ts_caller_body_adds_cross_file_call"}


@requires_git
class TestSmartEquivalence:
    @pytest.mark.parametrize("committed", [False, True], ids=["uncommitted", "committed"])
    @pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
    def test_smart_matches_full_index(self, mixed_project, tmp_path, scenario, committed):
        graph = tmp_path / "graph.json"
        _cli_index(mixed_project, graph)
        _SCENARIOS[scenario](mixed_project)
        if committed:
            _commit_all(mixed_project, scenario)

        res = _cli_index(mixed_project, graph, "--smart")
        took_fast_path = "Full index needed" not in res.output
        assert took_fast_path == (scenario in _INCREMENTAL), res.output
        full = tmp_path / "full.json"
        _cli_index(mixed_project, full)

        smart_nodes, smart_edges, smart_commit = _public(_load(graph))
        full_nodes, full_edges, full_commit = _public(_load(full))
        assert smart_nodes == full_nodes
        assert smart_edges == full_edges
        assert smart_commit == full_commit


@requires_git
class TestSmartRegressions:
    """The three defects behind the equivalence failures, one test each."""

    def test_cross_file_call_points_at_the_definition(self, project, tmp_path):
        graph = tmp_path / "graph.json"
        _cli_index(project, graph)
        (project / "app.py").write_text(
            "from helpers import helper\n\n\ndef main():\n    return helper()\n\n\n"
            "def second():\n    return helper()\n", encoding="utf-8")
        _cli_index(project, graph, "--smart")
        g = _load(graph)
        kinds = {n["id"]: n.get("type") for n in g["nodes"]}
        calls = {(e["source"], e["target"]) for e in g["links"] if e["relation"] == "calls"}
        assert ("app.second", "helpers.helper") in calls
        assert not [t for _, t in calls if kinds.get(t) == "import"]

    def test_pending_symbol_never_reaches_the_output(self, project, tmp_path):
        graph = tmp_path / "graph.json"
        _cli_index(project, graph)
        _write(project, "app.py", "from helpers import helper\n\n\ndef run():\n    return helper()\n")
        _cli_index(project, graph, "--smart")
        assert "_pending_symbol" not in graph.read_text(encoding="utf-8")

    def test_index_records_the_commit_it_was_built_from(self, project, tmp_path):
        graph = tmp_path / "graph.json"
        _cli_index(project, graph)
        head = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        assert _load(graph)["built_at_commit"] == head

    def test_commit_before_smart_is_not_up_to_date(self, project, tmp_path):
        graph = tmp_path / "graph.json"
        _cli_index(project, graph)
        _write(project, "unrelated.py", "def solo():\n    pass\n\n\ndef added():\n    pass\n")
        _commit_all(project, "edit")
        res = _cli_index(project, graph, "--smart")
        assert "up to date" not in res.output
        assert "unrelated.added" in _node_ids(graph)

    def test_graph_without_a_recorded_commit_is_rebuilt(self, project, tmp_path):
        # Graphs written before v1.0.7 carry built_at_commit=None: there is no
        # base to diff against, so --smart cannot know what changed.
        graph = tmp_path / "graph.json"
        stale = index_project(project)
        assert stale["built_at_commit"] is None
        graph.write_text(json.dumps(stale), encoding="utf-8")
        _write(project, "unrelated.py", "def solo():\n    pass\n\n\ndef added():\n    pass\n")
        _commit_all(project, "edit")
        _cli_index(project, graph, "--smart")
        assert "unrelated.added" in _node_ids(graph)
