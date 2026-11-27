# Orden de los resultados (v1.4) — diseño

## Estado

- **Fecha y situación:** propuesta del 2026-11-27, **aprobada por el autor ese mismo día** con
  las condiciones de «Decisiones del autor».
- **Alcance:** solo diseño. No se ha cambiado el scorer.
- **Lo único medido:**
  - un diagnóstico de por qué fallan las preguntas de commits, hecho **solo sobre el grupo de
    derivación** del banco y sin modificar slurp;
  - la latencia de la v1.3 que sirve de referencia.
- **Versión:** todo con slurp v1.3.0 más #99, que solo toca textos.
- **Dónde está el material:**
  - los scripts del diagnóstico, en `~/slurp-bench/tools/diag_v14.py` y `diag_v14_areas.py`,
    fuera del repo;
  - sus resultados, en `~/slurp-bench/diag_v14*.json`.

## Decisiones del autor (2026-11-27)

**Variantes y rejillas** (§3): aprobadas, con una condición en V6:

- **La lista de palabras vacías es genérica del inglés, no sacada del vocabulario de los
  commits.** Queda fijada aquí y son solo palabras funcionales: artículos, preposiciones,
  conjunciones, pronombres, auxiliares y cuantificadores. Ningún verbo de commit (add, fix,
  support, handle…) está en ella.

  > a, an, the, and, or, but, nor, so, yet, if, then, than, as, at, by, for, from, in, into, of,
  > off, on, onto, out, over, to, up, with, without, within, about, above, after, against,
  > before, below, between, during, through, under, until, via, per, i, me, my, we, us, our,
  > you, your, he, him, his, she, her, it, its, they, them, their, this, that, these, those,
  > who, whom, which, what, whose, am, is, are, was, were, be, been, being, do, does, did,
  > doing, have, has, had, having, will, would, shall, should, can, could, may, might, must,
  > there, here, when, where, why, how, all, any, both, each, few, more, most, other, some,
  > such, no, not, only, own, same, too, very, just, also

- **Si V6 mejora el banco pero empeora alguna pregunta etiquetada de PrismaStats o slurp, no
  entra.** «Empeora» quiere decir que su recall a 4000 en markdown baja con V6 frente a la misma
  combinación sin V6, mirando las 31 preguntas una a una.

**Criterio** (§6): aprobado tal cual.

**Regla del docstring**, fijada antes de medir:

- Se decide con la combinación elegida, comparando el recall medio a 4000 en markdown de los 7
  proyectos de derivación.
- Si imprimir la primera línea (V1a) cuesta **menos de 0,01** frente a usarla solo para puntuar
  (V1b), se imprime: R(V1b) − R(V1a) < 0,01.
- Si cuesta 0,01 o más, solo para puntuar.

**Reserva para un segundo intento**, elegida y congelada ahora, antes de abrir nada:

- Solo se usa si la validación falla.
- Generador congelado (`questions.py`, `cfeaddc`), semilla 12, 120 preguntas por proyecto, con
  los mismos filtros.
- Las preguntas se generan desde estos HEAD exactos; no se ha generado ninguna todavía.

| Proyecto | Lenguaje | Licencia | HEAD | Nodos | Candidatos | Commits que pasan la etapa A |
|---|---|---|---|---|---|---|
| httpx | Python | BSD-3-Clause | `e347fb5e93b9` | 1.784 | 544 | 506 |
| gson | Java | Apache-2.0 | `d9b5079eaa45` | 8.190 | 1.856 | 730 |
| echo (labstack) | Go | MIT | `59aacddc4a13` | 2.545 | 698 | 635 |
| zod | TypeScript | MIT | `d40c32991043` | 4.002 | 2.273 | 424 |
| axum | Rust | MIT | `8d8944e6db1a` | 4.570 | 1.881 | 244 |

**Dos precisiones de la medición**, anotadas antes de medir:

