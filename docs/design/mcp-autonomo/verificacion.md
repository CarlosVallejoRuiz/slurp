# Verificación de la fase (#76, #77) — 2026-11-26

Implementación de `docs/design/mcp-autonomo.md` en `main` (v1.3 en desarrollo, versión 1.2.1
sin bump). Este documento recoge la verificación del final de la fase: frescura con cambios
reales, mediciones frente a §8, escrituras concurrentes y lo que escribe `slurp init` en una
copia de PrismaStats. La medición con agentes (§7) está preparada en `medicion-agentes.md` y
sin lanzar.

## Entorno

- macOS (Darwin 27.2.0), `.venv` del repo (Python 3.12, todas las gramáticas).
- Copias desechables, como en §8:
  - **PrismaStats:** el árbol de `~/Desktop/prismastats` (HEAD `5d345dc`) sin `node_modules`,
    `.next`, `.git`, `graphify-out` ni `.slurp`, con `git init` y un commit. 1.582 nodos.
  - **slurp:** `git archive HEAD` de esta fase, con `git init` y un commit. **5.477 nodos**
    (§8 midió 4.516: el código creció un 21 % en esta fase).
- Script: `verify_freshness.py` (esta carpeta). Grafos en `<copia>/.bench-out/graph.json`.
- La primera ejecución completa coincidió con otra carga en la máquina (load average 11,5):
  las cifras de slurp salieron el doble de lentas. Se repitió slurp solo, con la máquina en
  reposo; abajo van esa ejecución y la de PrismaStats de la ejecución completa.

## 1. La siguiente llamada ya lo refleja, con el grafo de un índice completo

Para cada copia, un `slurp serve` real por tuberías y seis tipos de cambio, cinco veces cada
uno: editar el cuerpo de una función, añadir un símbolo, crear un fichero, borrarlo,
renombrarlo y cambiar `.slurpignore`. Tras cada cambio, una llamada a `slurp_query`: su
`_meta` dice `refreshed`, la respuesta contiene el símbolo nuevo cuando lo hay, y el grafo
que el servidor dejó en disco es **idéntico** (nodos y relaciones, atributos públicos) al de
un índice completo del árbol en ese momento. Después, lo mismo por el CLI (`slurp QUERY`),
dos veces cada tipo.

| | PrismaStats | slurp |
|---|---|---|
| Cambios por el servidor, distintos de un índice completo | 0 de 30 | 0 de 30 |
| Cambios por el CLI, distintos de un índice completo | 0 de 12 | 0 de 12 |
| stdout del servidor | solo líneas JSON-RPC | solo líneas JSON-RPC |

## 2. Mediciones frente al diseño (§8)

Comprobación por llamada en régimen estable (entre llamadas de un agente pasan segundos; ver
el hallazgo 1), refresco según `refresh_ms` del `_meta` y la llamada entera medida desde el
cliente. Mediana y peor caso.

| | §8 PrismaStats | Medido | §8 slurp | Medido |
|---|---|---|---|---|
| Comprobación por llamada | 0,4 ms (stat simulado) | **0,4 ms** · peor 1,6 | 0,1 ms (54 + 16) | **0,2 ms** · peor 0,6 (73 ficheros + 23 directorios) |
| Vía rápida (cuerpo editado) | 118 ms (con git) | **30 ms** · peor 32 | 126 ms | **84 ms** · peor 95 |
| Índice completo (símbolo nuevo) | 381 ms | **290 ms** · peor 305 | 541 ms | **592 ms** · peor 612 |
| Índice completo (alta, baja, renombre, `.slurpignore`) | — | 282–288 ms · peor 308 | — | 586–595 ms · peor 600 |
| Llamada entera con refresco completo | ≈ 0,46 s | **0,32 s** · peor 0,34 | ≈ 0,62 s | **0,68 s** · peor 0,70 |
| Llamada sin cambios | 9,0 ms (query) | 8,5 ms · peor 13,9 | 22,4 ms | 22,8 ms · peor 32,0 |
| Manifiesto dentro de graph.json | ~35 KB (estimado) | 39 KB (0,77 → 0,81 MB) | despreciable | 11 KB |

- La vía rápida cuesta menos que en §8 porque ya no lanza los cuatro subprocesos git ni
  recorre el árbol; solo queda `git rev-parse HEAD` para `built_at_commit`.
