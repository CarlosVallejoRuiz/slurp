# v1.4 — latencia tras optimizar `budget.py` (2026-11-27)

**Qué se midió.**

- La v1.4 con la optimización (`ba056f5`), frente a la v1.3 (`git archive v1.3.0`) y frente a la
  v1.4 sin optimizar (`6394502`), en la misma sesión y con el método corregido de
  `orden-v1.4.md` (regla de decisión tras la latencia).
  - Antes de un rebase sin cambios de código, esos commits eran `bad84de` y `a461adf`. Los árboles
    `slurp/` y `tests/` son idénticos, y con esos nombres aparecen en los ficheros de medida.
- Deciden los cocientes frente a la v1.3 medida al lado (ver «Cómo se leen los límites» en
  `orden-v1.4.md`). La tabla fija de §6 se informa, pero no decide.
- Scripts y datos, fuera del repositorio, en `~/slurp-bench/v14-latencia/`:
  - huellas: `tools/fp_run.py`;
  - latencia: `tools/lat_run.py` y `tools/lat_report.py`;
  - espera de estabilidad: `tools/stable.py`;
  - resultados: `fp/` y `lat/`.

## Condiciones de la medida

- **Cuándo:** de 19:34 a 19:39.
- **Espera previa:** ningún modelo de Ollama cargado, al menos un 60 % de memoria libre y un swap
  que no crece durante 2 minutos seguidos (19:34:36–19:36:37). Se cumplió el criterio principal,
  sin llegar al alternativo.
- **Durante la medida** (comprobado antes y después de cada proceso):
  - ningún modelo cargado en ningún punto;
  - swap de 1.460 MB, sin crecer;
  - memoria libre del 70–72 %;
  - carga a 1 minuto de 3,5–10,6;
  - otra aplicación en marcha, `PlantsonFire.app`, al ~43 % de una CPU.
- **Cómo:** un solo proceso de medida a la vez, con las versiones intercaladas.
- **Dos tandas anteriores se descartaron sin usarlas** (`lat/descartadas-presion-memoria/`).
  - Tenían un modelo de Ollama cargado, y por tanto la memoria bajo presión.
  - Las cifras salían hasta 4 veces más altas: tokio v1.3 en frío daba 510 ms.

## Condición previa: la salida no cambia

**Sobre las 1.556 preguntas** (las 1.525 del banco y las 31 etiquetadas), el texto, los nodos, las
relaciones y los tokens son idénticos byte a byte antes y después de la optimización.

- Se compararon tres puntos: markdown 4000, JSON 4000 y markdown 16000.
- Los dos ficheros de huellas tienen el mismo sha256.

**El arnés** sigue la huella de `~/slurp-bench/tools/fingerprint.py`, con tres cambios:

- un proceso nuevo por proyecto;
- las cachés de slurp se vacían antes de cada pregunta;
- se puede retomar pregunta a pregunta.

**Por qué se vacían las cachés.**

- `fingerprint.py` arrastraba la caché de costes de un grafo al siguiente dentro del mismo proceso.
  `budget.py` declara que eso no es válido: ids iguales con contenido distinto.
- Con ese arnés, la misma v1.4 da otra huella en 5 preguntas (4 de gin y 1 de excalidraw): es lo
  que se ve en `~/slurp-bench/v14/fp-before.jsonl`.
- Con las cachés vacías, cada respuesta se calcula como en producción: un proceso de la CLI, o el
  servidor MCP tras recargar.

**Un test lo vigila sobre una muestra** (`TestNodesThatCannotFit`, en `tests/test_budget.py`). En
las preguntas etiquetadas de slurp y en asuntos de commits de slurp del banco, sobre el índice del
propio slurp:

- la salida es la misma con el atajo y costeando todos los nodos;
- también en grafos aleatorios con muchos presupuestos, y con o200k, p50k, JSON, YAML, un factor de
  tokens y la caché caliente;
- una cota que supere el coste real en un solo token hace fallar ese test.

## Qué se optimizó

**No eran las relaciones.**

- En frío, en tokio, la v1.4 costeaba 6.197 nodos y solo 69 relaciones.
- Con el presupuesto casi lleno, el hueco restante (1–26 tokens) no deja entrar ningún nodo (el más
  barato cuesta 20 o más). Aun así, el bucle sigue hasta agotar la cola y tokeniza cada candidato.
- En tokio, 6.096 de esos nodos no cabían: 40 de los 58 ms de `build_context`.

**Tokenizar en lote no ayuda.**

- `encode_batch` de tiktoken es 4 veces más lento con estos textos cortos.
- Llamar directamente a su codificador ahorra un ~12 %.

