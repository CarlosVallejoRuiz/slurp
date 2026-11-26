"""The phase's measurement scripts in docs/design/mcp-autonomo/ stay runnable."""

import json
import runpy
from pathlib import Path

DESIGN = Path(__file__).resolve().parent.parent / "docs" / "design" / "mcp-autonomo"


def _analyzer():
    return runpy.run_path(str(DESIGN / "analyze_sessions.py"))


def _task(root: Path, name: str, calls: list[dict], edited: list[str], denials: int = 0,
          other_denials: int = 0, stream: list[dict] | None = None, end: bool = True) -> None:
    d = root / name
    d.mkdir()
    lines = [{"event": "start", "session": "s"}] + [
        {"event": "call", "session": "s", **c} for c in calls]
    if end:
        lines.append({"event": "end", "session": "s",
                      "changed": {"count": len(edited), "files": edited}})
    (d / "session.log").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    refused = ([{"tool_name": "mcp__slurp__slurp_query"}] * denials
               + [{"tool_name": "Bash"}] * other_denials)
    (d / "claude.json").write_text(json.dumps({"permission_denials": refused}),
                                   encoding="utf-8")
    if stream is not None:
        (d / "stream.jsonl").write_text("".join(json.dumps(e) + "\n" for e in stream),
                                        encoding="utf-8")
    (d / "changed-files.txt").write_text("\n".join(edited) + "\n", encoding="utf-8")


def _tool_use(name: str, **tool_input) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": name, "input": tool_input}]}}


class TestAnalyzeSessions:
    def test_the_criterion_is_met_with_eight_spontaneous_sessions(self, tmp_path):
        good = [{"tool": "slurp_query", "graph": "fresh"},
                {"tool": "slurp_impact", "args": {"file_path": "a.py"}, "graph": "fresh"}]
        for i in range(8):
            _task(tmp_path, f"t{i}", good, ["a.py"])
        for i in range(8, 10):
            _task(tmp_path, f"t{i}", [], ["b.py"])
        mod = _analyzer()
        met, lines = mod["analyse"]([mod["load_task"](d) for d in sorted(tmp_path.iterdir())])
        assert met, "\n".join(lines)
        assert "info  edited files with a slurp_impact/slurp_explain first: 8/10 (80%)" in lines

    def test_a_late_first_call_and_a_denial_fail_it(self, tmp_path):
        late = [{"tool": "slurp_query", "graph": "refreshed", "refresh_ms": 300,
                 "changed": {"count": 1, "files": ["a.py"]}}]
        for i in range(10):
            _task(tmp_path, f"t{i}", late, ["a.py"], denials=1 if i == 0 else 0)
        mod = _analyzer()
        met, lines = mod["analyse"]([mod["load_task"](d) for d in sorted(tmp_path.iterdir())])
        assert not met
        assert any(line.startswith("FAIL  first call before any edit in 0/10") for line in lines)
        assert any(line.startswith("FAIL  slurp tools denied: 1") for line in lines)

    def test_only_slurps_denials_count_for_the_criterion(self, tmp_path):
        # Agreed before launching: "0 approvals" = 0 denials of mcp__slurp__* tools;
        # other denials (a shell command off the fixed list) are reported apart.
        good = [{"tool": "slurp_query", "graph": "fresh"}]
        for i in range(10):
            _task(tmp_path, f"t{i}", good, ["a.py"], other_denials=2)
        mod = _analyzer()
        met, lines = mod["analyse"]([mod["load_task"](d) for d in sorted(tmp_path.iterdir())])
        assert met, "\n".join(lines)
        assert any("other denials, not in the criterion: 20" in line for line in lines)

    def test_graphify_use_is_recorded_per_session(self, tmp_path):
        stream = [_tool_use("Skill", skill="graphify"), _tool_use("Bash", command="graphify ."),
                  _tool_use("mcp__slurp__slurp_query", query="q")]
        _task(tmp_path, "t0", [{"tool": "slurp_query", "graph": "fresh"}], [], stream=stream)
        _task(tmp_path, "t1", [], [], stream=[_tool_use("Grep", pattern="x")])
        mod = _analyzer()
        results = [mod["load_task"](d) for d in sorted(tmp_path.iterdir())]
        assert results[0].graphify == ["Skill graphify", "Bash: graphify ."]
        assert results[1].graphify == []
        assert results[0].agent_tools["mcp__slurp__slurp_query"] == 1
        _met, lines = mod["analyse"](results)
        assert any("graphify used in 1/2 sessions" in line for line in lines)

    def test_only_running_graphify_counts_not_naming_its_paths(self, tmp_path):
        # Found in the measurement: every "graphify" match was a path —
        # graphify-out/graph.json, tests/fixtures/graphify_project, grep -v.
        paths = ["ls graphify-out/", "grep -rn x . | grep -v graphify-out",
                 "uv run slurp q --graph graphify-out/graph.json",
                 "ls tests/fixtures/graphify_project", "echo 'No graphify or LLM'",
                 "cat docs/graphify.md"]
        runs = ["graphify .", "cd repo && graphify update .", "uvx graphify .",
                "python3 -m graphify .", "uv run graphify .", "git status; graphify",
                "python -c 'import graphify'"]
        stream = [_tool_use("Bash", command=c) for c in paths + runs]
        _task(tmp_path, "t0", [], [], stream=stream)
        found = _analyzer()["load_task"](tmp_path / "t0").graphify
        assert found == [f"Bash: {c}" for c in runs]

    def test_denials_count_from_every_result_event_not_only_the_last(self, tmp_path):
        # Found in the measurement (sl-2): an agent that leaves a background task
        # running gets two result events; claude.json keeps the last, which listed
        # none of the session's nine denials.
        def denial(tool, use_id):
            return {"tool_name": tool, "tool_use_id": use_id, "tool_input": {}}
        first = {"type": "result", "permission_denials": [
            denial("mcp__slurp__slurp_query", "u1"), denial("Bash", "u2")]}
        last = {"type": "result", "permission_denials": [denial("Bash", "u2")]}
        _task(tmp_path, "t0", [], [], stream=[first, last])
        (tmp_path / "t0" / "claude.json").write_text(json.dumps(last), encoding="utf-8")
        mod = _analyzer()
        result = mod["load_task"](tmp_path / "t0")
        assert (result.denials, result.other_denials) == (1, 1)
        names = [d["tool_name"] for d in mod["permission_denials"](tmp_path / "t0")]
        assert names == ["mcp__slurp__slurp_query", "Bash"]

    def test_without_an_end_event_the_diff_says_what_was_edited(self, tmp_path):
        # If the client kills the server instead of closing its stdin, the end
        # event never comes; the clone's diff stands in.
        _task(tmp_path, "t0", [{"tool": "slurp_query", "graph": "fresh"}], ["a.py", "README.md"],
              end=False)
        mod = _analyzer()
        assert mod["load_task"](tmp_path / "t0").edited == ["a.py"]

    def test_a_stale_answer_without_a_warning_fails_it(self, tmp_path):
        for i in range(10):
            _task(tmp_path, f"t{i}", [{"tool": "slurp_query", "graph": "stale"}], [])
        mod = _analyzer()
        met, lines = mod["analyse"]([mod["load_task"](d) for d in sorted(tmp_path.iterdir())])
        assert not met
        assert any("stale graph without a warning: 10" in line for line in lines)

    def test_missing_files_are_reported(self, tmp_path):
        (tmp_path / "t0").mkdir()
        mod = _analyzer()
        assert mod["main"]([str(tmp_path)]) == 2


