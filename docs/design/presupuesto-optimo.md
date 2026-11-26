# Presupuesto óptimo según el tipo y el tamaño del proyecto — diseño (fase 1)

## Estado

- **Fecha y situación:** propuesta del 2026-11-26, **aprobada por el autor ese mismo día** con
  la variante del corte por relevancia (§4.5). Ver «Decisiones del autor» abajo.
- **Alcance de la fase 1:** solo diseño. No se midió el recall de ningún proyecto open source.
- **Fase 2, ejecutada el 2026-11-26.** Resultados en `presupuesto-optimo/resultados/`:
  - `conclusiones.md`, con la recomendación;
  - `tablas.md`, las curvas en SVG y `puerta.md`.
- **En resumen:**
  - la regla pasa su criterio, pero solo por la censura, así que no se recomienda;
  - el corte por relevancia no es elegible;
  - el 4000 se mantiene.
- **Lo único medido hasta ahora:**
  - tamaños con `slurp index`;
  - los filtros de commits;
  - tres muestras de 20 preguntas revisadas a mano;
  - una curva piloto sobre los dos juegos de preguntas de referencia (PrismaStats y slurp), que ya se usaron en la v1.2.
- **Versión:** todo con slurp v1.3.0 (`2a5daa9`).
- **Dónde está el material:**
  - los repos, en `~/slurp-bench/repos/`, fuera de este repo;
  - el prototipo del generador de preguntas, en `~/slurp-bench/tools/questions.py`;
  - las muestras, en `~/slurp-bench/questions/`.

## Decisiones del autor (2026-11-26)

- **Proyectos y grupos** (§1): aprobados.
- **Criterio y márgenes** (§4): aprobados tal cual.
- **Rejilla de presupuestos** (§3): aprobada, con 64000 solo como control.
- **Comprobación con un LLM** (§5): sí, pero solo si se acepta una regla, con `claude -p` y la
  suscripción del autor. Se prepara, pero no se lanza sin avisarle.
- **Variante nueva: corte por relevancia** (§4.5). slurp deja de añadir nodos cuando la
  relevancia de lo que queda cae por debajo de un umbral, con el presupuesto como techo.
  - Se proponen tres formas de definir el umbral, cada una con su criterio fijado antes de medir.
  - Debe perder como mucho lo mismo que la regla y ahorrar al menos un 25 % de tokens frente a
    ella.
  - Se ajusta solo en el grupo de derivación.
- **Sin cambios en el código de slurp** hasta ver los resultados.

## La pregunta

¿Qué presupuesto es el óptimo según el tipo y el tamaño del proyecto? Es decir, el menor
presupuesto con el que slurp no pierde contexto relevante, y por encima del cual dar más tokens
ya no encuentra nada nuevo.

Hoy el presupuesto por defecto es 4000 para todo, en CLI y MCP. Se revisó en la v1.1.0 y en la
v1.2, pero solo con PrismaStats y slurp (31 preguntas en total).

## Resumen de la propuesta

- **Proyectos:** 14 en total, 12 open source más PrismaStats y slurp, con Python, TypeScript,
  Go, Rust y Java. Van de 673 a 59.518 nodos. Se separan ahora en dos grupos: 8 para sacar la
  regla (derivación) y 6 para validarla.
- **Preguntas:** hasta 120 por proyecto, sacadas del historial de git.
  - La pregunta es el asunto del commit.
  - Los nodos relevantes son las definiciones que el commit modifica, en el grafo construido
    sobre su commit padre.
- **Medida:** recall y tokens reales en 11 presupuestos de 1000 a 32000, en markdown y JSON, más
  un punto de control en 64000.
- **Óptimo:** el menor presupuesto que alcanza el 95 % del recall máximo del proyecto.
- **Regla:** una función de lo que slurp sabe antes de la consulta.
- **Aceptación:** la regla solo se acepta si en el grupo de validación pierde como mucho 3 puntos
  de recall por celda (1 de media) frente al óptimo de cada proyecto, sin gastar de media más de
  1,5 veces ese óptimo.
- **Coste:** unas 2,6 horas de cálculo en esta máquina (unos 45 minutos con 4 procesos) y unos
  2,5 GB de disco además de los repos ya clonados.

---

## 1. Proyectos

### 1.1 Tabla de proyectos

Todo está medido con `slurp index` v1.3.0 sobre el HEAD del 2026-11-26, con las 10 gramáticas
instaladas.

**Qué significa cada columna:**

- **Nodos:** los del grafo completo.
- **Candidatos:** los nodos que pueden entrar en un subgrafo por defecto, es decir, sin tests
  ni imports.
- **Tests:** la proporción de nodos que son de test.
- **Tokens:** el grafo entero impreso como respuesta (`full_graph_tokens`), en markdown / JSON.
  Es lo que se mandaría sin slurp.
- **Índice:** el tiempo de un índice completo.
- **Historial:** commits sin merge en la ventana (§2.2) → los que pasan la etapa A → las
  preguntas que se esperan.

