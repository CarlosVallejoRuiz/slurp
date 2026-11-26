"""slurp init in v1.3: own graph by default, Claude Code approvals, the agent's paragraph."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from slurp.cli import cli
from slurp.clients import BLOCK_BEGIN, instruction_block

FIXTURE = Path(__file__).parent / "fixtures" / "graphify_project"


@pytest.fixture(autouse=True)
def _no_global_git_config(tmp_path_factory, monkeypatch):
    """Git without the developer's global config: Claude Code adds
    **/.claude/settings.local.json to the global excludes file, which would
    decide what these tests are about."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path_factory.mktemp("git") / "config"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg")))


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


def _project(root: Path, git: bool = True, gitignore: str | None = "node_modules/\n") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    if gitignore is not None:
        (root / ".gitignore").write_text(gitignore, encoding="utf-8")
    if git:
        _git(root, "init", "-q")
    return root


def _init(root: Path, monkeypatch, *args: str, input: str | None = None):
    monkeypatch.chdir(root)
    return CliRunner().invoke(cli, ["init", *args], input=input)


def _mcp_graph(root: Path) -> str:
    return json.loads((root / ".mcp.json").read_text(encoding="utf-8"))[
        "mcpServers"]["slurp"]["args"][-1]


class TestGraphChoice:
    def test_no_graph_builds_slurps_own(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p")
        res = _init(root, monkeypatch, "--yes")
        assert res.exit_code == 0, res.output
        graph = root / "graphify-out" / "graph.json"
        assert "slurp" in json.loads(graph.read_text(encoding="utf-8"))
        assert _mcp_graph(root) == str(graph.resolve())

    def test_beside_graphifys_graph_by_default(self, tmp_path, monkeypatch):
        root = tmp_path / "g"
        shutil.copytree(FIXTURE, root)
        theirs = root / "graphify-out" / "graph.json"
        before = theirs.read_bytes()
        res = _init(root, monkeypatch, "--yes")
        assert res.exit_code == 0, res.output
        ours = root / "graphify-out" / "slurp-graph.json"
        assert "slurp" in json.loads(ours.read_text(encoding="utf-8"))
        assert theirs.read_bytes() == before
        assert _mcp_graph(root) == str(ours.resolve())
        assert "stays in sync" in (root / "CLAUDE.local.md").read_text(encoding="utf-8")

    def test_keep_graphify(self, tmp_path, monkeypatch):
        root = tmp_path / "g"
        shutil.copytree(FIXTURE, root)
        theirs = root / "graphify-out" / "graph.json"
        before = theirs.read_bytes()
        res = _init(root, monkeypatch, "--yes", "--keep-graphify")
        assert res.exit_code == 0, res.output
        assert not (root / "graphify-out" / "slurp-graph.json").exists()
        assert theirs.read_bytes() == before
        assert _mcp_graph(root) == str(theirs.resolve())
        text = (root / "CLAUDE.local.md").read_text(encoding="utf-8")
        assert "built by graphify" in text

    def test_slurps_own_graph_is_reused(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p")
        assert CliRunner().invoke(cli, ["index", str(root)]).exit_code == 0
        graph = root / "graphify-out" / "graph.json"
        before = graph.read_bytes()
        res = _init(root, monkeypatch, "--yes")
        assert res.exit_code == 0, res.output
        assert "Reuse" in res.output
        assert graph.read_bytes() == before

    def test_the_plan_names_the_options(self, tmp_path, monkeypatch):
        root = tmp_path / "g"
        shutil.copytree(FIXTURE, root)
        res = _init(root, monkeypatch, input="n\n")
        plan = res.output[:res.output.index("Proceed?")]
        assert "slurp-graph.json" in plan and "--keep-graphify" in plan
        assert "CLAUDE.local.md" in plan and "--instructions" in plan
        assert ".claude/settings.local.json" in plan

    def test_declining_writes_nothing(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p")
        before = sorted(p.name for p in root.iterdir())
        res = _init(root, monkeypatch, input="n\n")
        assert res.exit_code != 0
        assert sorted(p.name for p in root.iterdir()) == before


class TestClaudeCodeApprovals:
    def test_settings_local_json_is_merged(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p")
        res = _init(root, monkeypatch, "--yes")
        assert res.exit_code == 0, res.output
        data = json.loads((root / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        assert data == {"enabledMcpjsonServers": ["slurp"],
                        "permissions": {"allow": ["mcp__slurp__*"]}}

    def test_it_goes_to_the_git_root_from_a_subdirectory(self, tmp_path, monkeypatch):
        repo = _project(tmp_path / "repo")
        sub = repo / "services" / "api"
        sub.mkdir(parents=True)
        (sub / "api.py").write_text("def api():\n    pass\n", encoding="utf-8")
        res = _init(sub, monkeypatch, "--yes")
        assert res.exit_code == 0, res.output
        assert (repo / ".claude" / "settings.local.json").is_file()
        assert not (sub / ".claude").exists()
        assert (sub / ".mcp.json").is_file()

    def test_without_git_it_goes_to_the_current_directory(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p", git=False)
        assert _init(root, monkeypatch, "--yes").exit_code == 0
        assert (root / ".claude" / "settings.local.json").is_file()

    def test_an_invalid_settings_file_is_left_and_the_fragment_printed(self, tmp_path,
                                                                       monkeypatch):
        root = _project(tmp_path / "p")
        (root / ".claude").mkdir()
        (root / ".claude" / "settings.local.json").write_text("{oops", encoding="utf-8")
        res = _init(root, monkeypatch, "--yes")
        assert res.exit_code == 0, res.output
        assert (root / ".claude" / "settings.local.json").read_text(encoding="utf-8") == "{oops"
        assert '"mcp__slurp__*"' in res.output

    def test_a_tracked_settings_file_is_reported(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p")
        (root / ".claude").mkdir()
        (root / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")
        _git(root, "add", "-f", ".claude/settings.local.json")
        res = _init(root, monkeypatch, "--yes")
        assert "git rm --cached" in res.output


class TestInstructions:
    def test_local_by_default(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p")
        assert _init(root, monkeypatch, "--yes").exit_code == 0
        assert (root / "CLAUDE.local.md").read_text(encoding="utf-8") == instruction_block("own")

    def test_agents_md_is_imported_when_it_would_stop_being_read(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p")
        (root / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
        assert _init(root, monkeypatch, "--yes").exit_code == 0
        assert (root / "CLAUDE.local.md").read_text(encoding="utf-8").startswith("@AGENTS.md")

    def test_versioned(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p")
        assert _init(root, monkeypatch, "--yes", "--instructions", "versioned").exit_code == 0
        assert BLOCK_BEGIN in (root / "CLAUDE.md").read_text(encoding="utf-8")
        assert not (root / "CLAUDE.local.md").exists()

    def test_none(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p")
        assert _init(root, monkeypatch, "--yes", "--instructions", "none").exit_code == 0
        assert not (root / "CLAUDE.local.md").exists() and not (root / "CLAUDE.md").exists()

    def test_running_init_twice_changes_nothing_the_second_time(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p")
        assert _init(root, monkeypatch, "--yes").exit_code == 0
        files = {p: p.read_bytes() for p in (root / "CLAUDE.local.md", root / ".gitignore",
                                             root / ".claude" / "settings.local.json")}
        assert _init(root, monkeypatch, "--yes").exit_code == 0
        assert {p: p.read_bytes() for p in files} == files

    def test_the_summary_mentions_worktrees(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p")
        res = _init(root, monkeypatch, "--yes")
        assert "worktree" in res.output


class TestGitignore:
    def test_the_three_entries(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p")
        assert _init(root, monkeypatch, "--yes").exit_code == 0
        lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert lines == ["node_modules/", ".slurp/", ".claude/settings.local.json",
                         "CLAUDE.local.md"]

    def test_already_ignored_files_are_not_added(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p", gitignore=".claude/\n*.local.md\n")
        assert _init(root, monkeypatch, "--yes").exit_code == 0
        lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert lines == [".claude/", "*.local.md", ".slurp/"]

    def test_no_gitignore_is_created_but_the_risk_is_said(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p", gitignore=None)
        res = _init(root, monkeypatch, "--yes")
        assert res.exit_code == 0
        assert not (root / ".gitignore").exists()
        assert "not ignored by git" in res.output


class TestOtherClients:
    def test_fragments_are_printed_not_written(self, tmp_path, monkeypatch):
        root = _project(tmp_path / "p")
        res = _init(root, monkeypatch, "--yes")
        for name in ("Codex", "Gemini CLI", "Cursor"):
            assert name in res.output
        assert "[mcp_servers.slurp]" in res.output
        assert not (root / ".codex").exists() and not (root / ".gemini").exists()
        assert not (root / ".cursor").exists()


@pytest.mark.parametrize("bad", ["everywhere"])
def test_instructions_takes_three_values(tmp_path, monkeypatch, bad):
    root = _project(tmp_path / "p")
    res = _init(root, monkeypatch, "--yes", "--instructions", bad)
    assert res.exit_code != 0


class TestNeverOverwritesAGraphOfAnotherTool:
    def test_graphifys_graph_where_slurps_would_go_is_not_replaced(self, tmp_path, monkeypatch):
        root = tmp_path / "g"
        shutil.copytree(FIXTURE, root)
        theirs = root / "graphify-out" / "graph.json"
        target = root / "graphify-out" / "slurp-graph.json"
        theirs.rename(target)                      # graphify's graph, under slurp's name
        before = target.read_bytes()
        res = _init(root, monkeypatch, "--yes")
        assert res.exit_code != 0
        assert target.read_bytes() == before
        assert "--keep-graphify" in res.output

    def test_an_old_slurp_graph_there_is_rebuilt(self, tmp_path, monkeypatch):
        # slurp ≤ 1.2 wrote it; its files are gone, so it cannot be adopted.
        root = tmp_path / "g"
        shutil.copytree(FIXTURE, root)
        old = root / "graphify-out" / "slurp-graph.json"
        old.write_text(json.dumps({"nodes": [{"id": "x", "label": "x",
                                              "source_file": "gone.py"}],
                                   "links": [], "built_at_commit": None}), encoding="utf-8")
        res = _init(root, monkeypatch, "--yes")
        assert res.exit_code == 0, res.output
        assert "slurp" in json.loads(old.read_text(encoding="utf-8"))