- El índice completo de slurp cuesta un 9 % más que en §8 con un 21 % más de nodos.
- **El peor caso medido (0,70 s) deja un margen de ×2,9 sobre el límite de 2 s.**

## 3. `slurp index` a mano mientras el servidor refresca

Diez veces: se añade una función a un fichero, se lanza `slurp index` a mano y, a la vez, una
llamada al servidor (que refresca). Un hilo lee y parsea `graph.json` sin parar.

| | PrismaStats | slurp |
|---|---|---|
| Lecturas parseadas | 2.340 | 1.815 |
| Lecturas que vieron un fichero a medias | **0** | **0** |
| `slurp index` que esperaron el lock | 10 de 10 | 10 de 10 |
| Grafo final igual a un índice completo | sí | sí |

## 4. `slurp init --yes` en una copia de PrismaStats

La copia reproduce el estado real del proyecto: `git clone` de `~/Desktop/prismastats` (los
ficheros versionados en `5d345dc`, incluido `.claude/settings.local.json`, que PrismaStats
versiona) más lo que hay sin versionar en el árbol real: `.mcp.json`, la versión modificada
de `.claude/settings.local.json` y `graphify-out/` con el grafo de graphify (`graph.json`) y un
`slurp-graph.json` antiguo (slurp 1.1, 1.796 nodos, con `.next/` dentro). Se ejecutó
`PATH=.venv/bin:$PATH slurp init --yes`.

**Ficheros escritos o modificados** (comparando el sha de todos los ficheros antes y después):

| Fichero | Qué |
|---|---|
| `graphify-out/slurp-graph.json` | **reescrito**: el de slurp 1.1 no se pudo adoptar (en la copia no está `.next/`, así que menos de nueve de cada diez de sus ficheros existen) y se reconstruyó con manifiesto: 1.581 nodos, 1.852 relaciones |
| `graphify-out/.slurp-graph.json.lock` | nuevo, vacío: el lock consultivo, que nunca se borra |
| `.mcp.json` | la entrada `slurp` apunta ahora a `slurp-graph.json` |
| `.claude/settings.local.json` | `enabledMcpjsonServers: ["slurp"]` y `mcp__slurp__*` en `permissions.allow` |
| `CLAUDE.local.md` | nuevo, el párrafo para el agente |
| `.gitignore` | `.slurp/` y `CLAUDE.local.md` al final |

`graphify-out/graph.json` (graphify) no cambió. PrismaStats tiene `CLAUDE.md`, así que
`CLAUDE.local.md` no empieza con `@AGENTS.md`.

`.mcp.json`:

```diff
-      "args": ["serve", "--graph", "/Users/juancarlosvallejoruiz/Desktop/prismastats/graphify-out/graph.json"]
+      "args": [
+        "serve",
+        "--graph",
+        "/private/tmp/slurp-v13-init/ps/graphify-out/slurp-graph.json"
+      ]
```

`.claude/settings.local.json` (final del fichero; lo demás, intacto):

```diff
-      "mcp__slurp__slurp_query"
+      "mcp__slurp__slurp_query",
+      "mcp__slurp__*"
     ],
     "additionalDirectories": [ … ]
-  }
+  },
+  "enabledMcpjsonServers": [
+    "slurp"
+  ]
 }
```

`CLAUDE.local.md` (completo):

```markdown
<!-- slurp:begin — managed by `slurp init`; re-running it replaces this block -->
## Code navigation (slurp)

This project has a slurp MCP server: a code graph that stays in sync with the working tree. Before searching or reading files to find where something lives, call `slurp_query` with a plain-language description of the behaviour. Before editing a file, call `slurp_impact` with its path; before changing a function or class, call `slurp_explain` with its name. Use grep for exact strings, and open the files slurp points to before editing them.
<!-- slurp:end -->
```

`.gitignore` (el original acababa sin salto de línea):

```diff
-.copilot_workspace/
\ No newline at end of file
+.copilot_workspace/
+.slurp/
+CLAUDE.local.md
```

Manifiesto de `slurp-graph.json` (claves de primer nivel `built_at_commit`, `links`, `nodes`,
`slurp`):

```
format 1 · slurp_version 1.2.1 · root ".." · ignore {"path": ".slurpignore", "sha": null}
parsers {"js": "tree-sitter", "py": "ast", "ts": "tree-sitter", "tsx": "tree-sitter"}
161 dirs · 184 files, p. ej. ".agents/skills/gsap/scripts/extract-audio-data.py":
  [1795682815757248001, 6463, 29054239, "89acd7436d8e4b2c01890ab254a17127"]
```