| Grupo | Proyecto (HEAD) | Lenguaje | Licencia | Nodos | Candidatos | Tests | Tokens md / JSON | Índice | Historial |
|---|---|---|---|---|---|---|---|---|---|
| D | ky (`3541888`) | TypeScript | MIT | 673 | 204 | 52 % | 31.735 / 70.886 | 0,44 s | 592 (todo) → 276 → 120 |
| D | PrismaStats (`e975140`) | TypeScript (Next.js) | privado | 1.581 | 888 | 1 % | 90.839 / 184.762 | 0,44 s | 148 (todo) → 73 → ~51 |
| D | hono (`9c2f2948`) | TypeScript | MIT | 2.880 | 1.267 | 37 % | 138.244 / 303.338 | 0,94 s | 1.630 → 828 → 120 |
| D | slurp (`2a5daa9`) | Python | MIT | 5.576 | 998 | 73 % | 436.695 / 881.522 | 0,74 s | 175 (todo) → 65 → ~46 |
| D | jsoup (`d90d5752`) | Java | MIT | 8.138 | 2.954 | 52 % | 675.830 / 1.270.180 | 0,75 s | 662 → 260 → 120 |
| D | caddy (`679806f9`) | Go | Apache-2.0 | 8.360 | 2.912 | 41 % | 519.866 / 1.051.052 | 1,38 s | 1.080 → 736 → 120 |
| D | tokio (`97c2e851`) | Rust | MIT | 14.328 | 6.197 | 37 % | 812.952 / 1.678.905 | 1,85 s | 1.230 → 593 → 120 |
| D | sqlalchemy (`9c88919be`) | Python | MIT | 59.518 | 13.058 | 62 % | 3.739.593 / 7.617.074 | 5,21 s | 1.318 → 632 → 120 |
| V | gin (`0f09c3a`) | Go | MIT | 2.059 | 623 | 59 % | 85.938 / 204.580 | 0,37 s | 1.807 (todo) → 637 → 120 |
| V | click (`08b803c`) | Python | BSD-3 | 2.841 | 749 | 58 % | 138.551 / 308.171 | 0,42 s | 602 → 187 → 120 |
| V | ripgrep (`3e85c6c`) | Rust | Unlicense/MIT | 4.000 | 2.971 | 18 % | 243.326 / 529.721 | 0,68 s | 433 → 158 → ~111 |
| V | excalidraw (`dabd83c4`) | TypeScript (React) | MIT | 8.326 | 3.337 | 21 % | 559.629 / 1.100.881 | 1,62 s | 879 → 458 → 120 |
| V | commons-lang (`657ebb3d9`) | Java | Apache-2.0 | 19.154 | 5.098 | 67 % | 1.544.849 / 2.858.474 | 3,80 s | 2.683 → 668 → 120 |
| V | prometheus (`1e794b83a`) | Go (+9 % TS) | Apache-2.0 | 21.280 | 8.484 | 38 % | 1.249.525 / 2.621.443 | 7,02 s | 5.191 → 1.990 → 120 |

### 1.2 Por qué cada proyecto

**Grupo de derivación**

- **ky:** el extremo pequeño, un cliente HTTP de menos de 1.000 nodos. Su historial es corto
  (592 commits), así que se usa entero.
- **PrismaStats:** la aplicación Next.js del autor y referencia con preguntas etiquetadas a mano.
  - Tiene solo 148 commits, con mensajes en español.
  - Da pocas preguntas: unas 51.
- **hono:** framework web con adaptadores para varios runtimes.
  - Sus commits convencionales con ámbito (`fix(router): …`) dan preguntas con la zona del
    código.
  - Son PR pequeñas y enfocadas.
- **slurp:** la referencia con 16 preguntas que etiquetó Claude Code. Tiene 175 commits, muchos
  de documentación.
- **jsoup:** parser de HTML en Java, con mensajes descriptivos. La etapa A quita 147 commits de
  Dependabot.
- **caddy:** servidor web Go, modular. Los asuntos con prefijo de zona (`caddyhttp: …`) hacen que
  el 68 % pase la etapa A.
- **tokio:** runtime async de Rust, un workspace de varios crates. Es grande y usa el estilo
  `zona: mensaje`.
- **sqlalchemy:** el mayor, con 59.518 nodos. Sus mensajes son de estilo Gerrit, largos y
  técnicos.

**Grupo de validación**

- **gin:** framework web Go pequeño, con mucho commit de baja calidad (125 vagos y 125 de bots).
  Pone a prueba los filtros. Su historial se usa entero.
- **click:** librería CLI de Python, con historial de PR fusionadas por merge. Pasa la etapa A un
  31 %.
- **ripgrep:** Rust, casi de un solo autor, con prefijos de zona (`searcher: …`) y mensajes muy
  descriptivos.
- **excalidraw:** aplicación TS/React, como PrismaStats, con un grafo dominado por JSX. Hay que
  recordar que JSX no genera llamadas en el grafo.
- **commons-lang:** librería Java con 10.248 commits, muchos triviales (272 vagos, 245 por palabra
  clave). Es la prueba más dura de los filtros: solo pasa la etapa A un 25 %.
- **prometheus:** Go grande con una interfaz web en TS (9 % de los nodos), así que es
  multilenguaje.

### 1.3 Cómo se forman los grupos

Los grupos quedan fijados aquí y no se cambian.

- **Lenguajes:** cada grupo tiene los cinco lenguajes.
- **Tamaños:** el de validación (de 2,1k a 21k nodos) cae dentro del rango del de derivación (de
  0,7k a 59,5k). Así la validación interpola y no extrapola.
- **Proyectos grandes:** cada grupo tiene al menos dos de más de 10k nodos.
- **Referencias:** PrismaStats y slurp van a derivación porque sus preguntas ya se usaron para
  ajustar la v1.2.
- **Revisión:** las muestras revisadas a mano (§2.5) son de derivación.
- **Apertura de la validación:** se abre una sola vez, con la regla y su criterio ya escritos.

### 1.4 Descartados

- **httpx y gson:** medidos (1.784 y 8.190 nodos), pero repiten lenguaje y tamaño de click y
  jsoup.
- **guava y nest:** sus repos ocupan 1,4 GB y 484 MB.
- **Repos con licencia no permisiva:** MPL, BUSL, EPL.

---

## 2. Preguntas sin etiquetar a mano

### 2.1 Procedimiento

Para un commit C con padre P:

- **Pregunta:** el asunto de C, limpio (§2.2).
- **Nodos relevantes:** las definiciones cuyas líneas cambia C (funciones, métodos, clases,
  interfaces, structs, tipos…). Se toman como nodos del grafo de `slurp index` construido sobre
  P.
- **Grafo:** el de cada pregunta es el de su padre. La pregunta se hace sobre el código tal como
  estaba antes del cambio, que es cuando un agente consultaría slurp.

Hay dos etapas:

