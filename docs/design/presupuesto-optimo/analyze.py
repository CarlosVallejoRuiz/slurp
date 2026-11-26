"""The budget study's analysis: optimal budgets, the rule, the relevance cut. Not part of slurp.

Design: docs/design/presupuesto-optimo.md §4. Every threshold below was fixed
there before measuring. Run after run_study.py:

    ~/Desktop/Slurp/slurp/.venv/bin/python -I analyze.py [OUT_DIR]

Reads ~/slurp-bench/results/ and writes OUT_DIR/resultados.md, resumen.json and
one SVG of recall against tokens per format (default: resultados/ beside this
file). Only per-project aggregates are written: no question of a private project.
"""

from __future__ import annotations

import json
import math
import random
import statistics
import sys
from pathlib import Path

GRID = [1000, 1500, 2000, 3000, 4000, 6000, 8000, 12000, 16000, 24000, 32000]
CONTROL = 64000
FORMATS = ("markdown", "json")
TARGET = 0.95            # B*: the smallest budget reaching 95 % of the maximum
CENSOR_GAP = 0.02        # R(64000) − R(32000) above this: not saturated at 32000
BOOT_N = 2000
MARGIN_CELL = 0.03       # recall loss against each project's own B*, per cell
MARGIN_MEAN = 0.01       # ... and on average
GEO_COST_MAX = 1.5       # geometric mean of B_rule / B*
CELL_COST_MAX = 4.0
SIMPLICITY = 0.25        # a rule must cut the constant's geometric cost by 25 %
CUT_SAVINGS = 0.25       # the cut must save 25 % of the reference's tokens
HALF_STEP = 0.25         # half a grid step, in log2 (the grid grows by ~√2)
DEFAULT_BUDGET = 4000
MIN_QUESTIONS = 40       # a derivation project needs this many to enter a fit (§2.4)

GROUPS = {
    "derivation": ["ky", "prismastats", "hono", "slurp", "jsoup", "caddy", "tokio", "sqlalchemy"],
    "validation": ["gin", "click", "ripgrep", "excalidraw", "commons-lang", "prometheus"],
}
LANG = {"ky": "TypeScript", "prismastats": "TypeScript", "hono": "TypeScript",
        "excalidraw": "TypeScript", "slurp": "Python", "sqlalchemy": "Python", "click": "Python",
        "jsoup": "Java", "commons-lang": "Java", "caddy": "Go", "gin": "Go", "prometheus": "Go",
        "tokio": "Rust", "ripgrep": "Rust"}
PRIVATE = {"prismastats"}


# --- §4.1 the optimal budget -------------------------------------------------------

def _row(rec: dict, fmt: str, kind: str, key: str | None) -> dict:
    return rec["base"][fmt] if kind == "base" else rec["cut"][key][fmt]


def curve(records: list[dict], fmt: str, kind: str = "base", key: str | None = None) -> dict:
    """Mean recall, real tokens and nodes per budget, over the scorable questions."""
    recs = [r for r in records if r.get("present")]
    budgets = GRID + ([CONTROL] if kind == "base" else [])
    out = {}
    for b in budgets:
        pts = [_row(r, fmt, kind, key)[str(b)] for r in recs]
        if pts:
            out[b] = {"recall": statistics.fmean(p[0] for p in pts),
                      "tokens": statistics.fmean(p[1] for p in pts),
                      "nodes": statistics.fmean(p[2] for p in pts), "n": len(pts)}
    return out


def optimal(c: dict) -> dict:
    r_max = max(c[b]["recall"] for b in GRID)
    b_star = next(b for b in GRID if c[b]["recall"] >= TARGET * r_max)
    r_control = c.get(CONTROL, {}).get("recall")
    censored = r_control is not None and r_control - c[GRID[-1]]["recall"] > CENSOR_GAP
    return {"b_star": b_star, "r_max": r_max, "r_at_b_star": c[b_star]["recall"],
            "censored": censored, "r_control": r_control}


