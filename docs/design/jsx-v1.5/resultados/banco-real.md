# Aristas de JSX (#87): el banco con la implementación real (2026-11-28)

**Qué se midió.**

- Las 1.525 preguntas del banco con el paquete de la v1.5 tal como queda en `c210b1d` (snapshot con
  `git archive` en `~/slurp-bench/v15/src/c210b1d`).
- **Cada grafo padre se reindexa** con el `index_project` de ese snapshot, en un worktree limpio
  por proyecto (`checkout -f` + `clean -fdx`, como se construyó el banco). Después se puntúa y se
  selecciona a 4000 en markdown.
- El prototipo añadía aristas a los grafos guardados; aquí no se supone nada. La v1.5 cambia más
  que la JSX: la rama regex de TS y Java sirve también a los ficheros cuyo árbol de tree-sitter
  se rompe, así que ningún proyecto se da por igual.
- La referencia es la v1.4 del propio banco, pregunta a pregunta. Antes de medir, `--precheck`
  reindexó con la v1.4.0 la primera pregunta de cada proyecto y devolvió el grafo guardado en los
  14 (nodos y aristas, atributos incluidos; `precheck-v14.txt`).
- Scripts:
  - `bank_real.py`: ejecución; un proceso por proyecto, como mucho dos a la vez, y espera
    mientras la presión de memoria del sistema no es normal;
  - `analyze_real.py`: criterio y resumen;
  - `ablation_real.py`: de dónde sale cada cambio.
- Datos en `~/slurp-bench/v15/bank-real/`.

## Criterio (fijado antes de medir, §5 del diseño): se cumple

| | v1.4 | prototipo W2 | **v1.5** |
|---|---|---|---|
| **Validación, media de 6** | 0,6741 | 0,6763 (+0,0022) | **0,6774 (+0,0033)** |
| Peor proyecto de los 14 | — | 0,000 | **0,000**: ninguno baja |
| Derivación, media de 7 | 0,7249 | 0,7369 (+0,0119) | 0,7371 (+0,0121) |
| **¿Cumple?** (media ≥ −0,01 y ningún proyecto < −0,03) | | sí | **sí** |

**Por proyecto** (recall a 4000 en markdown; preguntas que mejoran / empeoran frente a la v1.4):

| Proyecto | Grupo | v1.4 | prototipo W2 | v1.5 | Δ | mejor / peor | 1.er grafo = banco | `renders` (mediana) |
|---|---|---|---|---|---|---|---|---|
| ky (120) | derivación | 0,959 | 0,959 | 0,959 | 0 | 0 / 0 | sí | 0 |
| PrismaStats (68) | derivación | 0,527 | 0,610 | **0,610** | +0,083 | 9 / 2 | no | 258 |
| hono (120) | derivación | 0,891 | 0,891 | 0,893 | +0,002 | 1 / 0 | no | 41 |
| jsoup (120) | derivación | 0,646 | | 0,646 | 0 | 0 / 0 | sí | 0 |
| caddy (120) | derivación | 0,714 | | 0,714 | 0 | 0 / 0 | sí | 0 |
| tokio (120) | derivación | 0,800 | | 0,800 | 0 | 0 / 0 | sí | 0 |
| sqlalchemy (120) | derivación | 0,538 | | 0,538 | 0 | 0 / 0 | sí | 0 |
| gin (120) | validación | 0,824 | | 0,824 | 0 | 0 / 0 | sí | 0 |
| click (120) | validación | 0,775 | | 0,775 | 0 | 0 / 0 | sí | 0 |
| ripgrep (99) | validación | 0,587 | | 0,587 | 0 | 0 / 0 | sí | 0 |
| excalidraw (120) | validación | 0,583 | 0,593 | **0,599** | +0,016 | 6 / 6 | no | 384 |
| commons-lang (120) | validación | 0,740 | | 0,740 | 0 | 0 / 0 | sí | 0 |
| prometheus (120) | validación | 0,535 | 0,539 | 0,539 | +0,004 | 2 / 0 | no | 142 |
| slurp (38) | — | 0,834 | | 0,834 | 0 | 0 / 0 | sí | 0 |

- **Sin `.tsx`/`.jsx` no cambia nada.** En los diez proyectos sin JSX, el grafo de la primera
  pregunta es el del banco y las 1.097 preguntas dan exactamente el recall de la v1.4. Entre esos
  diez están los de Java (jsoup, commons-lang), a los que también toca el arreglo del blanqueo.
