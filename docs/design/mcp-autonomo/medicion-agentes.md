# Medición con agentes (§7 del diseño) — medida el 2026-11-26: cumple el criterio

**Estado:** preparado el 2026-11-26 sobre la v1.3 en desarrollo (`main`, versión 1.2.1 sin
bump). Tareas confirmadas por el autor el 2026-11-26 (las cinco de PrismaStats le valen).
Resultados en `resultados/`; conclusiones en `resultados/conclusiones.md`. Medida el
2026-11-26 (condición B, diez sesiones): **cumple el criterio**. Pendiente: el paso 2,
la comprobación interactiva del diálogo de aprobación, que hace el autor.

**Decisiones del autor antes de lanzar (2026-11-26):**

- `claude -p` con `--permission-mode acceptEdits` y una lista fija de comandos permitidos
  (`git status/diff/log`, `ls`, `cat`, `grep`, `find`, `uv run pytest`, `uv run ruff`,
  `npx tsc`, `npm test`), **sin ninguna regla para slurp**: su aprobación tiene que venir del
  `.claude/settings.local.json` que escribe `slurp init`.
- **"0 aprobaciones" del criterio = 0 denegaciones de herramientas `mcp__slurp__*`** en
  `permission_denials`. Las demás denegaciones se dan aparte, fuera del criterio.
- La skill de graphify del entorno del autor se queda como está; se registra en cada sesión si
  el agente usó graphify (la skill o el comando).
- "Terminó la tarea": resultado `success` de `claude`, diff no vacío, checks del proyecto
  (pytest y ruff en slurp, `tsc --noEmit` en PrismaStats) y lectura del diff.

**Enmienda tras la primera ejecución, abortada en ps-1 (2026-11-26, decisión del autor):**
Claude Code ignora un `.claude/settings.local.json` que git sigue, y PrismaStats lo versiona:
la aprobación de `slurp init` no se aplicaba y el `slurp_query` con que el agente empezó ps-1
fue denegado. En cada clon, antes de `slurp init`, se hace lo que `init` aconseja
(`git rm --cached .claude/settings.local.json`) y `contain_settings.py rewrite` apunta dentro
del clon toda ruta del checkout real en los settings del proyecto (PrismaStats lista
`additionalDirectories` de `~/Desktop/prismastats`); los dos cambios se confirman en el clon.
Antes de cada sesión, `contain_settings.py check` exige que ninguna regla de permiso ni
directorio adicional del proyecto apunte fuera del clon. La medición se relanzó desde cero;
ps-1 abortada no cuenta. Los settings de usuario (`~/.claude/settings.json`) no se tocan.

Ficheros de esta carpeta:

- `tareas.json` — las diez tareas. Las cinco de slurp salen del roadmap (filas 24, 26, 28, 31
  y 33 de `Roadmap slurp.xlsx`). **Las cinco de PrismaStats son propuestas** sacadas de
  `docs/roadmap.md` y del código: hay que confirmarlas o sustituirlas.
- `run_tasks.sh` — lanza las tareas, una sesión nueva de `claude -p` por tarea.
- `analyze_sessions.py` — lee los resultados y dice si se cumple el criterio.

## Qué se mide

La condición **B**: #76 (grafo siempre fresco) y #77 (cero fricción) instalados con
`slurp init --yes`. La condición A (v1.2.x sin instrucciones ni permisos) solo se mide si B
no llega al criterio, para separar el efecto de las instrucciones del de la fricción.

## Protocolo

1. **Confirmar las tareas.** Revisar `tareas.json`; los prompts no pueden mencionar slurp, el
   grafo ni MCP (`tests/test_measurement_scripts.py` lo comprueba).
2. **Una vez, a mano, en una sesión interactiva:** abrir Claude Code en una copia con
   `slurp init` hecho y comprobar que no aparece el diálogo de aprobación del servidor de
   `.mcp.json` ni el de cada herramienta. En `claude -p` ese diálogo no aparece, así que la
   ejecución automática no lo puede medir. El diálogo de confianza del workspace sí puede
   aparecer la primera vez: es de Claude Code, no de slurp.
3. **Lanzar:** `docs/design/mcp-autonomo/run_tasks.sh /tmp/slurp-medicion/AAAA-MM-DD`
   (fuera de los repos reales; el script se niega si no). Por cada tarea:
   - un clon nuevo del proyecto, sin remoto (PrismaStats con una copia copy-on-write de su
     `node_modules`; slurp con `uv sync`);
   - `slurp init --yes` con el slurp de este checkout (escribe `.mcp.json`,
     `.claude/settings.local.json`, `CLAUDE.local.md` y el grafo propio);
   - `.slurp/` creado antes de empezar, para que se escriba el log de sesión;
   - **una sesión nueva** de `claude -p "<prompt>" --output-format stream-json --verbose`,
     con los mismos permisos en las diez (arriba);
   - queda en el directorio de trabajo `<id>/`: `stream.jsonl`, `claude.json`, `session.log`,
     `mcp-get.txt`, el diff y los checks; en `resultados/<id>/` del repo solo `sessions.txt`
     (`slurp session --sessions`) y `resultado.json`.
4. **Analizar:** `uv run python docs/design/mcp-autonomo/analyze_sessions.py <trabajo>`.

## Criterio (fijado antes de medir, §7)

- **Al menos 8 de 10 sesiones** con una llamada espontánea a slurp, y **en al menos 7 de 10**
  esa primera llamada llega antes de cualquier edición (la respuesta de la primera llamada no
  vio ningún fichero cambiado).
- **Requisitos:**
  - 0 aprobaciones pedidas: 0 denegaciones de `mcp__slurp__*` en `permission_denials`;
  - ninguna respuesta servida con grafo viejo sin aviso (una llamada con `graph` `stale` o
    `failed` siempre lleva `warned`);
  - como mucho un 5 % de respuestas con aviso;
  - p95 de `refresh_ms` ≤ 2 s.
- **Informativo, sin umbral:** porcentaje de ficheros editados con un `slurp_impact` o
  `slurp_explain` sobre ellos antes de la edición.
- **Resolución:** con n = 10, cada tarea vale 10 puntos porcentuales.

## Cómo se calcula cada cifra

Del log de sesión v2 (`slurp/session.py`):

- **Llamada espontánea:** al menos una línea `call`.
- **Primera llamada antes de editar:** `changed.count` de la primera llamada es 0. El refresco
  de arranque se lleva lo que cambió antes de la sesión, así que lo que ve la primera llamada
  se editó después de arrancar el servidor.
- **Ficheros editados:** la lista del evento `end`, que compara el disco al cerrar con una
  instantánea tomada antes del primer mensaje.
- **Editado tras un impact/explain:** hubo un `slurp_impact` con ese fichero, o un
  `slurp_explain` de un nodo de ese fichero, antes de la llamada cuya respuesta vio el fichero
  cambiado por primera vez (o antes del final, si se editó tras la última llamada).

## Riesgos de la medición (§11.14)

n = 10; el modelo varía de una sesión a otra; el párrafo de instrucciones forma parte de la
configuración (no es uso espontáneo "puro"); las denegaciones solo se ven fuera de slurp, en
`claude.json`. Las tareas de PrismaStats son propuestas de Claude Code, no del backlog del
autor.
