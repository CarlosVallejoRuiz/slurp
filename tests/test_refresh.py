"""Tests for slurp.refresh — whose graph it is, and keeping slurp's own current (v1.3, #76)."""

import json
import os
import shutil
import time
from pathlib import Path

import networkx as nx
import pytest

from slurp.graphfile import write_graph
from slurp.refresh import GraphTracker, index_with_manifest

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


@pytest.fixture
def graphify_copy(tmp_path: Path) -> Path:
    """A throwaway copy of the real graphify project; returns its graph.json."""
    root = tmp_path / "graphify_project"
    shutil.copytree(FIXTURE, root)
    return root / "graphify-out" / "graph.json"


@pytest.fixture
def own_graph(tmp_path: Path) -> Path:
    """A project indexed by this slurp into graphify-out/graph.json."""
    root = tmp_path / "proj"
    _put(root, "app.py", "from lib.util import helper\n\n\ndef main():\n    return helper()\n")
    _put(root, "lib/util.py", "def helper():\n    return 1\n")
    out = root / "graphify-out" / "graph.json"
    write_graph(out, index_with_manifest(root, out, root / ".slurpignore"))
    _age(root)
    return out


def _legacy(own: Path) -> Path:
    """Turn a v1.3 graph into one slurp ≤ 1.2 would have written."""
    data = json.loads(own.read_text(encoding="utf-8"))
    del data["slurp"]
    own.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return own


class TestWhoseGraph:
    def test_graphifys_graph_is_read_only(self, graphify_copy):
        tracker = GraphTracker(graphify_copy)
        assert tracker.kind == "graphify"
        assert not tracker.refreshable
        assert isinstance(tracker.graph, nx.DiGraph) and tracker.graph.number_of_nodes() == 8

    def test_slurps_own_graph_is_refreshable(self, own_graph):
        tracker = GraphTracker(own_graph)
        assert tracker.kind == "own" and tracker.refreshable
        assert tracker.root == own_graph.parent.parent.resolve()

    def test_a_slurp_1_2_graph_whose_root_validates_is_adopted(self, own_graph):
        tracker = GraphTracker(_legacy(own_graph))
        assert tracker.kind == "legacy" and tracker.refreshable
        assert "slurp ≤ 1.2" in tracker.check().reason

    def test_a_slurp_1_2_graph_outside_graphify_out_validates_against_its_directory(
            self, tmp_path):
        root = tmp_path / "p"
        _put(root, "a.py", "def a():\n    return 1\n")
        out = root / "graph.json"
        write_graph(out, index_with_manifest(root, out, root / ".slurpignore"))
        assert GraphTracker(_legacy(out)).refreshable

    def test_a_slurp_1_2_graph_whose_files_are_gone_stays_read_only(self, own_graph):
        _legacy(own_graph)
        (own_graph.parent.parent / "app.py").unlink()
        (own_graph.parent.parent / "lib" / "util.py").unlink()
        tracker = GraphTracker(own_graph)
        assert not tracker.refreshable
        assert "run `slurp index` once to enable auto-refresh" in tracker.note

    def test_adoption_needs_nine_files_in_ten(self, tmp_path):
        root = tmp_path / "p"
        for i in range(10):
            _put(root, f"m{i}.py", f"def f{i}():\n    return {i}\n")
        out = root / "graph.json"
        write_graph(out, index_with_manifest(root, out, root / ".slurpignore"))
        _legacy(out)
        (root / "m0.py").unlink()
        assert GraphTracker(out).refreshable            # 9 of 10
        (root / "m1.py").unlink()
        assert not GraphTracker(out).refreshable        # 8 of 10

    def test_a_generic_graph_is_read_only(self, tmp_path):
        out = tmp_path / "graph.json"
        out.write_text(json.dumps({"nodes": [{"id": "a", "label": "a"}], "links": []}),
                       encoding="utf-8")
        tracker = GraphTracker(out)
        assert tracker.kind == "foreign" and not tracker.refreshable

    def test_a_graphml_graph_is_read_only(self, tmp_path):
        out = tmp_path / "g.graphml"
        G = nx.DiGraph()
        G.add_node("a", label="a")
        nx.write_graphml(G, out)
        tracker = GraphTracker(out)
        assert not tracker.refreshable and tracker.graph.number_of_nodes() == 1

    def test_a_graph_whose_root_is_not_here_is_read_only(self, own_graph, tmp_path):
        moved = tmp_path / "elsewhere" / "graph.json"
        moved.parent.mkdir()
        data = json.loads(own_graph.read_text(encoding="utf-8"))
        data["slurp"]["root"] = "../no-such-project"
        moved.write_text(json.dumps(data), encoding="utf-8")
        tracker = GraphTracker(moved)
        assert not tracker.refreshable and "does not exist" in tracker.note

    def test_a_manifest_from_a_newer_slurp_is_read_only(self, own_graph):
        data = json.loads(own_graph.read_text(encoding="utf-8"))
        data["slurp"]["format"] = 2
        own_graph.write_text(json.dumps(data), encoding="utf-8")
        tracker = GraphTracker(own_graph)
        assert not tracker.refreshable and "format 2" in tracker.note

    def test_whose_graph_is_decided_again_when_the_file_changes(self, own_graph, graphify_copy):
        tracker = GraphTracker(own_graph)
        shutil.copyfile(graphify_copy, own_graph)    # graphify overwrote it
        assert tracker.disk_changed()
        tracker.load()
        assert tracker.kind == "graphify" and not tracker.refreshable