Y el aviso del resumen:

```
.claude/settings.local.json is tracked by git — `git rm --cached .claude/settings.local.json`
to stop versioning it
```

La salida completa de `slurp init` está en el apéndice B.

## 5. Desviaciones del diseño

Ninguna cambia una decisión del autor. Son huecos que el diseño no cubría o detalles que la
implementación obligó a concretar:

1. **Raíz candidata de un grafo de slurp ≤ 1.2 fuera de `graphify-out/`:** el diseño dice "el
   padre de `graphify-out/`". Para un grafo en otra parte se prueba su propio directorio, con
   la misma validación del 90 %.
2. **Tercera variante de los textos:** el diseño define la de grafo propio y la de graphify.
   Un grafo de solo lectura que no es de graphify (genérico, GraphML, slurp ≤ 1.2 sin validar)
   recibe "This graph is served as it is on disk and slurp does not rebuild it…": la variante
   propia diría que sigue el árbol de trabajo, y la de graphify, que lo construyó graphify.
3. **`--no-refresh` comprueba y avisa:** no refresca, pero si el grafo está viejo lo dice
   (`stale` y aviso), igual que `benchmark` y `eval`. Así se cumple "ninguna respuesta servida
   con grafo viejo sin aviso" también con el refresco desactivado.
4. **Ventana "racily clean" por entrada:** el diseño la ancla al inicio del índice. Así, un
   directorio tocado justo antes (el propio `graphify-out/`, o la raíz) se volvía a listar en
   cada llamada hasta el siguiente índice: 2,7 ms por llamada en PrismaStats. Ahora cuenta
   desde la última vez que se verificó cada fichero y directorio; la garantía es la misma.
5. **Recarga fallida:** cuando otro proceso deja un `graph.json` que no se puede leer, el
   servidor responde con el grafo anterior y un aviso (estado `failed`), como para un
   refresco fallido. Hasta la v1.2.1 era un `isError`.
6. **Instantánea del log de sesión:** el evento `end` compara con una instantánea tomada al
   arrancar, antes de leer el primer mensaje (~50 ms en PrismaStats), solo si `.slurp/`
   existe. Hecha en segundo plano, una edición rápida podía colarse en ella.
7. **Fichero de reglas registrado:** el de `--ignore-file` tal como lo usa el CLI (relativo al
   directorio actual), guardado relativo a la raíz si está dentro y absoluto si no.
8. **`slurp init` ante un grafo ajeno en `graphify-out/slurp-graph.json`:** pregunta antes de
   sustituirlo y `--yes` se niega. El diseño no contempla ese caso.

## 6. Hallazgos

1. **La comprobación por llamada del diseño era solo el coste de los stat.** Con objetos
   `Path` por fichero y la ventana anclada al índice salía a 2,7 ms en PrismaStats; con
   cadenas y la ventana por entrada, 0,4 ms. La primera comprobación de un proceso nuevo del
   CLI sobre un grafo de más de 30 s hace el recorrido completo de seguridad (31 ms en
   PrismaStats, §8), como dice el diseño.
2. **PrismaStats versiona `.claude/settings.local.json`.** `init` añade las aprobaciones, pero
   quedarían en git: lo avisa con el `git rm --cached` que lo resuelve. Añadirlo al
   `.gitignore` no cambia nada para un fichero ya versionado.
3. **Claude Code ya ignora `**/.claude/settings.local.json`** en el `.gitignore` global de esta
   máquina. `init` lo respeta (`git check-ignore`), así que en proyectos sin versionar ese
   fichero no añade la línea.
4. **El `slurp-graph.json` real de PrismaStats es de slurp 1.1** (agosto, con `.next/`). Con la
   nueva búsqueda automática se encuentra antes que el de graphify; en el proyecto real, donde
   `.next/` existe, puede validarse y adoptarse, y el primer refresco lo reconstruye sin
   `.next/`.
5. **El aviso de solo lectura salta en el PrismaStats real:** dos ficheros que cubre el grafo
   de graphify cambiaron después de construirlo (`.claude/settings.local.json` y
   `lib/supabase/admin-actions.ts`). Es correcto, pero depende de las mtimes del checkout, así
   que `scripts/readme_examples.py` lo quita al normalizar.
