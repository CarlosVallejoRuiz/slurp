# Medición con agentes — conclusiones (2026-11-26)

**Resultado: la condición B cumple el criterio fijado** (`analyze_sessions.py` sale con 0).
La condición A no hace falta medirla.

**Condiciones:**

- **slurp:** el de este checkout en `b6ec37f`. Los commits posteriores solo tocan los scripts
  de medición.
- **Cliente:** Claude Code 2.1.331 con el modelo `claude-opus-5-5`, con `claude -p` y los
  permisos acordados (`medicion-agentes.md`).
- **Ejecución:** diez sesiones, una por tarea, cada una en un clon nuevo fuera de los repos
  reales. En total, 52 min de reloj y 21,24 $ de coste según `claude`, a cargo de la
  suscripción.

Los datos de cada sesión están en `<id>/resultado.json` y `<id>/sessions.txt`. Los diffs, las
respuestas del agente y los streams se quedan fuera del repo.

## Tabla por sesión

| Tarea | Llamadas a slurp | 1.ª antes de editar | slurp denegadas | Otras denegaciones | graphify | Avisos | Refrescos | Terminó | Tiempo |
|---|---|---|---|---|---|---|---|---|---|
| ps-1 | query 1 · impact 2 (1 con error) | sí | 0 | 5 | no | 0 | 0 | sí | 3 min 13 s |
| ps-2 | query 1 · impact 2 (1 con error) | sí | 0 | 2 | no | 0 | 0 | sí | 1 min 51 s |
| ps-3 | query 1 | sí | 0 | 4 | no | 0 | 0 | sí | 1 min 18 s |
| ps-4 | query 1 · explain 1 | sí | 0 | 5 | no | 0 | 1 (325 ms) | sí | 7 min 0 s |
| ps-5 | query 1 · impact 2 (1 con error) | sí | 0 | 3 | no | 0 | 0 | sí | 1 min 37 s |
| sl-1 | — | — | 0 | 5 | no | — | — | sí | 2 min 36 s |
| sl-2 | query 1 | sí | 0 | 9 | no | 0 | 0 | sí | 9 min 26 s |
| sl-3 | query 1 | sí | 0 | 7 | no | 0 | 0 | sí | 7 min 1 s |
| sl-4 | query 1 | sí | 0 | 7 | no | 0 | 0 | sí | 11 min 17 s |
| sl-5 | query 1 | sí | 0 | 1 | no | 0 | 0 | sí | 6 min 41 s |

**Cómo se juzgó "Terminó":** las diez cumplen los cuatro requisitos acordados:

- `claude` devolvió `success`;
- el diff no está vacío;
- los checks del proyecto pasan: `tsc --noEmit` en PrismaStats, y pytest y ruff en slurp;
- leído el diff, hace lo que pide el prompt.

Las dos tareas de PrismaStats que añadían tests los tienen en verde: `npm test` 7/7 y 9/9.

**Tiempo:** es el de reloj (`seconds.txt`). En sl-2, `claude.json` solo cubre el último tramo
de la sesión: el agente dejó una tarea en segundo plano y la sesión tuvo dos eventos `result`.

## Resultado contra el criterio

| Requisito | Umbral | Medido | |
|---|---|---|---|
| Sesiones con llamada espontánea a slurp | ≥ 8/10 | 9/10 | PASS |
| Primera llamada antes de cualquier edición | ≥ 7/10 | 9/10 | PASS |
| Herramientas de slurp denegadas | 0 | 0 | PASS |
| Respuestas con grafo viejo sin aviso | 0 | 0 | PASS |
| Respuestas con aviso | ≤ 5 % | 0 % (0 de 16) | PASS |
| p95 de `refresh_ms` | ≤ 2 s | 325 ms | PASS |

**Informativo:** 4 de los 26 ficheros editados (15 %) tuvieron antes un `slurp_impact` o un
`slurp_explain`. Las otras 48 denegaciones, que no entran en el criterio, se desglosan más
abajo. graphify se usó en 0/10 sesiones.

**Lo que el criterio no dice:**

- **El refresco casi no se ejerció.** De las 16 llamadas, 15 encontraron el grafo al día; solo
  una (ps-4) tuvo que refrescarlo. Los agentes consultan slurp al empezar, antes de editar, y
  casi nunca después. El p95 de 325 ms es, por tanto, un único valor, y los dos requisitos de
  avisos se cumplen sin haberse puesto a prueba. La prueba del grafo siempre fresco con
  ediciones concurrentes está en `../verificacion.md`, no aquí.
- **"0 denegaciones" está medido en `claude -p`.** Antes de cada sesión, `claude mcp get slurp`
  decía "⏸ Pending approval" en los diez clones. Aun así, dentro de la sesión el servidor
  conectó y sus herramientas se usaron sin ninguna denegación. El paso 2 del protocolo lo
  confirma: el autor abrió un clon de PrismaStats con `slurp init` en una sesión
  interactiva, y Claude Code no pidió ninguna aprobación de slurp; el servidor aparecía
  conectado.

## La ejecución abortada

La primera ejecución se paró en ps-1 y no cuenta. **Su primera acción también fue una llamada
espontánea a `slurp_query`.** Claude Code la denegó porque PrismaStats versiona
`.claude/settings.local.json`, y Claude Code ignora ese fichero mientras git lo siga. De ahí
salieron dos cambios:

