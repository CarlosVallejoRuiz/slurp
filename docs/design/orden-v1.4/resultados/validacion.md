# v1.4 — validación (2026-11-27), abierta una vez

**Qué se midió.**

- El candidato `V3#2+V2#2` (ver `puerta.md`), en los 6 proyectos de validación del banco (719
  preguntas) y en los dos juegos etiquetados.
- El candidato se fijó antes de abrir la validación: commit `ab58d01`.
- La v1.3, medida con el `score_nodes` de slurp, da en cada proyecto exactamente la cifra del banco.

## Criterio (§6)

**Recall a 4000 en markdown:**

| Proyecto | v1.3 | Candidato | Diferencia |
|---|---|---|---|
| gin | 0,779 | 0,824 | +0,044 |
| click | 0,610 | 0,775 | +0,166 |
| ripgrep | 0,421 | 0,587 | +0,166 |
| excalidraw | 0,428 | 0,583 | +0,155 |
| commons-lang | 0,678 | 0,740 | +0,062 |
| prometheus | 0,344 | 0,535 | +0,190 |
| **Media** | **0,543** | **0,674** | **+0,131** |

**Las cuatro condiciones:**

1. **Mejora media:** +0,131 (≥ +0,05). **Cumple.**
2. **Peor proyecto:** gin, +0,044 (≥ −0,03). **Cumple.**
3. **Latencia:** dentro de los límites en los seis grafos, medida en la puerta con el prototipo.
   Se vuelve a medir con la implementación en slurp.
4. **Juegos de arquitectura, recall a 4000 en markdown:**
   - PrismaStats, de 0,967 a 0,967, ninguna pregunta peor;
   - slurp, de 0,875 a 0,938, ninguna pregunta peor.

   **Cumple.**

**VALIDACIÓN: PASA.**

## Informativo, sin que decida

| Proyecto | MRR | Recall de las preguntas que no nombran el símbolo | JSON 4000 | Markdown 8000 | Markdown 16000 |
|---|---|---|---|---|---|
| gin | 0,221 → 0,234 | 0,714 → 0,761 | 0,594 → 0,731 | 0,897 → 0,942 | 1,000 → 1,000 |
| click | 0,188 → 0,311 | 0,494 → 0,701 | 0,488 → 0,626 | 0,760 → 0,891 | 0,874 → 0,951 |
| ripgrep | 0,044 → 0,124 | 0,375 → 0,538 | 0,251 → 0,414 | 0,506 → 0,711 | 0,648 → 0,876 |
| excalidraw | 0,091 → 0,134 | 0,416 → 0,580 | 0,332 → 0,482 | 0,541 → 0,671 | 0,687 → 0,754 |
| commons-lang | 0,335 → 0,492 | 0,288 → 0,328 | 0,593 → 0,697 | 0,726 → 0,787 | 0,748 → 0,800 |
| prometheus | 0,113 → 0,199 | 0,231 → 0,444 | 0,290 → 0,477 | 0,446 → 0,592 | 0,525 → 0,653 |

**Juegos etiquetados, los mismos datos:**

| Juego | MRR | JSON 4000 | Markdown 8000 | Markdown 16000 |
|---|---|---|---|---|
| PrismaStats | **0,405 → 0,337** | **0,900 → 0,867** | 0,967 → 0,967 | 0,967 → 0,967 |
| slurp | 0,397 → 0,442 | **0,812 → 0,750** | 0,938 → 0,938 | **1,000 → 0,938** |

**Las cuatro bajadas** (en negrita en la tabla) no deciden nada, pero se señalan:

- MRR de PrismaStats: de 0,405 a 0,337;
- JSON 4000 en PrismaStats: media pregunta;
- JSON 4000 en slurp: una pregunta;
- markdown 16000 en slurp: una pregunta.

Ninguna de ellas afecta al recall a 4000 en markdown, que es el que fija el criterio.

**Tokens:** iguales. La salida sigue usando el ~98 % del presupuesto.
