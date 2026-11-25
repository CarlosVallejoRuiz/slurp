"""scripts/retrieval.py measures selection quality on a fixed tuning/holdout split.

The script is not part of the package. These tests pin the split — it was drawn
once, with a documented seed, before the v1.2 scorer work, and must never be
redrawn — and keep the script runnable against the code in this checkout.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "retrieval.py"

pytestmark = pytest.mark.skipif(not _SCRIPT.exists(), reason="no scripts/ (e.g. an sdist)")


def _module():
    spec = importlib.util.spec_from_file_location("retrieval_script", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheSplit:
    def test_the_split_is_pinned(self):
        m = _module()
        assert m.SEED == 12
        assert m.SPLIT["prismastats"] == {
            "tune": ["arch-01", "arch-05", "auth-01", "auth-02", "auth-04", "stats-01",
                     "stats-03", "stats-04"],
            "holdout": ["arch-02", "arch-03", "arch-04", "auth-03", "auth-05", "stats-02",
                        "stats-05"],
        }
        assert m.SPLIT["slurp"] == {
            "tune": ["self-02", "self-04", "self-07", "self-08", "self-09", "self-12",
                     "self-13", "self-15"],
            "holdout": ["self-01", "self-03", "self-05", "self-06", "self-10", "self-11",
                        "self-14", "self-16"],
        }

    @pytest.mark.parametrize("project", ["prismastats", "slurp"])
    def test_the_seed_reproduces_it(self, project):
        m = _module()
        ids = m.SPLIT[project]["tune"] + m.SPLIT[project]["holdout"]
        assert m.draw_split(ids, project) == m.SPLIT[project]

    def test_each_project_is_partitioned(self):
        from slurp.evaluator import default_questions

        m = _module()
        everything = {
            "prismastats": {q.id for q in default_questions()},
            "slurp": {q["id"] for q in m.SLURP_QUESTIONS},
        }
        for project, groups in m.SPLIT.items():
            tune, holdout = set(groups["tune"]), set(groups["holdout"])
            assert not tune & holdout
            assert tune | holdout == everything[project]
            assert len(tune) - len(holdout) in (0, 1)

    def test_the_sixteen_slurp_questions(self):
        m = _module()
        ids = [q["id"] for q in m.SLURP_QUESTIONS]
        assert ids == [f"self-{i:02d}" for i in range(1, 17)]
        assert all(len(q["relevant_nodes"]) == 1 for q in m.SLURP_QUESTIONS)
        assert all(q["relevant_nodes"][0].startswith("slurp.") for q in m.SLURP_QUESTIONS)


def _tiny_graph(tmp_path: Path) -> Path:
    """A graph holding one tuning node of each project, and filler."""
    nodes = [
        {"id": "lib.auth.actions.loginAction", "label": "loginAction",   # auth-04 only
         "type": "function", "source_file": "lib/auth/actions.ts", "file_type": "code"},
        {"id": "slurp.budget.count_tokens", "label": "count_tokens", "type": "function",
         "source_file": "slurp/budget.py", "file_type": "code"},
    ] + [
        {"id": f"filler.f{i}", "label": f"filler{i}", "type": "function",
         "source_file": "filler.py", "file_type": "code"} for i in range(5)
    ]
    path = tmp_path / "graph.json"
    path.write_text(json.dumps({"nodes": nodes, "links": []}))
    return path


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_SCRIPT), *args],
                          capture_output=True, text=True, check=False)


class TestMeasuring:
    def test_measures_the_tuning_group_by_default(self, tmp_path):
        graph = _tiny_graph(tmp_path)
        out = tmp_path / "rows.json"
        res = _run("--prisma", str(graph), "--slurp", str(graph), "--budgets", "4000",
                   "--formats", "markdown", "--json", str(out))
        assert res.returncode == 0, res.stderr
        rows = json.loads(out.read_text())
        assert {(r["project"], r["group"]) for r in rows} == {
            ("prismastats", "tune"), ("slurp", "tune")}
        for row in rows:
            assert row["format"] == "markdown" and row["budget"] == 4000
            assert row["questions_scored"] == 1     # one tuning node per project
            assert row["recall"] == 1.0
            assert row["tokens_per_node"] > 0
            assert row["results"]                   # per question, from slurp eval
        assert "holdout" not in res.stdout

    def test_all_groups_on_request(self, tmp_path):
        graph = _tiny_graph(tmp_path)
        out = tmp_path / "rows.json"
        res = _run("--prisma", str(graph), "--slurp", str(graph), "--budgets", "4000",
                   "--formats", "json", "--group", "all", "--json", str(out))
        assert res.returncode == 0, res.stderr
        rows = json.loads(out.read_text())
        groups = {(r["project"], r["group"]): r for r in rows}
        assert set(groups) == {(p, g) for p in ("prismastats", "slurp")
                               for g in ("tune", "holdout")}
        # No holdout node is in the tiny graph: reported, not scored, no crash.
        assert groups[("slurp", "holdout")]["questions_scored"] == 0
        assert groups[("slurp", "holdout")]["recall"] is None

    def test_a_directory_is_indexed_with_the_code_under_test(self, tmp_path):
        project = tmp_path / "proj"
        (project / "slurp").mkdir(parents=True)
        (project / "slurp" / "budget.py").write_text(
            "def count_tokens(text):\n    return len(text.split())\n")
        out = tmp_path / "rows.json"
        res = _run("--prisma", str(_tiny_graph(tmp_path)), "--slurp", str(project),
                   "--budgets", "4000", "--formats", "markdown", "--json", str(out))
        assert res.returncode == 0, res.stderr
        slurp_row = next(r for r in json.loads(out.read_text()) if r["project"] == "slurp")
        assert slurp_row["recall"] == 1.0
        assert slurp_row["graph_nodes"] >= 2        # module + function, indexed

    def test_src_must_hold_a_slurp_package(self, tmp_path):
        graph = _tiny_graph(tmp_path)
        res = _run("--prisma", str(graph), "--slurp", str(graph), "--src", str(tmp_path))
        assert res.returncode != 0
        assert "no slurp package" in res.stderr