- **Docstrings sin reindexar con la rama.** Se aplica la extracción de la rama #21
  (`ast.get_docstring` y `_doc_comment_above`, copiadas tal cual) a los grafos de los padres de la
  v1.3, en vez de reindexar con el indexador de la rama, que parte de la v1.2.
  - Comprobado: el indexador de la rama da exactamente los mismos nodos que el de la v1.3 en hono,
    jsoup y sqlalchemy (2.880, 8.138 y 59.518, ninguna diferencia).
  - Así lo único que cambia es la documentación, y las aristas siguen siendo las del banco.
  - El docstring completo va a un atributo que solo lee el scorer experimental. En V1a,
    `description` lleva su primera línea, que es lo que imprimiría la rama.
- **V2: empates del PPR.** Los nodos que el PageRank personalizado no alcanza empatan a 0. Esos
  empates exactos se ordenan por el PageRank global con un peso de 1e-6, para no dejarlos al
  orden de los ids. No cambia ningún otro orden.

## Regla de decisión tras la latencia (autor, 2026-11-27), fijada antes de volver a medir

La implementación (rama `v1.4-orden`) cumple el recall y en caliente, pero no la latencia en
frío en hono ni en tokio (`orden-v1.4/resultados/latencia.md`). El autor pide optimizar
`budget.py` sin cambiar la salida y fija esta regla antes de medir.

**Condición previa: la salida no cambia.** Sobre las 1.556 preguntas (las 1.525 del banco y las
31 etiquetadas), la selección, los tokens y el texto son idénticos byte a byte antes y después de
la optimización. Un test lo vigila sobre una muestra.

**Tres casos de latencia, todos con el método corregido:**

- **En frío:** un proceso nuevo por grafo y versión, con el codificador de tokens precargado, y
  la primera consulta sobre el grafo recién cargado. Es la mediana de 3 ejecuciones.
- **En caliente:** la mediana de 20 consultas, tras una de calentamiento.
- **Tras un refresco del servidor MCP:** sobre una copia del proyecto con el grafo propio de
  slurp, en un proceso en marcha que ya ha respondido. Se edita un fichero fuente, el servidor
  refresca (con `GraphState.prepare`, que cambia el grafo y vacía sus cachés como en
  producción) y se mide la primera llamada a `slurp_query` sobre el grafo nuevo.
  - Se mide solo la consulta, sin el refresco, que es el mismo en las dos versiones porque el
    indexador no cambia. Es la medida más estricta. El tiempo total (refresco más consulta) se
    informa aparte.
  - Es la mediana de 3 ediciones por grafo.
  - Su límite es 1,5 × la v1.3 medida igual, porque es la primera consulta sobre un grafo recién
    cargado, la misma condición que el límite en frío de §6.

**La v1.3 de referencia** sale del tag `v1.3.0`, con `git archive`, y se mide en la misma sesión.

**Regla:**

1. **Si cumple los límites originales, se fusiona.** Los límites son:
   - en frío y en caliente, los de §6;
   - tras un refresco, 1,5 × la v1.3.
2. **Si no, se acepta una excepción solo para la primera consulta en frío**, con dos condiciones:
   - que no pase de 1,67 × la v1.3 medida con el método corregido;
   - que en caliente y tras un refresco cumpla los límites en los seis grafos.

   La excepción y su motivo se documentan aquí y en las notas de la versión.
3. **Si tampoco cumple eso, no se fusiona** y se para.

**Cómo se leen los límites (autor, 2026-11-27), fijado antes de volver a medir:**

- **Deciden los cocientes frente a la v1.3 medida en esta misma sesión**, con el mismo método y en
  las mismas condiciones, como dice el criterio original de §6:
  - en caliente, como mucho 1,25 × la v1.3, con un margen absoluto de 5 ms: el límite es el
    mayor de 1,25 × v1.3 y v1.3 + 5 ms;
  - en frío, como mucho 1,5 × la v1.3; la excepción de la regla 2, hasta 1,67 ×, vale solo para
    la primera consulta en frío;
  - tras un refresco, como mucho 1,5 × la v1.3.
- **La tabla fija de §6 se informa, pero no decide.** Sus límites aplican los cocientes a la v1.3
  medida al diseñar, en otra sesión. `resultados/latencia.md` comparó con ella; con la v1.3
  medida al lado, el cociente es la comparación que el criterio pide.