**Lo que se hizo: una cota inferior del coste de cada nodo.**

- El texto markdown del nodo se corta donde el tokenizador siempre corta:
  - antes de un espacio que sigue a un carácter que no lo es;
  - tras un salto de línea, delante de un carácter que no es espacio.
- La puntuación y la línea del fichero, que comparten muchos nodos, se cuentan una vez.
- Un nodo cuya cota supera el hueco se salta sin tokenizarlo.
- Solo para cl100k_base y o200k_base, con sus patrones fijados. Los demás codificadores, JSON y YAML
  siguen como antes.

## Primera consulta en frío

Mediana de 3 procesos nuevos.

| Grafo | v1.3 | v1.4 sin optimizar | v1.4 | Cociente | Límite 1,5× | Cumple | Excepción 1,67× | Tabla fija §6 (informa) |
|---|---|---|---|---|---|---|---|---|
| PrismaStats | 27,4 ms | 42,1 ms | 38,6 ms | 1,41× | 41,1 ms | sí | sí (45,8 ms) | 120 ms: sí |
| hono | 41,9 ms | 70,4 ms | 64,4 ms | **1,54×** | 62,8 ms | **no** | sí (69,9 ms) | 63 ms: **no** |
| slurp | 77,5 ms | 95,0 ms | 92,6 ms | 1,20× | 116,3 ms | sí | sí (129,5 ms) | 113 ms: sí |
| tokio | 127,8 ms | 210,4 ms | 180,6 ms | 1,41× | 191,6 ms | sí | sí (213,4 ms) | 197 ms: sí |
| prometheus | 274,5 ms | 318,0 ms | 271,8 ms | 0,99× | 411,8 ms | sí | sí (458,5 ms) | 428 ms: sí |
| sqlalchemy | 649,5 ms | 836,6 ms | 816,0 ms | 1,26× | 974,3 ms | sí | sí (1.084,7 ms) | 1.004 ms: sí |

## En caliente

Mediana de 20 consultas tras una de calentamiento; se da la mediana de los 3 procesos.

| Grafo | v1.3 | v1.4 sin optimizar | v1.4 | Cociente | Límite (1,25× o +5 ms) | Cumple | Tabla fija §6 (informa) |
|---|---|---|---|---|---|---|---|
| PrismaStats | 10,2 ms | 14,2 ms | 14,4 ms | 1,41× | 15,2 ms | sí | 15 ms: sí |
| hono | 16,5 ms | 11,3 ms | 11,6 ms | 0,70× | 21,5 ms | sí | 22 ms: sí |
| slurp | 32,7 ms | 22,8 ms | 23,3 ms | 0,71× | 40,8 ms | sí | 41 ms: sí |
| tokio | 74,0 ms | 26,5 ms | 27,4 ms | 0,37× | 92,5 ms | sí | 94 ms: sí |
| prometheus | 111,7 ms | 41,8 ms | 43,7 ms | 0,39× | 139,6 ms | sí | 141 ms: sí |
| sqlalchemy | 278,3 ms | 85,4 ms | 87,3 ms | 0,31× | 347,8 ms | sí | 356 ms: sí |

## Primera consulta tras un refresco del servidor MCP

**Cómo se mide.**

- Una copia del proyecto con el grafo propio de slurp y un `GraphState` que ya ha respondido.
- Se inserta una línea de comentario al principio del fichero de producción con más nodos (la vía
  rápida de `--smart`).
- `prepare()` refresca, cambia el grafo y vacía sus cachés; en las 54 ediciones se comprobó que el
  grafo era otro y las cachés estaban vacías.
- Se mide la primera `slurp_query` sobre el grafo nuevo (`_tool_query`). Mediana de 3 ediciones.
- El refresco no cambia entre versiones (indexador idéntico) y se da aparte.

| Grafo | v1.3 | v1.4 sin optimizar | v1.4 | Cociente | Límite 1,5× | Cumple | Refresco (v1.4) | Total, v1.3 → v1.4 |
|---|---|---|---|---|---|---|---|---|
| PrismaStats | 27,5 ms | 41,8 ms | 38,6 ms | 1,40× | 41,3 ms | sí | 52,3 ms | 72,9 → 91,5 ms |
| hono | 41,3 ms | 64,2 ms | 60,1 ms | 1,45× | 61,9 ms | sí | 56,2 ms | 100,4 → 119,9 ms |
| slurp | 74,5 ms | 103,6 ms | 100,1 ms | 1,34× | 111,8 ms | sí | 192,8 ms | 261,0 → 293,0 ms |
| tokio | 141,2 ms | 211,6 ms | 183,4 ms | 1,30× | 211,8 ms | sí | 275,3 ms | 388,5 → 458,7 ms |
| prometheus | 270,2 ms | 320,8 ms | 277,4 ms | 1,03× | 405,3 ms | sí | 483,8 ms | 750,1 → 761,2 ms |
| sqlalchemy | 687,3 ms | 853,4 ms | 899,8 ms | 1,31× | 1.030,9 ms | sí | 815,7 ms | 1.468,9 → 1.685,2 ms |

