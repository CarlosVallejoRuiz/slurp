"""Tests for slurp.smart — git-driven incremental re-indexing."""

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from slurp.cli import cli
from slurp.indexer import index_project
from slurp.smart import (
    _module_id_for_label,
    expand_dependencies,
    get_changed_files,
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
    path.write_text(json.dumps(index_project(project), indent=2), encoding="utf-8")
    return path


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _node_ids(path: Path) -> set[str]:
    return {n["id"] for n in _load(path)["nodes"]}


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
        assert "No changes detected since last commit — index is up to date" in res.output

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


class TestExpandDependencies:
    @requires_git
    def test_importer_is_pulled_in(self, project, graph_path):
        """app.py imports helpers.py, so editing helpers must re-index app."""
        graph = _load(graph_path)
        changed = [project / "helpers.py"]
        expanded = expand_dependencies(changed, graph, project)
        assert (project / "app.py").resolve() in expanded

    @requires_git
    def test_unrelated_file_not_pulled_in(self, project, graph_path):
        graph = _load(graph_path)
        expanded = expand_dependencies([project / "helpers.py"], graph, project)
        assert (project / "unrelated.py").resolve() not in expanded

    @requires_git
    def test_changed_file_always_included(self, project, graph_path):
        graph = _load(graph_path)
        expanded = expand_dependencies([project / "unrelated.py"], graph, project)
        assert (project / "unrelated.py").resolve() in expanded

    @requires_git
    def test_zero_hops_returns_changed_only(self, project, graph_path):
        graph = _load(graph_path)
        expanded = expand_dependencies([project / "helpers.py"], graph, project, hops=0)
        assert expanded == [(project / "helpers.py").resolve()]

    @requires_git
    def test_hop_limit_is_respected(self, tmp_path):
        """A → B → C: one hop from C reaches B but not A."""
        root = tmp_path / "chain"
        root.mkdir()
        (root / "c.py").write_text("def c():\n    pass\n", encoding="utf-8")
        (root / "b.py").write_text("from c import c\n", encoding="utf-8")
        (root / "a.py").write_text("from b import c\n", encoding="utf-8")
        graph = index_project(root)
        one = expand_dependencies([root / "c.py"], graph, root, hops=1)
        two = expand_dependencies([root / "c.py"], graph, root, hops=2)
        assert (root / "b.py").resolve() in one
        assert (root / "a.py").resolve() not in one
        assert (root / "a.py").resolve() in two

    def test_deleted_file_is_not_reindexed(self, tmp_path):
        missing = tmp_path / "gone.py"
        assert expand_dependencies([missing], {"nodes": [], "links": []}, tmp_path) == []

    def test_empty_graph_returns_changed_only(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("def x():\n    pass\n", encoding="utf-8")
        assert expand_dependencies([f], {"nodes": [], "links": []}, tmp_path) == [f.resolve()]

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("slurp.ignore.load_ignore", "slurp.ignore"),
            ("slurp.ignore", "slurp.ignore"),
            ("slurp", "slurp"),
            ("os.path", None),
            ("", None),
        ],
    )
    def test_module_id_resolution(self, label, expected):
        modules = {"slurp": "slurp/__init__.py", "slurp.ignore": "slurp/ignore.py"}
        assert _module_id_for_label(label, modules) == expected

    def test_longest_prefix_wins(self):
        modules = {"a": "a.py", "a.b": "a/b.py"}
        assert _module_id_for_label("a.b.c", modules) == "a.b"


class TestNodeReplacement:
    @requires_git
    def test_old_nodes_removed(self, project, graph_path):
        assert "helpers.helper" in _node_ids(graph_path)
        (project / "helpers.py").write_text(
            "def renamed():\n    return 1\n", encoding="utf-8"
        )
        smart_reindex(project, graph_path)
        assert "helpers.helper" not in _node_ids(graph_path)

    @requires_git
    def test_new_nodes_added(self, project, graph_path):
        (project / "helpers.py").write_text(
            "def renamed():\n    return 1\n", encoding="utf-8"
        )
        smart_reindex(project, graph_path)
        assert "helpers.renamed" in _node_ids(graph_path)

    @requires_git
    def test_untouched_files_survive(self, project, graph_path):
        (project / "helpers.py").write_text("def renamed():\n    pass\n", encoding="utf-8")
        smart_reindex(project, graph_path)
        assert "unrelated.solo" in _node_ids(graph_path)

    @requires_git
    def test_new_file_nodes_appear(self, project, graph_path):
        (project / "extra.py").write_text("def added_fn():\n    pass\n", encoding="utf-8")
        result = smart_reindex(project, graph_path)
        assert "extra.added_fn" in _node_ids(graph_path)
        assert result.updated_nodes > 0

    @requires_git
    def test_deleted_file_nodes_dropped(self, project, graph_path):
        assert "unrelated.py" in _files_in(graph_path)
        (project / "unrelated.py").unlink()
        result = smart_reindex(project, graph_path)
        assert "unrelated.py" not in _files_in(graph_path)
        assert result.removed_nodes > 0

    @requires_git
    def test_result_counts_are_reported(self, project, graph_path):
        (project / "helpers.py").write_text("def renamed():\n    pass\n", encoding="utf-8")
        result = smart_reindex(project, graph_path)
        assert result.updated_nodes > 0
        assert result.removed_nodes > 0
        assert result.elapsed_ms > 0
        assert result.full_index_ms_estimate > 0

    @requires_git
    def test_no_dangling_edges(self, project, graph_path):
        (project / "helpers.py").write_text("def renamed():\n    pass\n", encoding="utf-8")
        smart_reindex(project, graph_path)
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
        smart_reindex(project, graph_path)
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
        (project / "helpers.py").write_text("def renamed():\n    pass\n", encoding="utf-8")
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
        (project / "helpers.py").write_text("def renamed():\n    pass\n", encoding="utf-8")
        res = CliRunner().invoke(
            cli, ["index", str(project), "--smart", "-o", str(graph_path)]
        )
        assert "changed file" in res.output
        assert "(git diff)" in res.output
        assert "✓ Updated" in res.output
        assert "nodes ·" in res.output
        assert f"Saved: {graph_path}" in res.output
        assert 'Next: slurp "your query"' in res.output

    @requires_git
    def test_reports_dependency_expansion(self, project, graph_path):
        (project / "helpers.py").write_text("def renamed():\n    pass\n", encoding="utf-8")
        res = CliRunner().invoke(
            cli, ["index", str(project), "--smart", "-o", str(graph_path)]
        )
        assert "→ Expanding to" in res.output
        assert "dependent added" in res.output

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