- La primera tanda de esta sesión se descartó sin usarla: con un modelo de Ollama cargado, la
  memoria del equipo estaba bajo presión y las cifras salían hasta 4 veces más altas que en
  `latencia.md` (tokio v1.3 en frío: 510 ms frente a 131 ms).

**Resultado (2026-11-27): rama 2, la excepción.** Detalle en `resultados/latencia-optimizada.md`.

- **La salida no cambia:** idéntica byte a byte en las 1.556 preguntas.
- **No cumple los límites originales** solo en un punto: hono en frío, 1,54 × la v1.3.
- **La excepción sí se cumple:** en frío ningún grafo pasa de 1,67 ×, y en caliente y tras un
  refresco cumplen los seis.
- **Motivo de la excepción:** lo que queda en hono es el scorer nuevo, que en la primera consulta
  construye una vez por grafo los tokens de nombres y rutas y las listas del paseo. Gracias a eso, en
  caliente es más rápido que la v1.3: en hono, de 16,5 a 11,6 ms.

## Por qué

El estudio del presupuesto óptimo (`docs/design/presupuesto-optimo/resultados/conclusiones.md`)
dejó claro que la palanca es el orden, no el presupuesto:

- **El primer relevante cae lejos.** Con preguntas que piden la función que cambió un commit,
  cae en la mediana entre el puesto 4 y el 270 de los candidatos. Con las preguntas de
  arquitectura etiquetadas, en el 3,5–4.
- **En 4000 caben unos 110 nodos**, así que lo que cae más abajo solo entra subiendo el
  presupuesto: unos 10 puntos de recall por cada duplicación, sin techo hasta 32000.

**Cifras de partida en el banco** (v1.3, recall a 4000 en markdown, media por proyecto):

| Grupo | Recall a 4000 md | MRR | Recall sin las preguntas que nombran el símbolo |
|---|---|---|---|
| Derivación (los 7 con 40 preguntas o más) | **0,532** | 0,114 | 0,470 |
| Validación (6 proyectos) | **0,543** | 0,165 | 0,420 |
| Referencia PrismaStats (15, arquitectura) | 0,967 | 0,405 | — |
| Referencia slurp (16) | 0,875 | 0,397 | — |

**Por proyecto:**

- Derivación: ky 0,901, PrismaStats 0,413, hono 0,670, jsoup 0,525, caddy 0,381,
  tokio 0,461, sqlalchemy 0,372. slurp, que se informa aparte, 0,773.
- Validación: gin 0,779, click 0,610, ripgrep 0,421, excalidraw 0,428, commons-lang 0,678,
  prometheus 0,344.

---

## 1. Cómo ordena hoy slurp (leído del código, con certeza)

- **`score = 0,4 · PageRank + 0,6 · TF-IDF + 0,3`.** El +0,3 se da a todo nodo de código, con
  tope 1,0 (`scorer._compute_scores`).
  - En un grafo de `slurp index` todos los nodos son código, así que el +0,3 no separa nada.
  - El **suelo** de un nodo sin ningún término de la consulta es 0,30 más su PageRank.
- **El PageRank es global.** Se calcula una vez por grafo y no depende de la consulta.
  - Entre los nodos del suelo, que son casi todos, decide el orden.
  - Favorece los nodos con muchas relaciones, no los cercanos a lo que se pregunta.
- **El TF-IDF compara la consulta con `label + description`.**
  - `slurp index` deja `description` vacía, así que en sus grafos **solo se mira el nombre del
    identificador**. Ni la ruta del fichero, ni la clase o el módulo que lo contiene, ni su
    docstring.
  - El coseno se restringe a los términos de la consulta.
  - No hay stemming: «headers» y «header» son términos distintos.
- **El impulso de vecino.** Un candidato presta score × `neighbor_decay` a como mucho 10
  vecinos. Solo entre candidatos, y solo un salto.
- **Cada consulta vuelve a tokenizar las etiquetas de todos los nodos**, porque `_tfidf_scores`
  reconstruye los documentos. Por eso la latencia crece con el tamaño del grafo. Cualquier
  variante que añada texto a cada nodo necesitará cachear esa tokenización por grafo.
