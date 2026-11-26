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