class TestReadOnlyHint:
    def test_counts_the_covered_files_changed_after_the_build(self, graphify_copy):
        root = graphify_copy.parent.parent
        _age(root)
        tracker = GraphTracker(graphify_copy)
        assert tracker.readonly_changed() == 0
        _put(root, "lib/util.py", "def helper():\n    return 2\n")
        assert tracker.readonly_changed() == 1

    def test_graphify_wording(self, graphify_copy):
        root = graphify_copy.parent.parent
        _age(root)
        tracker = GraphTracker(graphify_copy)
        _put(root, "app.py", "x = 1\n")
        assert tracker.readonly_warning() == (
            "1 file this graph covers changed after it was built; slurp doesn't rebuild "
            "graphify graphs — rerun graphify.")

    def test_slurp_1_2_wording(self, own_graph):
        _legacy(own_graph)
        root = own_graph.parent.parent
        (root / "lib" / "util.py").unlink()
        (root / "app.py").unlink()
        _put(root, "app.py", "def main():\n    return 2\n")
        tracker = GraphTracker(own_graph)
        assert "run `slurp index` once to enable auto-refresh" in tracker.readonly_warning()

    def test_nothing_to_say_when_nothing_changed(self, graphify_copy):
        _age(graphify_copy.parent.parent)
        assert GraphTracker(graphify_copy).readonly_warning() == ""


class TestRebuildRefusesWhatIsNotOurs:
    def test_a_read_only_tracker_never_rebuilds(self, graphify_copy):
        from slurp.manifest import Changes

        before = graphify_copy.read_bytes()
        with pytest.raises(RuntimeError):
            GraphTracker(graphify_copy).rebuild(Changes(modified=["app.py"]))
        assert graphify_copy.read_bytes() == before
        assert not (graphify_copy.parent / ".graph.json.lock").exists()


def _public(graph: dict) -> tuple:
    nodes = {n["id"]: n for n in graph["nodes"]}
    edges = sorted(json.dumps(e, sort_keys=True) for e in graph["links"])
    return nodes, edges


def _full(root: Path, tmp_path: Path) -> dict:
    out = tmp_path / "full" / "graph.json"
    return index_with_manifest(root, out, root / ".slurpignore")