def bootstrap(records: list[dict], fmt: str, n: int = BOOT_N, seed: int = 0) -> dict:
    recs = [r for r in records if r.get("present")]
    rng = random.Random(seed)
    stars = sorted(optimal(curve([rng.choice(recs) for _ in recs], fmt))["b_star"]
                   for _ in range(n))
    return {"median": stars[n // 2], "p10": stars[n // 10], "p90": stars[(9 * n) // 10]}


def round_up(x: float) -> int:
    return next((b for b in GRID if b >= x - 1e-9), GRID[-1])


def steps(a: int, b: int) -> int:
    return abs(GRID.index(a) - GRID.index(b))


# --- §4.2 the rule -------------------------------------------------------------------

def _solve(rows: list[list[float]], ys: list[float]) -> list[float]:
    """Least squares by the normal equations (a handful of unknowns, no numpy)."""
    k = len(rows[0])
    ata = [[sum(r[i] * r[j] for r in rows) for j in range(k)] for i in range(k)]
    aty = [sum(r[i] * y for r, y in zip(rows, ys)) for i in range(k)]
    for i in range(k):                         # a little ridge keeps it solvable
        ata[i][i] += 1e-9
    for col in range(k):
        piv = max(range(col, k), key=lambda r: abs(ata[r][col]))
        ata[col], ata[piv] = ata[piv], ata[col]
        aty[col], aty[piv] = aty[piv], aty[col]
        for r in range(k):
            if r != col and ata[col][col]:
                f = ata[r][col] / ata[col][col]
                ata[r] = [x - f * y for x, y in zip(ata[r], ata[col])]
                aty[r] -= f * aty[col]
    return [aty[i] / ata[i][i] if ata[i][i] else 0.0 for i in range(k)]


def _design(family: str, f: dict) -> list[float]:
    if family == "R0":
        return [1.0]
    x = family.split(":")[1] if family.startswith("R1") else family.split(":")[1]
    base = [1.0, math.log2(max(f[x], 1))]
    if family.startswith("R3"):
        base.append(f["tau"])
    return base


def _fit(family: str, data: list[tuple[dict, float]]):
    """A predictor of log2 B* fitted on (features, log2 B*) pairs."""
    rows = [_design(family, f) for f, _ in data]
    coef = _solve(rows, [y for _, y in data])
    offsets: dict[str, float] = {}
    if family.startswith("R2"):
        resid: dict[str, list[float]] = {}
        for (f, y), row in zip(data, rows):
            resid.setdefault(f["lang"], []).append(y - sum(c * v for c, v in zip(coef, row)))
        offsets = {lang: statistics.fmean(v) for lang, v in resid.items()}

    def predict(f: dict) -> float:
        return sum(c * v for c, v in zip(coef, _design(family, f))) + offsets.get(f["lang"], 0.0)
    predict.coef, predict.offsets = coef, offsets  # type: ignore[attr-defined]
    return predict


def _lopo(family: str, data: list[tuple[dict, float]]) -> float:
    errs = []
    for i in range(len(data)):
        pred = _fit(family, data[:i] + data[i + 1:])
        errs.append(abs(math.log2(round_up(2 ** pred(data[i][0]))) - data[i][1]))
    return statistics.fmean(errs)


COMPLEXITY = {"R0": 0, "R1": 1, "R2": 2, "R3": 2}


def fit_rule(cells: dict) -> dict:
    """Choose and fit the rule on the derivation cells (§4.2)."""
    md = [(c["features"], math.log2(c["b_star"]["markdown"])) for c in cells.values()
          if not c["censored"]["markdown"]]
    lopo = {fam: _lopo(fam, md) for fam in ("R0", "R1:N", "R1:C", "R1:T")}
    best_x = min(("N", "C", "T"), key=lambda x: lopo[f"R1:{x}"])
    for fam in (f"R2:{best_x}", f"R3:{best_x}"):
        lopo[fam] = _lopo(fam, md)
    best = min(lopo.values())
    family = min((fam for fam, e in lopo.items() if e <= best + HALF_STEP),
                 key=lambda fam: (COMPLEXITY[fam.split(":")[0]], lopo[fam]))
    md_pred = _fit(family, md)

    both = [(c["features"], math.log2(c["b_star"]["markdown"]), math.log2(c["b_star"]["json"]))
            for c in cells.values() if not c["censored"]["markdown"] and not c["censored"]["json"]]
    js = [(c["features"], math.log2(c["b_star"]["json"])) for c in cells.values()
          if not c["censored"]["json"]]

    def k_lopo() -> float:
        errs = []
        for i in range(len(both)):
            rest = both[:i] + both[i + 1:]
            pred = _fit(family, [(f, y) for f, y, _ in rest])
            k = statistics.fmean(yj - ym for _, ym, yj in rest)
            errs.append(abs(math.log2(round_up(2 ** (pred(both[i][0]) + k))) - both[i][2]))
        return statistics.fmean(errs)

    if not both:
        raise SystemExit("no derivation project saturates in both formats: no JSON factor")
    # DECISION: JSON gets its own fit only if cross-validation shows it is better
    # (§4.2). With fewer than two projects saturated in both formats it cannot run,
    # and the simpler option, the factor k, stands.
    if len(both) >= 2 and len(js) >= 2:
        k_err, sep_err = k_lopo(), _lopo(family, js)
        json_mode = "separate" if sep_err < k_err else "k"
    else:
        k_err = sep_err = None
        json_mode = "k"
    log2_k = statistics.fmean(yj - ym for _, ym, yj in both)
    js_pred = _fit(family, js)

    def rule(features: dict, fmt: str) -> int:
        if fmt == "markdown":
            return round_up(2 ** md_pred(features))
        if json_mode == "k":
            return round_up(2 ** (md_pred(features) + log2_k))
        return round_up(2 ** js_pred(features))

    return {"family": family, "rule": rule, "lopo": lopo, "json_mode": json_mode,
            "json_lopo": {"k": k_err, "separate": sep_err}, "k": 2 ** log2_k,
            "coef": md_pred.coef, "offsets": md_pred.offsets,
            "json_coef": js_pred.coef}


def fit_constant(cells: dict) -> dict:
    """R0 per format, fitted on derivation: the constant to beat (§4.3)."""
    out = {}
    for fmt in FORMATS:
        ys = [math.log2(c["b_star"][fmt]) for c in cells.values() if not c["censored"][fmt]]
        out[fmt] = round_up(2 ** statistics.fmean(ys))
    return out


# --- §4.3 acceptance ---------------------------------------------------------------

def accept(cells: list[dict]) -> dict:
    """cells: {curve, b_star, b_rule}. The margins and cost limits of §4.3."""
    losses, costs = [], []
    for c in cells:
        r_star = c["curve"][c["b_star"]]["recall"]
        losses.append(max(0.0, r_star - c["curve"][c["b_rule"]]["recall"]))
        costs.append(c["b_rule"] / c["b_star"])
    geo = math.exp(statistics.fmean(math.log(x) for x in costs))
    out = {"losses": losses, "costs": costs, "max_loss": max(losses),
           "mean_loss": statistics.fmean(losses), "geo_cost": geo, "max_cost": max(costs)}
    out["passed"] = (out["max_loss"] <= MARGIN_CELL + 1e-9 and out["mean_loss"] <= MARGIN_MEAN
                     + 1e-9 and geo <= GEO_COST_MAX + 1e-9 and out["max_cost"] <= CELL_COST_MAX)
    return out


def method_gate(pairs: dict) -> dict:
    """pairs: {project: {fmt: (B* from commits, B* from the reference set)}}."""
    st = {p: {fmt: steps(*v) for fmt, v in by.items()} for p, by in pairs.items()}
    return {"steps": st, "stop": all(s > 2 for by in st.values() for s in by.values())}


# --- §4.5 the relevance cut ------------------------------------------------------------

def _cut_stats(cells: list[dict], key: str) -> dict:
    losses = [max(0.0, c["b_star_recall"] - c["cut"][key]["recall"]) for c in cells]
    savings = [1 - c["cut"][key]["tokens"] / c["ref_tokens"] for c in cells]
    return {"key": key, "max_loss": max(losses), "mean_loss": statistics.fmean(losses),
            "savings": statistics.fmean(savings), "losses": losses, "savings_cells": savings,
            "recall": statistics.fmean(c["cut"][key]["recall"] for c in cells)}


def _within_margins(s: dict) -> bool:
    return s["max_loss"] <= MARGIN_CELL + 1e-9 and s["mean_loss"] <= MARGIN_MEAN + 1e-9


def choose_cut(cells: list[dict]) -> dict:
    """The eligible form and parameter saving the most tokens on derivation."""
    table = {k: _cut_stats(cells, k) for k in cells[0]["cut"]}
    eligible = [s for s in table.values() if _within_margins(s)]
    pick = max(eligible, key=lambda s: (s["savings"], s["recall"])) if eligible else None
    return {**(pick or {"key": None}), "table": table}


def accept_cut(cells: list[dict], key: str) -> dict:
    s = _cut_stats(cells, key)
    return {**s, "mean_savings": s["savings"],
            "passed": _within_margins(s) and s["savings"] >= CUT_SAVINGS - 1e-9}


# --- the whole study -------------------------------------------------------------------

def fit_set(counts: dict[str, int]) -> list[str]:
    """Derivation projects with enough scorable questions to enter a fit (§2.4)."""
    return [p for p in GROUPS["derivation"] if counts.get(p, 0) >= MIN_QUESTIONS]


def load(results: Path, name: str) -> list[dict]:
    path = results / f"{name}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def features(records: list[dict], meta: dict, project: str) -> dict:
    return {"N": statistics.median(r["graph_nodes"] for r in records),
            "C": statistics.median(r["candidates"] for r in records),
            "T": meta["median_graph"]["tokens_markdown"],
            "tau": statistics.median(r["test_nodes"] / r["graph_nodes"] for r in records),
            "lang": LANG[project]}


def _fmt_rule(fit: dict) -> str:
    fam, coef = fit["family"], fit["coef"]
    if fam == "R0":
        return f"B = {round_up(2 ** coef[0])} (constante)"
    x = fam.split(":")[1]
    s = f"log2 B = {coef[0]:.3f} + {coef[1]:.3f}·log2 {x}"
    if fam.startswith("R3"):
        s += f" + {coef[2]:.3f}·τ"
    if fit["offsets"]:
        s += " + desplazamiento por lenguaje " + ", ".join(
            f"{k} {v:+.2f}" for k, v in sorted(fit["offsets"].items()))
    return s + ", redondeado hacia arriba a la rejilla"


def svg(curves: dict, fmt: str, stars: dict) -> str:
    """Recall against mean real tokens, one line per project (log x axis)."""
    w, h, ml, mb, mt, mr = 900, 640, 60, 170, 20, 20       # legend below the plot
    x0, x1 = math.log10(500), math.log10(70000)

    def px(t: float) -> float:
        return ml + (math.log10(max(t, 500)) - x0) / (x1 - x0) * (w - ml - mr)

    def py(r: float) -> float:
        return mt + (1 - r) * (h - mt - mb)
    palette = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2",
               "#7f7f7f", "#bcbd22", "#17becf", "#393b79", "#637939", "#8c6d31", "#843c39"]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
           f'viewBox="0 0 {w} {h}" font-family="sans-serif" font-size="11">',
           f'<rect width="{w}" height="{h}" fill="white"/>']
    for t in (1000, 2000, 4000, 8000, 16000, 32000, 64000):
        out.append(f'<line x1="{px(t):.1f}" y1="{mt}" x2="{px(t):.1f}" y2="{h - mb}" '
                   f'stroke="#eee"/><text x="{px(t):.1f}" y="{h - mb + 15}" '
                   f'text-anchor="middle">{t // 1000}k</text>')
    for r in (0, 0.2, 0.4, 0.6, 0.8, 1.0):
        out.append(f'<line x1="{ml}" y1="{py(r):.1f}" x2="{w - mr}" y2="{py(r):.1f}" '
                   f'stroke="#eee"/><text x="{ml - 8}" y="{py(r) + 4:.1f}" '
                   f'text-anchor="end">{r:.1f}</text>')
    out.append(f'<text x="{(ml + w - mr) / 2}" y="{h - mb + 34}" text-anchor="middle">'
               f'tokens reales medios de la salida ({fmt})</text>')
    out.append(f'<text x="14" y="{(mt + h - mb) / 2}" transform="rotate(-90 14 '
               f'{(mt + h - mb) / 2})" text-anchor="middle">recall medio</text>')
    for i, (proj, c) in enumerate(curves.items()):
        color = palette[i % len(palette)]
        dash = "" if proj in GROUPS["derivation"] else ' stroke-dasharray="5,3"'
        pts = " ".join(f"{px(c[b]['tokens']):.1f},{py(c[b]['recall']):.1f}"
                       for b in GRID + [CONTROL] if b in c)
        out.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.6"'
                   f'{dash}/>')
        b = stars[proj]
        out.append(f'<circle cx="{px(c[b]["tokens"]):.1f}" cy="{py(c[b]["recall"]):.1f}" r="4" '
                   f'fill="{color}"/>')
        lx, ly = ml + (i % 4) * 200, h - mb + 64 + (i // 4) * 18
        out.append(f'<line x1="{lx}" y1="{ly - 4}" x2="{lx + 22}" y2="{ly - 4}" stroke="{color}" '
                   f'stroke-width="2"{dash}/><text x="{lx + 28}" y="{ly}">{proj}</text>')
    out.append(f'<text x="{ml}" y="{h - 12}">continua: derivación · discontinua: validación · '
               f'●: presupuesto óptimo B*</text></svg>')
    return "\n".join(out)


def analyse(results: Path) -> tuple[dict, str, dict]:
    projects = GROUPS["derivation"] + GROUPS["validation"]
    recs = {p: load(results, p) for p in projects}
    metas = {p: json.loads((results / f"{p}.meta.json").read_text()) for p in projects}
    cv = {p: {fmt: curve(recs[p], fmt) for fmt in FORMATS} for p in projects}
    opt = {p: {fmt: optimal(cv[p][fmt]) for fmt in FORMATS} for p in projects}
    boot = {p: {fmt: bootstrap(recs[p], fmt) for fmt in FORMATS} for p in projects}
    feats = {p: features(recs[p], metas[p], p) for p in projects}

    # §4.3 method check with the reference sets, before any fitting.
    refs = {n: load(results, f"ref-{n}") for n in ("prismastats", "slurp")}
    ref_opt = {n: {fmt: optimal(curve(refs[n], fmt)) for fmt in FORMATS} for n in refs}
    gate = method_gate({n: {fmt: (opt[n][fmt]["b_star"], ref_opt[n][fmt]["b_star"])
                            for fmt in FORMATS} for n in refs})

    counts = {p: len([r for r in recs[p] if r.get("present")]) for p in projects}
    fitting = fit_set(counts)

    def cells_of(group: str) -> dict:
        names = fitting if group == "derivation" else GROUPS[group]
        return {p: {"features": feats[p],
                    "b_star": {fmt: opt[p][fmt]["b_star"] for fmt in FORMATS},
                    "censored": {fmt: opt[p][fmt]["censored"] for fmt in FORMATS}}
                for p in names}

    summary: dict = {"method_gate": gate, "projects": {}, "fit_projects": fitting}
    for p in projects:
        summary["projects"][p] = {
            "group": "derivación" if p in GROUPS["derivation"] else "validación",
            "questions": len([r for r in recs[p] if r.get("present")]),
            "names_symbol": sum(1 for r in recs[p] if r.get("names_symbol")),
            "features": feats[p], "optimal": opt[p], "bootstrap": boot[p],
            "curve": {fmt: {str(b): {k: round(v, 4) for k, v in pt.items()}
                            for b, pt in cv[p][fmt].items()} for fmt in FORMATS}}
    if gate["stop"]:
        summary["stopped"] = "method gate"
        return summary, "", {}

    der = cells_of("derivation")
    fit = fit_rule(der)
    const = fit_constant(der)

    def judge(policy) -> dict:
        cells = [{"curve": cv[p][fmt], "b_star": opt[p][fmt]["b_star"], "b_rule": policy(p, fmt),
                  "project": p, "format": fmt} for p in GROUPS["validation"] for fmt in FORMATS]
        res = accept(cells)
        res["cells"] = [{"project": c["project"], "format": c["format"], "b_star": c["b_star"],
                         "b_rule": c["b_rule"], "loss": round(lo, 4), "cost": round(co, 3)}
                        for c, lo, co in zip(cells, res["losses"], res["costs"])]
        return res

    rule_v = judge(lambda p, fmt: fit["rule"](feats[p], fmt))
    const_v = judge(lambda p, fmt: const[fmt])
    d4000 = {g: accept([{"curve": cv[p][fmt], "b_star": opt[p][fmt]["b_star"],
                         "b_rule": DEFAULT_BUDGET} for p in GROUPS[g] for fmt in FORMATS])
             for g in GROUPS}
    rule_better = rule_v["geo_cost"] <= (1 - SIMPLICITY) * const_v["geo_cost"] + 1e-9
    if rule_v["passed"] and (not const_v["passed"] or rule_better):
        policy, ref = "rule", (lambda p, fmt: fit["rule"](feats[p], fmt))
    elif const_v["passed"]:
        policy, ref = "constant", (lambda p, fmt: const[fmt])
    else:
        policy, ref = "4000", (lambda p, fmt: DEFAULT_BUDGET)
    rule_accepted = rule_v["passed"] and policy == "rule"

    # §4.5 the relevance cut, against the recommended budget as ceiling.
    def cut_cells(group: str, ceiling) -> list[dict]:
        out = []
        for p in (fitting if group == "derivation" else GROUPS[group]):
            for fmt in FORMATS:
                b_ref = ceiling(p, fmt)
                keys = recs[p][0]["cut"].keys()
                cuts = {}
                for k in keys:
                    cc = curve(recs[p], fmt, "cut", k)
                    cuts[k] = {"recall": cc[b_ref]["recall"], "tokens": cc[b_ref]["tokens"]}
                out.append({"project": p, "format": fmt, "ceiling": b_ref,
                            "b_star_recall": cv[p][fmt][opt[p][fmt]["b_star"]]["recall"],
                            "ref_tokens": cv[p][fmt][b_ref]["tokens"],
                            "ref_recall": cv[p][fmt][b_ref]["recall"],
                            "tokens_4000": cv[p][fmt][DEFAULT_BUDGET]["tokens"], "cut": cuts})
        return out

    der_cut = cut_cells("derivation", ref)
    pick = choose_cut(der_cut)
    val_cut = cut_cells("validation", ref)
    cut_v = accept_cut(val_cut, pick["key"]) if pick["key"] else {"passed": False}
    cut_others = {k: accept_cut(val_cut, k) for k in val_cut[0]["cut"]}
    cut_32k = {k: accept_cut(cut_cells("validation", lambda p, f: GRID[-1]), k)
               for k in val_cut[0]["cut"]} if pick["key"] else {}

    summary.update({
        "rule": {k: v for k, v in fit.items() if k != "rule"}, "rule_formula": _fmt_rule(fit),
        "rule_validation": {k: v for k, v in rule_v.items() if k not in ("losses", "costs")},
        "constant": const, "constant_validation": {k: v for k, v in const_v.items()
                                                   if k not in ("losses", "costs")},
        "rule_beats_constant_by_25pct": rule_better, "policy": policy,
        "rule_accepted": rule_accepted, "default_4000": d4000,
        "reference_budgets": {p: {fmt: ref(p, fmt) for fmt in FORMATS} for p in projects},
        "cut_derivation": {k: {kk: vv for kk, vv in s.items()} for k, s in pick["table"].items()},
        "cut_pick": pick.get("key"), "cut_validation": cut_v,
        "cut_validation_others": cut_others, "cut_validation_32k": cut_32k,
        "cut_cells_validation": [{k: v for k, v in c.items() if k != "cut"} |
                                 {"cut": c["cut"].get(pick["key"])} for c in val_cut]
        if pick["key"] else [],
    })
    svgs = {fmt: svg({p: cv[p][fmt] for p in projects}, fmt,
                     {p: opt[p][fmt]["b_star"] for p in projects}) for fmt in FORMATS}
    return summary, "", svgs


def main(argv: list[str]) -> int:
    here = Path(__file__).resolve().parent
    out = Path(argv[0]) if argv else here / "resultados"
    results = Path.home() / "slurp-bench" / "results"
    summary, _md, svgs = analyse(results)
    out.mkdir(parents=True, exist_ok=True)
    (out / "resumen.json").write_text(json.dumps(summary, indent=1, default=float,
                                                 ensure_ascii=False))
    for fmt, text in svgs.items():
        (out / f"curvas-{fmt}.svg").write_text(text)
    print(json.dumps({k: summary.get(k) for k in ("method_gate", "policy", "rule_formula",
                                                  "rule_accepted", "cut_pick")}, default=float,
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
