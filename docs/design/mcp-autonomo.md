# Diseño: slurp por MCP sin intervención del usuario (#76, #77)

**Estado:** diseño aprobado por el autor (2026-11-25), **implementado en `main`** (v1.3 en
desarrollo, 2026-11-26). Verificación, desviaciones y hallazgos:
`docs/design/mcp-autonomo/verificacion.md`. La medición con agentes (§7) está preparada en
`docs/design/mcp-autonomo/medicion-agentes.md` y sin lanzar.
**Punto de partida de la implementación:** v1.2.1, que sale antes en otra sesión con tres
arreglos y un cambio de compatibilidad que este diseño da por hechos (ver §0).
**Objetivo:** usar slurp por MCP no requiere ninguna intervención del usuario. El grafo está
siempre al día y el agente usa las herramientas sin aprobaciones.

Contenido:

0. Prerrequisitos (v1.2.1) y hallazgos
1. Decisiones del autor
2. #76 — Grafo siempre fresco
3. Frescura en el CLI
4. `--watch` (#32)
5. #77 — Cero fricción
6. Negociación de la versión de protocolo
7. Medición con `slurp session`
8. Mediciones de tiempo
9. Notas de la versión
10. Orden de implementación y tests
11. Riesgos
12. Fuentes consultadas
13. Fuera de alcance
- Apéndice A — script de medición
- Apéndice B — salida literal de la medición

---

## 0. Prerrequisitos (v1.2.1) y hallazgos

Se arreglan en la **v1.2.1**, en otra sesión, antes de empezar esto:

1. **Bug de `--smart` con `.slurpignore`.** Un fichero excluido entero por una regla `file:`
   cuenta como "añadido" en cada ejecución, y `--smart` hace un índice completo siempre.
   - Reproducido el 2026-11-25: un repo git con `a.py` y `b.py`, y `.slurpignore` con
     `file:b.py`. Tras `slurp index .`, dos `slurp index . --smart` seguidos sin cambios
     imprimen los dos `1 added and 0 removed or renamed files … Full index needed`.
   - Causa: `smart.py` (`added = sorted(set(on_disk) - in_graph)`) compara los ficheros en
     disco con los `source_file` del grafo, y no con los ficheros que se indexaron.
   - Si el servidor refrescara apoyándose en `smart_reindex` sin arreglarlo, haría un índice
     completo en cada llamada en cualquier proyecto con reglas `file:`.
2. **Escritura atómica del grafo.** Hoy `cli.py` (`_run` de `index`, y `init`) y `smart.py`
   escriben con `write_text` directo; `--watch` también, a través de `_run`. Un lector
   concurrente (el servidor MCP) puede leer un fichero a medias y responder "could not be
   reloaded".
3. **Corrección del `# DECISION` de `readOnlyHint`** en `mcp.py` (y de la frase de CLAUDE.md
   que dice lo mismo). Afirma que las anotaciones permiten al cliente auto-aprobar. Según la
   documentación actual de Claude Code eso es falso (ver §5.1). Prueba empírica:
   `prismastats/.claude/settings.local.json` contiene `mcp__slurp__slurp_query`, la
   aprobación manual guardada con "don't ask again".
4. **`"slurp"` en `_SLURP_GRAPH_KEYS`** (#86). La v1.2.1 reconoce como propios los grafos con
   manifiesto que escribirá esta versión (§9).

Este diseño **extiende** el escritor atómico de la v1.2.1 con compare-and-swap y lock
(§2.3). Para los grafos nuevos sustituye la detección de cambios por git (§2.1); el arreglo
de la v1.2.1 sigue sirviendo para los grafos antiguos.

Otros hallazgos que condicionan el diseño:

- **PrismaStats sirve el grafo de graphify** (`.mcp.json` → `graphify-out/graph.json`, con
  `directed/multigraph/graph/hyperedges`). Con este diseño ese grafo no se refresca nunca.
  Por eso `slurp init` ofrecerá por defecto el grafo propio de slurp (§5.2).
- **`smart_reindex()` cuesta 60–92 ms aunque no haya cambios** (lee el JSON, lanza cuatro
  subprocesos git y recorre el árbol). No sirve como comprobación en cada llamada (§8).
- **Hoy el session log solo registra `slurp_query`**, sin id de sesión ni cliente.
- **El `.slurp/session.log` del repo no sirve como línea base.** Son 2.790 líneas de ruido
  de tests: queries "auth"/"auth flow" de fixtures, algunas con `savings_pct`, que es código
  anterior a v1.1.0.

---

## 1. Decisiones del autor

| # | Pregunta | Respuesta |
|---|---|---|
| 1 | Dónde va el manifiesto | **Dentro de graph.json.** Las notas de la versión dirán que slurp ≤ 1.2.0 verá los grafos nuevos como ajenos (la v1.2.1 ya los reconoce, #86). |
| 2 | Límite de espera del refresco | **2,0 s**, configurable como se propuso. |
| 3 | `--watch` | **Reescribirlo** como bucle sobre el manifiesto, con debounce y escritura atómica, y **quitar la dependencia `watchdog`**. La comprobación de frescura bajo demanda se aplica **también a las consultas del CLI**, no solo al MCP. |
| 4 | Grafo por defecto de `slurp init` | **El grafo propio de slurp, siempre fresco.** Mantener el de graphify sigue siendo una opción del plan. |
| 5 | `anthropic/alwaysLoad` | **Sí, solo en `slurp_query`.** |
| 6 | Dónde va el párrafo de instrucciones | **Por defecto, a un fichero local.** Escribir en un fichero versionado es una opción explícita del plan. Antes, comprobar en la documentación actual si Claude Code sigue leyendo CLAUDE.local.md y cuál es el equivalente para AGENTS.md (resultado en §5.4). |
| 7 | Versión de protocolo | **Negociarla:** anunciar las versiones soportadas y responder con la que pida el cliente si está entre ellas (§6). |
| 8 | Criterio de medición | **Aprobado tal cual** (§7). |
| 9 | Prerrequisitos | El bug de `.slurpignore`, la escritura atómica y el `# DECISION` de `readOnlyHint` van en una **v1.2.1** previa. Esta implementación parte de ella. |
| 10 | Argumentos inválidos en el MCP | **Error de la herramienta (`isError`)**, siguiendo la especificación 2025-11-25, con las versiones de protocolo ≥ 2025-11-25 y la moderna. Confirmado (§6). Sustituye la decisión "Dos categorías de error" de CLAUDE.md. |

---

## 2. #76 — Grafo siempre fresco

Antes de responder cada llamada, el servidor MCP comprueba si algún fichero del proyecto
cambió desde que se construyó el grafo. Si cambió, refresca el grafo dentro del propio
proceso antes de responder, sin esperar más de un límite de tiempo.

### 2.1 Detección barata: un manifiesto dentro del grafo

Coste por llamada de cada alternativa (mediana, en proceso; detalle en §8):

| Comprobación | PrismaStats | slurp |
|---|---|---|
| Recorrer el árbol (`iter_source_files` + stat de cada fichero) | 31,2 ms | 3,1 ms |
| `git status --porcelain` (subproceso) | 12,4 ms | 9,3 ms |
| `smart_reindex()` sin cambios | 92,0 ms | 60,1 ms |
| **Manifiesto: stat de ficheros, directorios recorridos y `.slurpignore`** | **0,4 ms** (185 ficheros + 161 directorios) | **0,1 ms** (54 ficheros + 16 directorios) |
| Referencia: una query en caliente | 9,0 ms | 22,4 ms |

**Formato.** Todo escritor de grafos de slurp (`slurp index`, `--smart`, `slurp init`, el
refresco del servidor, el del CLI y `--watch`) escribe una clave nueva de primer nivel:

```json
"slurp": {
  "format": 1,
  "slurp_version": "1.3.0",
  "root": "..",
  "started_ns": 1764090000000000000,
  "ignore": {"path": ".slurpignore", "sha": "…" | null},
  "parsers": {"ts": "tree-sitter", "py": "ast", "go": "regex"},
  "dirs":  {"": 1764089990000000000, "lib": 1764089990000000000},
  "files": {"lib/a.ts": [mtime_ns, size, ino, "blake2b…"]}
}
```

- **`root`** es relativo al directorio del grafo (`os.path.relpath`, o absoluto si están en
  volúmenes distintos en Windows). Funciona con cualquier `-o` y no depende del directorio
  desde el que el cliente lance el servidor.
- **`files`** registra los ficheros **descubiertos** (`iter_source_files`), no los
  `source_file` del grafo. Un fichero excluido entero por `.slurpignore` sigue en el
  manifiesto, y por eso el bug de §0.1 no puede reaparecer por esta vía.
- **`ignore.path`** es el fichero de reglas con el que se construyó el grafo. Cuenta
  `--ignore-file`, y también se registra cuando el fichero no existe.
- **Los stats se toman antes de leer cada fichero y antes de listar cada directorio.** Un
  cambio durante la indexación queda como pendiente y se detecta en la siguiente
  comprobación; nunca se pierde.
- **Tamaño.** PrismaStats crece unos 35 KB sobre 770 KB (estimado; con `indent=2` cada
  entrada ocupa varias líneas). En slurp el aumento es despreciable.
- **`built_at_commit` se sigue escribiendo** por compatibilidad. El servidor no lo usa.

**Comprobación en cada llamada.** Solo hace `stat`, y lee un fichero únicamente cuando su
stat no coincide:

| Caso | Cómo se detecta |
|---|---|
| Modificado | (mtime_ns, size, ino) distinto → se compara el hash del contenido. Si el hash es igual, no cambió: un `touch`, un formatter que reescribe lo mismo, un checkout de ida y vuelta, o un grafo clonado en otra máquina. |
| Modificado en la misma marca de tiempo | Todo fichero con mtime ≥ `started_ns − 2 s` se verifica por hash siempre. La granularidad del mtime es de 1 s en HFS+ y 2 s en FAT. Es el mismo tratamiento que da git a las entradas "racily clean". |
| Nuevo, borrado o renombrado | El mtime de un directorio cambia cuando se crea, borra o renombra una entrada dentro. Solo se vuelven a listar (`scandir`) los directorios cuyo mtime cambió, aplicando las reglas de `_SKIP_DIRS`, `out/` de Next y `*.min.js`, y se baja a los subdirectorios nuevos. El listado manda: si un nombre del manifiesto no aparece, está borrado. Esto cubre los renombrados de mayúsculas en sistemas de ficheros que no distinguen mayúsculas. |
| `.slurpignore` | stat de la ruta registrada (que el fichero aparezca también cuenta) → hash. Si el hash difiere → **índice completo**, porque las reglas pueden quitar o devolver nodos de cualquier fichero. Esto cierra la limitación que documenta CLAUDE.md ("si cambia `.slurpignore` … `--smart` no lo detecta"). |
| Otra versión de slurp u otros parsers disponibles | Índice completo una vez. Los parsers se calculan al arrancar el proceso, no en cada llamada. Cubre el caso de instalar el extra `ts` después de construir el grafo. |
| Otro proceso escribió el grafo | Cambió la identidad del fichero (mtime_ns, ino, size) → se recarga, sin reindexar. Sustituye a la comparación de `st_mtime` en float que hace hoy `GraphState`. |
| Red de seguridad | Un recorrido completo si el último tiene más de 30 s (3–31 ms), para sistemas de ficheros que no actualizan el mtime de los directorios (algunos montajes de red o de Docker). En ellos, el retraso máximo queda acotado a 30 s. |

Un directorio "falso positivo", como un `node_modules/` recién creado que cambia el mtime de
la raíz, solo cuesta listar ese directorio. No lanza un reindexado si el conjunto de
ficheros indexables no cambió. El manifiesto se actualiza en memoria y no se reescribe el
grafo solo por eso.

**Por qué no git como mecanismo principal:**

- Exige que el proyecto sea un repo.
- Cuesta un subproceso por llamada (unos 10 ms).
- "Sucio respecto a HEAD" no significa "cambiado desde el build": un fichero sucio al
  construir sigue sucio después.

Git y `built_at_commit` quedan solo para los grafos sin manifiesto, en `slurp index --smart`.

**`slurp index --smart`** usa el manifiesto cuando existe (sin git) y decide igual que el
servidor:

- Solo modificados → vía rápida (volver a parsear y comparar símbolos, como hoy).
- Altas, bajas, cambios de `.slurpignore` o de versión → índice completo.

`TestSmartEquivalence` se amplía al camino del manifiesto.

### 2.2 Grafos de graphify: nunca se reindexan ni se sobrescriben

La propiedad del grafo se decide con **detección positiva**, y se vuelve a evaluar cada vez
que cambia la identidad del fichero. graphify puede sobrescribir `graphify-out/graph.json`
con el servidor en marcha.

| El grafo | Tratamiento |
|---|---|
| Lleva la clave `"slurp"` con `format` | Propio → refresco automático. |
| Claves exactamente `{nodes, links, built_at_commit}` (salida de slurp ≤ 1.2) | **Adopción.** La raíz candidata es el padre de `graphify-out/`. Se acepta solo si al menos el 90 % de los `source_file` del grafo existen bajo ella. El primer refresco es completo y escribe el manifiesto. Si no valida → solo lectura, con el aviso "run `slurp index` once to enable auto-refresh". |
| Cualquier otra cosa (claves de graphify, JSON que no parsea, `{nodes, links}` genérico, `edges`, GraphML, neo4j…) | **Solo lectura:** sin reindexado, sin escritura y sin lock. |

En modo solo lectura, una ayuda barata (≈0,5 ms): al cargar el grafo se hace stat de los
`source_file` que contiene. En cada llamada, si alguno cambió después del mtime del grafo,
se añade una línea:
`N files this graph covers changed after it was built; slurp doesn't rebuild graphify graphs — rerun graphify.`
No detecta ficheros nuevos.

**Protección final (compare-and-swap).** Justo antes del `os.replace` se comprueba que la
identidad del fichero sigue siendo la del inicio del refresco. Si alguien (graphify, otro
slurp) escribió entretanto, el resultado se descarta y se recarga lo que hay en disco.

La lógica de propiedad (`_is_foreign_graph`, `_SLURP_GRAPH_KEYS`) sale de `cli.py` a un
módulo de librería, porque la necesitan el CLI, el servidor y `--watch`. `_SLURP_GRAPH_KEYS`
ya incluye `"slurp"` desde la v1.2.1 (#86).

### 2.3 Escritura atómica y concurrencia

**Un único escritor.** `write_graph(path, data, expected=None)`, el de la v1.2.1 ampliado:
fichero temporal en el mismo directorio → `fsync` → si `expected` no coincide con la
identidad actual, se aborta → `os.replace`. Un lector ve el grafo viejo o el nuevo, nunca
uno a medias. En Windows, `os.replace` falla si otro proceso tiene el fichero abierto; se
reintenta con espera creciente.

**Dentro del servidor:**

- El bucle stdio es secuencial. Aunque Claude Code mande dos llamadas en paralelo (lo hace
  con las herramientas read-only), se atienden una detrás de otra.
- Se añade como mucho **un hilo de refresco**. Solo construye el dict del grafo y lo escribe
  con `write_graph`.
- **El cambio de grafo lo hace siempre el hilo principal**, al empezar una llamada. Construye
  el `nx.DiGraph` desde el dict que devuelve el hilo, sin volver a leer el disco, y limpia
  las cachés (`clear_pagerank_cache`, `clear_node_token_cache`, `clear_left_out_cache`).
  Ninguna llamada ve un estado a medias, y el hilo nunca toca cachés globales.
- **Si llega una llamada con un refresco en marcha, espera a ese refresco** en vez de
  lanzar otro.
- **Grafos federados** (`--graph` repetido): un rastreador de frescura por miembro; solo se
  refrescan los propios, en orden, y todos comparten el mismo límite de tiempo.

**`slurp index` lanzado a mano mientras el servidor corre:**

- **Lock consultivo** `.graph.json.lock` junto al grafo (`fcntl.flock` en POSIX, `msvcrt` en
  Windows). Si no se puede usar, se sigue sin él. Nunca se borra: borrar ficheros de lock
  provoca carreras.
- **El lock solo evita trabajo duplicado; la corrección no depende de él.** La garantizan la
  escritura atómica, un manifiesto coherente con su contenido y el compare-and-swap. Si gana
  un escritor con una instantánea más antigua, su manifiesto lo delata y la siguiente
  comprobación vuelve a refrescar.
- El servidor, si encuentra el lock ocupado, espera dentro del mismo límite y recarga.
- La CLI espera el lock con el mensaje
  `waiting for another slurp process to finish writing <path>…`.

**Reglas duras del hilo de refresco:**

- **Nunca escribe en stdout**, porque corrompería el protocolo. Hoy el indexador y smart
  solo escriben en stderr (`click.echo(..., err=True)` y `print(..., file=sys.stderr)`); un
  test lo fija.
- **Nunca lee stdin.**
- **Nunca usa funciones del CLI:** ningún prompt de "foreign graph" ni `click.confirm`.

### 2.4 Límite de tiempo: 2,0 s

Peor caso medido de una llamada justo después de editar (en proceso, mediana; detalle en §8):

| | PrismaStats (1.582 nodos) | slurp (4.516 nodos) |
|---|---|---|
| smart, vía rápida (editar el cuerpo de una función) | 118 ms | 126 ms |
| smart → índice completo (símbolo nuevo) | 381 ms | 541 ms |
| `build_graph(dict)` tras refrescar | 2,4 ms | 9,7 ms |
| Primera query tras recargar (PageRank en frío) | 72 ms | 68 ms |
| **Total** | **≈ 0,46 s** | **≈ 0,62 s** |

Las cifras de smart incluyen sus subprocesos git y el recorrido del árbol. Con el manifiesto
desaparecen, así que son una **cota superior**.

**Justificación de 2,0 s:**

- Da un margen de ×3,2 sobre el peor caso de slurp.
- El indexado procesa 6–10 nodos/ms en estos dos proyectos, así que deberían entrar
  proyectos de unos 10–18 k nodos (extrapolación, no medido).
- Queda muy por debajo del umbral de 2 minutos con el que Claude Code pasa una llamada MCP a
  segundo plano, y por debajo de lo que dura un turno del LLM.

**Comportamiento:**

- **Si el refresco termina dentro del límite**, se responde con el grafo nuevo. El texto no
  cambia; el estado va en `_meta` y en el log de sesión.
- **Si no termina**, se responde con el grafo anterior y una línea **al principio** del
  texto, porque el modelo solo ve `content`:
  `⚠ Graph refresh still running — 3 files changed since this graph was built (lib/a.ts, app/b.tsx, +1). This answer may miss them; call again in a few seconds.`
  El refresco **no se cancela** (un hilo no se puede cancelar de forma segura, y el trabajo
  sirve), y la siguiente llamada lo recoge.
- **Una llamada espera como mucho hasta `inicio del refresco + límite`.** Con un refresco
  lento ya en curso, las llamadas siguientes responden sin esperar otra vez, con
  `refresh in progress (started Ns ago)`.
- **Al arrancar**, la comprobación y el refresco se lanzan en segundo plano y no retrasan
  `initialize`. La primera llamada aplica la misma regla de espera.
- **Si el refresco falla** (excepción del indexador), se responde con el grafo anterior y
  `⚠ Graph refresh failed: <tipo>: <mensaje>`. El detalle va a stderr. No se reintenta
  hasta que cambie otra vez el conjunto de ficheros, para no entrar en un bucle.
- **Si el directorio del grafo no admite escritura**, se refresca solo en memoria y se avisa
  una vez.

**Configuración** (MCP y CLI):

- `--refresh-timeout S` / `SLURP_REFRESH_TIMEOUT` (por defecto 2,0). Con `0` no se espera
  nunca: se responde al momento y el refresco sigue en segundo plano.
- `--no-refresh` / `SLURP_REFRESH=0`: desactiva el refresco automático.
- En el MCP se fija con el bloque `env` de `.mcp.json` o con los `args`.

**Estado estructurado** en `_meta` del resultado, con un prefijo en DNS inverso como pide la
especificación:
`{"dev.imjuank.slurp/graph": {"status": "fresh|refreshed|stale|failed|readonly", "refresh_ms": …, "changed_files": […]}}`.
El `graph_reloaded` de primer nivel que se escribe hoy no es estándar y pasa a este campo.

### 2.5 Vigilante en segundo plano dentro del servidor: no, por ahora

- **La comprobación bajo demanda basta:** cuesta 0,1–0,4 ms y garantiza el grafo fresco en
  el momento de responder.
- **Ganancia de un vigilante:** adelantaría los 0,12–0,6 s del refresco al rato que pasa
  entre la edición y la siguiente llamada, que en un agente ya son segundos de generación.
- **Costes:**
  - CPU mientras el usuario edita a mano con la sesión abierta (un índice completo por
    cada guardado que cambie símbolos).
  - Contención del GIL con las queries.
  - Límites de inotify en Linux.
  - Más estados concurrentes justo en el camino que no puede corromper stdout ni el grafo.
- **Criterio para revisarlo:** que el log de sesión muestre un p95 de espera por refresco
  mayor de 500 ms, o más de un 5 % de respuestas con aviso de grafo viejo.
- **Variante prevista para ese caso:** sondear el manifiesto cada 2 s con debounce de 1 s,
  solo mientras la sesión esté activa (alguna llamada en los últimos 10 minutos). Sin
  watchdog.

---

## 3. Frescura en el CLI (decisión 3)

El motor de §2 es una pieza de librería, compartida por el servidor, el CLI y `--watch`.

| Comando | Comportamiento |
|---|---|
| `slurp QUERY` (`run`), `export`, `explain`, `impact`, `stats`, `advisor` | Comprobación y refresco, como el MCP, si el grafo es propio. |
| `benchmark`, `eval` | **Sin refresco.** Miden sobre una instantánea fija, y refrescar movería el suelo bajo la medición. Si el grafo está viejo, aviso en stderr. |
| `diff` | Nada: compara dos ficheros explícitos. |
| Grafos de graphify | Solo lectura, como en §2.2, y el aviso va a stderr. |

- **Toda salida del refresco va a stderr.** stdout lleva la respuesta, que puede ir por una
  tubería, en `--format json` o en `export --output`.
- **Límite:** el mismo, 2,0 s. Si el refresco no termina, se imprime la respuesta con el
  grafo anterior (aviso en stderr) y, **antes de salir, el proceso termina el refresco**
  mostrando `finishing graph refresh… (Ctrl+C to skip; the graph stays as it was)`.
  Matarlo tiraría el trabajo, y el siguiente comando volvería a empezar. Ctrl+C es seguro
  gracias a la escritura atómica.
- **`--no-refresh` en todos esos comandos**, y las mismas variables de entorno.
- **Autodescubrimiento sin `--graph`:** se añade `graphify-out/slurp-graph.json` **antes**
  de `graphify-out/graph.json` (orden: `graph.json`, `graphify-out/slurp-graph.json`,
  `graphify-out/graph.json`, `.graphify/graph.json`). Ese fichero solo existe si el usuario
  o `slurp init` eligieron construir el grafo de slurp junto al de graphify, así que expresa
  una preferencia. Es un cambio de comportamiento y va en las notas de la versión (§9).
- **`audit.jsonl`** registra el estado del grafo en cada consulta (`fresh/refreshed/stale/…`).

---

## 4. `--watch` (#32): se reescribe y se quita watchdog

Defectos del manejador actual (`cli.py`, `class _Handler`):

- Solo atiende `on_modified`, así que pierde ficheros creados, borrados y renombrados. Las
  escrituras "seguras" de vim o JetBrains generan un create seguido de un move.
- No tiene debounce: un guardado lanza 2–3 índices completos, y un `git checkout` de 100
  ficheros lanza 100.
- Siempre hace un índice completo e ignora `--smart`.
- Filtra solo por extensión, no por `_SKIP_DIRS`: un `npm install` dispara un índice por
  cada `.js` de `node_modules/`.
- Escribe sin atomicidad.
- No ve los cambios de `.slurpignore`.
- Reindexa en el hilo del observer, de modo que los eventos se acumulan.

**Diseño nuevo.** Se mantiene el flag y se reimplementa como bucle sobre el motor:

- Sondea el manifiesto cada 1 s (0,4 ms por sondeo en PrismaStats).
- Refresca tras **1 s sin cambios**.
- Usa el mismo camino smart/completo y la misma escritura atómica con lock.
- Cubre por construcción todos los tipos de cambio: modificados, nuevos, borrados,
  renombrados y `.slurpignore`.
- Respeta los directorios saltados y no depende de inotify ni de FSEvents.
- Imprime una línea por refresco (ficheros, vía y tiempo) y termina con Ctrl+C.

**`watchdog` sale de `dependencies` en `pyproject.toml`** (y de `uv.lock`).

**Documentación.** `--watch` sirve para herramientas que leen `graph.json` fuera de slurp;
los usuarios del MCP y del CLI ya no lo necesitan. #32 queda absorbido por #76.

---

## 5. #77 — Cero fricción

### 5.1 Anotaciones

**Estado:** las cinco herramientas ya declaran `readOnlyHint: true`, `idempotentHint: true`,
`destructiveHint: false` y `openWorldHint: false`.

**Qué hace cada cliente con ellas:**

- **Claude Code:** `readOnlyHint` sirve para ejecutar la llamada en paralelo con otras
  read-only, y `readOnlyHint`/`idempotentHint` permiten repetir una llamada tras una
  reconexión (HTTP). **No aprueban nada.** El comentario lo corrige la v1.2.1.
- **Codex:** sí las usa para aprobar. Con `default_tools_approval_mode = "writes"` solo
  pregunta por las herramientas no marcadas como read-only.
- **Especificación MCP:** los clientes deben considerar las anotaciones no fiables salvo que
  vengan de servidores de confianza.

**Cambios:**

- **Se mantiene `readOnlyHint` con el refresco automático**, con un `# DECISION`: lo que se
  reescribe es una caché derivada del código, no el código. Hay precedentes:
  - el servidor ya escribe `session.log` en cada query;
  - Claude Code trata `git status` como read-only aunque refresque `.git/index`.
  Marcarlas `readOnlyHint: false` haría perder la ejecución en paralelo en Claude Code sin
  ganar nada.
- **`_meta: {"anthropic/alwaysLoad": true}` solo en `slurp_query`** (decisión 5). Con tool
  search, que está activado por defecto, Claude solo ve al inicio los nombres de las
  herramientas y las instrucciones del servidor; la herramienta de entrada carga su
  descripción desde el principio. Coste: unos cientos de tokens por sesión. Se valida con la
  medición de §7.

### 5.2 `slurp init`

**Plan en pantalla** (todo se confirma antes de escribir nada; `--yes` acepta los valores
por defecto):

1. **Grafo.** Por defecto, **el grafo propio de slurp, siempre fresco** (decisión 4).
   - Sin grafo previo: `graphify-out/graph.json`, como hoy.
   - Con un grafo de graphify: `graphify-out/slurp-graph.json`, sin tocar el de graphify. La
     opción del plan "keep graphify's graph (slurp won't refresh it)" lo mantiene. En modo
     no interactivo, `--keep-graphify`.
   - Con un grafo propio previo: se reutiliza (y se refrescará solo).
2. **`.mcp.json`**, como hoy, apuntando al grafo elegido.
3. **`.claude/settings.local.json`** con la aprobación del servidor y de sus herramientas
   (detalle a continuación).
4. **Párrafo de instrucciones**, por defecto en un fichero local (§5.4). Opciones:
   `--instructions local|versioned|none`.
5. **`.gitignore`:** `.slurp/`, `.claude/settings.local.json` y `CLAUDE.local.md`, solo si
   `.gitignore` existe y siguiendo las reglas de `_ensure_gitignored` (sin duplicar y
   respetando reglas previas, también las negaciones `!`).

**Claude Code: dos aprobaciones distintas.** `.mcp.json` por sí solo no quita ninguna:

- la aprobación del servidor de proyecto ("¿usar este servidor de `.mcp.json`?") se quita
  con `enabledMcpjsonServers`;
- la aprobación de cada herramienta en modo Manual se quita con una regla en
  `permissions.allow`.

Se fusiona en `.claude/settings.local.json` **en la raíz del repo git**, que es donde Claude
Code lee y escribe ese fichero aunque se arranque en un subdirectorio. Sin git, se usa el
directorio actual.

```json
{
  "enabledMcpjsonServers": ["slurp"],
  "permissions": { "allow": ["mcp__slurp__*"] }
}
```

- Es lo mismo que escribe Claude Code cuando el usuario aprueba el servidor en su diálogo y
  elige "don't ask again" para una herramienta.
- Es local y no se versiona, coherente con un `.mcp.json` que ya lleva rutas absolutas de
  esta máquina.
- En modo auto, la regla de permiso se resuelve antes que el clasificador.

**Reglas al escribir:**

- Fusionar sin duplicar entradas.
- Si el JSON es inválido, no tocar el fichero e imprimir el fragmento para añadirlo a mano.
  Un fichero de settings nunca se sobrescribe.
- Si `disabledMcpjsonServers` contiene `slurp`, o hay una regla deny/ask que afecte a
  `mcp__slurp…`, respetarla y avisar. El rechazo tiene precedencia en Claude Code.
- Asegurar que el fichero no se versiona (`git check-ignore`; si no lo está, ver el paso 5).
  La documentación de Claude Code dice que si creas el fichero a mano, lo ignoras tú.

**Descartado:**

- `enableAllProjectMcpServers`: aprobaría también cualquier otro servidor del `.mcp.json`.
- `.claude/settings.json`: se versiona, y sus reglas de permiso esperan a que el usuario
  confíe en el workspace.
- `claude mcp add --scope local`: exige tener el binario `claude` y escribe en
  `~/.claude.json`.

**Fricción que queda, inevitable:** el diálogo de confianza del workspace la primera vez que
se abre Claude Code en una carpeta. Es de Claude Code, no de slurp.

**Otros clientes.**

- **Fase 1:** el resumen final imprime el fragmento de cada uno.
- **Fase 2:** se escribe solo si ya existe el directorio del cliente en el proyecto, y
  después de probar cada cliente de punta a punta.

| Cliente | Servidor | Aprobación |
|---|---|---|
| Codex | `.codex/config.toml` (solo proyectos de confianza), `[mcp_servers.slurp]` | `default_tools_approval_mode = "writes"` (se apoya en `readOnlyHint`) |
| Gemini CLI | `.gemini/settings.json`, `mcpServers.slurp` | `"trust": true` en la entrada del servidor |
| Cursor | `.cursor/mcp.json` | CLI: `Mcp(slurp:*)` en `permissions.allow`; editor: lista de permitidos desde la interfaz |
| VS Code / Copilot | `.vscode/mcp.json` | No se ha encontrado ninguna clave documentada para auto-aprobar: aprobación manual |

### 5.3 Instrucciones del servidor y descripciones de las herramientas

**Restricciones de las fuentes:**

- Claude Code corta las instrucciones a partir de 4.096 caracteres.
- Codex pide que los primeros 512 se entiendan por sí solos.
- Con tool search, las descripciones se buscan por sus primeras palabras.

Las instrucciones propuestas tienen 1.231 caracteres, y el primer párrafo 470. Se generan
al servir `initialize` / `server/discover`: si el grafo es de graphify, la última frase del
primer párrafo se sustituye por
`This graph was built by graphify and slurp does not rebuild it: answers can lag behind recent edits, and say so when files they cover have changed.`

**Server instructions**

```
slurp answers structural questions about this codebase from its code graph: where a behaviour lives, what calls what, and what breaks if a file or function changes. Use it before grep or reading whole files when you orient yourself or are about to edit: one call returns the relevant functions, classes and modules, their relations and their files, within a token budget. The graph follows the working tree: edits, new and deleted files are picked up before each answer.

Which tool:
- Starting a task, or unsure where something lives -> slurp_query, in plain language ("how an uploaded stats file is validated"). The default budget answers architecture questions; to pin down one specific function in a large project, pass budget 16000–32000.
- About to edit a file -> slurp_impact(file_path=...) with its path: which definitions have callers elsewhere, which only tests use, which are free to change.
- About to change one function or class -> slurp_explain(node_name=...) with its name: its role, production callers vs tests, risk.
- A slurp_query answer looks incomplete -> slurp_suggest with the same query.
- Comparing two graph.json snapshots, e.g. two branches -> slurp_diff. Rarely needed.

All five are read-only. Not for exact strings or regexes (use grep), a file you already know (read it), or non-code files. Answers are pointers: open the files they name before editing them.
```

Cambiado en #93, tras la medición con agentes: la versión aprobada decía "slurp_impact with
its path" y "slurp_explain with its name". Las dos herramientas son diferidas, y en las tres
sesiones que usaron `slurp_impact` la primera llamada adivinó el argumento (`target`,
`path`) sin haber cargado el esquema. Ahora las instrucciones lo nombran, y el servidor
acepta también `target` y `path` en `slurp_impact` (gana `file_path` si vienen los dos). Las
instrucciones pasan a tener 1.260 caracteres. `alwaysLoad` sigue solo en `slurp_query`
(decisión del autor).

**slurp_query** (lleva `alwaysLoad`)

```
Find where something lives in this codebase. Returns the functions, classes and modules most relevant to a plain-language description, with their files and the call/import relations between them, trimmed to a token budget. Call it first when starting a task or when you don't know which files are involved — it is cheaper than grepping and reading files speculatively. Describe behaviour ('where uploaded stats files are validated'), not a bare identifier: for one known symbol use slurp_explain; for exact text use grep. The default budget, 4000 tokens, answers architecture and orientation questions; to pin down the specific function to change in a large project, raise it to 16000–32000. Tests and import nodes are left out unless include_tests / include_imports. Reflects the current working tree, uncommitted edits included.
```

Parámetros:
- `query`: `Plain-language description of the behaviour or feature you are looking for.`
- `budget`: `Token budget for the answer (default 4000, enough for architecture questions). Raise it to 16000–32000 to pin down a specific function in a large project.`

Cambiado en #99, tras el estudio del presupuesto óptimo
(`docs/design/presupuesto-optimo/resultados/conclusiones.md`). La versión aprobada decía
«Default budget 4000 tokens; 8000–12000 for broad architecture questions» y, para `budget`,
«Raise it for broad questions». El estudio mide lo contrario:

- las preguntas de arquitectura se resuelven con el valor por defecto (las etiquetadas de
  PrismaStats: recall 0,97 en 4000);
- localizar la función concreta que cambia un commit, en proyectos con más de unos 2.000
  candidatos, sigue ganando recall hasta 32000 (16000 encuentra del 52 al 75 %, 32000 del 59
  al 81 %, frente al 34–68 % de 4000).

Las instrucciones del servidor ganan la misma frase en la línea de `slurp_query` y pasan a 1.390
caracteres (el primer párrafo sigue en 470). El valor por defecto no cambia.
- `include_tests`: `Also rank test code (left out by default).`
- `include_imports`: `Also rank import statements (left out by default).`

Variante para graphify: la última frase pasa a ser
`Reflects graph.json as graphify last built it; answers flag files changed since.`

**slurp_impact**

```
Blast radius of editing a file, before you edit it. For each definition in the file: callers in other files (direct and transitive), affected files, risk level; plus which definitions only tests call and which nothing calls. Call it before modifying, deleting, renaming or changing the signature of code in a file. Pass the path relative to the project root; a unique suffix such as 'auth/session.ts' works. Risk is UNKNOWN, not LOW, for languages without a call graph — don't read that as safe.
```

Parámetros:
- `file_path`: `Project-relative path (or unique suffix) of the file you are about to edit.`
- `hops`: `How many levels of indirect callers to follow (default 2).`

**slurp_explain**

```
Explain one function, class or module by name: the role it plays, who calls it (production callers and tests counted apart), what it calls, and how risky changing it is. Call it once you know which symbol you will change, or when a name from slurp_query needs context. Accepts a plain name ('createAdminClient') or a dotted id. An unknown name falls back to the closest match — check the name in the answer. Structural by default, no API cost; no_llm=false adds prose only if the user configured an LLM provider.
```

Parámetros:
- `node_name`: `Function, class or module name, or a dotted node id.`
- `no_llm`: `Keep true (default) for the structural answer; false asks the configured LLM for prose.`

**slurp_suggest**

```
Follow-up queries after a slurp_query answer that looks incomplete. Reads the nodes that fell just outside the budget and proposes two or three queries that reach them: a drill-down into the top result, a related cluster, or a heavily-used neighbour. Pass the same query and budget you used. When the budget already covered the neighbourhood, the answer says "No follow-up suggested" instead of proposing queries.
```

Corregido en #90: la versión aprobada decía "An empty answer means the budget already
covered the neighbourhood", pero la herramienta responde con una frase.

**slurp_diff**

```
Compare two graph.json snapshots — e.g. built on two branches, or before and after a refactor — and report added, removed and modified nodes plus the production callers put at risk. Needs paths to two graph files; it does not read git or the working tree. For the effect of editing a file now, use slurp_impact.
```

Nota: la salida markdown de `slurp_query` incluye el fichero de cada nodo, pero no el
número de línea (comprobado con PrismaStats). Por eso las descripciones dicen "with their
files" y no prometen líneas.

### 5.4 Párrafo para el agente: fichero local por defecto

**Comprobación en la documentación actual** (memory.md de Claude Code y guía AGENTS.md de
Codex, consultadas el 2026-11-25):

- **Claude Code sigue leyendo `CLAUDE.local.md`.** Lo carga desde el directorio de trabajo
  y los superiores, después del `CLAUDE.md` de cada nivel. La documentación indica añadirlo
  al `.gitignore`.
- **Efecto secundario documentado:** `CLAUDE.local.md` cuenta en la comprobación que decide
  si Claude Code lee `AGENTS.md`. Con el valor por defecto de "Project instructions"
  (`claude-md-or-agents-md`), crear un `CLAUDE.local.md` en un proyecto que solo usa
  `AGENTS.md` **hace que Claude Code deje de leer ese `AGENTS.md`**.
- **Importar con `@AGENTS.md`** desde un fichero CLAUDE lo recupera, y la documentación
  garantiza que no se carga dos veces.
- **Equivalente local de AGENTS.md: no existe uno que se sume al original.**
  - Claude Code no lee `AGENTS.local.md` ni `AGENTS.override.md`.
  - Codex lee `AGENTS.override.md`, pero **en lugar de** `AGENTS.md` en ese directorio
    (incluye como mucho un fichero por directorio). Escribir ahí ocultaría a Codex el
    `AGENTS.md` del proyecto.
  - El estándar agents.md no define una variante local.
  - Conclusión: el modo local cubre Claude Code. Los demás agentes reciben la guía por las
    instrucciones del servidor (Codex las lee) o por la opción versionada.

**Modo local (por defecto):**

- **Destino:** `CLAUDE.local.md` en la raíz del proyecto, donde se escribe `.mcp.json`.
- **Si existe `AGENTS.md`,** no hay `CLAUDE.md` ni `.claude/CLAUDE.md` en el directorio o
  por encima, y `CLAUDE.local.md` todavía no existe: el fichero nuevo **empieza con una
  línea `@AGENTS.md`**, para que Claude Code siga leyendo las instrucciones del proyecto.
- **Si además hay `AGENTS.md` en subdirectorios,** se avisa: Claude Code dejará de cargarlos
  bajo demanda. La solución es que el usuario ponga "Project instructions" en
  `claude-md-and-agents-md`, un ajuste que solo se lee de la configuración de usuario;
  `slurp init` lo explica pero no lo escribe.
- **Si `CLAUDE.local.md` ya existe,** se añade el bloque sin tocar lo demás. Tampoco se
  añade el import: si el proyecto ya convive con ese fichero, no es decisión de slurp.
- **Asegurar que no se versiona** (`git check-ignore`; si no, al `.gitignore` existente). Si
  no hay `.gitignore`, se avisa claramente en el resumen.
- **Worktrees:** según la documentación, un `CLAUDE.local.md` ignorado solo existe en el
  worktree donde se creó. Se menciona en el resumen.

**Modo versionado (opción explícita del plan):**

| El proyecto tiene | Se escribe en |
|---|---|
| Solo `CLAUDE.md` | `CLAUDE.md` |
| Solo `AGENTS.md` | `AGENTS.md` |
| Ambos, y `CLAUDE.md` importa `@AGENTS.md` | `AGENTS.md` |
| Ambos, sin import | Los dos (cada uno lo lee un agente distinto) |
| Ninguno | `CLAUDE.md` nuevo |

**Sin duplicar (los dos modos):**

- Antes de escribir se revisan todos los ficheros que Claude cargaría: `CLAUDE.md`,
  `.claude/CLAUDE.md`, `CLAUDE.local.md`, y `AGENTS.md` cuando aplique.
- Si están los marcadores, se reemplaza lo que hay entre ellos. Así se puede volver a
  ejecutar `init` sin duplicar, y el bloque se actualiza con cada versión.
- Si no hay marcadores pero algún fichero ya menciona `slurp_query` o `mcp__slurp`, no se
  escribe y se avisa.
- Si no, se añade al final.
- Los marcadores son comentarios HTML de bloque; Claude Code los elimina antes de inyectar
  el fichero, así que cuestan 0 tokens.

**Texto** (variante para un grafo propio):

```markdown
<!-- slurp:begin — managed by `slurp init`; re-running it replaces this block -->
## Code navigation (slurp)

This project has a slurp MCP server: a code graph that stays in sync with the working tree. Before searching or reading files to find where something lives, call `slurp_query` with a plain-language description of the behaviour. Before editing a file, call `slurp_impact` with its path; before changing a function or class, call `slurp_explain` with its name. Use grep for exact strings, and open the files slurp points to before editing them.
<!-- slurp:end -->
```

Variante para graphify: la primera frase pasa a ser
`This project has a slurp MCP server over a graph built by graphify (rebuild it after large changes; slurp does not refresh it).`

---

## 6. Negociación de la versión de protocolo (decisión 7)

**Hoy:** el servidor responde siempre `2024-11-05` y contesta a `server/discover` con
`-32601`. Un cliente "dual-era" interpreta eso como servidor antiguo y cae a `initialize`.

**Versiones soportadas:**

```
MODERN = ("2026-07-28",)
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
```

**Cómo se anuncian y cómo se responde:**

- **Peticiones modernas** (llevan `params._meta["io.modelcontextprotocol/protocolVersion"]`):
  - **`server/discover`** devuelve:
    - `supportedVersions`: todas, las modernas primero;
    - `capabilities: {tools: {}}`;
    - `_meta["io.modelcontextprotocol/serverInfo"]`;
    - las `instructions`, dinámicas según el grafo;
    - `ttlMs` y `cacheScope: "private"` (las instrucciones dependen del grafo local);
    - `resultType: "complete"`.
  - **Versión soportada** → se sirve sin estado, según esa revisión: todo resultado lleva
    `resultType: "complete"` y el `serverInfo` en `_meta`, y `tools/list` lleva `ttlMs` y
    `cacheScope`.
  - **Versión no soportada** → error `-32022` "Unsupported protocol version" con
    `data: {supported: [todas], requested}`.
  - **`ping`** responde "method not found", porque la revisión 2026-07-28 lo eliminó.
  - El `clientInfo` de cada petición alimenta el log de sesión.
- **Peticiones legacy** (`initialize`):
  - Si la versión pedida está en `LEGACY`, se responde **esa misma**.
  - Si no, se responde la última legacy (`2025-11-25`), como dice la especificación.
  - El `instructions` del resultado es el mismo texto dinámico.
  - La versión queda fijada para todo el proceso (stdio = un cliente).
  - En legacy no existe ningún campo para listar versiones: la única forma de "anunciar" es
    la versión de la respuesta. La lista completa va también en la ayuda de `slurp serve` y
    en el README.

**Comportamiento que depende de la versión negociada:**

| Versión | Diferencia |
|---|---|
| `2025-03-26` | Debe aceptar lotes JSON-RPC (un array): se procesa cada mensaje y se responde con un array, omitiendo las notificaciones. En las demás versiones un array sigue siendo `-32600`, como hoy. |
| ≥ `2025-06-18` | Admite `title` de primer nivel en cada herramienta (se rellena con el `annotations.title` actual). |
| ≥ `2025-11-25` y moderna | La especificación aclara (SHOULD) que los errores de validación de entrada deben devolverse como error de ejecución de la herramienta (`isError`), para que el modelo pueda corregirse. **Decisión del autor (confirmada, decisión 10):** con estas versiones, argumentos inválidos y `BudgetTooSmallError` pasan de `-32602` a `isError`, con el mismo mensaje legible en el texto; un nombre de herramienta desconocido sigue siendo `-32602`. Con las versiones anteriores no cambia nada. Sustituye la decisión "Dos categorías de error" de CLAUDE.md, que ya lo recoge. |

Con este cambio, los clientes dual-era (Claude Code soporta conexiones 2026-07-28) pasarán a
hablar con slurp en la revisión moderna. Hay que probar Claude Code en las dos eras.

---

## 7. Medición con `slurp session` (criterio aprobado)

**Cambios en el log de sesión** (`.slurp/session.log`, sigue siendo opcional: solo se
escribe si existe `.slurp/`):

- **Una línea por cada llamada a cualquiera de las cinco herramientas**, con:
  - `session`: pid más la hora de inicio;
  - `client`: `clientInfo` de `initialize` o de cada petición moderna;
  - `protocol`;
  - `tool` y los argumentos resumidos (`query`, `budget`, `file_path`, `node_name`…);
  - `ms`;
  - `graph`: `fresh|refreshed|stale|failed|readonly`;
  - `refresh_ms` y `changed_files` (número y los primeros nombres);
  - `nodes`/`tokens` cuando aplique;
  - `error`.
- **Eventos de ciclo de vida:**
  - `start`: grafo, propiedad y versión;
  - `end`: al llegar EOF, una comprobación final lista los ficheros que cambiaron durante la
    sesión. En una tarea medida, esas son las ediciones del agente.
- **Las entradas antiguas se siguen leyendo** (`read_session_log` ya tolera formatos).
- **`slurp session --sessions`** resume por sesión:
  - cliente y duración;
  - llamadas por herramienta;
  - cuándo llega la primera llamada (+Xs desde el inicio);
  - ficheros cambiados antes de la primera llamada;
  - ficheros editados con o sin un `slurp_impact`/`slurp_explain` previo;
  - respuestas con aviso;
  - p95 de `refresh_ms`.

**Protocolo:**

- **10 tareas reales:** 5 de PrismaStats y 5 de slurp, sacadas del backlog (por ejemplo
  #28, #31, #33).
- **Los prompts no mencionan slurp, el grafo ni MCP.** Una sesión nueva por tarea, y
  `.slurp/` creado antes de empezar.
- **Condición B** = #76 + #77 instalados con `slurp init`. La condición A (v1.2.x sin
  instrucciones ni permisos) solo se mide si B no llega al criterio, para saber si el
  problema son las instrucciones o la fricción.
- **Prueba automática de fricción:** `slurp init --yes` en un proyecto temporal y luego
  `claude -p "<tarea>" --output-format json`. Se exige `permission_denials` vacío y al
  menos una llamada en el log.
  - Las llamadas denegadas no llegan al servidor, así que eso no lo puede medir slurp.
  - En `-p` no aparece el diálogo de aprobación del servidor; ese se comprueba a mano una
    vez, en una sesión interactiva.

**Cifra para dar el resultado por bueno** (fijada antes de medir):

- **Al menos 8 de 10 sesiones** con una llamada espontánea a slurp, y **en al menos 7 de
  10** esa primera llamada llega antes de cualquier edición.
- **Requisitos:**
  - 0 aprobaciones pedidas;
  - ninguna respuesta servida con grafo viejo sin aviso;
  - como mucho un 5 % de respuestas con aviso;
  - p95 de refresco ≤ 2 s.
- **Informativo, sin umbral:** porcentaje de ficheros editados con un `slurp_impact` previo.
- **Resolución:** con n = 10, cada tarea vale 10 puntos porcentuales.

---

## 8. Mediciones de tiempo

**Entorno:**

- macOS (Darwin 27.2.0) y el `.venv` del repo (Python 3.12, todas las gramáticas).
- slurp en `5d256e5` (v1.2.0 + docs).
- Fecha: 2026-11-25.
- En proceso, sin el arranque del CLI. Mediana de 5 repeticiones (20 o 200 en las
  comprobaciones baratas).

**Entradas desechables:**

- **PrismaStats:** copia del árbol de trabajo de `~/Desktop/prismastats` (HEAD `5d345dc`)
  sin `node_modules`, `.next`, `.git`, `graphify-out` ni `.slurp`, con `git init` y un
  commit. Da **1.582 nodos**, la misma cifra que CLAUDE.md usa para PrismaStats sin `.next/`.
- **slurp:** `git archive HEAD` con `git init` y un commit. Da **4.516 nodos**.
- Grafos escritos en `<copia>/.bench-out/graph.json`.

| | PrismaStats | slurp |
|---|---|---|
| Nodos / relaciones / tamaño de graph.json | 1.582 / 1.852 / 0,77 MB | 4.516 / 9.330 / 2,90 MB |
| `index_project()` | 266,4 ms | 439,1 ms |
| Índice completo + `json.dumps` + escritura atómica | 282,9 ms | 477,7 ms |
| `json.dumps(indent=2)` | 6,1 ms | 22,6 ms |
| `load_graph()` | 4,2 ms | 16,1 ms |
| `build_graph(dict)` | 2,4 ms | 9,7 ms |
| Query en frío (primera tras cargar) | 71,5 ms | 68,1 ms |
| Query en caliente | 9,0 ms | 22,4 ms |
| Recorrido completo + stat | 31,2 ms | 3,1 ms |
| **Manifiesto** (stat de ficheros, directorios y `.slurpignore`) | **0,4 ms** | **0,1 ms** |
| `git status --porcelain` | 12,4 ms | 9,3 ms |
| `git rev-parse HEAD` | 7,5 ms | 8,1 ms |
| `smart_reindex()` sin cambios | 92,0 ms | 60,1 ms |
| smart, vía rápida | 118,0 ms | 125,8 ms |
| smart → índice completo (símbolo nuevo) | 380,8 ms | 541,2 ms |

**Notas:**

- La vía rápida editó `.agents/skills/gsap/scripts/extract-audio-data.py` en PrismaStats y
  `scripts/latency.py` en slurp: añadir líneas al final, sin cambiar símbolos. El fallback
  añadió una función nueva al mismo fichero.
- La comprobación por manifiesto se simuló con stat de los mismos ficheros y directorios que
  recorre el indexador. No incluye la comparación por hash, que solo se hace cuando un stat
  no coincide.
- El CLI tarda más (0,50 s en PrismaStats, según CLAUDE.md) por el arranque del intérprete y
  el segundo recorrido del resumen de parsers.
- El script está en el Apéndice A y la salida literal en el Apéndice B.

---

## 9. Notas de la versión (obligatorias)

- **slurp ≤ 1.2.0 verá los grafos nuevos como ajenos** (decisión 1): la clave `slurp` está
  fuera de su `_SLURP_GRAPH_KEYS`. `slurp index` de esas versiones preguntará antes de
  sobrescribir, y `--force` lo resuelve.
  - La v1.2.1 ya lleva `"slurp"` en `_SLURP_GRAPH_KEYS` (#86) y los trata como propios:
    sobrescribe un grafo nuevo perdiendo el manifiesto, y la versión nueva lo adopta con un
    índice completo, sin pérdida de datos.
- **`graph.json` crece** con el manifiesto (PrismaStats, unos +35 KB estimados).
- **El MCP y las consultas del CLI refrescan el grafo y pueden reescribir `graph.json`.**
  Se desactiva con `--no-refresh` / `SLURP_REFRESH=0`, y el límite se cambia con
  `SLURP_REFRESH_TIMEOUT`. Los grafos de graphify nunca se tocan.
- **Las respuestas pueden empezar con un aviso** de grafo viejo o de refresco fallido.
- **`--watch` ahora sondea en lugar de escuchar eventos, y `watchdog` deja de ser
  dependencia.**
- **`slurp init`:**
  - construye por defecto el grafo propio de slurp (`graphify-out/slurp-graph.json` si ya
    hay uno de graphify);
  - escribe `.claude/settings.local.json` y `CLAUDE.local.md`;
  - añade líneas al `.gitignore`.
- **El autodescubrimiento prefiere `graphify-out/slurp-graph.json`** a
  `graphify-out/graph.json`.
- **El servidor negocia la versión de protocolo** (2024-11-05 a 2026-07-28) y responde a
  `server/discover`.
- **Con versiones de protocolo ≥ 2025-11-25, los argumentos inválidos se devuelven como error
  de la herramienta** (`isError: true`, con el mismo mensaje) en lugar del error JSON-RPC
  `-32602`, siguiendo la especificación 2025-11-25; también un presupuesto demasiado pequeño.
  Un nombre de herramienta desconocido sigue siendo `-32602`, y con versiones anteriores no
  cambia nada (§6, decisión 10).
- **El log de sesión cambia de formato:** campos nuevos y todas las herramientas. Las
  entradas antiguas se siguen leyendo.

---

## 10. Orden de implementación y tests

**Orden:**

0. **v1.2.1** (otra sesión): el bug de `.slurpignore`, la escritura atómica, el
   `# DECISION` y `"slurp"` en `_SLURP_GRAPH_KEYS` (#86).
1. **Módulo del manifiesto:** construir y comparar, solo con la biblioteca estándar. Todos
   los escritores lo graban. Propiedad del grafo en la librería (`_SLURP_GRAPH_KEYS` ya
   incluye `"slurp"`).
2. **Refresco dirigido por el manifiesto** en `smart.py`, compartido por el CLI, el
   servidor y `--watch`. `TestSmartEquivalence` se amplía.
3. **`write_graph`** con compare-and-swap y lock.
4. **Refrescador** con hilo, límite de tiempo, regla de espera y vuelta atrás tras un fallo.
5. **MCP:** `GraphState`, refresco al arrancar, avisos y `_meta`, federación por miembro.
6. **CLI:** la tabla de §3, `--no-refresh`, las variables de entorno y el autodescubrimiento.
7. **`--watch`** reescrito; `watchdog` fuera de `pyproject.toml` y `uv.lock`.
8. **#77 en el servidor:** textos, instrucciones dinámicas, `alwaysLoad` y negociación de
   protocolo.
9. **`slurp init`:** elección del grafo, `settings.local.json`, fichero de instrucciones,
   `.gitignore` y fragmentos para otros clientes.
10. **Log de sesión v2** y `slurp session --sessions`.
11. **Documentación:**
    - README y USAGE, con salidas reales vía `scripts/readme_examples.py`;
    - CLAUDE.md: secciones de `mcp.py`, `smart.py` e `init`, y la frase de las anotaciones;
    - notas de la versión de §9.
12. **Medición de §7** sobre la versión candidata.

**Invariantes con test:**

- **Manifiesto:**
  - detecta ficheros modificados, nuevos, borrados y renombrados (también los de solo
    mayúsculas), cambios de `.slurpignore` (incluida su aparición) y cambios de versión o de
    parsers;
  - el hash evita falsos positivos (`touch`, contenido idéntico);
  - la ventana de 2 s se verifica por hash;
  - un fichero excluido entero por `.slurpignore` no provoca un refresco en cada llamada.
- **Equivalencia:** el grafo tras un refresco es idéntico a un índice completo, en todos los
  escenarios de `TestSmartEquivalence` y en los nuevos.
- **Propiedad:**
  - un grafo de graphify queda idéntico byte a byte tras N llamadas con fuentes cambiadas;
  - el compare-and-swap aborta si otro escritor intervino;
  - la adopción solo ocurre si la raíz valida.
- **Atomicidad:** un lector concurrente nunca ve un fichero a medias.
- **Servidor:**
  - stdout solo contiene líneas JSON-RPC durante un refresco;
  - al superar el límite, el aviso va al principio de `content` y el estado en `_meta`;
  - la siguiente llamada sirve el grafo fresco;
  - nunca hay más de un refresco en marcha;
  - un fallo no se reintenta en bucle;
  - `initialize` no espera al refresco del arranque.
- **CLI:**
  - refrescan los comandos de §3, y `benchmark`/`eval` no escriben;
  - los avisos van a stderr y stdout queda limpio en `--format json`;
  - el refresco se termina antes de salir.
- **`--watch`:** debounce, todos los tipos de cambio, directorios saltados respetados, y
  ningún `import watchdog`.
- **`init`:**
  - reglas de fusión de `settings.local.json` (sin duplicar, deny y `disabledMcpjsonServers`
    respetados, JSON inválido intacto, ruta en la raíz git);
  - tabla de destino del párrafo, incluido el `@AGENTS.md`;
  - marcadores idempotentes, sin duplicar entre ficheros;
  - opción versionada;
  - entradas del `.gitignore`.
- **Protocolo:**
  - matriz de versiones legacy (eco o última) y moderna (sirve o `-32022` con `supported`);
  - `server/discover`;
  - lotes solo en 2025-03-26;
  - `ping` en las dos eras;
  - `resultType` y `ttlMs`/`cacheScope` donde toquen;
  - argumentos inválidos y `BudgetTooSmallError`: `isError` con ≥ 2025-11-25 y la moderna,
    `-32602` con las anteriores; herramienta desconocida: `-32602` en todas.
- **Textos:**
  - instrucciones ≤ 4.096 caracteres y primer párrafo ≤ 512;
  - `alwaysLoad` solo en `slurp_query`;
  - variante de graphify cuando corresponde.

---

## 11. Riesgos

1. **Sobrescribir un grafo de graphify.** La detección positiva y el compare-and-swap lo
   reducen al mínimo. Queda una ventana de microsegundos entre la comprobación y el
   `os.replace` frente a escritores que no usan nuestro lock.
2. **Raíz equivocada al adoptar un grafo antiguo.** Por eso la adopción se valida (90 % de
   los `source_file`) y, si no valida, se queda en solo lectura.
3. **Primer hilo dentro de slurp.** "No usar async" sigue cumpliéndose, pero es concurrencia
   nueva. Afecta a stdout y a las cachés, y lo cubren los tests de §10. Una query durante un
   refresco puede tardar el doble por el GIL (estimado, no medido).
4. **Repos grandes:** refrescos completos de decenas de segundos en segundo plano por cada
   edición que cambie símbolos. Mientras tanto se sirven respuestas con aviso.
5. **Windows:** `os.replace` con el fichero abierto por otro proceso requiere reintentos, y
   el lock usa `msvcrt`.
6. **Sistemas de ficheros que no actualizan el mtime de los directorios:** el retraso queda
   acotado a 30 s por la red de seguridad.
7. **Compatibilidad del formato:** slurp ≤ 1.2.0 ve los grafos nuevos como ajenos (§9).
8. **`readOnlyHint` junto a una escritura del grafo.** Se justifica como caché (§5.1), pero
   es discutible.
9. **Comportamiento de Claude Code que puede cambiar:** `settings.local.json` en la raíz git,
   la confianza del workspace, `enabledMcpjsonServers`, la lectura de AGENTS.md (v2.1.277+).
   Hay que verificar el caso en que `init` escribe `.mcp.json` en un subdirectorio y
   `settings.local.json` va a la raíz git.
10. **`CLAUDE.local.md` oculta `AGENTS.md` a Claude Code.** Se mitiga con el import, pero
    los `AGENTS.md` anidados dejan de cargarse salvo que el usuario cambie "Project
    instructions". Además, el fichero local no se comparte entre worktrees.
11. **Otros agentes no ven el párrafo local** (no hay equivalente local de AGENTS.md);
    dependen de las instrucciones del servidor.
12. **Negociación moderna (2026-07-28):** se implementa una era que no se puede probar con
    todos los clientes. Se prueba al menos Claude Code en las dos eras.
13. **`alwaysLoad`** cuesta tokens de contexto en cada sesión.
14. **Medición:** n = 10, el modelo varía de una sesión a otra, el párrafo de instrucciones
    forma parte de la configuración (no es uso espontáneo "puro") y las denegaciones solo se
    ven fuera de slurp.
15. **El CLI termina el refresco antes de salir.** Puede sorprender que un comando tarde más
    de lo habitual tras imprimir la respuesta; se mitiga con el mensaje y con Ctrl+C.
16. **El autodescubrimiento cambia** para quien ya tenga `graphify-out/slurp-graph.json`.

---

## 12. Fuentes consultadas (2026-11-25)

**Claude Code** (`https://code.claude.com/docs/en/…`):

- `permissions.md`:
  - sintaxis `mcp__<server>__*` (las reglas de permiso no admiten especificadores);
  - "don't ask again" guarda en `.claude/settings.local.json` en la raíz git;
  - confianza del workspace y qué se aplica antes de confiar.
- `mcp.md`:
  - aprobación de servidores de proyecto y confianza;
  - "Mark tools that are safe to call twice" (qué hacen `readOnlyHint` e
    `idempotentHint`);
  - `anthropic/requiresUserInteraction` y `anthropic/alwaysLoad`;
  - límites de 4.096 y 16.384 caracteres;
  - tool search;
  - paso a segundo plano a los 2 minutos y `MCP_TOOL_TIMEOUT`;
  - `CLAUDE_PROJECT_DIR`.
- `settings.md`: ubicación de `settings.local.json` y quién lo ignora en git.
- `settings-reference.md`: `enabledMcpjsonServers`, `enableAllProjectMcpServers` y
  `disabledMcpjsonServers` (el rechazo tiene precedencia).
- `permission-modes.md`: en modo auto, las reglas se resuelven antes que el clasificador.
- `memory.md`:
  - `CLAUDE.local.md`;
  - AGENTS.md (cuándo se lee, qué ficheros cuentan, `AGENTS.local.md` y
    `AGENTS.override.md` no se leen, "Project instructions", el import `@AGENTS.md`);
  - los comentarios HTML se eliminan.

**Especificación MCP** (`https://modelcontextprotocol.io/specification/…`):

- `2025-11-25/server/tools`: las anotaciones no son de confianza.
- `2025-11-25/schema`: `ToolAnnotations`.
- `2025-11-25/basic/lifecycle`: negociación en `initialize`.
- `2025-11-25/changelog`: errores de validación como `isError`.
- `2025-06-18/changelog`: eliminación de los lotes, `title`.
- `versioning`: revisión actual 2026-07-28.
- `2026-07-28/basic/versioning`: negociación por petición, `-32022`, servidores dual-era.
- `2026-07-28/server/discover`.
- `2026-07-28/changelog`: sin estado, `resultType`, `ttlMs`/`cacheScope`, eliminación de
  `ping`.
- `2026-07-28/basic/index`: formato de las claves de `_meta`.

**Otros clientes:**

- Codex: `developers.openai.com/codex/mcp` (`default_tools_approval_mode`, instrucciones y
  sus 512 caracteres, `.codex/config.toml`) y `developers.openai.com/codex/guides/agents-md`
  (`AGENTS.override.md` sustituye a `AGENTS.md`, uno por directorio).
- Gemini CLI: `github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md`
  (`trust`).
- Cursor: `cursor.com/docs/context/mcp` (aprobación, `.cursor/mcp.json`) y
  `cursor.com/docs/cli/reference/permissions` (`Mcp(server:tool)`).
- VS Code: `code.visualstudio.com/docs/copilot/customization/mcp-servers` y
  `…/reference/mcp-configuration`. No se encontró ninguna clave para auto-aprobar.
- `agents.md`: el estándar no define una variante local.

---

## 13. Fuera de alcance

- **Vigilante en segundo plano en el servidor** (§2.5, con criterio de revisión).
- **Refrescar o regenerar grafos de graphify.**
- **Escribir configuración de otros clientes** antes de probarlos (fase 2 de §5.2).
- **Idea para más adelante, no diseñada:** `slurp_diff` sin argumentos para comparar el
  grafo del inicio de la sesión con el actual. El servidor tendría los dos en memoria tras
  un refresco.

---

## Apéndice A — script de medición

Se ejecutó como `uv run python measure.py <copia-de-PrismaStats> <copia-de-slurp>` desde el
repo, sobre las copias desechables de §8 (con `.bench-out/` en `.git/info/exclude`).

```python
"""Design-phase measurements for #76 (always-fresh graph). Not part of slurp.

Usage: uv run python /tmp/slurp-design/bench/measure.py ROOT [ROOT...]
Every write goes to ROOT/.bench-out/graph.json inside the throwaway copies.
"""

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

from slurp.budget import DEFAULT_MIN_SCORE, build_context
from slurp.ignore import load_ignore
from slurp.indexer import _is_skipped_dir, index_project, iter_source_files
from slurp.loader import build_graph, load_graph
from slurp.scorer import clear_pagerank_cache, score_nodes
from slurp.smart import head_commit, smart_reindex


def med(fn, n):
    ts = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t) * 1000)
    return statistics.median(ts), min(ts), max(ts)


def fmt(label, r):
    print(f"  {label:<48} median {r[0]:8.1f} ms   (min {r[1]:.1f}, max {r[2]:.1f})")


def atomic_write(path: Path, text: str):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def full_index(root: Path, out: Path, ignore):
    g = index_project(root, ignore)
    g["built_at_commit"] = head_commit(root)
    atomic_write(out, json.dumps(g, indent=2))
    return g


def walk_dirs_and_files(root: Path):
    dirs, files = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not _is_skipped_dir(d, here / d)]
        dirs.append(here)
    files = list(iter_source_files(root))
    return dirs, files


def main(root: Path):
    print(f"\n=== {root}")
    out = root / ".bench-out" / "graph.json"
    out.parent.mkdir(exist_ok=True)
    ignore = load_ignore(root / ".slurpignore")

    g = full_index(root, out, ignore)
    print(f"  nodes {len(g['nodes'])}, links {len(g['links'])}, "
          f"graph.json {out.stat().st_size / 1e6:.2f} MB")

    fmt("index_project() only", med(lambda: index_project(root, ignore), 5))
    fmt("full index + json.dumps + atomic write", med(lambda: full_index(root, out, ignore), 5))
    data = json.loads(out.read_text())
    fmt("json.dumps(indent=2) only", med(lambda: json.dumps(data, indent=2), 5))
    fmt("load_graph(graph.json)", med(lambda: load_graph(out), 5))
    fmt("build_graph(dict) (no disk read)", med(lambda: build_graph(data), 5))

    G = load_graph(out)
    q = "authentication and session handling"

    def query():
        s = score_nodes(G, q)
        build_context(G, s, budget=4000, min_score=DEFAULT_MIN_SCORE, query=q)
    clear_pagerank_cache()
    fmt("query, cold (first after load)", med(query, 1))
    fmt("query, warm", med(query, 10))

    # --- freshness checks -------------------------------------------------
    print("  -- freshness check candidates (per call)")
    fmt("walk: iter_source_files() + stat each", med(
        lambda: [p.stat() for p in iter_source_files(root)], 20))
    dirs, files = walk_dirs_and_files(root)
    ign = root / ".slurpignore"

    def manifest_check():
        for d in dirs:
            d.stat()
        for f in files:
            try:
                f.stat()
            except FileNotFoundError:
                pass
        try:
            ign.stat()
        except FileNotFoundError:
            pass
        out.stat()
    print(f"  manifest size: {len(files)} files + {len(dirs)} dirs")
    fmt("manifest: stat files + walked dirs + .slurpignore", med(manifest_check, 200))

    def git_status():
        subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                       capture_output=True, check=True)
    fmt("git status --porcelain (subprocess)", med(git_status, 20))

    def git_rev():
        subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                       capture_output=True, check=True)
    fmt("git rev-parse HEAD (subprocess)", med(git_rev, 20))

    fmt("smart_reindex(), nothing changed", med(lambda: smart_reindex(root, out, ignore), 5))

    # --- smart, fast path: body-only edit --------------------------------
    target = next(p for p in files if p.suffix in (".ts", ".tsx", ".py"))
    original = target.read_bytes()
    times = []
    for i in range(5):
        target.write_bytes(original + b"\n" * (i + 1))
        t = time.perf_counter()
        r = smart_reindex(root, out, ignore)
        times.append((time.perf_counter() - t) * 1000)
        assert not r.needs_full_index, r.fallback_reason
    target.write_bytes(original)
    smart_reindex(root, out, ignore)
    fmt(f"smart fast path (edit {target.relative_to(root)})",
        (statistics.median(times), min(times), max(times)))

    # --- smart, fallback: a new symbol forces a full index --------------
    times = []
    for i in range(5):
        if target.suffix == ".py":
            extra = f"\n\ndef bench_added_{i}():\n    return {i}\n".encode()
        else:
            extra = f"\nexport function benchAdded{i}() {{ return {i}; }}\n".encode()
        target.write_bytes(original + extra)
        t = time.perf_counter()
        r = smart_reindex(root, out, ignore)
        assert r.needs_full_index
        full_index(root, out, ignore)
        times.append((time.perf_counter() - t) * 1000)
    target.write_bytes(original)
    full_index(root, out, ignore)
    fmt("smart fallback: new symbol → full index",
        (statistics.median(times), min(times), max(times)))


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        main(Path(arg).resolve())
```

## Apéndice B — salida literal de la medición

```
=== /private/tmp/slurp-design/ps
  nodes 1582, links 1852, graph.json 0.77 MB
  index_project() only                             median    266.4 ms   (min 262.0, max 269.9)
  full index + json.dumps + atomic write           median    282.9 ms   (min 278.3, max 288.8)
  json.dumps(indent=2) only                        median      6.1 ms   (min 6.0, max 6.2)
  load_graph(graph.json)                           median      4.2 ms   (min 4.1, max 4.6)
  build_graph(dict) (no disk read)                 median      2.4 ms   (min 2.4, max 2.5)
  query, cold (first after load)                   median     71.5 ms   (min 71.5, max 71.5)
  query, warm                                      median      9.0 ms   (min 8.9, max 9.8)
  -- freshness check candidates (per call)
  walk: iter_source_files() + stat each            median     31.2 ms   (min 31.0, max 35.5)
  manifest size: 185 files + 161 dirs
  manifest: stat files + walked dirs + .slurpignore median      0.4 ms   (min 0.3, max 0.6)
  git status --porcelain (subprocess)              median     12.4 ms   (min 11.9, max 14.9)
  git rev-parse HEAD (subprocess)                  median      7.5 ms   (min 7.1, max 8.0)
  smart_reindex(), nothing changed                 median     92.0 ms   (min 88.9, max 100.4)
  smart fast path (edit .agents/skills/gsap/scripts/extract-audio-data.py) median    118.0 ms   (min 117.1, max 122.9)
  smart fallback: new symbol → full index          median    380.8 ms   (min 378.6, max 396.8)

=== /private/tmp/slurp-design/sl
  nodes 4516, links 9330, graph.json 2.90 MB
  index_project() only                             median    439.1 ms   (min 432.4, max 448.2)
  full index + json.dumps + atomic write           median    477.7 ms   (min 472.4, max 479.5)
  json.dumps(indent=2) only                        median     22.6 ms   (min 22.5, max 23.0)
  load_graph(graph.json)                           median     16.1 ms   (min 15.9, max 20.8)
  build_graph(dict) (no disk read)                 median      9.7 ms   (min 9.5, max 13.4)
  query, cold (first after load)                   median     68.1 ms   (min 68.1, max 68.1)
  query, warm                                      median     22.4 ms   (min 22.3, max 22.7)
  -- freshness check candidates (per call)
  walk: iter_source_files() + stat each            median      3.1 ms   (min 3.0, max 3.4)
  manifest size: 54 files + 16 dirs
  manifest: stat files + walked dirs + .slurpignore median      0.1 ms   (min 0.1, max 0.1)
  git status --porcelain (subprocess)              median      9.3 ms   (min 8.9, max 12.4)
  git rev-parse HEAD (subprocess)                  median      8.1 ms   (min 7.5, max 8.5)
  smart_reindex(), nothing changed                 median     60.1 ms   (min 59.3, max 62.8)
  smart fast path (edit scripts/latency.py)        median    125.8 ms   (min 123.2, max 129.0)
  smart fallback: new symbol → full index          median    541.2 ms   (min 533.6, max 544.3)
```