class TestRebuild:
    def test_a_body_edit_takes_the_fast_path_and_writes(self, own_graph, tmp_path):
        root = own_graph.parent.parent
        tracker = GraphTracker(own_graph)
        _put(root, "lib/util.py", "def helper():\n    return 42\n")
        changes = tracker.check()
        rebuilt = tracker.rebuild(changes)
        assert rebuilt.way == "smart" and rebuilt.written
        tracker.apply(rebuilt)
        on_disk = json.loads(own_graph.read_text(encoding="utf-8"))
        assert _public(on_disk) == _public(_full(root, tmp_path))
        assert not tracker.check().any
        assert not tracker.disk_changed()

    def test_a_new_file_takes_a_full_index(self, own_graph, tmp_path):
        root = own_graph.parent.parent
        tracker = GraphTracker(own_graph)
        _put(root, "lib/extra.py", "def extra():\n    return 3\n")
        rebuilt = tracker.rebuild(tracker.check())
        assert rebuilt.way == "full" and "added" in rebuilt.reason
        tracker.apply(rebuilt)
        assert _public(tracker.data) == _public(_full(root, tmp_path))

    def test_adoption_writes_the_manifest(self, own_graph):
        tracker = GraphTracker(_legacy(own_graph))
        rebuilt = tracker.rebuild(tracker.check())
        tracker.apply(rebuilt)
        assert tracker.kind == "own"
        assert "slurp" in json.loads(own_graph.read_text(encoding="utf-8"))

    def test_another_writer_first_means_reload_not_overwrite(self, own_graph):
        tracker = GraphTracker(own_graph)
        _put(own_graph.parent.parent, "lib/util.py", "def helper():\n    return 7\n")
        changes = tracker.check()
        other = json.loads(own_graph.read_text(encoding="utf-8"))
        other["nodes"].append({"id": "x", "label": "x"})
        own_graph.write_text(json.dumps(other), encoding="utf-8")   # another slurp, say
        written = own_graph.read_bytes()
        assert tracker.rebuild(changes).reloaded
        assert own_graph.read_bytes() == written

    def test_compare_and_swap_catches_a_write_during_the_rebuild(self, own_graph, monkeypatch):
        from slurp import smart

        tracker = GraphTracker(own_graph)
        _put(own_graph.parent.parent, "lib/util.py", "def helper():\n    return 8\n")
        changes = tracker.check()
        graphify = '{"directed": true, "multigraph": false, "graph": {}, "nodes": [], "links": []}'
        real = smart.rebuild_modified

        def overtaken(*args, **kwargs):
            result = real(*args, **kwargs)
            own_graph.write_text(graphify, encoding="utf-8")
            return result

        monkeypatch.setattr(smart, "rebuild_modified", overtaken)
        assert tracker.rebuild(changes).reloaded
        assert own_graph.read_text(encoding="utf-8") == graphify

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_an_unwritable_directory_refreshes_in_memory(self, own_graph):
        tracker = GraphTracker(own_graph)
        _put(own_graph.parent.parent, "lib/util.py", "def helper():\n    return 9\n")
        before = own_graph.read_bytes()
        own_graph.parent.chmod(0o500)
        try:
            rebuilt = tracker.rebuild(tracker.check())
        finally:
            own_graph.parent.chmod(0o700)
        assert not rebuilt.written and rebuilt.write_error
        tracker.apply(rebuilt)
        assert own_graph.read_bytes() == before
        assert not tracker.check().any          # the manifest in memory is the new one

    def test_waits_for_the_lock_another_slurp_holds(self, own_graph):
        import threading

        from slurp.graphfile import GraphLock

        tracker = GraphTracker(own_graph)
        _put(own_graph.parent.parent, "lib/util.py", "def helper():\n    return 10\n")
        changes = tracker.check()
        holder = GraphLock(own_graph)
        holder.acquire(timeout=0)
        threading.Timer(0.3, holder.release).start()
        start = time.monotonic()
        assert tracker.rebuild(changes).written
        assert time.monotonic() - start >= 0.25


# ---------------------------------------------------------------------------
# Refresher: freshness on demand, one refresh thread, a time limit
# ---------------------------------------------------------------------------

from slurp.refresh import META_KEY, Refresher, resolve_refresh  # noqa: E402


def _slow(monkeypatch, seconds: float, calls: list | None = None):
    """Make every rebuild take *seconds* longer."""
    real = GraphTracker.rebuild

    def slow(self, changes, should_stop=lambda: False):
        if calls is not None:
            calls.append(changes.files)
        time.sleep(seconds)
        return real(self, changes, should_stop)

    monkeypatch.setattr(GraphTracker, "rebuild", slow)


