# Aristas de JSX (#87) — el banco con y sin el cambio (2026-11-28)

**Qué se midió.**

- Las 1.525 preguntas del banco, con el código de la v1.4 (`score_nodes` y `build_context` a 4000
  en markdown), en tres variantes:
  - **W0:** el grafo padre guardado del banco;
  - **W1:** W0 más las aristas `renders` del prototipo (`jsx.py`), recorridas por el PageRank
    personalizado igual que `calls`;
  - **W2:** los ficheros TS/JS del padre reindexados con los componentes de `React.memo` y
    `React.forwardRef` como nodos (`w2.py`), más las aristas `renders` sobre ese grafo.
- Solo pueden cambiar los proyectos con `.tsx`/`.jsx`: PrismaStats, hono, excalidraw y prometheus.
  Comprobado: ningún grafo padre de los otros diez tiene un nodo de un `.tsx`/`.jsx`, así que
  ahí W1 y W2 son W0 por construcción y entran con su cifra de la v1.4.
- Scripts: `bank.py` (ejecución), `analyze.py` (resumen). Datos en `~/slurp-bench/v15/bank/`.

**Comprobaciones del arnés.**

- W0 da, pregunta a pregunta, el recall que la v1.4 midió con su implementación: 0 discrepancias en
  las 428 preguntas de los cuatro proyectos.
- La reconstrucción de la parte TS sin la regla W2 reproduce exactamente el grafo guardado (nodos y
  aristas, atributos incluidos) en las tres primeras preguntas de cada proyecto y en el HEAD de los
  cuatro.

## Criterio (fijado antes de medir): se cumple con W1 y con W2

| | W0 | W1 | W2 |
|---|---|---|---|
| **Validación, media de 6** | 0,6741 | 0,6754 (**+0,0013**) | 0,6764 (**+0,0024**) |
| Peor proyecto de validación | — | −0,006 (prometheus) | 0,000 |
| Peor proyecto de los 14 | — | −0,006 | 0,000 |
| Derivación, media de 7 | 0,7249 | 0,7368 (+0,0119) | 0,7368 (+0,0119) |
| **¿Cumple?** (media ≥ −0,01 y ningún proyecto < −0,03) | | **sí** | **sí** |

**Por proyecto** (recall a 4000 en markdown; preguntas que mejoran / empeoran frente a W0):

| Proyecto | Grupo | W0 | W1 | W2 |
|---|---|---|---|---|
| PrismaStats (68) | derivación | 0,527 | **0,610** (7 / 0) | 0,610 (9 / 2) |
| hono (120) | derivación | 0,891 | 0,891 (0 / 0) | 0,891 (0 / 0) |
| excalidraw (120) | validación | 0,583 | 0,597 (4 / 2) | 0,593 (6 / 5) |
| prometheus (120) | validación | 0,535 | 0,529 (1 / 1) | 0,539 (2 / 0) |

- Mediana del puesto del primer relevante en PrismaStats: 47,5 (W0), 26,5 (W1), 47 (W2).
- Aristas `renders` por pregunta (mediana): PrismaStats 98 (W1) y 258 (W2); excalidraw 254 y 381;
  hono 41 y 41; prometheus 141 y 142. Nodos nuevos de W2 por pregunta: PrismaStats 56,
  excalidraw 43, prometheus 1, hono 0 (su `memo` es el de hono, no el de React).

## Juegos etiquetados (se informan, no deciden)

| | W0 | W1 | W2 |
|---|---|---|---|
| PrismaStats (15) | 0,967 | 0,967, ninguna peor | **0,933, una peor** |
| slurp (16) | sin `.tsx`: no cambia | | |

La que empeora con W2 es `arch-02` («Which files concentrate the most functions?»), que pasa de
0,5 a 0: deja fuera el módulo `admin_actions`. Es una pregunta sin coincidencias léxicas cuyo primer
relevante está en el puesto 870: los 56 nodos nuevos de shadcn/ui, con sus `contains`, mueven el
PageRank global lo justo para que el módulo no quepa en 4000.

## Cambio del grafo en el HEAD (de `graph_change.py`)

| | PrismaStats | excalidraw |
|---|---|---|
| Aristas `renders` (W1) | 151 | 320 |
| Bandas W0 (LOW / MEDIUM / HIGH) | 530 / 5 / 1 de 536 | 796 / 51 / 7 de 854 |
| Bandas W1 | 518 / 15 / 3 (12 nodos suben) | 751 / 88 / 15 (45 suben) |
| W2: nodos nuevos, aristas `renders` | 56, 337 | 48, 501 |
| Bandas W2 (sobre todos los nodos) | 540 / 40 / 12 de 592 | 787 / 95 / 20 de 902 |
| W2: nodos que ya existían y suben | 12 (igual que W1) | 49 |
| W2: banda de los nodos nuevos | 22 LOW, 25 MEDIUM, 9 HIGH | 40 LOW, 4 MEDIUM, 4 HIGH |
| W2: los más dependidos | `Button` 32, `Badge` 17, `Input` 13, `Select*` 10 | `IconButton` 30, `Island` 11 |

**Coste de indexar** (cota superior del prototipo, que vuelve a parsear cada fichero): 146 ms en
PrismaStats, frente a 0,44 s de `slurp index`; 675 ms en excalidraw, frente a 1,62 s. En la
implementación el árbol ya está parseado y el JSX es otro recorrido: se medirá entonces.
