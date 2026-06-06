"""Tests for slurp/audit.py."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from slurp.audit import log_query, read_audit, top_nodes_from_audit

# ---------------------------------------------------------------------------
# Constants shared across tests
# ---------------------------------------------------------------------------

_STATS = {
    "nodes_selected": 3,
    "nodes_total": 7,
    "tokens_used": 58,
    "tokens_budget": 100,
    "coverage_pct": 42.9,
}

_TOP_NODES = ["authenticate_user", "JWTMiddleware", "hash_password"]
_GRAPH_PATH = Path("graph.json")
_AUDIT_FILE = "audit.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(
    audit_dir: Path,
    query: str = "auth flow",
    top_nodes: list[str] | None = None,
    stats: dict | None = None,
    graph_path: Path = _GRAPH_PATH,
) -> None:
    """Thin wrapper so tests don't repeat all keyword arguments."""
    log_query(
        query=query,
        graph_path=graph_path,
        stats=stats or _STATS,
        top_nodes=top_nodes if top_nodes is not None else _TOP_NODES,
        audit_dir=audit_dir,
    )


# ---------------------------------------------------------------------------
# log_query — directory and file creation
# ---------------------------------------------------------------------------


class TestLogQueryFileSystem:
    def test_creates_audit_directory(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        assert not audit_dir.exists()
        _log(audit_dir)
        assert audit_dir.is_dir()

    def test_creates_nested_audit_directory(self, tmp_path):
        audit_dir = tmp_path / "deep" / "nested" / ".slurp"
        _log(audit_dir)
        assert audit_dir.is_dir()

    def test_creates_audit_jsonl_file(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        _log(audit_dir)
        assert (audit_dir / _AUDIT_FILE).is_file()

    def test_does_not_fail_when_directory_already_exists(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        audit_dir.mkdir()
        _log(audit_dir)  # must not raise
        assert (audit_dir / _AUDIT_FILE).is_file()


# ---------------------------------------------------------------------------
# log_query — entry contents
# ---------------------------------------------------------------------------


class TestLogQueryEntry:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.audit_dir = tmp_path / ".slurp"
        _log(self.audit_dir)
        self.entry = read_audit(self.audit_dir)[0]

    def test_entry_is_valid_json_object(self):
        raw = (self.audit_dir / _AUDIT_FILE).read_text().strip()
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)

    def test_entry_has_all_required_fields(self):
        required = {
            "timestamp", "query", "budget", "tokens_used",
            "nodes_selected", "nodes_total", "graph_path", "top_nodes",
        }
        assert required <= set(self.entry.keys())

    def test_timestamp_is_string(self):
        assert isinstance(self.entry["timestamp"], str)

    def test_timestamp_is_parseable_iso_format(self):
        # Must not raise
        dt = datetime.fromisoformat(self.entry["timestamp"])
        assert isinstance(dt, datetime)

    def test_timestamp_has_second_precision(self):
        # "YYYY-MM-DDTHH:MM:SS" — no sub-second part
        ts = self.entry["timestamp"]
        assert "T" in ts
        assert len(ts) == 19  # 2025-06-06T12:34:56

    def test_query_field_matches_input(self):
        assert self.entry["query"] == "auth flow"

    def test_budget_field_maps_from_tokens_budget(self):
        assert self.entry["budget"] == _STATS["tokens_budget"]

    def test_tokens_used_field_matches_stats(self):
        assert self.entry["tokens_used"] == _STATS["tokens_used"]

    def test_nodes_selected_field_matches_stats(self):
        assert self.entry["nodes_selected"] == _STATS["nodes_selected"]

    def test_nodes_total_field_matches_stats(self):
        assert self.entry["nodes_total"] == _STATS["nodes_total"]

    def test_graph_path_is_string(self):
        assert isinstance(self.entry["graph_path"], str)

    def test_graph_path_value_matches_input(self):
        assert self.entry["graph_path"] == str(_GRAPH_PATH)

    def test_top_nodes_is_list(self):
        assert isinstance(self.entry["top_nodes"], list)

    def test_top_nodes_matches_input(self):
        assert self.entry["top_nodes"] == _TOP_NODES

    def test_top_nodes_order_preserved(self):
        ordered = ["z_node", "a_node", "m_node"]
        audit_dir = self.audit_dir.parent / ".slurp_order"
        _log(audit_dir, top_nodes=ordered)
        entry = read_audit(audit_dir)[0]
        assert entry["top_nodes"] == ordered

    def test_empty_top_nodes_stored_as_empty_list(self):
        audit_dir = self.audit_dir.parent / ".slurp_empty"
        _log(audit_dir, top_nodes=[])
        entry = read_audit(audit_dir)[0]
        assert entry["top_nodes"] == []


# ---------------------------------------------------------------------------
# log_query — append behaviour
# ---------------------------------------------------------------------------


class TestLogQueryAppend:
    def test_second_call_appends_not_overwrites(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        _log(audit_dir, query="first")
        _log(audit_dir, query="second")
        entries = read_audit(audit_dir)
        assert len(entries) == 2

    def test_each_call_adds_exactly_one_line(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        for i in range(5):
            _log(audit_dir, query=f"query {i}")
        lines = [
            l for l in (audit_dir / _AUDIT_FILE).read_text().splitlines() if l.strip()
        ]
        assert len(lines) == 5

    def test_each_line_is_valid_json(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        for i in range(3):
            _log(audit_dir, query=f"q{i}")
        for line in (audit_dir / _AUDIT_FILE).read_text().splitlines():
            if line.strip():
                parsed = json.loads(line)
                assert isinstance(parsed, dict)

    def test_appended_queries_maintain_order(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        queries = ["alpha", "beta", "gamma"]
        for q in queries:
            _log(audit_dir, query=q)
        entries = read_audit(audit_dir)
        assert [e["query"] for e in entries] == queries


# ---------------------------------------------------------------------------
# read_audit
# ---------------------------------------------------------------------------


class TestReadAudit:
    def test_returns_empty_list_when_file_missing(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        audit_dir.mkdir()
        assert read_audit(audit_dir) == []

    def test_returns_empty_list_when_directory_missing(self, tmp_path):
        audit_dir = tmp_path / "nonexistent" / ".slurp"
        assert read_audit(audit_dir) == []

    def test_returns_list_of_dicts(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        _log(audit_dir)
        result = read_audit(audit_dir)
        assert isinstance(result, list)
        assert all(isinstance(e, dict) for e in result)

    def test_returns_all_logged_entries(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        for i in range(4):
            _log(audit_dir, query=f"q{i}")
        assert len(read_audit(audit_dir)) == 4

    def test_entries_in_logged_order(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        queries = ["first", "second", "third"]
        for q in queries:
            _log(audit_dir, query=q)
        entries = read_audit(audit_dir)
        assert [e["query"] for e in entries] == queries

    def test_entry_values_round_trip(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        _log(audit_dir, query="round trip", top_nodes=["a", "b"])
        entry = read_audit(audit_dir)[0]
        assert entry["query"] == "round trip"
        assert entry["top_nodes"] == ["a", "b"]
        assert entry["budget"] == _STATS["tokens_budget"]

    def test_ignores_blank_lines(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        audit_dir.mkdir()
        # Write a file with a trailing blank line between entries
        audit_file = audit_dir / "audit.jsonl"
        entry = {"timestamp": "2025-01-01T00:00:00", "query": "q", "budget": 10,
                 "tokens_used": 5, "nodes_selected": 1, "nodes_total": 3,
                 "graph_path": "g.json", "top_nodes": []}
        audit_file.write_text(json.dumps(entry) + "\n\n" + json.dumps(entry) + "\n")
        assert len(read_audit(audit_dir)) == 2


# ---------------------------------------------------------------------------
# top_nodes_from_audit
# ---------------------------------------------------------------------------


class TestTopNodesFromAudit:
    def test_returns_list(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        result = top_nodes_from_audit(audit_dir)
        assert isinstance(result, list)

    def test_returns_empty_list_when_no_entries(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        assert top_nodes_from_audit(audit_dir) == []

    def test_returns_tuples_of_name_and_count(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        _log(audit_dir, top_nodes=["a", "b"])
        result = top_nodes_from_audit(audit_dir)
        for item in result:
            assert isinstance(item, tuple) and len(item) == 2
            assert isinstance(item[0], str)
            assert isinstance(item[1], int)

    def test_counts_single_entry(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        _log(audit_dir, top_nodes=["x", "y", "z"])
        counts = dict(top_nodes_from_audit(audit_dir))
        assert counts["x"] == 1
        assert counts["y"] == 1
        assert counts["z"] == 1

    def test_counts_across_multiple_entries(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        _log(audit_dir, top_nodes=["a", "b", "c"])
        _log(audit_dir, top_nodes=["a", "b", "d"])
        _log(audit_dir, top_nodes=["a", "e"])
        counts = dict(top_nodes_from_audit(audit_dir))
        assert counts["a"] == 3
        assert counts["b"] == 2
        assert counts["c"] == 1
        assert counts["d"] == 1
        assert counts["e"] == 1

    def test_sorted_by_frequency_descending(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        _log(audit_dir, top_nodes=["rare"])
        _log(audit_dir, top_nodes=["common", "rare"])
        _log(audit_dir, top_nodes=["common", "rare"])
        # common=2, rare=3 → rare first
        result = top_nodes_from_audit(audit_dir)
        counts = [count for _, count in result]
        assert counts == sorted(counts, reverse=True)

    def test_n_limits_number_of_results(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        _log(audit_dir, top_nodes=["a", "b", "c", "d", "e"])
        result = top_nodes_from_audit(audit_dir, n=3)
        assert len(result) == 3

    def test_n_larger_than_unique_nodes_returns_all(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        _log(audit_dir, top_nodes=["a", "b"])
        result = top_nodes_from_audit(audit_dir, n=100)
        assert len(result) == 2

    def test_n_zero_returns_empty(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        _log(audit_dir, top_nodes=["a", "b"])
        assert top_nodes_from_audit(audit_dir, n=0) == []

    def test_n_one_returns_most_common(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        _log(audit_dir, top_nodes=["winner", "loser"])
        _log(audit_dir, top_nodes=["winner"])
        result = top_nodes_from_audit(audit_dir, n=1)
        assert len(result) == 1
        assert result[0][0] == "winner"
        assert result[0][1] == 2

    def test_entries_with_empty_top_nodes_ignored(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        _log(audit_dir, top_nodes=[])
        _log(audit_dir, top_nodes=[])
        assert top_nodes_from_audit(audit_dir) == []

    def test_default_n_is_ten(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        nodes = [f"node_{i}" for i in range(15)]
        _log(audit_dir, top_nodes=nodes)
        result = top_nodes_from_audit(audit_dir)
        assert len(result) == 10

    def test_top_node_is_most_frequent(self, tmp_path):
        audit_dir = tmp_path / ".slurp"
        for _ in range(5):
            _log(audit_dir, top_nodes=["champion"])
        _log(audit_dir, top_nodes=["challenger"])
        result = top_nodes_from_audit(audit_dir)
        assert result[0] == ("champion", 5)


# ---------------------------------------------------------------------------
# Integration: full pipeline
# ---------------------------------------------------------------------------


class TestAuditIntegration:
    def test_pipeline_log_then_read(self, tmp_path, sample_graph):
        from slurp.budget import select_subgraph
        from slurp.scorer import score_nodes

        audit_dir = tmp_path / ".slurp"
        scores = score_nodes(sample_graph, "authentication jwt")
        subG, stats = select_subgraph(sample_graph, scores, budget=10_000)
        top = sorted(subG.nodes, key=lambda n: scores.get(n, 0.0), reverse=True)[:3]

        log_query(
            query="authentication jwt",
            graph_path=Path("graph.json"),
            stats=stats,
            top_nodes=top,
            audit_dir=audit_dir,
        )

        entries = read_audit(audit_dir)
        assert len(entries) == 1
        assert entries[0]["query"] == "authentication jwt"
        assert entries[0]["nodes_total"] == sample_graph.number_of_nodes()

    def test_pipeline_multiple_queries_top_nodes(self, tmp_path, sample_graph):
        from slurp.budget import select_subgraph
        from slurp.scorer import score_nodes

        audit_dir = tmp_path / ".slurp"

        for query in ["auth", "auth", "payment"]:
            scores = score_nodes(sample_graph, query)
            subG, stats = select_subgraph(sample_graph, scores, budget=10_000)
            top = sorted(subG.nodes, key=lambda n: scores.get(n, 0.0), reverse=True)[:3]
            log_query(
                query=query,
                graph_path=Path("graph.json"),
                stats=stats,
                top_nodes=top,
                audit_dir=audit_dir,
            )

        assert len(read_audit(audit_dir)) == 3
        result = top_nodes_from_audit(audit_dir)
        # All top nodes appear; result is sorted by frequency
        counts = [count for _, count in result]
        assert counts == sorted(counts, reverse=True)