- **en slurp**, `220bc61`: el aviso de `init` lo dice claramente, con el comando exacto;
- **en la medición**, la enmienda de `medicion-agentes.md`: el fichero se saca de git en cada
  clon, y ningún permiso puede apuntar fuera del clon.

## Lo aprendido de la sesión sin slurp (sl-1)

Solo una sesión no llamó a slurp: sl-1. Su prompt ya nombraba los dos símbolos (`_anyDiff` y
`_resolve_node_type`) y el componente que los contiene (el visualizador HTML). La primera
acción del agente fue un `grep` de esos dos nombres; después leyó `slurp/viz.py` y editó.

Cuando el prompt ya dice qué buscar, la tarea es localizar, no descubrir. Para eso, una
búsqueda de texto es más directa que el grafo, y la descripción de `slurp_query` lo prevé:
"for one known symbol use slurp_explain; for exact text use grep".

No es un fallo de fricción: el servidor estaba conectado y aprobado. Tampoco parece un fallo
del texto: las otras cuatro tareas de slurp, todas más abiertas, empezaron por `slurp_query`.

Queda una hipótesis sin comprobar. Para `_resolve_node_type`, la descripción recomienda
`slurp_explain`, pero esa herramienta es diferida: hasta cargarla con `ToolSearch`, el agente
solo ve su nombre (hallazgo 1).

**Un factor que esta medición no separa:** en los clones de slurp, `init` no escribió el
párrafo de instrucciones, porque el `CLAUDE.md` del proyecto ya menciona `slurp_query` (lo
documenta como parte del código). Por eso las cinco sesiones de slurp solo tuvieron las
instrucciones del servidor y las descripciones de las herramientas. Las de PrismaStats tenían
además `CLAUDE.local.md`. El resultado fue 5/5 en PrismaStats y 4/5 en slurp. Con n = 5 por
lado, y con tareas distintas, no se puede atribuir esa diferencia al párrafo.

## Otros hallazgos

1. **La primera llamada a `slurp_impact` falló en las tres sesiones que lo usaron** (ps-1,
   ps-2 y ps-5):
   - **Qué pasó:** el agente adivinó el nombre del argumento (`target`, `path`) sin haber
     visto el esquema. Solo `slurp_query` lleva `alwaysLoad` (decisión 5 del diseño), así que
     Claude Code difiere las otras cuatro herramientas.
   - **Cómo se recuperó:** el error vino con el esquema y el reintento funcionó.
   - **Coste:** una llamada perdida por sesión.
   - **El otro caso:** `slurp_explain` se cargó con `ToolSearch` (ps-4) y acertó a la
     primera.
   - **Opciones para el autor:**
     - poner `alwaysLoad` también en `slurp_impact`, a cambio de más contexto en cada sesión;
     - nombrar el argumento en la descripción de las instrucciones del servidor;
     - dejarlo como está.

   **Decisión del autor (#93):** sin más `alwaysLoad`. Las instrucciones del servidor nombran
   ahora el argumento (`slurp_impact(file_path=...)`, `slurp_explain(node_name=...)`), y
   `slurp_impact` acepta también `target` y `path`.
2. **Uso de slurp:** los agentes lo usan para orientarse al empezar.
   - En las 9 sesiones que lo usaron, la primera llamada fue a `slurp_query`.
   - En 8 de ellas fue la primera acción de la sesión; en sl-2 llegó segunda, tras un comando
     de shell.
   - Rara vez lo consultan antes de cada edición: 15 % de ficheros con un impact o explain
     previo.
   - Después de esa primera llamada trabajan con `grep`, `Read` y `Bash`.
3. **Las otras 48 denegaciones**, ninguna de herramientas de slurp:
   - 44 son comandos de shell. Unos están fuera de la lista permitida: `uv run python` (7),
     `node --test`, `npx next lint`, `uvx vulture`. Otros están en la lista, pero Claude Code
     no los aprueba solo porque van encadenados o llevan variables o comillas, como
     `npx tsc --noEmit` con tuberías.
   - De esos 44, 4 son `uv run slurp …` en las tareas de slurp: el agente probaba el CLI que
     estaba modificando, no consultaba el grafo.
   - 3 son escrituras fuera del clon (`/tmp/...`). Que se denegaran es justo lo que se
     buscaba con el aislamiento.
   - 1 es la edición de un fichero de instrucciones que es un enlace simbólico.

   Los agentes siguieron adelante en todos los casos.
4. **graphify, 0/10:** la primera cuenta salió 4/10, pero eran falsos positivos del detector,
   que contaba rutas como `graphify-out/graph.json` o el fixture `graphify_project`. Se
   corrigió en `4f1cc99`: ahora solo cuenta la skill, el comando o un `import`.
5. **Errores del análisis corregidos tras la ejecución:**
   - `1060659`: las denegaciones se cuentan sobre todos los eventos `result`, porque sl-2
     tenía 9 y el análisis inicial le contaba 0;
   - el detector de graphify (punto 4).

   Ninguno de los dos cambia el criterio.
6. **La premisa de ps-3 era en parte inexacta.** La tarea se diseñó así; el agente lo detectó
   y explicó qué había cambiado.
7. **Un detalle de `init`:** cuando no iba a escribir el párrafo de instrucciones, el plan lo
   anunciaba igual, como "4. Tell the agent when to use slurp in — (local, not versioned)".
   Corregido en #94: ahora dice que no escribe ningún párrafo y qué fichero ya menciona
   slurp.
