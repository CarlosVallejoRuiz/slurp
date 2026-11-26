"""Tests for slurp.clients — what `slurp init` writes for coding agents (v1.3, design §5.2, §5.4)."""

import json
import subprocess
from pathlib import Path

import pytest

from slurp.clients import (
    BLOCK_BEGIN,
    BLOCK_END,
    client_fragments,
    ensure_ignored,
    instruction_block,
    merge_claude_settings,
    plan_instructions,
    write_instructions,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


# ---------------------------------------------------------------------------
# .claude/settings.local.json
# ---------------------------------------------------------------------------

class TestClaudeSettings:
    def test_creates_the_file_with_both_approvals(self, tmp_path):
        path = tmp_path / ".claude" / "settings.local.json"
        result = merge_claude_settings(path)
        assert result.written and not result.warnings
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "enabledMcpjsonServers": ["slurp"],
            "permissions": {"allow": ["mcp__slurp__*"]},
        }

    def test_merges_without_touching_the_rest(self, tmp_path):
        path = tmp_path / "settings.local.json"
        path.write_text(json.dumps({
            "enabledMcpjsonServers": ["db"],
            "permissions": {"allow": ["Bash(npm test)"], "deny": ["Read(.env)"]},
            "model": "opus",
        }), encoding="utf-8")
        merge_claude_settings(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["enabledMcpjsonServers"] == ["db", "slurp"]
        assert data["permissions"] == {"allow": ["Bash(npm test)", "mcp__slurp__*"],
                                       "deny": ["Read(.env)"]}
        assert data["model"] == "opus"

    def test_never_duplicates(self, tmp_path):
        path = tmp_path / "settings.local.json"
        merge_claude_settings(path)
        before = path.read_text(encoding="utf-8")
        result = merge_claude_settings(path)
        assert not result.written
        assert path.read_text(encoding="utf-8") == before

    def test_a_rule_that_already_covers_slurp_is_enough(self, tmp_path):
        path = tmp_path / "settings.local.json"
        path.write_text(json.dumps({"enabledMcpjsonServers": ["slurp"],
                                    "permissions": {"allow": ["mcp__slurp"]}}),
                        encoding="utf-8")
        assert not merge_claude_settings(path).written

    def test_invalid_json_is_never_touched(self, tmp_path):
        path = tmp_path / "settings.local.json"
        path.write_text('{"permissions": {"allow": [', encoding="utf-8")
        result = merge_claude_settings(path)
        assert not result.written and result.invalid
        assert path.read_text(encoding="utf-8") == '{"permissions": {"allow": ['
        assert '"mcp__slurp__*"' in result.fragment

    def test_a_disabled_server_stays_disabled(self, tmp_path):
        path = tmp_path / "settings.local.json"
        path.write_text(json.dumps({"disabledMcpjsonServers": ["slurp"]}), encoding="utf-8")
        result = merge_claude_settings(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "slurp" not in data.get("enabledMcpjsonServers", [])
        assert data["disabledMcpjsonServers"] == ["slurp"]
        assert any("disabledMcpjsonServers" in w for w in result.warnings)

    @pytest.mark.parametrize("kind", ["deny", "ask"])
    def test_a_deny_or_ask_rule_is_kept_and_reported(self, tmp_path, kind):
        path = tmp_path / "settings.local.json"
        path.write_text(json.dumps({"permissions": {kind: ["mcp__slurp__slurp_diff"]}}),
                        encoding="utf-8")
        result = merge_claude_settings(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["permissions"][kind] == ["mcp__slurp__slurp_diff"]
        assert any(kind in w and "mcp__slurp__slurp_diff" in w for w in result.warnings)

    def test_a_file_that_is_not_an_object_is_never_touched(self, tmp_path):
        path = tmp_path / "settings.local.json"
        path.write_text("[]", encoding="utf-8")
        result = merge_claude_settings(path)
        assert result.invalid and path.read_text(encoding="utf-8") == "[]"


# ---------------------------------------------------------------------------
# The paragraph for the agent
# ---------------------------------------------------------------------------

class TestInstructionBlock:
    def test_own_graph_text(self):
        assert instruction_block("own") == (
            f"{BLOCK_BEGIN}\n## Code navigation (slurp)\n\n"
            "This project has a slurp MCP server: a code graph that stays in sync with the "
            "working tree. Before searching or reading files to find where something lives, "
            "call `slurp_query` with a plain-language description of the behaviour. Before "
            "editing a file, call `slurp_impact` with its path; before changing a function or "
            "class, call `slurp_explain` with its name. Use grep for exact strings, and open "
            f"the files slurp points to before editing them.\n{BLOCK_END}\n")

    def test_graphify_variant(self):
        text = instruction_block("graphify")
        assert ("This project has a slurp MCP server over a graph built by graphify (rebuild "
                "it after large changes; slurp does not refresh it).") in text
        assert "stays in sync" not in text

    def test_markers_are_html_comments(self):
        assert BLOCK_BEGIN.startswith("<!--") and BLOCK_END.endswith("-->")


class TestLocalMode:
    def test_writes_claude_local_md(self, tmp_path):
        plan = plan_instructions(tmp_path, "local", "own")
        written = write_instructions(plan)
        assert written == [tmp_path / "CLAUDE.local.md"]
        assert (tmp_path / "CLAUDE.local.md").read_text(encoding="utf-8") == \
            instruction_block("own")

    def test_imports_agents_md_when_nothing_else_reads_it(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
        write_instructions(plan_instructions(tmp_path, "local", "own"))
        text = (tmp_path / "CLAUDE.local.md").read_text(encoding="utf-8")
        assert text.startswith("@AGENTS.md\n\n")

    def test_no_import_when_a_claude_md_exists(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
        (tmp_path / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")
        write_instructions(plan_instructions(tmp_path, "local", "own"))
        assert not (tmp_path / "CLAUDE.local.md").read_text(encoding="utf-8").startswith("@")

    def test_no_import_when_a_claude_md_exists_above(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Monorepo\n", encoding="utf-8")
        sub = tmp_path / "apps" / "web"
        sub.mkdir(parents=True)
        (sub / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
        write_instructions(plan_instructions(sub, "local", "own"))
        assert not (sub / "CLAUDE.local.md").read_text(encoding="utf-8").startswith("@")

    def test_nested_agents_md_files_are_warned_about(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "AGENTS.md").write_text("# Pkg\n", encoding="utf-8")
        plan = plan_instructions(tmp_path, "local", "own")
        assert any("claude-md-and-agents-md" in w for w in plan.warnings)

    def test_an_existing_claude_local_md_gets_the_block_appended_and_no_import(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
        (tmp_path / "CLAUDE.local.md").write_text("my notes\n", encoding="utf-8")
        write_instructions(plan_instructions(tmp_path, "local", "own"))
        text = (tmp_path / "CLAUDE.local.md").read_text(encoding="utf-8")
        assert text.startswith("my notes\n") and "@AGENTS.md" not in text
        assert text.endswith(instruction_block("own"))


class TestVersionedMode:
    @pytest.mark.parametrize("present,expected", [
        ({"CLAUDE.md": "# C\n"}, ["CLAUDE.md"]),
        ({"AGENTS.md": "# A\n"}, ["AGENTS.md"]),
        ({"CLAUDE.md": "@AGENTS.md\n", "AGENTS.md": "# A\n"}, ["AGENTS.md"]),
        ({"CLAUDE.md": "# C\n", "AGENTS.md": "# A\n"}, ["AGENTS.md", "CLAUDE.md"]),
        ({}, ["CLAUDE.md"]),
    ])
    def test_the_destination_table(self, tmp_path, present, expected):
        for name, text in present.items():
            (tmp_path / name).write_text(text, encoding="utf-8")
        written = write_instructions(plan_instructions(tmp_path, "versioned", "own"))
        assert sorted(p.name for p in written) == expected
        for name in expected:
            assert BLOCK_BEGIN in (tmp_path / name).read_text(encoding="utf-8")


class TestNeverTwice:
    def test_rerunning_replaces_the_block_between_the_markers(self, tmp_path):
        write_instructions(plan_instructions(tmp_path, "local", "own"))
        path = tmp_path / "CLAUDE.local.md"
        path.write_text(path.read_text(encoding="utf-8").replace("stays in sync", "OLD") +
                        "after\n", encoding="utf-8")
        write_instructions(plan_instructions(tmp_path, "local", "graphify"))
        text = path.read_text(encoding="utf-8")
        assert text.count(BLOCK_BEGIN) == 1 and "OLD" not in text
        assert "built by graphify" in text and text.endswith("after\n")

    def test_markers_in_another_file_are_updated_there(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# C\n\n" + instruction_block("own"),
                                            encoding="utf-8")
        written = write_instructions(plan_instructions(tmp_path, "local", "graphify"))
        assert written == [tmp_path / "CLAUDE.md"]
        assert not (tmp_path / "CLAUDE.local.md").exists()

    def test_a_file_that_already_mentions_slurp_is_left_alone(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("Always call slurp_query first.\n",
                                            encoding="utf-8")
        plan = plan_instructions(tmp_path, "local", "own")
        assert write_instructions(plan) == []
        assert any("slurp_query" in w for w in plan.warnings)
        assert not (tmp_path / "CLAUDE.local.md").exists()

    def test_none_writes_nothing(self, tmp_path):
        assert write_instructions(plan_instructions(tmp_path, "none", "own")) == []


# ---------------------------------------------------------------------------
# .gitignore and other clients
# ---------------------------------------------------------------------------

class TestEnsureIgnored:
    def test_appends_an_entry(self, tmp_path):
        gi = tmp_path / ".gitignore"
        gi.write_text("node_modules/\n", encoding="utf-8")
        assert ensure_ignored(gi, "CLAUDE.local.md")
        assert gi.read_text(encoding="utf-8") == "node_modules/\nCLAUDE.local.md\n"

    @pytest.mark.parametrize("existing", ["CLAUDE.local.md", "/CLAUDE.local.md",
                                          "!CLAUDE.local.md", "**/CLAUDE.local.md"])
    def test_respects_what_is_there(self, tmp_path, existing):
        gi = tmp_path / ".gitignore"
        gi.write_text(existing + "\n", encoding="utf-8")
        assert not ensure_ignored(gi, "CLAUDE.local.md")

    def test_a_missing_gitignore_is_not_created(self, tmp_path):
        assert not ensure_ignored(tmp_path / ".gitignore", "CLAUDE.local.md")
        assert not (tmp_path / ".gitignore").exists()


class TestClientFragments:
    def test_codex_gemini_and_cursor(self):
        frags = client_fragments("/opt/bin/slurp", Path("/p/graphify-out/graph.json"))
        assert set(frags) == {"Codex", "Gemini CLI", "Cursor"}
        assert '[mcp_servers.slurp]' in frags["Codex"]
        assert 'command = "/opt/bin/slurp"' in frags["Codex"]
        assert 'default_tools_approval_mode = "writes"' in frags["Codex"]
        assert '"trust": true' in frags["Gemini CLI"]
        assert ".gemini/settings.json" in frags["Gemini CLI"]
        assert ".cursor/mcp.json" in frags["Cursor"] and "Mcp(slurp:*)" in frags["Cursor"]