- **Ningún nodo relevante desaparece:** en las 1.525 preguntas, todos los relevantes de la v1.4
  siguen en el grafo de la v1.5.

## De dónde sale cada cambio (`ablation_real.py`)

Las 26 preguntas que cambian frente a la v1.4 se reindexan con tres snapshots: `7bafa0a` (los
arreglos de la rama regex), `c6b9378` (más W2) y `c210b1d` (más `renders`, la v1.5).

| | lo mueven los arreglos regex | lo mueve W2 | lo mueve `renders` |
|---|---|---|---|
| PrismaStats (11) | — | — | 11 (9 mejor, 2 peor) |
| hono (1) | 1 mejor | — | — |
| excalidraw (12) | — | 4 (2 mejor, 2 peor) | 8 (4 mejor, 4 peor) |
| prometheus (2) | — | 1 mejor | 1 mejor |

- La pregunta de hono (`jsx: allow a function component to return an array`) pasa de 0,8 a 1,0
  por las llamadas regex corregidas: con las aristas `renders` de la v1.5 puestas sobre el grafo
  de la v1.4 sigue en 0,8.
  - Desaparecen autollamadas falsas (`isRawRequest → isRawRequest`, del desplazamiento de
    líneas).
  - `→ bufferToFormData` pasa a salir de quien la llama (`parseFormData`).
- **Frente al prototipo** difieren 8 preguntas de excalidraw, en los dos sentidos (neto +0,006).
  Los grafos difieren por tres cosas:
  - el orden de los imports (abajo);
  - los ficheros `.js` con JSX (`revision-manual.md`);
  - los ficheros que van por la rama regex.

## Juegos etiquetados (se informan, no deciden)

| | v1.4 | prototipo W2 | v1.5 |
|---|---|---|---|
| PrismaStats (15) | 0,967 | 0,933 | **0,933**, una peor (`arch-02`) |
| slurp (16) | 0,938 | sin `.tsx` | 0,938, ninguna peor |

- La que empeora es la misma que con el prototipo: `arch-02` («Which files concentrate the most
  functions?»), sin coincidencias léxicas, con su primer relevante muy abajo. Los nodos nuevos de
  shadcn/ui mueven el PageRank global lo justo para que el módulo `admin_actions` no quepa.
- El criterio de la v1.4 para estos juegos (perder como mucho una pregunta) se seguiría cumpliendo.

## Defecto anterior encontrado: un `import type` antes del import de valores del mismo módulo

En el padre `ff0b4394b128` de excalidraw, el prototipo tiene `ConfirmDialog → Dialog` y
`App.render → ContextMenu`, y la v1.5 no. En el HEAD es al revés. La diferencia está en el orden
de dos imports del mismo módulo:

```ts
import type { DialogProps } from "./Dialog";   // primero el de tipos...
import { Dialog } from "./Dialog";             // ...y el de valores se pierde
```

- **Causa.** Los dos imports dan el mismo id de nodo import (`<fichero>.import_Dialog`), y
  `_assemble_graph` se queda con el primero. El de tipos no lleva símbolos ni
  `_TS_SOURCE_MODULE`, así que el resolutor entre ficheros no encuentra el módulo. Pasa en las dos
  ramas (`visit_import_statement` y el bucle de `_TS_IMPORT_CLAUSE_RE`).
- **Es anterior a la v1.5 y afecta también a las `calls`.** En un proyecto mínimo, con el
  `import type` delante, la v1.4.0 tampoco da `Confirm → helper`. Con los imports en el otro orden,
  con uno solo o con `type` en línea (`import { Dialog, type DialogProps }`), las dos versiones
  aciertan. Dos imports de valores del mismo módulo también funcionan.
- **Alcance en el HEAD del banco** (cuenta aproximada por regex: ficheros con un `import type` de un
  módulo del proyecto antes de su import de valores):
  - hono: 47 de 424;
  - PrismaStats: 1 de 181;
  - excalidraw, prometheus y ky: 0.
- Da falsos negativos, nunca aristas falsas. No se ha arreglado: corregirlo cambia las `calls` de
  la v1.4 en esos ficheros y, con ellas, el grafo que este banco ha medido. Queda como decisión
  del autor.