La tabla fija de §6 no tiene límites para este caso.

## Regla de decisión: rama 2, la excepción

- **Los límites originales no se cumplen:** hono en frío da 1,54× la v1.3 (límite 1,5×). La tabla
  fija dice lo mismo: 64,4 ms frente a 63.
- **La excepción sí se cumple:**
  - la primera consulta en frío no pasa de 1,67× en ningún grafo (hono, 1,54×);
  - en caliente y tras un refresco se cumplen los límites en los seis grafos.
- **Se fusiona.** La excepción y su motivo constan en `orden-v1.4.md` y deben constar en las notas de
  la versión.

**Motivo de la excepción**, medido por partes en hono en un proceso nuevo:

| Parte | v1.3 | v1.4 sin optimizar | v1.4 |
|---|---|---|---|
| Puntuar | 32,5 ms | 47,8 ms | 49,1 ms |
| `build_context` | 8,2 ms | 19,3 ms | 14,7 ms |
| Nodos costeados | 119 | 1.267 | 133 |

- La optimización quitó casi todo el costeo inútil.
- Lo que queda es el scorer nuevo, que en la primera consulta construye una vez por grafo los tokens
  de nombres y rutas y las listas del paseo.
- Con ellas, cada consulta siguiente es más rápida que en la v1.3: en hono, de 16,5 a 11,6 ms; en
  los grafos grandes, de 2,5 a 3 veces.
- El resto de `build_context` son las cotas de los nodos que no caben y más relaciones costeadas.

**Efecto de la optimización en la primera consulta en frío**, sin optimizar → optimizada:

- PrismaStats, 42,1 → 38,6 ms;
- hono, 70,4 → 64,4;
- slurp, 95,0 → 92,6;
- tokio, 210,4 → 180,6;
- prometheus, 318,0 → 271,8;
- sqlalchemy, 836,6 → 816,0.

En caliente no cambia.

## Fuera de la regla: `slurp explain` en grafos de graphify

**Cómo se encontró.** Al reejecutar los ejemplos de README y USAGE para el release,
`slurp explain "recalcularPlayerStats"` ya no explica `recalcularPlayerStats()`. Explica el script
`scripts/recalcular-todos-los-clubs.ts`.

**Medido** (`~/slurp-bench/v14-latencia/tools/explain_resolution.py`). Para cada función con
etiqueta única se pide `explain` con su nombre sin paréntesis:

| Grafo | v1.3 | v1.4 |
|---|---|---|
| PrismaStats, graphify (340 funciones) | 288 (84,7 %) | **219 (64,4 %)** |
| PrismaStats, `slurp index` (432 funciones) | 431 (99,8 %) | 430 (99,5 %) |

**Causa: un fallo de `find_node` que ya estaba en la v1.3.**

- `find_node` quita los `()` de la consulta, pero no de la etiqueta.
- graphify etiqueta las funciones como `f()`, así que con ellas la coincidencia exacta nunca se da y
  decide la relevancia.
- En la v1.3 la relevancia acertaba el 85 % de las veces. Con los términos de ruta al peso del
  nombre, la v1.4 adelanta en un 20 % más de casos nodos cuya ruta comparte la palabra (aquí,
  `recalcular`).
- En los grafos de `slurp index` las etiquetas no llevan `()` y la coincidencia exacta funciona.

No lo cubren el criterio de §6 ni la regla de latencia.

**Arreglado en la v1.4.0** (decisión del autor, `aaebf8f`):

- las etiquetas se comparan tal cual y luego sin `()` final ni `.` inicial, con las mismas
  mayúsculas primero;
- PrismaStats graphify: 337 de 337 funciones cuyo nombre no comparte ningún otro nodo
  (v1.3: 288; v1.4 sin el arreglo: 219);
- PrismaStats `slurp index`: 431 de 432. `get` vuelve a resolverse (antes ganaba uno de ocho
  `GET`); `middleware` resuelve al módulo `middleware`, cuyo id coincide con el nombre: un id
  gana a toda etiqueta, por diseño.