6. **El lock queda en `graphify-out/`** (`.graph.json.lock`, `.slurp-graph.json.lock`). En un
   proyecto que versiona `graphify-out/` aparecería como fichero sin seguir: desde #89
   `slurp init` lo añade al `.gitignore` en ese caso.
7. **La descripción aprobada de `slurp_suggest`** decía "An empty answer means the budget
   already covered the neighbourhood", pero la herramienta responde con una frase en ese caso
   ("No follow-up suggested: …"), no vacía. Corregido en #90.
8. **Los fragmentos de Codex, Gemini CLI y Cursor** se imprimen tal como los describe el
   diseño; no se han probado contra esos clientes (fase 2 del diseño).

## Apéndice A — salida literal de `verify_freshness.py`

```
=== /private/tmp/slurp-v13/ps
  nodes 1582, links 1852, graph.json 0.81 MB, manifest 185 files + 161 dirs (39 KB)
  check per call (ManifestChecker.check)        median     0.4 ms   worst     1.6 ms   (n=200)
  check per call (Refresher.prepare, nothing)   median     0.4 ms   worst     0.4 ms   (n=200)
  edited file: .agents/skills/gsap/scripts/extract-audio-data.py
  stdout: JSON-RPC lines only
  server, per kind of change (refresh_ms from _meta; wall = whole call):
    body edit      refresh median    30.2 ms   worst    32.0 ms   (n=5)
                   wall    median    59.7 ms   worst   111.2 ms   (n=5)
    new symbol     refresh median   290.3 ms   worst   305.2 ms   (n=5)
                   wall    median   320.5 ms   worst   336.2 ms   (n=5)
    new file       refresh median   287.0 ms   worst   297.7 ms   (n=5)
                   wall    median   317.2 ms   worst   329.7 ms   (n=5)
    deleted file   refresh median   284.8 ms   worst   307.7 ms   (n=5)
                   wall    median   314.2 ms   worst   330.1 ms   (n=5)
    renamed file   refresh median   282.1 ms   worst   300.4 ms   (n=5)
                   wall    median   310.3 ms   worst   330.9 ms   (n=5)
    .slurpignore   refresh median   287.8 ms   worst   300.7 ms   (n=5)
                   wall    median   315.7 ms   worst   330.2 ms   (n=5)
    no change      wall    median     8.5 ms   worst    13.9 ms   (n=20)
  equality with a full index: server 30 changes, 0 mismatches; CLI 12 changes, 0 mismatches
  concurrent slurp index x10: 2340 reads parsed, 0 half-written; 10 waited for the lock; final graph equals a full index: True

=== /private/tmp/slurp-v13/sl
  nodes 5477, links 11107, graph.json 3.48 MB, manifest 73 files + 23 dirs (11 KB)
  check per call (ManifestChecker.check)        median     0.2 ms   worst     0.6 ms   (n=200)
  check per call (Refresher.prepare, nothing)   median     0.1 ms   worst     0.2 ms   (n=200)
  edited file: docs/design/mcp-autonomo/verify_freshness.py
  stdout: JSON-RPC lines only
  server, per kind of change (refresh_ms from _meta; wall = whole call):
    body edit      refresh median    83.6 ms   worst    94.5 ms   (n=5)
                   wall    median   175.5 ms   worst   219.7 ms   (n=5)
    new symbol     refresh median   592.3 ms   worst   611.8 ms   (n=5)
                   wall    median   677.6 ms   worst   703.4 ms   (n=5)
    new file       refresh median   595.4 ms   worst   596.4 ms   (n=5)
                   wall    median   673.6 ms   worst   685.8 ms   (n=5)
    deleted file   refresh median   586.0 ms   worst   594.0 ms   (n=5)
                   wall    median   668.3 ms   worst   672.0 ms   (n=5)
    renamed file   refresh median   586.3 ms   worst   595.8 ms   (n=5)
                   wall    median   669.6 ms   worst   678.1 ms   (n=5)
    .slurpignore   refresh median   594.6 ms   worst   599.6 ms   (n=5)
                   wall    median   675.7 ms   worst   680.3 ms   (n=5)
    no change      wall    median    22.8 ms   worst    32.0 ms   (n=20)
  equality with a full index: server 30 changes, 0 mismatches; CLI 12 changes, 0 mismatches
  concurrent slurp index x10: 1815 reads parsed, 0 half-written; 10 waited for the lock; final graph equals a full index: True
```

## Apéndice B — salida literal de `slurp init --yes` en la copia de PrismaStats

