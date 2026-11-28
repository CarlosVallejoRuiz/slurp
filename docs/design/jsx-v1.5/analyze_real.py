"""The bank with v1.5's real implementation (bank_real.py) against the author's criterion,
beside v1.4 and the prototype (bank.py, W2). Not part of slurp.

    python3 analyze_real.py

v1.4 is the bank's own result question by question (~/slurp-bench/v14/impl-der, impl-val):
bank_real.py --precheck showed v1.4.0's index gives back every stored parent graph tried.
The prototype's figures come from ~/slurp-bench/v15/bank-summary.json (analyze.py).
Criterion (docs/design/jsx-v1.5.md §5): validation mean recall@4000 (markdown) may drop at
most 0.01 against v1.4, and no project of the 14 more than 0.03.
"""

import json
import statistics as st
from pathlib import Path

BENCH = Path.home() / "slurp-bench"
REAL = BENCH / "v15" / "bank-real"
DERIVATION = ["ky", "prismastats", "hono", "jsoup", "caddy", "tokio", "sqlalchemy"]
VALIDATION = ["gin", "click", "ripgrep", "excalidraw", "commons-lang", "prometheus"]


def _rows(path: Path) -> list[dict]:
    """Complete lines only: a run may be writing the last one. No file: no rows."""
    if not path.exists():
        return []
    data = path.read_text()
    return [json.loads(x) for x in data[: data.rfind("\n") + 1].splitlines() if x.strip()]


def _v14(name: str) -> dict[str, list]:
    for group in ("impl-der", "impl-val"):
        p = BENCH / "v14" / group / f"{name}.jsonl"
        if p.exists():
            return {r["id"]: r["r"]["V0"] for r in _rows(p)}
    raise FileNotFoundError(name)


def main() -> None:
    proto = json.loads((BENCH / "v15" / "bank-summary.json").read_text())
    report: dict = {"projects": {}}
    means: dict[str, dict[str, float]] = {}
    for p in DERIVATION + VALIDATION + ["slurp"]:
        v14 = _v14(p)
        bank = {r["id"]: r for r in _rows(BENCH / "results" / f"{p}.jsonl") if r.get("present")}
        rows = _rows(REAL / f"{p}.jsonl")
        missing = sorted(set(v14) - {r["id"] for r in rows})
        if missing:
            report["projects"][p] = {"incomplete": f"{len(rows)}/{len(v14)}"}
            continue
        real = {r["id"]: r["r"][0] for r in rows}
        base = {i: v14[i][0] for i in real}
        m = {"v14": st.fmean(base.values()), "real": st.fmean(real.values()),
             "proto_w2": proto["projects"][p]["recall"]["W2"]}
        means[p] = m
        changed = [i for i in real if real[i] != base[i]]
        report["projects"][p] = {
            "questions": len(rows),
            "recall": {k: round(v, 4) for k, v in m.items()},
            "delta_real_v14": round(m["real"] - m["v14"], 4),
            "delta_proto_v14": round(m["proto_w2"] - m["v14"], 3),
            "questions_changed": len(changed),
            "questions_worse": sum(1 for i in changed if real[i] < base[i]),
            "questions_better": sum(1 for i in changed if real[i] > base[i]),
            # a relevant node of the v1.4 graph that v1.5's graph lacks (expect 0)
            "relevant_lost": sum(1 for r in rows if r["r"][3] != len(bank[r["id"]]["relevant"])),
            "renders_median": st.median(r["renders"] for r in rows),
            "first_graph_equals_bank": next(r["v15_equals_bank"] for r in rows
                                            if "v15_equals_bank" in r),
        }
    done = [p for p in DERIVATION + VALIDATION if p in means]
    if len(done) == len(DERIVATION + VALIDATION):
        for group, members in (("derivation", DERIVATION), ("validation", VALIDATION)):
            g = {k: st.fmean(means[p][k] for p in members) for k in ("v14", "real", "proto_w2")}
            report[group] = {"recall": {k: round(v, 4) for k, v in g.items()},
                             "delta_real_v14": round(g["real"] - g["v14"], 4),
                             "delta_proto_v14": round(g["proto_w2"] - g["v14"], 4)}
        val = report["validation"]["delta_real_v14"]
        worst = min(DERIVATION + VALIDATION, key=lambda p: means[p]["real"] - means[p]["v14"])
        drop = round(means[worst]["real"] - means[worst]["v14"], 4)
        report["criterion"] = {"validation_mean_delta": val, "validation_ok": val >= -0.01,
                               "worst_project": worst, "worst_delta": drop,
                               "per_project_ok": drop >= -0.03,
                               "PASS": val >= -0.01 and drop >= -0.03}
    ref = REAL / "ref.jsonl"
    if ref.exists():
        report["labelled"] = {}
        for name in ("prismastats", "slurp"):
            v14 = _v14(f"ref-{name}")
            rows = [r for r in _rows(ref) if r["set"] == name]
            report["labelled"][name] = {
                "v14": round(st.fmean(v14[r["id"]][0] for r in rows), 3),
                "real": round(st.fmean(r["r"][0] for r in rows), 3),
                "worse": [r["id"] for r in rows if r["r"][0] < v14[r["id"]][0]],
                "better": [r["id"] for r in rows if r["r"][0] > v14[r["id"]][0]]}
        report["labelled"]["prismastats"]["proto_w2"] = \
            proto["labelled_prismastats"]["W2"]["recall"]
    (BENCH / "v15" / "bank-real-summary.json").write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "projects"}, indent=1))
    for p, info in report["projects"].items():
        print(p, json.dumps(info))


if __name__ == "__main__":
    main()
