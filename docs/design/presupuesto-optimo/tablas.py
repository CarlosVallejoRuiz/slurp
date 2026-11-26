"""Render resultados/resumen.json (from analyze.py) as markdown tables. Not part of slurp.

    ~/Desktop/Slurp/slurp/.venv/bin/python -I tablas.py > resultados/tablas.md

No decisions here: analyze.py takes them, this only prints them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GRID = [1000, 1500, 2000, 3000, 4000, 6000, 8000, 12000, 16000, 24000, 32000]


def k(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else f"{n:.0f}"


def main() -> int:
    s = json.loads((HERE / "resultados" / "resumen.json").read_text())
    P = s["projects"]
    out = ["# Estudio del presupuesto óptimo: tablas (generadas por `tablas.py`)", ""]

    out += ["## Proyectos medidos", "",
            "| Proyecto | Grupo | Preguntas | Nombran el símbolo | N (mediana) | C | T md | Tests | Lenguaje |",
            "|---|---|---|---|---|---|---|---|---|"]
    for name, p in P.items():
        f = p["features"]
        out.append(f"| {name} | {p['group']} | {p['questions']} | {p['names_symbol']} | "
                   f"{f['N']:,.0f} | {f['C']:,.0f} | {f['T']:,} | {f['tau'] * 100:.0f} % | {f['lang']} |")

    for fmt in ("markdown", "json"):
        out += ["", f"## Recall medio · tokens reales medios, {fmt}", "",
                "| Proyecto | " + " | ".join(k(b) for b in GRID) + " | 64k (control) |",
                "|---" * (len(GRID) + 2) + "|"]
        for name, p in P.items():
            c = p["curve"][fmt]
            cells = [f"{c[str(b)]['recall']:.2f} · {k(c[str(b)]['tokens'])}" for b in GRID + [64000]]
            b_star = p["optimal"][fmt]["b_star"]
            cells[GRID.index(b_star)] = f"**{cells[GRID.index(b_star)]}**"
            out.append(f"| {name} | " + " | ".join(cells) + " |")
        out.append("")
        out.append("En negrita, B\\* (el menor presupuesto con el 95 % del recall máximo hasta 32000).")

    out += ["", "## Presupuesto óptimo por proyecto", "",
            "| Proyecto | Grupo | B\\* md (bootstrap p10–p90) | R\\* md | ¿Satura en 32k? | B\\* JSON (p10–p90) | R\\* JSON | ¿Satura? |",
            "|---|---|---|---|---|---|---|---|"]
    for name, p in P.items():
        o, b = p["optimal"], p["bootstrap"]
        out.append(
            f"| {name} | {p['group']} | {o['markdown']['b_star']} ({b['markdown']['p10']}–"
            f"{b['markdown']['p90']}) | {o['markdown']['r_max']:.3f} | "
            f"{'no' if o['markdown']['censored'] else 'sí'} | {o['json']['b_star']} "
            f"({b['json']['p10']}–{b['json']['p90']}) | {o['json']['r_max']:.3f} | "
            f"{'no' if o['json']['censored'] else 'sí'} |")

    g = s["method_gate"]
    out += ["", "## Comprobación del método (juegos de referencia)", "",
            "Pasos de rejilla entre el B\\* de las preguntas de commits y el de las etiquetadas:", ""]
    for proj, by in g["steps"].items():
        out.append(f"- {proj}: markdown {by['markdown']}, JSON {by['json']}")
    out.append(f"- ¿Se para? {'sí' if g['stop'] else 'no'} (solo si los cuatro pasan de 2)")

    if s.get("stopped"):
        print("\n".join(out))
        return 0

    r = s["rule"]
    out += ["", "## Regla (ajustada en derivación)", "",
            "| Familia | Error LOPO medio (log2) |", "|---|---|"]
    for fam, e in r["lopo"].items():
        out.append(f"| {fam}{' ← elegida' if fam == r['family'] else ''} | {e:.3f} |")
    out += ["", f"- Regla: {s['rule_formula']}.",
            f"- JSON: {'factor k = ' + format(r['k'], '.2f') + ' sobre markdown' if r['json_mode'] == 'k' else 'ajuste propio'} "
            f"(LOPO k {r['json_lopo']['k']:.3f}, propio {r['json_lopo']['separate']:.3f}).",
            f"- Constante a batir (R0 en derivación): markdown {s['constant']['markdown']}, JSON {s['constant']['json']}."]

    def judge_table(title: str, v: dict) -> list[str]:
        rows = ["", f"### {title}", "", "| Proyecto | Formato | B\\* | Presupuesto | Pérdida | Coste |",
                "|---|---|---|---|---|---|"]
        for c in v["cells"]:
            rows.append(f"| {c['project']} | {c['format']} | {c['b_star']} | {c['b_rule']} | "
                        f"{c['loss']:.3f} | {c['cost']:.2f}× |")
        rows += ["", f"Pérdida máxima {v['max_loss']:.3f} (límite 0,03) · media {v['mean_loss']:.3f} "
                     f"(límite 0,01) · coste geométrico {v['geo_cost']:.2f}× (límite 1,5) · coste máximo "
                     f"{v['max_cost']:.1f}× (límite 4) → **{'PASA' if v['passed'] else 'NO PASA'}**"]
        return rows

    out += ["", "## Validación (abierta una vez)"]
    out += judge_table("La regla", s["rule_validation"])
    out += judge_table("La constante", s["constant_validation"])
    out += ["", f"- ¿La regla rebaja el coste geométrico de la constante al menos un 25 %? "
                f"{'sí' if s['rule_beats_constant_by_25pct'] else 'no'}.",
            f"- Recomendación según §4.3: **{s['policy']}**."]

    out += ["", "## El 4000 actual", "",
            "| Grupo | Pérdida máxima | Pérdida media | Coste geométrico | Coste máximo | ¿Pasaría? |",
            "|---|---|---|---|---|---|"]
    for grp, v in s["default_4000"].items():
        out.append(f"| {grp} | {v['max_loss']:.3f} | {v['mean_loss']:.3f} | {v['geo_cost']:.2f}× | "
                   f"{v['max_cost']:.2f}× | {'sí' if v['passed'] else 'no'} |")

    out += ["", "## Corte por relevancia", "",
            f"Techo: el presupuesto recomendado ({s['policy']}). Derivación, las 18 combinaciones:", "",
            "| Corte | Pérdida máx. | Pérdida media | Ahorro medio de tokens | Recall medio |",
            "|---|---|---|---|---|"]
    for key, c in sorted(s["cut_derivation"].items(), key=lambda kv: -kv[1]["savings"]):
        mark = " ← elegido" if key == s["cut_pick"] else ""
        out.append(f"| {key}{mark} | {c['max_loss']:.3f} | {c['mean_loss']:.3f} | "
                   f"{c['savings'] * 100:.0f} % | {c['recall']:.3f} |")
    v = s["cut_validation"]
    if s["cut_pick"]:
        out += ["", f"Validación de {s['cut_pick']}: pérdida máxima {v['max_loss']:.3f}, media "
                    f"{v['mean_loss']:.3f}, ahorro medio {v['mean_savings'] * 100:.0f} % (mínimo 25 %) → "
                    f"**{'PASA' if v['passed'] else 'NO PASA'}**", "",
                "| Proyecto | Formato | Techo | Recall con B\\* | Recall con corte | Tokens del techo | Tokens con corte | Tokens con 4000 |",
                "|---|---|---|---|---|---|---|---|"]
        for c in s["cut_cells_validation"]:
            out.append(f"| {c['project']} | {c['format']} | {c['ceiling']} | {c['b_star_recall']:.3f} | "
                       f"{c['cut']['recall']:.3f} | {k(c['ref_tokens'])} | {k(c['cut']['tokens'])} | "
                       f"{k(c['tokens_4000'])} |")
        out += ["", "Las demás combinaciones en validación (informativo, no deciden):", "",
                "| Corte | Pérdida máx. | Pérdida media | Ahorro | Techo 32000: pérdida máx. · ahorro |",
                "|---|---|---|---|---|"]
        for key, c in s["cut_validation_others"].items():
            c32 = s["cut_validation_32k"].get(key, {})
            out.append(f"| {key} | {c['max_loss']:.3f} | {c['mean_loss']:.3f} | {c['savings'] * 100:.0f} % | "
                       f"{c32.get('max_loss', 0):.3f} · {c32.get('savings', 0) * 100:.0f} % |")
    else:
        out.append("\nNinguna combinación cumple los márgenes en derivación: no hay corte que validar.")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