- **Etapa A:** usa solo el mensaje y la lista de ficheros. Sin checkout, tarda alrededor de 1 s
  por repo.
- **Etapa B:** usa el diff y el grafo del padre. Tarda de 0,4 a 7 s por commit, casi todo en
  `slurp index`.

### 2.2 Filtros

**Ventana.** Se toman los commits sin merge alcanzables desde HEAD en los últimos tres años.
Si salen menos de 400, se usa el historial entero; es el caso de ky, PrismaStats, slurp y gin.
Con la ventana, el tamaño del proyecto apenas varía entre preguntas.

**Limpieza del asunto.**

- `tipo(ámbito): texto` pasa a `ámbito: texto`, porque el ámbito nombra la zona, como haría
  quien pregunta.
- `zona: texto` se conserva tal cual.
- Se quitan las referencias (`#123`, `gh-123`, «fixes #…»), las URL y las etiquetas `[…]`.

**Etapa A: qué se descarta.**

| Regla | Detalle |
|---|---|
| Merges | `--no-merges`; los diffs, contra el primer padre |
| Bots | El autor contiene `[bot]`, dependabot, renovate, github-actions, pre-commit-ci o mergify |
| Tipo convencional | docs, chore, ci, build, style, test(s), release, revert, deps, bump, lint, format, typo, wip |
| Palabras clave en el asunto | bump, release, changelog, readme, docs, typo, lint, format, prettier/black/ruff/gofmt/rustfmt/clippy, whitespace, license, copyright, deps/dependency, revert, merge, backport/cherry-pick, test(s)/testing, mypy/pyright/flake8/pylint/eslint, coverage, benchmark(s) |
| Mensaje vago | Menos de 3 palabras de contenido tras quitar las vagas (fix, update, minor, cleanup, wip, typo, refactor, improve, misc, small, stuff…) y artículos y preposiciones |
| Asunto repetido | El segundo y siguientes |
| Sin fuente modificado | Ningún fichero fuente no-test modificado (solo altas, tests, docs o configuración) |
| Demasiados ficheros | Más de 5 ficheros fuente no-test, o más de 20 en total |
| Renombrados masivos | Más de 2 ficheros fuente renombrados o copiados |
| Cambio grande | Más de 300 líneas cambiadas en fuente no-test |

**Qué cuenta como fuente y como test.**

- Fuente es un fichero con una extensión que indexa slurp, fuera de los directorios que salta.
- Tampoco cuenta como fuente la configuración de build: `setup.py`, `noxfile.py`, `build.rs` y
  `*.config.js`, entre otros.
- Test es lo que dicen las reglas de `_is_test`.

**Etapa B: qué se descarta.**

- Cambios solo de espacios: `git diff -w` vacío.
- Cambios solo de comentarios o docstrings: el código coincide sin ellos. Se compara así:
  - Python: el AST sin docstrings;
  - TS/JS/Java/Rust: las hojas de tree-sitter sin comentarios;
  - Go: el texto sin comentarios.
- Ninguna definición cambiada: solo código nuevo, o código de módulo como imports o constantes.
- Más de 8 definiciones cambiadas.

**Cuánto queda tras la etapa A, por proyecto.**

| Proyecto | Ventana | Pasan A | Motivos principales de descarte |
|---|---|---|---|
| ky | 592 | 276 (47 %) | sin fuente modificado 230, vago 32, palabra clave 25 |
| PrismaStats | 148 | 73 (49 %) | más de 5 fuentes 22, más de 300 líneas 21, sin fuente 15 |
| hono | 1.630 | 828 (51 %) | sin fuente 481, palabra clave 90, más de 5 fuentes 61 |
| slurp | 175 | 65 (37 %) | sin fuente 47, más de 300 líneas 29, chore 17 |
| jsoup | 662 | 260 (39 %) | sin fuente 170, bot 147, vago 29 |
| caddy | 1.080 | 736 (68 %) | sin fuente 159, palabra clave 41, bot 41 |
| tokio | 1.230 | 593 (48 %) | sin fuente 326, palabra clave 125, más de 5 fuentes 76 |
| sqlalchemy | 1.318 | 632 (48 %) | sin fuente 427, más de 5 fuentes 63, palabra clave 55 |
| gin | 1.807 | 637 (35 %) | sin fuente 653, bot 125, vago 125 |
| click | 602 | 187 (31 %) | sin fuente 329, palabra clave 31, bot 21 |
| ripgrep | 433 | 158 (36 %) | sin fuente 206, chore 17, doc 15 |
| excalidraw | 879 | 458 (52 %) | más de 5 fuentes 129, sin fuente 116, más de 20 ficheros 90 |
| commons-lang | 2.683 | 668 (25 %) | sin fuente 1.176, vago 272, palabra clave 245 |
| prometheus | 5.191 | 1.990 (38 %) | sin fuente 1.577, bot 878, palabra clave 188 |

Estos recuentos son de antes de los últimos filtros (test, mypy, coverage), así que en la
fase 2 bajarán un poco.

### 2.3 Del diff a los nodos

**Líneas tocadas.** Se toman las del lado viejo en `git diff -U0 -w P C`, fichero a fichero.

**Dónde acaba cada definición.**

- El inicio es su `source_location`.
- El final lo da el parser del lenguaje:
  - Python: `ast` (`end_lineno`);
  - TS/JS/Java/Rust: tree-sitter, tomando la construcción más externa que empieza en esa línea;
  - Go: la lista de parámetros y después el cuerpo (`_go_body_span`).
- Si tree-sitter devuelve un árbol con errores (como en `_tree_is_broken` de slurp), se usa el
  balance de llaves.
- Nunca se pasa del inicio de la siguiente definición que no esté anidada en ella.

**Qué definición se lleva cada línea.**

- Cada línea cambiada va a la definición más interna que la contiene.
- Una inserción pura solo cuenta si cae estrictamente dentro de una definición, es decir, si sus
  dos líneas vecinas son de la misma. Añadir algo justo detrás de una función no la convierte en
  relevante.

**Qué no cuenta como relevante.**

- Los nodos de test, porque por defecto no pueden seleccionarse.
- Los imports.
- Los nodos de módulo o de fichero.

**Por qué el parser y no la indentación ni las llaves.** Salió en la revisión (§2.5), con dos
casos:

- En Python con estilo black, el `):` que cierra una firma larga va a la altura del `def`, y la
  indentación corta ahí la función. Los cambios de su cuerpo caían en la clase.
- En TS, un parámetro tipado con `{ … }` equilibra las llaves antes de que empiece el cuerpo.

### 2.4 Cuántas preguntas por proyecto

- **Objetivo: 120 por proyecto.** Se barajan con semilla los commits que pasan la etapa A
  (`random.Random(f"{semilla}:{proyecto}")`) y se pasa la etapa B hasta tener 120.
  - Con 120 preguntas, una pregunta mueve el recall 0,8 puntos.
  - Con los juegos de referencia, una pregunta lo mueve de 6 a 7 puntos.
- **Si el historial no da para tanto, menos:** PrismaStats ~51, slurp ~46, ripgrep ~111.
- **Mínimo para contar en el ajuste: 40.**
- **Aceptación de la etapa B en las muestras:** del 67 al 83 %.
  - hono: 10 de 15, 10 de 14 y 10 de 15.
  - sqlalchemy: 10 de 13, 10 de 14 y 10 de 12.
- **Marca `nombra_símbolo`:** se apunta si la pregunta nombra una de las definiciones cambiadas.
  Esas preguntas son fáciles para TF-IDF y bajan B*. Se informa de su proporción y del resultado
  sin ellas, pero no entran en la aceptación.

### 2.5 Revisión a mano: tres muestras de 20 (hono y sqlalchemy)

**Qué se considera razonable.** Una pregunta lo es si cumple dos condiciones:

- **(a)** Alguien que trabaja en el proyecto podría hacerla o buscarla para localizar el código,
  sin ver el diff.
- **(b)** Los nodos relevantes incluyen la definición principal que cambia, con como mucho uno
  de más.

**Resultados por ronda.**

| Ronda | Prototipo | (a) | (b) | Las dos |
|---|---|---|---|---|
| 1 | El inicial | 17/20 | 17/20 | **16/20 (80 %)** |
| 2 | + AST en Python, inserciones solo dentro, filtro «test» | 16/20 | 19/20 | **15/20 (75 %)** |
| 3 | + extensión por tree-sitter y Go, solo comentarios, palabras de herramientas | 20/20 | 20/20 | **20/20 (100 %)** |

**Qué falló en las rondas 1 y 2, y qué lo corrige.**

- **Ronda 1:**
  - *hono 204e10b7*, «cloudflare-pages: Add Cloudflare Pages middleware handler». El código
    principal era nuevo y los nodos eran tipos vecinos. Ahora se descarta: ninguna definición
    cambiada.
  - *hono 5ef800ee*, «timing: Test/add test for middleware timing». Era sobre tests. Ahora lo
    quita el filtro «test».
  - *sqlalchemy 0439f17f*, «warn on failed aliased». Es una pregunta vaga y además se perdió el
    método principal por el `):` de black. Sigue pasando el filtro de vaguedad; ahora sí incluye
    el método.
  - *sqlalchemy 3cf6c111*. Salía la clase en vez de `__init__`, por el mismo motivo.
- **Ronda 2:**
  - *sqlalchemy 2dd1451c*, «fix mypy complain in main». Ahora lo quita la palabra clave.
  - *sqlalchemy 056a96ce*, «save coverage database files», que tocaba `noxfile.py`. Ahora lo
    quitan la palabra clave y la regla de configuración de build.
  - *sqlalchemy f7b40c01*, que solo tocaba docstrings. Ahora se descarta por solo comentarios.
  - *sqlalchemy b4fdfd48*, «Mention no_autoflush in warning». Es demasiado escueta.
  - *hono 0edb243a*. Faltaba `serveStatic` por el parámetro `{ … }`. Ahora lo incluye.

**Cómo leer el 100 % de la ronda 3: es optimista.** Las correcciones se hicieron mirando las
rondas 1 y 2, y la 3 usa los mismos dos proyectos. Lo esperable con el generador congelado es
entre el 85 y el 90 %. Por eso la fase 2 empieza con una **puerta**, antes de medir nada:

- una muestra nueva de 20 preguntas de otros dos proyectos de derivación (jsoup y caddy: Java y
  Go, aún sin revisar);
- con el generador sin tocar;
- tiene que llegar al **80 %** en las dos condiciones. Si no llega, se para y se informa.

**La muestra de la ronda 3, revisada.** Las filas 5 y 16 ya habían salido en la ronda 1.

| # | Proyecto · commit | Pregunta | Nodos relevantes | (a) | (b) |
|---|---|---|---|---|---|
| 1 | hono fb2a7eff | ssg: add default plugin that defines the recommended behavior | `toSSG` | sí | sí |
| 2 | hono 16f73935 | types: allow blank env | `Hono` (×3 presets), `HandlerInterface`, `MiddlewareHandlerInterface`, `OnHandlerInterface` | sí, escueta | sí |
| 3 | hono e449c95e | jsx: improve form attribute types | `ButtonHTMLAttributes`, `FormHTMLAttributes`, `InputHTMLAttributes` | sí | sí |
| 4 | hono 2646696f | jsx: improve input attribute types based on react | `InputHTMLAttributes` | sí | parcial: el alias que cambia no es un nodo |
| 5 | hono 4a239ad2 | hono-base: fix not found handler calling timing when one middleware | `Hono.dispatch` (src y `deno_dist`) | sí | sí |
| 6 | hono a5b2382d | jsx: await all promises at once to avoid race condition | `stringBufferToString` | sí | sí |
| 7 | hono b88087b3 | jsx/dom: make `useTransition()` handling more simple | `useState` (src y `deno_dist`) | sí, difícil: nombra otro símbolo | sí |
| 8 | hono 7e113f30 | trie-router: parentPatterns is updated but never queried | `Node.insert` (src y `deno_dist`) | sí | sí |
| 9 | hono e8f81bf4 | fix (jsx/dom): keep ref.current value during lifecycle | `useRef` (src y `deno_dist`) | sí | sí |
| 10 | hono 33116647 | types: rm ExcludeEmptyObject to fix massively increased type instantiations | `Hono.route` | sí | sí |
| 11 | sqlalchemy e0f13cb6 | forwards-port cpython issue 141560 for getfullargspec | `FullArgSpec`, `inspect_getfullargspec`, `get_annotations`, `SuiteRequirements` | sí | parcial: `SuiteRequirements` sobra |
| 12 | sqlalchemy 553d02b3 | match ORM mapped cols to PK in interpret_returning_rows | `_interpret_returning_rows`, 2× `_do_post_synchronize_fetch` | sí | sí |
| 13 | sqlalchemy 7a68e2ae | Fix typing for `hybrid_property.__set__` to properly validate setter values | `hybrid_property.__set__` | sí | sí |
| 14 | sqlalchemy d1394d9e | add additional contextual path info when splicing eager joins | `JoinedLoader` y 2 métodos, `_ORMJoin._splice_into_center` | sí | sí |
| 15 | sqlalchemy f4d9c107 | apply correct pre-fetch params to post updated rows | `_emit_post_update_statements` | sí | sí |
| 16 | sqlalchemy b2648e69 | Render bind cast in json and jsonb in PG | 5 clases JSON/JSONB de los dialectos | sí | sí |
| 17 | sqlalchemy 7a823b5f | Fix is_pep695 misidentifying Annotated[TypeAliasType] as PEP 695 | `is_pep695` | sí | sí |
| 18 | sqlalchemy 9d6fa58d | add chunking to selectin_polymorphic | `_load_subclass_via_in.do_load` | sí | sí |
| 19 | sqlalchemy 652f610a | correct for FK reflecting on PK / index col at the same time | `MSDialect.get_foreign_keys` | sí | sí |
| 20 | sqlalchemy 10f4d1e2 | disable autoflush within Session.merge_all() | `Session.merge_all` | sí | sí |

**Qué se ve en la muestra.**

- **`nombra_símbolo`:** 1 de 10 preguntas en hono y 6 de 10 en sqlalchemy. Los mensajes de
  sqlalchemy citan el método con frecuencia.
- **Espejo generado en hono:** el historial antiguo tiene `deno_dist/`, una copia generada de
  `src/`. Duplica los nodos relevantes (filas 5, 7, 8 y 9). Se deja tal cual, porque es lo que
  hay en el grafo de ese commit.

### 2.6 Juegos de referencia: las preguntas que ya teníamos

**Qué son.**

- PrismaStats: 15 preguntas, etiquetadas a mano por el autor (`evaluator.default_questions`).
- slurp: 16 preguntas, etiquetadas por Claude Code (`scripts/retrieval.py`), sobre `af2ab66`.

**Para qué sirven.** No entran en el ajuste. Sirven para comprobar el método: B* sacado de
preguntas de commits frente a B* sacado de preguntas etiquetadas, en el mismo proyecto (§4.3).

**Piloto con la rejilla propuesta.** Es la única medida de recall hecha en esta fase; estos
juegos ya se habían medido en la v1.2. Recall con tests e imports fuera:

| Proyecto · formato | 1k | 1,5k | 2k | 3k | 4k | 6k | 8k | 12k | 16k | 24k | 32k | 64k | B* |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| PrismaStats md (n = 15) | 0,77 | 0,83 | 0,90 | 0,97 | 0,97 | 0,97 | 0,97 | 0,97 | 0,97 | 0,97 | 0,97 | 1,00 | 3000 |
| PrismaStats JSON | 0,70 | 0,77 | 0,77 | 0,83 | 0,90 | 0,97 | 0,97 | 0,97 | 0,97 | 0,97 | 0,97 | 0,97 | 6000 |
| slurp md (n = 16) | 0,69 | 0,69 | 0,81 | 0,88 | 0,88 | 0,88 | 0,94 | 0,94 | 1,00 | 1,00 | 1,00 | 1,00 | 16000 |
| slurp JSON | 0,62 | 0,62 | 0,69 | 0,69 | 0,81 | 0,88 | 0,88 | 0,88 | 0,94 | 0,94 | 0,94 | 1,00 | 16000 |

**Lo que enseña el piloto.**

1. **Hay punto de saturación dentro de la rejilla.** Y no es el mismo en los dos proyectos: el
   de PrismaStats está en 3000 y el de slurp en 16000, en markdown.
2. **Con n de 15 o 16, el 95 % equivale a exigir el 100 %.** 15 de 16 son 0,9375, por debajo de
   0,95, así que B* lo marca la pregunta más difícil. Esto justifica las 120 preguntas por
   proyecto.
3. **Entre 32000 y 64000 aún aparece recall.** En PrismaStats markdown, el módulo
   `dashboard_actions` solo entra en 64000. Por eso existe el punto de control de 64000 (§3).
4. **La salida usa siempre el 98 % del presupuesto, sature o no.** Por ejemplo, en PrismaStats
   markdown se mandan 31.366 tokens en 32000, aunque el recall no se mueva desde 3000. Pasado
   B*, los tokens de más se van en nodos que no aportan recall: es justo lo que este estudio
   quiere evitar.
5. **JSON necesita alrededor del doble que markdown en PrismaStats** (6000 frente a 3000). En
   slurp, con 16 preguntas, no se distingue.

---

## 3. Qué se mide

**Rejilla de presupuestos.** 1000, 1500, 2000, 3000, 4000, 6000, 8000, 12000, 16000, 24000 y
32000.

- Son 11 puntos, con pasos de ×1,33 a ×1,5, más finos por debajo de 4000, que es donde se mueve
  la curva.
- Se añade **64000 solo como control de saturación**: no puede ser B*.

**Formatos.** Markdown y JSON. El MCP manda markdown, y un nodo en JSON cuesta alrededor del
doble.

**Configuración.** La de serie de la v1.3.0:

- `min_score` 0,15;
- tests e imports fuera;
- impulso a 10 vecinos;
- `neighbor_decay` por defecto;
- `cl100k_base`;
- `token_factor` 1,0.

**Qué se registra.**

- **Por pregunta y punto:** recall, `tokens_used` (el texto medido, es decir, los tokens reales
  de la salida) y nodos del subgrafo.
- **Por pregunta, una sola vez:** `first_rank` / MRR y `first_rank_all`. El MRR no depende del
  presupuesto, porque es el orden de los scores. Explica B*: un nodo relevante en el puesto *r*
  necesita sitio para unos *r* nodos.
- **Del grafo de cada pregunta:** nodos, candidatos y proporción de tests.
- **Por proyecto:** tokens del grafo completo en el HEAD y en el padre de tamaño mediano.

**Mismo código que `slurp eval --retrieval`.** `run_retrieval_eval`, la función de la orden,
da exactamente los mismos resultados que la orden, pregunta a pregunta. Está comprobado con las
referencias de PrismaStats en 2000 y 8000, markdown y JSON.

Pero esa función vuelve a puntuar el grafo en cada llamada, y puntuar es casi todo el coste: en
el grafo de sqlalchemy, 0,3–1,4 s por puntuar frente a 20–100 ms por cada `build_context`. Por
eso el arnés hace lo mismo que `run_retrieval_eval`, pero puntúa una sola vez por pregunta:

- `score_nodes` una vez;
- luego `build_context` en cada punto, con los mismos argumentos;
- el recall, el rango y los tokens se calculan igual que en la función.

Antes de medir se comprueba que da resultados idénticos a `run_retrieval_eval` en preguntas de
varios proyectos. Si no fuera así, se mide con la función, aunque tarde más.

El estudio corre con **4 procesos**, un proyecto por proceso.

**Dónde se guardan los datos.**

- **En bruto:** en `~/slurp-bench/results/<proyecto>.jsonl`, una línea por pregunta, formato y
  presupuesto.
- **Resúmenes por proyecto:** en `docs/design/presupuesto-optimo/resultados/`.
- **PrismaStats:** solo cifras agregadas, porque su código es privado.

---

## 4. Criterio, fijado antes de medir

### 4.1 Presupuesto óptimo

Para un proyecto *p* y un formato *f*:

- **R(B):** el recall medio de las preguntas evaluables en el presupuesto B.
- **R\*:** el máximo de R(B) en la rejilla, hasta 32000.
- **B\*:** el menor B de la rejilla con R(B) ≥ 0,95 · R\*.
- **Saturación:** si R(64000) − R(32000) > 0,02, la celda «no satura en 32000».
  - B* se informa marcado como censurado.
  - No entra en el ajuste.
  - En validación cuenta con el B* definido arriba.
- **Incertidumbre:** bootstrap sobre las preguntas (2.000 remuestras, semilla 0). Da la mediana
  de B* y su intervalo del 80 %, en pasos de rejilla.
- **Se informa además** de los tokens reales en B*, y de lo que se manda de más en 4000 y en
  32000.

### 4.2 La regla

**Lo que slurp sabe antes de la consulta:**

- N, los nodos;
- C, los candidatos;
- T, los tokens del grafo completo en markdown;
- el lenguaje dominante, por nodos;
- τ, la proporción de tests.

El valor de un proyecto es la mediana sobre los grafos de sus preguntas.

**Familias de regla, de más simple a más compleja:**

| Familia | Forma |
|---|---|
| R0 | Constante: B = c (4000 es una regla R0) |
| R1 | B = a · X^b, con X ∈ {N, C, T}, redondeado hacia arriba a la rejilla |
| R2 | R1 × un factor por lenguaje |
| R3 | R1 con un término en τ |

**Formato.** Se ajusta markdown. Para JSON se usa B_json = k · B_md con una sola k, salvo que un
ajuste propio por formato sea mejor en la validación cruzada. En PrismaStats, k ≈ 2.

**Cómo se ajusta.**

- Mínimos cuadrados sobre log₂(B\*), con las celdas no censuradas de derivación.
- Se elige la familia por validación cruzada dejando un proyecto fuera, dentro de derivación.
  Gana la más simple cuyo error medio |log₂| quede a menos de medio paso de rejilla del mejor.
- El resultado se redondea hacia arriba a la rejilla. Ante la duda, se prefiere recall.

### 4.3 Aceptación en el grupo de validación (se abre una vez)

**Pérdida de recall frente al óptimo de cada proyecto.**

- Pérdida = max(0, R(B\*) − R(B_regla)).
- Como mucho **0,03 en cada celda** (proyecto × formato), unas 4 preguntas de 120.
- Como mucho **0,01 de media**.

**Coste.**

- La media geométrica de B_regla / B\* no pasa de **1,5**, más o menos un paso de rejilla.
- Ninguna celda pasa de **4×**.

**Simplicidad.** Si la mejor constante (R0 ajustada en derivación) también pasa en validación, y
la regla no rebaja el coste medio geométrico al menos un **25 %**, se recomienda la constante.

**Siempre se informa, fuera de la aceptación,** de lo que pierde y gasta el 4000 actual en cada
celda.

**Si ninguna regla pasa,** la conclusión es que el presupuesto no se predice con estos datos. Se
mantiene 4000 o se propone la mejor constante por formato, con las cifras.

**Comprobación del método (puerta antes de ajustar).** En PrismaStats y slurp se compara el B*
de las preguntas de commits con el de los juegos de referencia. Si se separan más de 2 pasos en
los dos proyectos y en los dos formatos, se para y se discute antes de ajustar nada. Querría
decir que el sustituto por commits mide otra cosa.

### 4.4 Lo que no cambia

Este estudio no toca slurp. Un modo de presupuesto automático, con el valor calculado a partir
del grafo, sería otro diseño, y solo si la regla se acepta.

### 4.5 Variante: corte por relevancia

**El problema.** La salida llena siempre el ~98 % del presupuesto aunque ya no gane recall
(§2.6). La variante consiste en que slurp deje de añadir nodos cuando la relevancia de lo que
queda cae por debajo de un umbral, con el presupuesto como techo.

**Cómo se emula, sin tocar slurp.** `build_context` solo toma como candidatos los nodos con
score ≥ `min_score`. Así que el corte equivale a un **`min_score` calculado por consulta** a
partir de sus propios scores: τ\_q. Se pasa `max(τ_q, 0,15)` como `min_score` y el presupuesto
como techo.

- Es exactamente lo que haría slurp con ese `min_score`, porque es su mismo código.
- La cabecera cuenta los nodos que deja fuera, igual que hoy con `min_score`.
- Un nodo por debajo de τ\_q no es candidato. Por tanto tampoco recibe el impulso de sus
  vecinos: un relevante que solo entraba por ese impulso se pierde, y eso es parte de lo que se
  mide.

**De qué scores se parte.** Se usan los de los **candidatos** de la consulta (sin tests ni
imports):

- s\_max, el mejor score;
- m, la mediana.

En los grafos de `slurp index`, todo nodo de código lleva +0,3, así que m ≈ 0,30: es el
**suelo**, el score de un nodo que no comparte ningún término con la consulta. En sqlalchemy
s\_max ≈ 0,70, y el percentil 90 es 0,301.

**Las tres formas del umbral**, cada una con su rejilla de parámetros, fijada ahora:

| Forma | Umbral τ\_q | Rejilla |
|---|---|---|
| C1, relativo al mejor | α · s\_max | α ∈ {0,50, 0,55, 0,60, 0,65, 0,70, 0,75, 0,80} |
| C2, sobre el suelo | m + β · (s\_max − m) | β ∈ {0,02, 0,05, 0,10, 0,20, 0,30, 0,40} |
| C3, salto brusco | Ver abajo | γ ∈ {0,05, 0,10, 0,15, 0,20, 0,30} |

**Cómo funciona C3.**

- Se ordenan los scores de mayor a menor: s\_1 ≥ s\_2 ≥ …
- Se busca el primer i ≥ 5 con s\_i − s\_{i+1} ≥ γ · (s\_max − m).
- El umbral es τ = s\_i. Así siempre entran los 5 primeros, y los empates en s\_i también.
- Si no hay salto, no se corta y el presupuesto decide.

**Techo.** El techo es B\_ref, el presupuesto contra el que se compara:

- el de la regla aceptada (§4.3), por proyecto y formato;
- si no se acepta ninguna regla, el de la recomendación de §4.3: la mejor constante, o 4000.

Además se informa, sin que decida nada, del corte con techo 32000, es decir, del corte solo.

**Ajuste, solo en derivación.**

- Para cada forma y valor de la rejilla (18 combinaciones), se mide en cada celda de derivación
  el recall con corte, R\_corte, y los tokens reales con techo B\_ref.
- Son elegibles las combinaciones que cumplen los márgenes de la regla en derivación.
- Gana la elegible con **más ahorro medio de tokens frente a B\_ref**.
- Solo esa combinación se abre en validación. Las otras se informan, pero no deciden.

**Aceptación en validación.** Se abre una vez, con una sola combinación.

- **Pierde como mucho lo mismo que la regla.** Interpretación: los mismos márgenes que la regla,
  frente al óptimo de cada proyecto.
  - max(0, R(B\*) − R\_corte) ≤ 0,03 en cada celda;
  - ≤ 0,01 de media.
- **Ahorra al menos un 25 % de tokens frente a la regla.**
  - Ahorro de una celda = 1 − (tokens medios con corte) / (tokens medios con B\_ref).
  - La media del ahorro en las celdas de validación tiene que ser ≥ 0,25.
- **Se informa además,** fuera de la aceptación:
  - el ahorro y la pérdida frente al 4000 actual;
  - el corte con techo 32000;
  - las otras dos formas.

---

## 5. Comprobación con un LLM (opcional)

**Qué se comprobaría.** El recall es un sustituto. La pregunta es si, por encima de B*, la
calidad de la respuesta de un modelo deja de mejorar.

**Decisión del autor: sí, solo si se acepta una regla, con `claude -p` y su suscripción.** Se
prepara al final de la fase 2 y no se lanza sin avisarle. No decide nada: confirma o pone en duda
el sustituto. Además de la nota del juez, se informa de una medida sin LLM: la fracción de
definiciones relevantes que la respuesta nombra.

**Muestra.**

- 3 proyectos de derivación: hono, tokio y sqlalchemy.
- 20 preguntas de cada uno, al azar de sus 120.
- 4 presupuestos en markdown: B*/2, B*, 2·B* y 4·B*.
- En total, 240 respuestas.

**Cómo se pregunta y cómo se juzga.**

- Al modelo se le pregunta: «¿qué código cambiarías para…?», con el subgrafo de slurp como
  contexto.
- Un juez puntúa de 0 a 1 contra lo que de verdad cambió: el asunto y el cuerpo del commit, las
  definiciones relevantes y el diff.

**Criterio.** La puntuación media en 2·B\* y 4·B\* no supera a la de B\* en más de 0,05
(intervalo bootstrap del 90 %). Si la supera en más de 0,10, se marca: el recall se estaría
quedando corto como sustituto.

**Volumen.** Suponiendo un B* típico de 4000:

- Las respuestas consumen alrededor de 1,8 M tokens de entrada en `cl100k`, unos 2,8 M en tokens
  de Anthropic (×1,56, medido en la v1.1).
- El juez consume unos 0,7 M.
- La salida son unos 0,2 M.
- Si B* sale en 16000, el volumen de entrada se multiplica por 3 o 4.

**Coste.** Es aproximadamente 3,5 M × precio de entrada + 0,2 M × precio de salida.

- A 3/15 $ por millón de tokens, unos 13 $.
- A 1/5 $, unos 5 $.
- A 15/75 $, unos 67 $.
- Son precios de referencia que hay que contrastar con la tarifa vigente del modelo elegido.

**Alternativa sin coste directo.** Usar `claude -p` con la suscripción, como en la medición con
agentes. Tardaría unos 40 minutos y consumiría parte del límite de la suscripción.

---

## 6. Coste y tiempo en esta máquina

La máquina es un Apple M4 de 10 núcleos con 16 GB de memoria.

**Cálculo.** La estimación sale de los tiempos medidos:

- **Índice completo por commit candidato:** el tiempo de §1, más ~0,3 s de checkout. Se procesan
  unos 172 candidatos para conseguir 120 preguntas, con una aceptación del 70 %.
- **Evaluación por pregunta:** 24 llamadas (2 formatos × 12 puntos), medidas así:
  - 0,7 s con 2,2k nodos;
  - 2,5 s con 14k;
  - 3,6 s con 21k;
  - ~18 s con 55k.

| Proyecto | Preguntas | Minutos |
|---|---|---|
| ky | 120 | 2,9 |
| PrismaStats | ~51 | 1,4 |
| hono | 120 | 5,2 |
| slurp | ~46 | 2,0 |
| jsoup | 120 | 6,2 |
| caddy | 120 | 8,0 |
| tokio | 120 | 11,2 |
| sqlalchemy | 120 | 54,8 |
| gin | 120 | 3,3 |
| click | 120 | 3,7 |
| ripgrep | ~111 | 4,4 |
| excalidraw | 120 | 8,7 |
| commons-lang | 120 | 18,3 |
| prometheus | 120 | 28,2 |
| **Total** | **~1.530** | **~158 (2,6 h)** |

**Con 4 procesos en paralelo:** unos 45 minutos. Con el arnés que puntúa una vez por pregunta
(§3), la evaluación cuesta mucho menos que esta estimación. El tiempo que queda es sobre todo el
de los índices. El corte por relevancia añade 18 combinaciones × 11 techos × 2 formatos llamadas
a `build_context` por pregunta. Cuando el corte ya cabe entero en un techo, los techos mayores
dan el mismo resultado y no se recalculan. Por memoria no conviene pasar de 4: un grafo de
sqlalchemy ocupa varios cientos de MB en memoria.

**Lo que no es cálculo:**

- la puerta de revisión (20 preguntas), unos 30 minutos de lectura;
- el análisis y el bootstrap, minutos;
- la comprobación con un LLM, si se hace: unos 40 minutos.

**Disco.**

- **Repos ya clonados:** 980 MB en `~/slurp-bench/repos/`, ya ocupados.
- **Worktrees:** entre 0,1 y 0,5 GB.
- **Grafos de los padres:** sin comprimir serían ~10 GB, porque cada grafo de sqlalchemy pesa
  ~28 MB. Se guardan con gzip, unas 20 veces más pequeños (27,7 → 1,4 MB): ~0,6 GB. Mientras se
  evalúa, solo queda descomprimido uno por proceso.
- **Resultados:** menos de 100 MB.
- **Total:** unos 2,5 GB más. Hoy quedan 19 GB libres.

---

## 7. Riesgos y límites

- **El mensaje no es una pregunta.** Se escribe después del cambio y lo describe.
  - Con el ámbito delante se parece a lo que preguntaría alguien que va a tocar esa zona.
  - Pero cuando nombra el símbolo (`nombra_símbolo`), TF-IDF lo tiene fácil y B* sale más bajo.
    Se informa de su proporción y del resultado sin esas preguntas.
- **La verdad de referencia es el código que cambió**, no todo el contexto necesario para
  entenderlo (llamadores, tipos). El recall mide si slurp «encuentra dónde cambiar», que es para
  lo que se usa `slurp_query` antes de editar.
- **Solo hay modificaciones.** Los commits que solo añaden código se descartan, así que hay un
  sesgo hacia arreglos y cambios de código existente.
- **Espejos generados** (`deno_dist` en hono): duplican nodos relevantes. Se cuentan como están
  en el grafo.
- **El tamaño cambia dentro de la ventana.** Las características del proyecto salen de los grafos
  de sus preguntas (la mediana), no solo del HEAD.
- **Las referencias son pequeñas:** 15 + 16 preguntas, y las de slurp las etiquetó Claude Code.
- **Los mensajes de PrismaStats están en español**, con identificadores mezclados, así que sus
  preguntas tienen otro idioma.
- **No hay ruido entre ejecuciones:** slurp es determinista y no interviene ningún LLM. Toda la
  incertidumbre viene de qué preguntas salen, y la mide el bootstrap.
- **El criterio usa el máximo hasta 32000.** Lo que solo aparece en 64000 queda como celda
  censurada, no como recall perdido.

---

## 8. Fase 2, pendiente de aprobación

1. **Congelar el generador.** El prototipo de `~/slurp-bench/tools/questions.py` pasa, limpio y
   con tests, a `docs/design/presupuesto-optimo/`, como los scripts de `mcp-autonomo/`.
2. **Puerta de preguntas:** 20 nuevas de jsoup y caddy, revisadas a mano. Hace falta un 80 % o
   más en (a) y (b).
3. **Generar las preguntas** de los 14 proyectos y el grafo del padre de cada una.
4. **Medir** (§3) y comprobar el método con las referencias (§4.3).
5. **Ajustar en derivación** y abrir la validación una vez (§4).
6. **Informar.** Un commit con los resúmenes; ningún cambio en slurp.

Las decisiones están tomadas (ver «Decisiones del autor»). Al paso 5 se le añade el corte
por relevancia (§4.5): se ajusta en derivación después de la regla, porque su techo depende de
ella, y se abre en validación una vez. Si se acepta la regla, la comprobación con un LLM se
prepara y se espera al autor.
