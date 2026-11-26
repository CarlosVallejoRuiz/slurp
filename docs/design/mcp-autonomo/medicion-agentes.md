# Medición con agentes (§7 del diseño) — protocolo preparado, sin lanzar

**Estado:** preparado el 2026-11-26 sobre la v1.3 en desarrollo (`main`, versión 1.2.1 sin
bump). **No se ha ejecutado.** Se lanza junto con el autor tras revisar la fase.

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
3. **Lanzar:** `docs/design/mcp-autonomo/run_tasks.sh ~/slurp-medicion/AAAA-MM-DD`. Por
   cada tarea:
   - un clon nuevo del proyecto (PrismaStats con su `node_modules` enlazado; slurp con
     `uv sync`);
   - `slurp init --yes` con el slurp de este checkout (escribe `.mcp.json`,
     `.claude/settings.local.json`, `CLAUDE.local.md` y el grafo propio);
   - `.slurp/` creado antes de empezar, para que se escriba el log de sesión;
   - **una sesión nueva** de `claude -p "<prompt>" --output-format json`, con los mismos
     `CLAUDE_FLAGS` en las diez.
   - Queda en `<resultados>/<id>/`: `claude.json`, `session.log`, `init.txt`, `diff.txt`.
4. **Analizar:** `uv run python docs/design/mcp-autonomo/analyze_sessions.py <resultados>`.
   También `slurp session --sessions --log-dir <resultados>/<id>` para ver una sesión.

## Criterio (fijado antes de medir, §7)

- **Al menos 8 de 10 sesiones** con una llamada espontánea a slurp, y **en al menos 7 de 10**
  esa primera llamada llega antes de cualquier edición (la respuesta de la primera llamada no
  vio ningún fichero cambiado).
- **Requisitos:**
  - 0 aprobaciones pedidas (`permission_denials` vacío en `claude.json`);
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
