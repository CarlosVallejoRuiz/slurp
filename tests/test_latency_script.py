"""scripts/latency.py reproduces the README's Performance figures.

The script is not part of the package; these tests keep it runnable against
the code in this checkout, by both methods the README describes.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "latency.py"

pytestmark = pytest.mark.skipif(not _SCRIPT.exists(), reason="no scripts/ (e.g. an sdist)")


def _module():
    spec = importlib.util.spec_from_file_location("latency_script", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_query_sets_are_the_ones_the_readme_lists():
    m = _module()
    assert m.ORIGINAL_QUERIES == [
        "auth flow", "prisma schema", "database pool", "player stats", "admin permissions",
        "match events", "dashboard data", "chat context", "league standings", "login logout"]
    assert len(m.CURRENT_QUERIES) == 5 and m.CURRENT_ROUNDS == 3


def test_one_run_by_each_method(sample_graph_json):
    m = _module()
    original = m.run_once(sample_graph_json, "original")
    assert set(original) == {"mean_ms", "median_ms"}
    current = m.run_once(sample_graph_json, "current")
    assert set(current) == {"cold_ms", "warm_median_ms", "warm_max_ms"}
    assert all(v > 0 for v in (*original.values(), *current.values()))


def test_command_line_measures_in_fresh_processes(sample_graph_json):
    res = subprocess.run(
        [sys.executable, str(_SCRIPT), str(sample_graph_json), "--method", "both",
         "--runs", "2"],
        capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    lines = [json.loads(line) for line in res.stdout.splitlines() if line.startswith("{")]
    runs = [r for r in lines if "run" in r]
    assert sorted((r["method"], r["run"]) for r in runs) == [
        ("current", 1), ("current", 2), ("original", 1), ("original", 2)]
    summary = {r["method"]: r for r in lines if r.get("summary")}
    assert set(summary) == {"original", "current"}
    assert summary["original"]["mean_ms"][0] <= summary["original"]["mean_ms"][1]


def test_src_points_at_another_source_tree(sample_graph_json, tmp_path):
    # --src measures a different checkout, e.g. `git archive v0.7.0 slurp`.
    repo = _SCRIPT.parent.parent
    res = subprocess.run(
        [sys.executable, str(_SCRIPT), str(sample_graph_json), "--method", "original",
         "--runs", "1", "--src", str(repo)],
        capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    missing = subprocess.run(
        [sys.executable, str(_SCRIPT), str(sample_graph_json), "--src", str(tmp_path)],
        capture_output=True, text=True, check=False)
    assert missing.returncode != 0
    assert "no slurp package" in missing.stderr
