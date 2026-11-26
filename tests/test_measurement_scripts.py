"""The phase's measurement scripts in docs/design/mcp-autonomo/ stay runnable."""

import json
import runpy
from pathlib import Path

DESIGN = Path(__file__).resolve().parent.parent / "docs" / "design" / "mcp-autonomo"


def _analyzer():
    return runpy.run_path(str(DESIGN / "analyze_sessions.py"))


def _task(root: Path, name: str, calls: list[dict], edited: list[str], denials: int = 0) -> None:
    d = root / name
    d.mkdir()
    lines = [{"event": "start", "session": "s"}] + [
        {"event": "call", "session": "s", **c} for c in calls] + [
        {"event": "end", "session": "s", "changed": {"count": len(edited), "files": edited}}]
    (d / "session.log").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    (d / "claude.json").write_text(json.dumps({"permission_denials": [{}] * denials}),
                                   encoding="utf-8")


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
        assert any(line.startswith("FAIL  approvals asked: 1") for line in lines)

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