class TestRefresher:
    def test_nothing_changed_is_fresh(self, own_graph):
        fresh = Refresher([GraphTracker(own_graph)]).prepare()
        assert fresh.status == "fresh" and not fresh.warnings
        assert fresh.meta() == {META_KEY: {"status": "fresh"}}

    def test_an_edit_is_served_refreshed_within_the_limit(self, own_graph):
        tracker = GraphTracker(own_graph)
        refresher = Refresher([tracker], timeout=10)
        _put(own_graph.parent.parent, "lib/util.py",
             "def helper():\n    return 1\n\n\ndef brand_new():\n    return 2\n")
        fresh = refresher.prepare()
        assert fresh.status == "refreshed" and not fresh.warnings
        assert fresh.changed_files == ["lib/util.py"]
        assert fresh.refresh_ms is not None and fresh.refresh_ms > 0
        assert "lib.util.brand_new" in tracker.graph
        assert refresher.prepare().status == "fresh"

    def test_past_the_limit_the_old_graph_is_served_with_a_warning(self, own_graph, monkeypatch):
        _slow(monkeypatch, 1.0)
        tracker = GraphTracker(own_graph)
        refresher = Refresher([tracker], timeout=0.1)
        _put(own_graph.parent.parent, "lib/util.py",
             "def helper():\n    return 1\n\n\ndef late():\n    return 2\n")
        fresh = refresher.prepare()
        assert fresh.status == "stale"
        assert fresh.warnings == [
            "⚠ Graph refresh still running — 1 file changed since this graph was built "
            "(lib/util.py). This answer may miss them; call again in a few seconds."]
        assert "lib.util.late" not in tracker.graph
        refresher.finish()
        assert refresher.prepare().status == "refreshed"
        assert "lib.util.late" in tracker.graph

    def test_a_later_call_does_not_wait_again(self, own_graph, monkeypatch):
        _slow(monkeypatch, 1.5)
        refresher = Refresher([GraphTracker(own_graph)], timeout=0.2)
        _put(own_graph.parent.parent, "lib/util.py", "def helper():\n    return 5\n")
        refresher.prepare()
        start = time.monotonic()
        fresh = refresher.prepare()
        assert time.monotonic() - start < 0.15
        assert fresh.status == "stale"
        assert fresh.warnings[0].startswith("⚠ Graph refresh in progress (started 0s ago) — ")
        refresher.finish()

    def test_never_more_than_one_refresh_at_a_time(self, own_graph, monkeypatch):
        calls: list = []
        _slow(monkeypatch, 0.6, calls)
        refresher = Refresher([GraphTracker(own_graph)], timeout=0.05)
        root = own_graph.parent.parent
        _put(root, "lib/util.py", "def helper():\n    return 6\n")
        refresher.prepare()
        _put(root, "app.py", "from lib.util import helper\n\n\ndef main():\n    return 7\n")
        refresher.prepare()
        refresher.prepare()
        assert len(calls) == 1
        refresher.finish()

    def test_a_failed_refresh_is_reported_and_not_retried_in_a_loop(self, own_graph, monkeypatch):
        calls: list = []

        def boom(self, changes, should_stop=lambda: False):
            calls.append(1)
            raise ValueError("boom")

        monkeypatch.setattr(GraphTracker, "rebuild", boom)
        refresher = Refresher([GraphTracker(own_graph)], timeout=5)
        root = own_graph.parent.parent
        _put(root, "lib/util.py", "def helper():\n    return 8\n")
        first = refresher.prepare()
        assert first.status == "failed"
        assert first.warnings == ["⚠ Graph refresh failed: ValueError: boom"]
        second = refresher.prepare()
        assert second.status == "failed" and len(calls) == 1
        time.sleep(0.01)
        _put(root, "lib/util.py", "def helper():\n    return 9\n")   # the disk moved again
        refresher.prepare()
        assert len(calls) == 2

    def test_disabled_checks_and_warns_but_never_writes(self, own_graph):
        before = own_graph.read_bytes()
        refresher = Refresher([GraphTracker(own_graph)], enabled=False,
                              disabled_reason="auto-refresh is off")
        _put(own_graph.parent.parent, "lib/util.py", "def helper():\n    return 10\n")
        fresh = refresher.prepare()
        assert fresh.status == "stale"
        assert fresh.warnings == ["⚠ 1 file changed since this graph was built (lib/util.py) — "
                                  "not refreshed: auto-refresh is off."]
        assert own_graph.read_bytes() == before

    def test_a_zero_limit_never_waits(self, own_graph, monkeypatch):
        _slow(monkeypatch, 0.3)
        refresher = Refresher([GraphTracker(own_graph)], timeout=0)
        _put(own_graph.parent.parent, "lib/util.py", "def helper():\n    return 11\n")
        start = time.monotonic()
        assert refresher.prepare().status == "stale"
        assert time.monotonic() - start < 0.2
        refresher.finish()

    def test_the_startup_refresh_runs_in_the_background(self, own_graph, monkeypatch):
        _slow(monkeypatch, 0.4)
        _put(own_graph.parent.parent, "lib/util.py", "def helper():\n    return 12\n")
        refresher = Refresher([GraphTracker(own_graph)], timeout=5)
        start = time.monotonic()
        refresher.start()
        assert time.monotonic() - start < 0.1      # initialize is not held up
        assert refresher.prepare().status == "refreshed"

    def test_another_process_writing_the_graph_is_a_reload(self, own_graph, tmp_path):
        tracker = GraphTracker(own_graph)
        refresher = Refresher([tracker])
        root = own_graph.parent.parent
        _put(root, "lib/more.py", "def more():\n    return 1\n")
        write_graph(own_graph, index_with_manifest(root, own_graph, root / ".slurpignore"))
        fresh = refresher.prepare()
        assert fresh.status == "refreshed" and "lib.more.more" in tracker.graph

    def test_a_graphify_graph_is_readonly_and_untouched(self, graphify_copy):
        root = graphify_copy.parent.parent
        _age(root)
        before = graphify_copy.read_bytes()
        refresher = Refresher([GraphTracker(graphify_copy)], timeout=5)
        assert refresher.prepare().status == "readonly"
        for i in range(3):
            _put(root, "lib/util.py", f"def helper():\n    return {i}\n")
            _put(root, f"lib/new{i}.py", "def n():\n    pass\n")
            fresh = refresher.prepare()
            assert fresh.status == "readonly"
            assert fresh.warnings[0].endswith("rerun graphify.")
        refresher.finish()
        assert graphify_copy.read_bytes() == before
        assert sorted(p.name for p in graphify_copy.parent.iterdir()) == ["graph.json"]

    def test_federation_refreshes_only_its_own_members(self, own_graph, graphify_copy):
        before = graphify_copy.read_bytes()
        own, theirs = GraphTracker(own_graph), GraphTracker(graphify_copy)
        swaps: list[int] = []
        refresher = Refresher([own, theirs], timeout=10, on_swap=lambda: swaps.append(1))
        _put(own_graph.parent.parent, "lib/util.py", "def helper():\n    return 13\n")
        fresh = refresher.prepare()
        assert fresh.status == "refreshed" and swaps == [1]
        assert graphify_copy.read_bytes() == before


class TestResolveRefresh:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("SLURP_REFRESH", raising=False)
        monkeypatch.delenv("SLURP_REFRESH_TIMEOUT", raising=False)
        assert resolve_refresh() == (True, 2.0)

    def test_environment(self, monkeypatch):
        monkeypatch.setenv("SLURP_REFRESH", "0")
        monkeypatch.setenv("SLURP_REFRESH_TIMEOUT", "0.5")
        assert resolve_refresh() == (False, 0.5)

    def test_flags_win(self, monkeypatch):
        monkeypatch.setenv("SLURP_REFRESH_TIMEOUT", "0.5")
        assert resolve_refresh(no_refresh=True, timeout=3) == (False, 3.0)

    @pytest.mark.parametrize("bad", ["-1", "soon", "nan"])
    def test_a_bad_timeout_is_refused(self, monkeypatch, bad):
        monkeypatch.setenv("SLURP_REFRESH_TIMEOUT", bad)
        with pytest.raises(ValueError, match="SLURP_REFRESH_TIMEOUT"):
            resolve_refresh()