```
╭─────────────────────────────────────────── slurp init ───────────────────────────────────────────╮
│                                                                                                  │
│  Language:  TypeScript  (TypeScript 180, Python 3, JavaScript 1 files)                           │
│  Project:   /private/tmp/slurp-v13-init/ps                                                       │
│                                                                                                  │
│    1. Rebuild slurp's graph at graphify-out/slurp-graph.json (refreshed before every answer)     │
│       The one there was written by slurp ≤ 1.2 and fewer than nine in ten of its files exist     │
│  here.                                                                                           │
│    2. Update .mcp.json registering slurp as an MCP server                                        │
│    3. Approve the slurp server and its tools for Claude Code in .claude/settings.local.json      │
│    4. Tell the agent when to use slurp in CLAUDE.local.md (local, not versioned)                 │
│       Or: --instructions versioned (CLAUDE.md/AGENTS.md) | none                                  │
│    5. Keep .slurp/, settings.local.json, CLAUDE.local.md out of git (.gitignore)                 │
│                                                                                                  │
│  slurp binary: /Users/juancarlosvallejoruiz/Desktop/Slurp/slurp/.venv/bin/slurp  (pip install    │
│  (virtualenv))                                                                                   │
│                                                                                                  │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
Indexing 184 files...
╭───────────────────────────────────────── slurp is ready ─────────────────────────────────────────╮
│                                                                                                  │
│  Nodes:   1581                                                                                   │
│  Edges:   1852                                                                                   │
│  Graph:   /private/tmp/slurp-v13-init/ps/graphify-out/slurp-graph.json                           │
│                                                                                                  │
│  .mcp.json (/private/tmp/slurp-v13-init/ps/.mcp.json):                                           │
│  {                                                                                               │
│    "mcpServers": {                                                                               │
│      "slurp": {                                                                                  │
│        "command": "/Users/juancarlosvallejoruiz/Desktop/Slurp/slurp/.venv/bin/slurp",            │
│        "args": [                                                                                 │
│          "serve",                                                                                │
│          "--graph",                                                                              │
│          "/private/tmp/slurp-v13-init/ps/graphify-out/slurp-graph.json"                          │
│        ]                                                                                         │
│      }                                                                                           │
│    }                                                                                             │
│  }                                                                                               │
│                                                                                                  │
│  Claude Code: .claude/settings.local.json updated (enabledMcpjsonServers, permissions.allow      │
│  mcp__slurp__*)                                                                                  │
│  Instructions: CLAUDE.local.md                                                                   │
│    An ignored CLAUDE.local.md exists only in this worktree; run `slurp init` in other worktrees  │
│  too.                                                                                            │
│  .gitignore: added .slurp/, CLAUDE.local.md                                                      │
│  .claude/settings.local.json is tracked by git — `git rm --cached .claude/settings.local.json`   │
│  to stop versioning it                                                                           │
│                                                                                                  │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
Other agents (printed, not written — copy what you use):

Codex:
# .codex/config.toml (Codex reads it in trusted projects)
[mcp_servers.slurp]
command = "/Users/juancarlosvallejoruiz/Desktop/Slurp/slurp/.venv/bin/slurp"
args = ["serve", "--graph", "/private/tmp/slurp-v13-init/ps/graphify-out/slurp-graph.json"]
# Every slurp tool is read-only: Codex asks only about tools that write.
default_tools_approval_mode = "writes"

Gemini CLI:
// .gemini/settings.json — trust: true skips the confirmation for its tools
{
  "mcpServers": {
    "slurp": {
      "command": "/Users/juancarlosvallejoruiz/Desktop/Slurp/slurp/.venv/bin/slurp",
      "args": [
        "serve",
        "--graph",
        "/private/tmp/slurp-v13-init/ps/graphify-out/slurp-graph.json"
      ],
      "trust": true
    }
  }
}

Cursor:
// .cursor/mcp.json
{
  "mcpServers": {
    "slurp": {
      "command": "/Users/juancarlosvallejoruiz/Desktop/Slurp/slurp/.venv/bin/slurp",
      "args": [
        "serve",
        "--graph",
        "/private/tmp/slurp-v13-init/ps/graphify-out/slurp-graph.json"
      ]
    }
  }
}
// Cursor CLI: add "Mcp(slurp:*)" to permissions.allow in .cursor/cli.json;
// in the editor, allow the tools from the MCP settings.

Next: restart your AI coding assistant to activate slurp
```