- **Rama `v1.2-21-docstrings` (#21), sin fusionar:**
  - guarda docstrings, JSDoc, Javadoc y los comentarios de documentación de Go y Rust, enteros,
    en `description`;
  - el formatter imprime su primera línea, lo que cuesta unos 15 tokens más por nodo
    documentado en markdown;
  - con TF-IDF coseno no mejoró: PrismaStats bajó en MRR de 0,410 a 0,273, porque un docstring
    largo con varios términos de la consulta puntúa como un nombre que coincide, y los scripts
    con docstrings en inglés de `.agents/skills/` adelantaron al código de la aplicación.

## 2. Por qué fallan las preguntas de commits (diagnóstico en derivación)

**Muestra.** Son los 8 proyectos de derivación, 806 preguntas y 1.798 definiciones relevantes.
A 4000 en markdown, la v1.3 encuentra 929 (el 52 %) y pierde 869.

**Qué tiene cada definición en común con la consulta y con los 10 mejores candidatos.** Las
coincidencias de términos se miden con stemming ligero y quitando palabras vacías.

| Señal | Perdidas | Encontradas |
|---|---|---|
| TF-IDF = 0: ningún término de la consulta en el nombre | **94 %** | 36 % |
| Algún término de la consulta en el nombre | 6 % | 62 % |
| Lo mismo, con stemming | 10 % | 66 % |
| Algún término en su ruta o en la clase o módulo que la contiene | **57 %** | 56 % |
| Tiene comentario de documentación | 43 % | 47 % |
| Su documentación comparte algún término con la consulta | **22 %** | 36 % |
| Vecina por `calls` de alguno de los 10 mejores | 10 % | 32 % |
| Mismo fichero que alguno de los 10 mejores | **33 %** | 75 % |

**Lo que muestra la tabla.**

- **Las perdidas están en el suelo de scores.** Su posición mediana es 851 (cuartiles 347 y
  1.975).
- **El 75 % de las perdidas sin coincidencia en el nombre tiene al menos una de las otras
  señales:** ruta o ámbito, documentación, stemming, vecindad por llamadas o fichero.
- **La ruta sola no discrimina:** también la tienen las encontradas. Lo que importa es el tamaño
  de la zona a la que apunta:

| Proyecto | Candidatos | Zona por ruta (mediana) | Zona que cabe en 4000 (≤ 110) | Relevante dentro de la zona | Ficheros de los 10 mejores (mediana) | Relevante dentro de esos ficheros |
|---|---|---|---|---|---|---|
| ky | 52 | 2 | 68 % (32 % vacía) | 11 % | 32 | 80 % |
| PrismaStats | 567 | 30 | 84 % | 50 % | 86 | 32 % |
| hono | 1.012 | 40 | 82 % | 83 % | 84 | 55 % |
| slurp | 715 | 18 | 34 % (26 % vacía) | 49 % | 448 | 53 % |
| jsoup | 2.442 | 95 | 43 % | 43 % | 327 | 61 % |
| caddy | 2.185 | 161 | 42 % | 81 % | 167 | 41 % |
| tokio | 5.794 | 796 | 5 % | 95 % | 174 | 57 % |
| sqlalchemy | 12.448 | 378 | 19 % | 53 % | 595 | 47 % |

**Cómo se lee.**

- **En los proyectos medianos, la ruta acota una zona que cabe en el presupuesto** y suele
  contener lo que se busca: en PrismaStats y hono, 30–40 candidatos que contienen al relevante
  el 50–83 % de las veces.
- **En los grandes, la zona es de cientos de nodos.** Ayuda a acotar, pero no basta sin otra
  señal dentro de ella.
- **En caddy y tokio, la ruta comparte términos con la consulta en el 80–91 % de las perdidas.**
  Sus commits empiezan por la zona del código: `caddyhttp: …`, `rt: …`. Eso es una propiedad de
  las preguntas de commits; las consultas de un agente pueden no llevarla (§8).

## 3. Variantes a probar

Cada variante cambia una parte del score y deja intacto el resto, incluida la selección
(`build_context`).

- La parte léxica es el 0,6 del score; la estructural, el 0,4. El +0,3 de código y el
  `min_score` se mantienen.
- La puntuación léxica de una variante se normaliza a 0–1 por consulta (dividiendo por el máximo
  de esa consulta), para que los pesos sigan significando lo mismo.
- Las rejillas de parámetros quedan fijadas aquí.

### V1. BM25 con docstrings (rama `v1.2-21-docstrings`)

**Qué cambia.**

- El TF-IDF coseno se sustituye por BM25 con dos campos: el nombre (con peso w\_n) y la
  documentación (peso 1).
- Los docstrings salen del indexador de la rama #21.

**Por qué.**

- El 43 % de las perdidas tiene documentación, y el 22 % comparte algún término con la consulta.
- BM25 satura la frecuencia (k1) y normaliza por longitud (b). Ataca justo lo que hundió a #21
  con el coseno: un docstring largo puntuaba como un nombre exacto.

**Rejilla.**

- k1 ∈ {0,9; 1,2; 1,5}, b ∈ {0,5; 0,75}, w\_n ∈ {2; 3}: 12 configuraciones.
- Con la mejor, dos controles:
  - **V1-0:** BM25 solo sobre el nombre, para separar el efecto de BM25 del de los docstrings;
  - **V1a frente a V1b:** con el docstring impreso (su primera línea, como en la rama) o usado
    solo para puntuar. Imprimirlo cuesta unos 15 tokens por nodo, y por tanto menos nodos en
    4000.

**Coste.** Hay que reindexar los grafos de los padres con el indexador de la rama, en un worktree
aparte. No cambia ningún id de nodo.

### V2. PageRank personalizado dirigido (PPR)

**Qué cambia.** El PageRank global (el 0,4) se sustituye por un PageRank personalizado para cada
consulta:

- reinicia en los k nodos con más score léxico, en proporción a ese score;
- recorre las aristas en su sentido: `calls` de quien llama a quien es llamado con peso 1, y al
  revés con peso r; `contains` en los dos sentidos con peso 1.
- Si la consulta no coincide léxicamente con ningún nodo, se usa el PageRank global, como hoy.

**Por qué.** El orden del suelo lo decide hoy un PageRank que no sabe nada de la consulta. Un PPR
pondría delante lo que está cerca de lo que coincide: vecinos por llamadas (10 % de las
perdidas) y hermanos de fichero o de clase a dos saltos por `contains` (33 %).

**Rejilla.** Probabilidad de reinicio ∈ {0,15; 0,3; 0,5}, r ∈ {0,5; 1,0}, k ∈ {10; 30}: 12
configuraciones.

**Coste.**

- No hay que reindexar.
- **Riesgo de latencia:** un PPR completo por consulta recorre todas las aristas en cada
  iteración (75.000 en sqlalchemy). Se implementa con empuje local, al estilo de Andersen-Chung-
  Lang, con ε = 1e-4, que solo toca la vecindad de las semillas.

### V3. Rutas y ámbito como términos

**Qué cambia.** El documento de cada nodo gana un segundo campo, con peso w\_p frente al nombre:

- los términos de la ruta de su fichero (directorios y nombre sin extensión), quitando los
  genéricos: `src`, `lib`, `index`, `main`, `internal`, `pkg`, `mod`, `util(s)`, `core`,
  `test(s)`, `org`, `com` y las extensiones;
- los de la clase o el módulo que lo contiene, sacados del id.

**Por qué.**

- El 57 % de las perdidas comparte algún término de ruta o de ámbito con la consulta.
- En los proyectos medianos, la zona resultante cabe en 4000 y contiene al relevante el 50–83 %
  de las veces (§2).

**Rejilla.** w\_p ∈ {0,25; 0,5; 1,0}, sobre el TF-IDF de la v1.3 y sobre la mejor V1: 6
configuraciones.

**Coste.** No hay que reindexar: `source_file` y el id ya están en el grafo.

### V4. Cohesión de fichero y de clase

**Qué cambia.** Un candidato toma como score léxico el mayor entre el suyo y λ × el mejor score
léxico de su fichero o de su clase.

**Por qué.** El 33 % de las perdidas está en el fichero de alguno de los 10 mejores. El impulso
de vecino actual no llega: va por relaciones, solo a un salto y como mucho a 10 vecinos.

**Rejilla.** λ ∈ {0,3; 0,5; 0,7}: 3 configuraciones.

**Coste.** No hay que reindexar.

### V5. Stemming ligero

**Qué cambia.** Se quitan -s, -es, -ies, -ing y -ed a los términos de más de 4 letras, en la
consulta y en los documentos.

**Por qué.** Es barato, pero el diagnóstico solo lo respalda en parte: lleva la coincidencia en
el nombre de las perdidas del 6 al 10 %. La ganancia esperada es pequeña; se prueba porque se
combina con V1 y V3.

**Rejilla.** 1 configuración.

### V6. Palabras vacías de commit en la consulta

**Qué cambia.** Se quitan de la consulta verbos y palabras genéricas: add, fix, support, allow,
handle, use, make, ensure, avoid, when, instead, new…, con una lista fija.

**Por qué.**

- El IDF ya rebaja los términos frecuentes en el grafo, pero no los frecuentes en las preguntas.
- Cuando una de esas palabras coincide con un identificador (`handle`, `use…`), el TF-IDF pone
  delante nodos que no tienen que ver.
- Está por verificar cuánto pesa.

**Rejilla.** 1 configuración.

### Combinaciones

Selección hacia delante en derivación:

1. Se empieza por la mejor variante sola.
2. Se añade otra, con su mejor parámetro, solo si:
   - sube el recall medio a 4000 en derivación al menos **+0,01**;
   - ningún proyecto de derivación pierde más de 0,03.
3. Al añadirla se puede reajustar su propio parámetro dentro de su rejilla.
4. Se para cuando ninguna aporta +0,01. A igualdad, gana la combinación con menos variantes.

### Descartadas, y por qué

- **Embeddings** (`--backend openai` ya existe): necesitan red, clave y coste por consulta. No
  sirven de opción por defecto.
- **Reordenar con un LLM:** mismo motivo, más la latencia.
- **Aprender a ordenar con el banco:** con 1.525 preguntas de un estilo concreto (asuntos de
  commit) se sobreajustaría. Podría plantearse después, con más fuentes de preguntas.
- **Tocar el +0,3 de código:** en los grafos de `slurp index` no ordena nada, y en los de
  graphify es lo que separa el código de los documentos. No es la causa.

## 4. Lo que se sabe y lo que hay que verificar

| Afirmación | Estado | Base |
|---|---|---|
| El TF-IDF de los grafos de `slurp index` solo mira el nombre del identificador | **Cierto** | Código: `description` vacía, `_tfidf_scores` |
| El orden del suelo lo decide un PageRank global, sin la consulta | **Cierto** | Código |
| El 94 % de las definiciones perdidas no comparte ningún término de la consulta con su nombre | **Medido** (derivación) | `diag_v14.py` |
| El 75 % de esas tiene alguna otra señal (ruta o ámbito, documentación, fichero, llamadas, stemming) | **Medido** (derivación) | `diag_v14.py` |
| En PrismaStats y hono, la zona por ruta cabe en 4000 y contiene al relevante el 50–83 % de las veces | **Medido** (derivación) | `diag_v14_areas.py` |
| Que una señal exista no significa que suba al relevante por encima del resto: también la tienen las encontradas (la ruta, el 56 %) | **Cierto**, y por eso hay que medir | `diag_v14.py` |
| BM25 evita el fallo de #21 con docstrings largos | **Por verificar** | Es teoría: saturación y normalización |
| El PPR adelanta a los hermanos y vecinos de lo que coincide | **Por verificar** | — |
| El PPR con empuje local cabe en el límite de latencia en sqlalchemy | **Por verificar** | — |
| Las rutas como términos no empeoran las preguntas de arquitectura | **Por verificar** | Lo vigila la condición de §6 |
| Cachear la tokenización por grafo hace sitio a documentos más ricos sin perder latencia | **Por verificar** (probable) | Hoy se tokeniza todo el grafo en cada consulta |
| Lo que mejore en preguntas de commits mejora las consultas reales de un agente | **Por verificar**, y el banco no puede decirlo | Ver §8 |

## 5. Banco y procedimiento

**El banco queda congelado.**

- Las 1.525 preguntas y sus grafos de padre, de `~/slurp-bench/results/`.
- Los grupos del estudio del presupuesto:
  - **Derivación:** ky, PrismaStats, hono, jsoup, caddy, tokio y sqlalchemy (decide).
  - **slurp** (38 preguntas): se informa, pero no decide.
  - **Validación:** gin, click, ripgrep, excalidraw, commons-lang y prometheus.
- La validación del banco se abrió en el estudio del presupuesto, pero solo con cifras
  agregadas por proyecto: curvas de recall, mediana del puesto del primer relevante y recall
  según si la pregunta nombra el símbolo. El diagnóstico definición a definición de §2 se hizo
  solo sobre derivación.

**El arnés es el del estudio** (`docs/design/presupuesto-optimo/measure.py`), con la puntuación
inyectada.

- Cada variante es una función `(G, consulta) → scores` que vive en
  `docs/design/orden-v1.4/variants.py`, fuera de slurp.
- La selección es la misma `build_context` de la v1.3: un dict de scores es lo único que recibe.
- La variante V0, que reproduce la v1.3, tiene que dar **exactamente** las cifras ya medidas del
  banco antes de medir ninguna otra.

**Fases.**

- **A.** Las variantes que no necesitan reindexar: V2–V6 y V1-0. Sobre los grafos de la v1.3,
  solo en derivación. Cada grafo se carga una vez por pregunta y se evalúan todas las
  configuraciones.
- **B.** Se reindexan los padres de derivación con el indexador de #21 y se mide V1.
- **C.** Combinaciones (§3) y medida de latencia del candidato.
- **D.** **Puerta en derivación** (§6). Si el candidato no la pasa, se para y se informa sin
  abrir la validación.
- **E.** Validación, abierta **una vez**, con un solo candidato: se reindexan los padres de
  validación si el candidato usa docstrings. Además se miden los juegos de referencia y la
  latencia.
- **F.** Solo si se acepta: se implementa en slurp como tareas aparte, con tests primero. Antes
  de cualquier release, el banco tiene que dar con el código implementado las mismas cifras que
  dio el arnés.

**Si la validación falla, no se reajusta sobre ella.** Un segundo candidato solo podría validarse
en una reserva nueva:

- httpx (Python, 1,8k nodos) y gson (Java, 8,2k nodos), medidos pero sin usar en el banco;
- y 2–4 proyectos más (Go, TS, Rust), con preguntas del generador congelado.

## 6. Criterio de aceptación, fijado antes de medir

**Métrica principal.** Recall a 4000 en markdown, media por proyecto (cada proyecto pesa lo mismo).

**Puerta en derivación, antes de abrir la validación.** El candidato tiene que cumplir las tres:

- sube el recall medio de los 7 proyectos de derivación al menos **+0,05**: de 0,532 a 0,582 o
  más;
- ningún proyecto de derivación pierde más de 0,03;
- cumple el límite de latencia de abajo.

**Aceptación en validación** (se abre una vez). Tiene que cumplir las cuatro:

1. **Mejora media ≥ +0,05** en los 6 proyectos de validación: de 0,543 a **0,593 o más**. Es
   media duplicación de presupuesto, sin gastar un token más.
2. **Ningún proyecto de validación pierde más de 0,03**, unas 4 preguntas de 120.
3. **Latencia.**
   - La mediana en caliente de una consulta (puntuar y seleccionar con 4000 en markdown) no pasa
     de **1,25× la de la v1.3**, con un margen absoluto de +5 ms para los grafos pequeños. Se
     mide en los grafos del HEAD de PrismaStats, hono, slurp, tokio, prometheus y sqlalchemy.
   - La primera consulta sobre un grafo recién cargado, con cachés incluidas, no pasa de 1,5×.
   - Si cambia el indexador, el tiempo de `slurp index` sobre los 14 HEAD no pasa de 1,25×.

   | Grafo (HEAD) | Nodos | v1.3 en caliente | Límite | v1.3 en frío | Límite |
   |---|---|---|---|---|---|
   | PrismaStats | 1.581 | 10 ms | 15 ms | 80 ms | 120 ms |
   | hono | 2.880 | 17 ms | 22 ms | 42 ms | 63 ms |
   | slurp | 5.576 | 33 ms | 41 ms | 75 ms | 113 ms |
   | tokio | 14.328 | 75 ms | 94 ms | 131 ms | 197 ms |
   | prometheus | 21.280 | 113 ms | 141 ms | 285 ms | 428 ms |
   | sqlalchemy | 59.518 | 285 ms | 356 ms | 669 ms | 1.004 ms |

4. **No empeorar las preguntas de arquitectura.** Los juegos etiquetados se informan aparte, y
   ninguno puede perder más de una pregunta en recall a 4000 markdown:
   - PrismaStats: de 0,967 a 0,900 o más;
   - slurp: de 0,875 a 0,8125 o más.

**Se informa siempre, sin que decida:**

- el MRR (validación, de partida 0,165);
- el recall a 4000 en JSON y a 8000 y 16000 en markdown;
- el recall de las preguntas que no nombran el símbolo (validación, de partida 0,420);
- las cifras de derivación y de slurp;
- los tokens por nodo, que cambian si se imprime el docstring.

## 7. Coste y tiempo en esta máquina

**Fase A.** Unas 35 configuraciones de una variante sola, sobre las 806 preguntas de derivación.

- Cada grafo se carga una vez; cada configuración cuesta puntuar (10–300 ms según el grafo) más
  una `build_context` (20–60 ms).
- Unos 30 minutos con 4 procesos. sqlalchemy domina, con unos 12 s por pregunta.

**Fase B.** Reindexar 806 padres con el indexador de #21 lleva unos 15 minutos con 4 procesos;
medir V1, unos 10.

**Fase C.** De 10 a 20 combinaciones: unos 15 minutos.

**Fase E.** Reindexar los padres de validación, si hace falta, más una configuración: de 20 a 30
minutos. Las referencias y la latencia, minutos.

**Total.** De 2 a 3 horas de cálculo y entre 0,6 y 1 GB más de disco, para los grafos con
docstrings comprimidos con gzip. Lo que no es cálculo: escribir las variantes con sus tests, y el
análisis.

## 8. Riesgos y límites

- **El estilo de las preguntas.** Los asuntos de commit llevan a menudo la zona delante
  (`router: …`), y eso favorece a V3. Las consultas de un agente («how an uploaded stats file is
  validated») no siempre la llevan. Lo mitigan dos cosas:
  - la condición de no empeorar los juegos de arquitectura;
  - el máximo de pérdida por proyecto en validación, que incluye proyectos sin prefijos de zona
    (click, excalidraw).

  Aun así, el banco no puede demostrar la mejora para consultas de agente.
- **Los grafos de graphify no están en el banco.** Sus `description` están escritas por un LLM, y
  las variantes léxicas les afectan distinto. Los juegos de referencia usan grafos de
  `slurp index`.
- **`.agents/skills/` en PrismaStats:** scripts vendorizados con docstrings en inglés. Es la causa
  conocida del fallo de #21, y sigue pendiente decidir si `slurp index` debe saltarlos. Se informa
  del resultado de V1 con ese directorio y sin él, sin que eso cambie el criterio.
- **Latencia del PPR:** riesgo real en sqlalchemy. Si el empuje local no cabe en el límite, V2 sale
  de la combinación, aunque mejore el recall.
- **Selección por recall a 4000 en markdown.** Una variante que solo mejore otros presupuestos no
  se elige.
- **Sobreajuste a derivación.** Las rejillas son pequeñas y están fijadas aquí, la combinación
  exige +0,01 por variante y la validación se abre una vez.

## 9. Decisiones del autor que hacen falta antes de medir

1. **Las variantes y sus rejillas** (§3). ¿Valen? ¿Falta o sobra alguna?
2. **El criterio** (§6). ¿Valen estos valores?
   - mejora media de +0,05;
   - pérdida máxima de 0,03 por proyecto;
   - latencia de 1,25× en caliente (con +5 ms de margen) y 1,5× en frío;
   - como mucho una pregunta perdida en cada juego de arquitectura;
   - la puerta en derivación antes de abrir la validación.
3. **V1a o V1b:** ¿se acepta imprimir la primera línea del docstring, si gana, con su coste en
   tokens por nodo?
4. **La reserva para un segundo intento** (§5): httpx, gson y 2–4 proyectos nuevos. ¿Vale?
