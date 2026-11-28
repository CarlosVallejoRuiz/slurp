"""Summary of the JSX bank run against the author's criterion. Not part of slurp.

    python3 analyze.py

Reads ~/slurp-bench/v15/bank/*.jsonl (bank.py) and, for the ten projects without
.tsx/.jsx (unchanged by construction), v1.4's per-question results
(~/slurp-bench/v14/impl-der, impl-val). Checks W0 against those v1.4 results question by
question, then applies the criterion (docs/design/jsx-v1.5.md §5).
"""

import json
import statistics as st
from pathlib import Path

BENCH = Path.home() / "slurp-bench"
BANK = BENCH / "v15" / "bank"
DERIVATION = ["ky", "prismastats", "hono", "jsoup", "caddy", "tokio", "sqlalchemy"]
VALIDATION = ["gin", "click", "ripgrep", "excalidraw", "commons-lang", "prometheus"]
AFFECTED = {"prismastats", "hono", "excalidraw", "prometheus"}


def _rows(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _v14(project: str) -> dict[str, float]:
    for group in ("impl-der", "impl-val"):
        p = BENCH / "v14" / group / f"{project}.jsonl"
        if p.exists():
            return {r["id"]: r["r"]["V0"][0] for r in _rows(p)}
    raise FileNotFoundError(project)


def main() -> None:
    means: dict[str, dict[str, float]] = {}
    report: dict = {"projects": {}, "checks": {}}
    for p in DERIVATION + VALIDATION + ["slurp"]:
        v14 = _v14(p)
        if p in AFFECTED:
            rows = _rows(BANK / f"{p}.jsonl")
            mismatch = sum(1 for r in rows if r["r"]["W0"][0] != v14.get(r["id"]))
            rebuild = [r["rebuild_identical"] for r in rows if "rebuild_identical" in r]
            m = {v: st.fmean(r["r"][v][0] for r in rows) for v in ("W0", "W1", "W2")}
            firsts = {v: st.median([r["r"][v][2] for r in rows if r["r"][v][2] is not None])
                      for v in ("W0", "W1", "W2")}
            worse = {v: sum(1 for r in rows if r["r"][v][0] < r["r"]["W0"][0]) for v in ("W1", "W2")}
            better = {v: sum(1 for r in rows if r["r"][v][0] > r["r"]["W0"][0]) for v in ("W1", "W2")}
            report["projects"][p] = {
                "questions": len(rows), "recall": {k: round(v, 3) for k, v in m.items()},
                "delta": {v: round(m[v] - m["W0"], 3) for v in ("W1", "W2")},
                "questions_worse": worse, "questions_better": better,
                "median_first_rank": firsts,
                "jsx_edges_per_question": {"W1": st.median(r["jsx_w1"] for r in rows),
                                           "W2": st.median(r["jsx_w2"] for r in rows)},
                "new_nodes_per_question_w2": st.median(r["nodes_w2"] for r in rows),
                "w0_vs_v14_mismatches": mismatch, "rebuild_identical": rebuild}
        else:
            m = {v: st.fmean(v14.values()) for v in ("W0", "W1", "W2")}
            report["projects"][p] = {"questions": len(v14), "recall": {k: round(v, 3) for k, v in m.items()},
                                     "delta": {"W1": 0.0, "W2": 0.0}, "unchanged": True}
        means[p] = m
    for group, members in (("derivation", DERIVATION), ("validation", VALIDATION)):
        g = {v: st.fmean(means[p][v] for p in members) for v in ("W0", "W1", "W2")}
        report[group] = {"recall": {k: round(v, 4) for k, v in g.items()},
                         "delta": {v: round(g[v] - g["W0"], 4) for v in ("W1", "W2")}}
    for v in ("W1", "W2"):
        val = report["validation"]["delta"][v]
        worst_val = min(means[p][v] - means[p]["W0"] for p in VALIDATION)
        worst_all = min(means[p][v] - means[p]["W0"] for p in DERIVATION + VALIDATION)
        report["checks"][v] = {
            "validation_mean_delta": val, "validation_mean_ok": val >= -0.01,
            "worst_validation_project": round(worst_val, 3), "worst_all_projects": round(worst_all, 3),
            "per_project_ok_validation_only": worst_val >= -0.03,
            "per_project_ok_all_14": worst_all >= -0.03,
            "PASS": val >= -0.01 and worst_all >= -0.03}
    ref = BANK / "ref-prismastats.jsonl"
    if ref.exists():
        rows = _rows(ref)
        report["labelled_prismastats"] = {
            v: {"recall": round(st.fmean(r["r"][v][0] for r in rows), 3),
                "questions_worse_than_W0": sum(1 for r in rows if r["r"][v][0] < r["r"]["W0"][0])}
            for v in ("W0", "W1", "W2")}
    (BENCH / "v15" / "bank-summary.json").write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps({k: report[k] for k in ("derivation", "validation", "checks")}, indent=1))
    for p in sorted(AFFECTED):
        print(p, report["projects"][p])
    if "labelled_prismastats" in report:
        print("labelled PrismaStats", report["labelled_prismastats"])


if __name__ == "__main__":
    main()
