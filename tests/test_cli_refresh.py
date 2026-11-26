"""The query commands keep slurp's own graph current (v1.3, docs/design/mcp-autonomo.md §3)."""

import json
import os
import shutil
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from slurp.cli import cli

FIXTURE = Path(__file__).parent / "fixtures" / "graphify_project"


def _put(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _age(root: Path, seconds: float = 60) -> None:
    past = time.time() - seconds
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + dirnames:
            os.utime(Path(dirpath) / name, (past, past), follow_symlinks=False)
        os.utime(dirpath, (past, past))


def _run(args: list[str], env: dict | None = None):
    return CliRunner().invoke(cli, args, env=env)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project indexed into graphify-out/graph.json; returns the graph path."""
    root = tmp_path / "proj"
    _put(root, "app.py", "from lib.billing import charge\n\n\ndef checkout():\n    return charge()\n")
    _put(root, "lib/billing.py", "def charge():\n    return 1\n")
    res = _run(["index", str(root), "--ignore-file", str(root / ".slurpignore")])
    assert res.exit_code == 0, res.output
    _age(root)
    return root / "graphify-out" / "graph.json"


def _add_refund(graph: Path) -> None:
    _put(graph.parent.parent, "lib/billing.py",
         "def charge():\n    return 1\n\n\ndef refund_payment():\n    return -1\n")


def _on_disk(graph: Path) -> set[str]:
    return {n["id"] for n in json.loads(graph.read_text(encoding="utf-8"))["nodes"]}


@pytest.fixture
def graphify_copy(tmp_path: Path) -> Path:
    root = tmp_path / "graphify_project"
    shutil.copytree(FIXTURE, root)
    _age(root)
    return root / "graphify-out" / "graph.json"


class TestQueriesSeeTheWorkingTree:
    def test_a_query_answers_from_the_refreshed_graph(self, project):
        _add_refund(project)
        res = _run(["refund payment", "--graph", str(project), "--no-audit"])
        assert res.exit_code == 0, res.output
        assert "refund_payment" in res.stdout
        assert "⚠" not in res.stderr
        assert "lib.billing.refund_payment" in _on_disk(project)

    @pytest.mark.parametrize("cmd", [
        ["explain", "refund_payment", "--no-llm"],
        ["impact", "lib/billing.py"],
        ["export", "refund payment"],
    ])
    def test_explain_impact_and_export_refresh_too(self, project, cmd):
        _add_refund(project)
        res = _run([*cmd, "--graph", str(project)])
        assert res.exit_code == 0, res.output
        assert "refund_payment" in res.stdout

    def test_stats_counts_the_refreshed_graph(self, project):
        before = len(_on_disk(project))
        _add_refund(project)
        res = _run(["stats", "--graph", str(project)])
        assert res.exit_code == 0, res.output
        assert f"Nodes: {before + 1}" in res.stdout

    def test_advisor_refreshes_the_graph_it_names(self, project, tmp_path):
        _add_refund(project)
        res = _run(["advisor", "refund", "--graph", str(project),
                    "--audit-dir", str(tmp_path / "no-audit")])
        assert res.exit_code == 0, res.output
        assert "lib.billing.refund_payment" in _on_disk(project)

    def test_json_output_stays_clean_while_warnings_go_to_stderr(self, project):
        _add_refund(project)
        res = _run(["refund", "--graph", str(project), "--format", "json", "--no-audit",
                    "--no-refresh"])
        assert res.exit_code == 0, res.output
        json.loads(res.stdout)
        assert "⚠" in res.stderr

    def test_the_audit_log_records_the_graph_state(self, project, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _add_refund(project)
        assert _run(["refund", "--graph", str(project)]).exit_code == 0
        assert _run(["refund", "--graph", str(project)]).exit_code == 0
        lines = (tmp_path / ".slurp" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["graph"] for line in lines] == ["refreshed", "fresh"]


class TestRefreshSettings:
    def test_no_refresh_warns_and_leaves_the_graph(self, project):
        before = project.read_bytes()
        _add_refund(project)
        res = _run(["refund payment", "--graph", str(project), "--no-audit", "--no-refresh"])
        assert res.exit_code == 0, res.output
        assert "refund_payment" not in res.stdout
        assert "1 file changed since this graph was built (lib/billing.py)" in res.stderr
        assert project.read_bytes() == before

    def test_slurp_refresh_0_is_the_same(self, project):
        before = project.read_bytes()
        _add_refund(project)
        res = _run(["refund payment", "--graph", str(project), "--no-audit"],
                   env={"SLURP_REFRESH": "0"})
        assert res.exit_code == 0, res.output
        assert project.read_bytes() == before

    def test_a_zero_timeout_answers_first_and_finishes_the_refresh_before_exiting(self, project):
        _add_refund(project)
        res = _run(["refund payment", "--graph", str(project), "--no-audit",
                    "--refresh-timeout", "0"])
        assert res.exit_code == 0, res.output
        assert "⚠ Graph refresh" in res.stderr
        assert ("finishing graph refresh… (Ctrl+C to skip; the graph stays as it was)"
                in res.stderr)
        assert "lib.billing.refund_payment" in _on_disk(project)

    def test_the_timeout_comes_from_the_environment(self, project):
        _add_refund(project)
        res = _run(["refund payment", "--graph", str(project), "--no-audit"],
                   env={"SLURP_REFRESH_TIMEOUT": "0"})
        assert "finishing graph refresh" in res.stderr

    def test_a_bad_timeout_is_a_clear_error(self, project):
        res = _run(["x", "--graph", str(project), "--refresh-timeout", "-1"])
        assert res.exit_code != 0
        assert "--refresh-timeout" in res.output


class TestMeasurementsDoNotRefresh:
    def test_benchmark_warns_and_measures_the_graph_as_it_is(self, project):
        before = project.read_bytes()
        _add_refund(project)
        res = _run(["benchmark", "--graph", str(project), "--queries", "refund",
                    "--budget", "2000"])
        assert res.exit_code == 0, res.output
        assert "not refreshed" in res.stderr
        assert project.read_bytes() == before

    def test_eval_retrieval_warns_too(self, project, tmp_path):
        before = project.read_bytes()
        questions = tmp_path / "q.json"
        questions.write_text(json.dumps([{"id": "q1", "question": "charge",
                                          "ground_truth": "charge",
                                          "relevant_nodes": ["lib.billing.charge"]}]),
                             encoding="utf-8")
        _add_refund(project)
        res = _run(["eval", "--retrieval", "--graph", str(project),
                    "--questions", str(questions)])
        assert res.exit_code == 0, res.output
        assert "not refreshed" in res.stderr
        assert project.read_bytes() == before


class TestGraphifyGraphsStayUntouched:
    @pytest.mark.parametrize("cmd", [
        ["helper", "--no-audit"],
        ["explain", "helper", "--no-llm"],
        ["impact", "lib/util.py"],
        ["stats"],
        ["export", "helper"],
    ])
    def test_not_one_byte_changes(self, graphify_copy, cmd):
        root = graphify_copy.parent.parent
        before = graphify_copy.read_bytes()
        _put(root, "lib/util.py", "def helper():\n    return 99\n")
        _put(root, "lib/brand_new.py", "def fresh():\n    pass\n")
        (root / "lib" / "client.ts").unlink()
        res = _run([*cmd, "--graph", str(graphify_copy)])
        assert res.exit_code == 0, res.output
        assert graphify_copy.read_bytes() == before
        assert sorted(p.name for p in graphify_copy.parent.iterdir()) == ["graph.json"]
        assert "slurp doesn't rebuild graphify graphs — rerun graphify." in res.stderr


class TestAutodiscovery:
    def test_slurps_graph_beside_graphifys_is_preferred(self, project, tmp_path, monkeypatch):
        root = project.parent.parent
        slurps = root / "graphify-out" / "slurp-graph.json"
        project.rename(slurps)
        shutil.copyfile(FIXTURE / "graphify-out" / "graph.json", project)
        monkeypatch.chdir(root)
        res = _run(["stats"])
        assert res.exit_code == 0, res.output
        assert "slurp-graph.json" in res.stdout

    def test_the_search_order(self):
        from slurp.cli import _GRAPH_SEARCH_PATHS

        assert [p.as_posix() for p in _GRAPH_SEARCH_PATHS] == [
            "graph.json", "graphify-out/slurp-graph.json", "graphify-out/graph.json",
            ".graphify/graph.json"]


class TestDiffNeverRefreshes:
    def test_diff_reads_both_files_as_they_are(self, project, tmp_path):
        other = tmp_path / "old.json"
        shutil.copyfile(project, other)
        before = project.read_bytes()
        _add_refund(project)
        res = _run(["diff", str(other), str(project)])
        assert res.exit_code == 0, res.output
        assert project.read_bytes() == before