class TestTasks:
    def test_ten_tasks_five_per_project_and_no_prompt_names_slurp(self):
        tasks = json.loads((DESIGN / "tareas.json").read_text(encoding="utf-8"))["tasks"]
        assert len(tasks) == 10
        assert sorted(t["project"] for t in tasks) == ["prismastats"] * 5 + ["slurp"] * 5
        for task in tasks:
            prompt = task["prompt"].lower()
            assert "slurp" not in prompt and "mcp" not in prompt and "grafo" not in prompt, \
                task["id"]


def _contain():
    return runpy.run_path(str(DESIGN / "contain_settings.py"))


class TestContainSettings:
    """The measurement clones' permissions never point at the real checkout."""

    def _clone(self, tmp_path: Path, real: Path) -> Path:
        clone = tmp_path / "work" / "ps-1" / "repo"
        (clone / ".claude").mkdir(parents=True)
        (clone / ".claude" / "settings.local.json").write_text(json.dumps({
            "permissions": {
                "allow": [f"Bash(npx --prefix {real} tsc --noEmit)",
                          f"Bash(node -e \"require('{real}/node_modules/x')\")",
                          "Bash(npm run:*)"],
                "additionalDirectories": [f"{real}/docs", f"{real}/supabase/migrations"],
            },
            "enabledMcpjsonServers": ["slurp"],
        }), encoding="utf-8")
        (clone / ".claude" / "settings.json").write_text(json.dumps({
            "permissions": {"additionalDirectories": [
                "c:\\Users\\Jairo Vallejo Ruiz\\Desktop\\PrismaStats\\app\\api\\cron"]}}),
            encoding="utf-8")
        return clone

    def test_prismastats_paths_are_pointed_into_the_clone(self, tmp_path):
        real = tmp_path / "Desktop" / "prismastats"
        real.mkdir(parents=True)
        clone = self._clone(tmp_path, real)
        mod = _contain()
        assert mod["offenders"](clone)                     # before: the real checkout
        assert sorted(mod["rewrite"](clone, real)) == [".claude/settings.json",
                                                       ".claude/settings.local.json"]
        assert mod["offenders"](clone) == []
        local = json.loads((clone / ".claude" / "settings.local.json").read_text())
        inside = str(clone.resolve())
        assert local["permissions"]["additionalDirectories"] == [
            f"{inside}/docs", f"{inside}/supabase/migrations"]
        shared = json.loads((clone / ".claude" / "settings.json").read_text())
        assert shared["permissions"]["additionalDirectories"] == [f"{inside}/app/api/cron"]

    def test_a_rule_reaching_outside_is_named(self, tmp_path):
        clone = tmp_path / "repo"
        (clone / ".claude").mkdir(parents=True)
        (clone / ".claude" / "settings.local.json").write_text(json.dumps({
            "permissions": {"allow": ["Read(//Users/someone/secrets/**)", "Bash(ls:*)"]}}),
            encoding="utf-8")
        mod = _contain()
        [line] = mod["offenders"](clone)
        assert "permissions.allow.0" in line and "//Users/someone/secrets/" in line
        assert mod["main"](["check", str(clone)]) == 1

    def test_paths_inside_the_clone_and_other_keys_pass(self, tmp_path):
        clone = tmp_path / "repo"
        (clone / ".claude").mkdir(parents=True)
        inside = str(clone.resolve())
        (clone / ".claude" / "settings.local.json").write_text(json.dumps({
            "permissions": {"allow": [f"Read(/{inside}/**)", "mcp__slurp__*"],
                            "additionalDirectories": [f"{inside}/docs"]},
            "env": {"HOME_COPY": "/Users/someone"}}), encoding="utf-8")
        assert _contain()["main"](["check", str(clone)]) == 0
