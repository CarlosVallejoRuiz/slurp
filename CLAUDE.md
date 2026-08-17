# CLAUDE.md — Proyecto Slurp

## Qué es Slurp

Slurp es una herramienta CLI open source de **token-budget-aware graph navigation** para AI coding agents.

**El problema:** Los knowledge graphs (como los que genera graphify) pueden tener miles de nodos. Inyectar el grafo entero en el contexto de un LLM es ineficiente y caro. Slurp selecciona inteligentemente el subgrafo más relevante que cabe dentro de un presupuesto de tokens dado.

**La metáfora:** Un bowl de ramen es un knowledge graph — miles de nodos entrelazados. Slurp extrae exactamente los noodles que necesita tu LLM, sin traerse el bowl entero.

> *"graphify builds the bowl. slurp serves exactly the noodles your LLM needs."*

**Propuesta de valor única:**
- Compatible con `graph.json` de graphify (formato principal) y cualquier grafo estándar
- Dado un budget en tokens y una query, devuelve el subgrafo óptimo
- Trazabilidad completa: audit log de qué nodos se usaron en cada query
- Funciona como CLI standalone, como librería Python importable, y como servidor MCP
- Publica en PyPI como `slurp-graph`, el comando CLI es `slurp`

**Autor:** Juank (Juan Carlos Vallejo Ruiz) — Full-Stack Developer & AI entrepreneur, Terrassa, Barcelona.
**Repo:** github.com/CarlosVallejoRuiz/slurp
**PyPI:** slurp-graph

---

## Stack técnico

- **Python 3.12+**
- **uv** para gestión de dependencias, packaging y ejecución
- **networkx** para operaciones de grafo (PageRank, traversal, subgraph)
- **tiktoken** para conteo de tokens preciso (cl100k_base por defecto)
- **rich** para CLI output con tablas, paneles y colores
- **click** para CLI interface
- **pathspec** para ignore patterns en `.slurpignore`
- **watchdog** para `--watch` mode en `slurp index`
- **anthropic** SDK (opcional en v1, para embeddings en v4)
- **tree-sitter** + gramáticas por lenguaje — **extras opcionales**, nunca requeridos
- Tests con **pytest**, linting con **ruff**, formatting con **black**

### Lenguajes soportados por `slurp index`

| Lenguaje | Parser | Extra |
|---|---|---|
| Python | stdlib `ast` | — (siempre disponible) |
| TypeScript / JavaScript | tree-sitter | `ts` |
| Java | tree-sitter | `java` |
| Rust | tree-sitter | `rust` |
| C# | tree-sitter | `csharp` |
| Ruby | tree-sitter | `ruby` |
| PHP | tree-sitter | `php` |
| Kotlin | tree-sitter ⚠️ | `kotlin` |
| Scala | tree-sitter | `scala` |
| Swift | tree-sitter | `swift` |
| C | tree-sitter | `cpp` |
| C++ | tree-sitter | `cpp` |
| Go | regex | — (regex es el parser principal) |
| Lua | regex | — (regex es el parser principal) |
| Elixir | regex | — (regex es el parser principal) |
| PowerShell | regex | — (regex es el parser principal) |

⚠️ **Kotlin:** `tree-sitter-kotlin 1.1.0` falla al parsear una clase con cuerpo
seguida de un `object` — Kotlin corriente. La guarda `_tree_is_broken()` lo
detecta y en la práctica Kotlin se indexa por regex. El extra se mantiene
declarado para que una versión corregida de la gramática entre sin tocar código.

```bash
uv sync --extra all-languages   # las 10 gramáticas de golpe
uv sync --extra rust            # solo una
```

**Invariantes del indexador:**
- Cada gramática es un extra **independiente**: su ausencia es una configuración
  soportada y debe degradar en silencio al fallback regex.
- **Regex principal vs regex fallback**: Go, Lua, Elixir y PowerShell no tienen
  gramática cableada — su regex es el diseño, entran por `_index_regex_only()`,
  no intentan tree-sitter y `parser_summary()` los marca `is_fallback=False`.
  Marcarlos como fallback sería mentir sobre un grafo que no está degradado.
- Un fallo de parseo **con** la gramática instalada sí se avisa por stderr —
  los dos casos nunca comparten el mismo `except`.
- `slurp index` imprime siempre qué parser usó por lenguaje; un grafo degradado
  nunca debe pasar desapercibido. Con más de 4 lenguajes la línea se envuelve a
  3 por línea con 11 espacios de indentación (`format_parser_summary()`).
- **`_tree_is_broken(tree)`** — una gramática que devuelve un árbol con nodos
  `ERROR` no lanza excepción: el visitor la recorre y pierde declaraciones
  enteras en silencio. Tras **cada** parseo se comprueba `root_node.has_error`;
  si hay error se trata como fallo del parser → aviso a stderr + fallback regex.
  Aplica a los 8 lenguajes con tree-sitter, no solo a Kotlin. Nunca quitar esta
  guarda: es lo que convierte un fallo silencioso en uno visible y recuperable.
- Los visitors de tree-sitter heredan de `_BaseVisitor` (pila de scopes, `_emit`
  con atributos extra, dispatch `visit_<node_type>`). Añadir un lenguaje = una
  subclase + un fallback regex + una entrada en `_INDEXED_EXTENSIONS`,
  `parser_summary()` y `_LANGUAGE_BY_EXT` del CLI.
- Metadatos específicos por lenguaje: anotaciones/atributos (Java, C#),
  visibilidad (Java, Rust, C#, PHP), lifetimes (Rust), `is_async` (C#, Swift),
  `is_class_method` y accessors (Ruby), magic methods (PHP),
  `is_data_class`/`is_sealed`/`is_suspend`/`extends_type` (Kotlin),
  `is_case`/`is_implicit` y `def`/`val`/`var` como tipos (Scala),
  `is_computed` y protocolos (Swift), `is_header`/`is_template`/`is_extern_c`
  (C/C++), `is_local` y tables como módulos (Lua), `is_private` (Elixir),
  `verb`/`noun`/`has_params` (PowerShell). Edges tipados: `extends`, `implements`
  (incluido `impl Trait for Type` de Rust), `with` (Scala), `conforms_to`
  (Swift), `mixin` (Ruby `include`/`extend`/`prepend`, PHP `use` de traits),
  `use`/`import`/`alias`/`require` (Elixir) y `requires` (PowerShell).
- Los companion objects (Kotlin, Scala) se anidan bajo su clase — si no,
  colisionan con ella en el mismo node id.

---

## Principios de desarrollo

1. **Tests primero.** Cada módulo lleva sus tests. Mínimo: happy path + edge cases obvios + caso de grafo vacío.
2. **Un módulo, una responsabilidad.** No mezclar lógica de grafo con lógica de tokens ni con CLI.
3. **Fail loud, fail early.** Errores claros con mensajes accionables. Nunca fallar silenciosamente.
4. **Sin dependencias innecesarias.** Antes de añadir una librería, preguntarse si stdlib lo resuelve.
5. **Compatibilidad graphify primero.** `graph.json` de graphify es el formato de entrada principal. Todo cambio debe mantener compatibilidad con él.
6. **Documentar decisiones no obvias** con comentario `# DECISION:` en el código.
7. **Type hints en todas las funciones públicas.** Sin excepción.

---

## Estructura del proyecto

**Working directory:** la raíz del repo git es `~/Desktop/Slurp/slurp/` — un nivel
por debajo de la carpeta que abre el editor (`~/Desktop/Slurp/`). Todos los comandos
de este documento (`uv run pytest`, `ruff check`, `uv build`, `git`) se ejecutan
desde `~/Desktop/Slurp/slurp/`, que es donde vive este CLAUDE.md.

**Estado actual:** v0.9.0 · 1746 tests · `ruff check slurp/` limpio.

```
~/Desktop/Slurp/slurp/         ← raíz del repo git — working directory
├── CLAUDE.md                  ← este fichero
├── README.md
├── USAGE.md
├── pyproject.toml
├── .slurpignore.example
├── slurp/
│   ├── __init__.py        ← versión y exports públicos
│   ├── cli.py             ← entrada CLI (click)
│   ├── loader.py          ← lee graph.json → nx.DiGraph
│   ├── scorer.py          ← puntúa nodos por relevancia
│   ├── budget.py          ← selecciona subgrafo óptimo por tokens
│   ├── formatter.py       ← serializa subgrafo a texto para LLM
│   ├── exporter.py        ← bloques de contexto Claude/ChatGPT/CLAUDE.md
│   ├── benchmark.py       ← mide ahorro real de tokens vs grafo completo
│   ├── audit.py           ← registra queries en .slurp/audit.jsonl
│   ├── diff.py            ← compara dos grafos y analiza impacto
│   ├── viz.py             ← genera HTML interactivo del subgrafo
│   ├── mcp.py             ← servidor MCP stdio (JSON-RPC 2.0)
│   ├── ignore.py          ← filtra nodos por .slurpignore
│   ├── injector.py        ← extrae código fuente real para --inject-code (v0.5.0)
│   ├── indexer.py         ← indexador estático autónomo (v0.6.0)
│   ├── smart.py           ← re-indexado incremental vía git (--smart)
│   ├── advisor.py         ← recomienda budget óptimo desde el historial de queries
│   ├── federation.py      ← mergea varios grafos en uno (--graph repetido)
│   └── explainer.py       ← explica un nodo en lenguaje natural (LLM + fallback estructural)
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_loader.py
    ├── test_scorer.py
    ├── test_budget.py
    ├── test_formatter.py
    ├── test_exporter.py
    ├── test_benchmark.py
    ├── test_audit.py
    ├── test_diff.py
    ├── test_ignore.py
    ├── test_mcp.py
    ├── test_cli.py
    ├── test_injector.py
    ├── test_indexer.py
    ├── test_smart.py
    ├── test_advisor.py
    ├── test_federation.py
    └── test_explainer.py
```

---

## Arquitectura de módulos

### `slurp/loader.py`

Lee `graph.json` en memoria como `nx.DiGraph`.

**Formato graphify soportado:**
```json
{
  "nodes": [
    {
      "id": "node_id",
      "label": "Human readable label",
      "type": "function|class|module|concept|...",
      "description": "What this node represents",
      "importance": 7,
      "file_path": "src/auth.py"
    }
  ],
  "links": [
    {
      "source": "node_id_a",
      "target": "node_id_b",
      "relation": "calls|imports|implements|...",
      "weight": 2
    }
  ]
}
```

**Nota:** graphify usa `links` como key para edges (no `edges`). El loader soporta ambos, con `links` teniendo prioridad. Los nodos de graphify usan `file_type` y `source_file` en lugar de `type` y `file_path`.

**API pública:**
```python
def load_graph(path: Path) -> nx.DiGraph:
    """Carga graph.json y retorna un DiGraph. Lanza SlurpLoadError si falla."""

def detect_format(data: dict) -> str:
    """Retorna 'graphify' o 'generic'."""
```

**Errores:**
- `SlurpLoadError` si el archivo no existe, no es JSON válido, o no tiene nodos.

---

### `slurp/scorer.py`

Dado el grafo y una query string, puntúa cada nodo por relevancia.

**Señales combinadas:**
- **Structural (40%):** PageRank implementado con power-iteration puro (sin numpy)
- **Semantic (60%):** TF-IDF manual con tokenización mejorada: separa camelCase, snake_case, normaliza a minúsculas
- **Boost +0.3** para nodos con `file_type == "code"` (clamped a 1.0)

**Score final:** `score = min(1.0, 0.4 * pagerank_normalized + 0.6 * tfidf_similarity + code_boost)`

**Decisiones de implementación:**
- `_pagerank()` — power-iteration puro, ~20 líneas, sin numpy. Converge en O(iter × edges)
- `_tokenize()` — inserta espacios en límites camelCase/PascalCase, extrae runs alfanuméricos, añade forma compuesta original
- `_tfidf_scores()` — IDF suavizado sklearn-style, similitud coseno
- `lru_cache` en tiktoken para evitar carga repetida del vocab

**API pública:**
```python
def score_nodes(G: nx.DiGraph, query: str) -> dict[str, float]:
    """Retorna dict {node_id: score} con scores normalizados entre 0 y 1."""

def score_nodes_detailed(G: nx.DiGraph, query: str) -> dict[str, tuple[float, float, float]]:
    """Retorna dict {node_id: (final, pagerank_norm, tfidf)} para --explain."""
```

---

### `slurp/budget.py`

Selecciona el subgrafo óptimo que cabe en el presupuesto de tokens.

**Algoritmo greedy con expansión de vecinos:**
1. Filtra nodos con `score >= min_score` antes del loop greedy
2. Ordena por score descendente
3. Toma el nodo top, añade sus vecinos directos con score reducido (`score * neighbor_decay`)
4. Añade el nodo si `tokens_usados + tokens_nodo <= budget`
5. Si un nodo no cabe, continúa con el siguiente (no para)
6. Retorna `(subgrafo, stats)`

**API pública:**
```python
def select_subgraph(
    G: nx.DiGraph,
    scores: dict[str, float],
    budget: int,
    model: str = "cl100k_base",
    neighbor_decay: float = 0.7,
    min_score: float = 0.0,
) -> tuple[nx.DiGraph, dict]:
    """Retorna (subgrafo, stats dict)."""

def count_tokens(text: str, model: str = "cl100k_base") -> int:
    """Cuenta tokens con tiktoken. Resultado cacheado con lru_cache."""
```

---

### `slurp/formatter.py`

Serializa un subgrafo a texto estructurado listo para inyectar en un prompt.

**Formatos:** `markdown` (default), `json`, `yaml` (serializer propio, sin PyYAML)

**API pública:**
```python
def format_subgraph(G: nx.DiGraph, stats: dict, format: str = "markdown") -> str:
    """Serializa el subgrafo al formato especificado."""
```

---

### `slurp/audit.py`

Registra cada query en `.slurp/audit.jsonl` (append-only).

**API pública:**
```python
def log_query(query, graph_path, stats, top_nodes, audit_dir=Path(".slurp")) -> None
def read_audit(audit_dir=Path(".slurp")) -> list[dict]
def top_nodes_from_audit(audit_dir=Path(".slurp"), n=10) -> list[tuple]
```

---

### `slurp/diff.py`

Compara dos versiones de graph.json y analiza impacto de cambios.

**API pública:**
```python
def diff_graphs(old: nx.DiGraph, new: nx.DiGraph) -> GraphDiff
def affected_subgraph(G: nx.DiGraph, diff: GraphDiff, hops: int = 2) -> nx.DiGraph
def format_diff(diff: GraphDiff, G_new: nx.DiGraph) -> str
def build_diff_viz_graph(G_old, G_new, diff, hops) -> nx.DiGraph
```

**`GraphDiff` dataclass:**
- `added_nodes`, `removed_nodes`, `modified_nodes` — listas de node IDs
- `added_edges`, `removed_edges` — listas de (source, target)
- `impact_score` — float 0-1 basado en centralidad de nodos afectados
- `has_changes` — property bool

**Colores en viz:** verde=added, rojo=removed, amarillo=modified, gris=unchanged

---

### `slurp/viz.py`

Genera HTML interactivo con vis.js para visualizar subgrafos.

**API pública:**
```python
def build_html(
    G: nx.DiGraph,
    stats: dict,
    scores: dict[str, float],
    query: str,
    max_nodes: int = 50,
    mode: str = "normal",  # "normal" | "diff"
) -> str
```

**Features:**
- Nodos coloreados por tipo, tamaño proporcional a score
- Panel lateral con label, tipo, score, descripción, archivo, conexiones
- Header con query, nodes selected/total, tokens used/budget, coverage%, savings badge
- Física: estabilización 500 iteraciones, freeze después de stabilize
- Nodos aislados con opacity 0.4
- Modo diff: verde/rojo/amarillo/gris, tamaños compactos (12-28px), font 11px

---

### `slurp/mcp.py`

Servidor MCP stdio, protocolo JSON-RPC 2.0, stdlib pura.

**Herramienta expuesta:** `slurp_query(query: str, budget: int = 4000) -> str`

**Métodos soportados:** `initialize`, `ping`, `tools/list`, `tools/call`

**Uso:**
```bash
slurp serve --graph graph.json
```

**Configuración `.mcp.json`:**
```json
{
  "mcpServers": {
    "slurp": {
      "command": "/path/to/.venv/bin/slurp",
      "args": ["serve", "--graph", "/path/to/graphify-out/graph.json"]
    }
  }
}
```

---

### `slurp/ignore.py`

Filtra nodos del grafo según `.slurpignore`.

**Reglas soportadas:**
- `type:document` — excluye por file_type
- `file:*.md` — excluye por source_file (glob)
- `id:prefix_*` — excluye por node ID (glob)

**API pública:**
```python
def load_ignore(path: Path) -> SlurpIgnore
def apply_ignore(G: nx.DiGraph, ignore: SlurpIgnore) -> nx.DiGraph
```

---

## CLI — Comandos completos

```bash
# Setup guiado — primer comando en un proyecto nuevo (v0.6.0)
slurp init
slurp init --yes    # acepta todos los prompts (CI / scripting)

# Query principal
slurp "auth flow" --graph graph.json --budget 4000
slurp "auth flow" --graph graph.json --budget 4000 --format json|yaml
slurp "auth flow" --graph graph.json --budget 4000 --explain
slurp "auth flow" --graph graph.json --budget 4000 --viz
slurp "auth flow" --graph graph.json --budget 4000 --min-score 0.15
slurp "auth flow" --graph graph.json --budget 4000 --neighbor-decay 0.7
slurp "auth flow" --graph graph.json --budget 4000 --no-audit
slurp "auth flow" --graph graph.json --budget 4000 --ignore-file .slurpignore

# Estadísticas
slurp stats --graph graph.json

# Historial
slurp audit
slurp audit --top-nodes 20
slurp audit --audit-dir .slurp

# Diff entre versiones
slurp diff old.json new.json
slurp diff old.json new.json --viz
slurp diff old.json new.json --hops 2
slurp diff old.json new.json --budget 4000

# Servidor MCP
slurp serve --graph graph.json
slurp serve --graph graph.json --no-log    # desactiva el session log

# Session log del servidor MCP (v0.6.4)
slurp session
slurp session --last 10
slurp session --tail
slurp session --log-dir .slurp

# Export (v0.3.0)
slurp export "auth flow" --graph graph.json --budget 4000 --format claude|chatgpt|claudemd
slurp export "auth flow" --graph graph.json --budget 4000 --output context.md

# Indexar proyecto sin graphify (v0.6.0)
slurp index .
slurp index /path/to/project --output graph.json
slurp index . --watch
```

**Auto-discovery de graph.json** (sin --graph):
1. `./graph.json`
2. `./graphify-out/graph.json`
3. `./.graphify/graph.json`

### `slurp init` — setup guiado

Punto de entrada para un proyecto nuevo. No requiere flags. Secuencia:

1. **Detecta el lenguaje dominante** contando ficheros indexables por extensión.
   Reutiliza `iter_source_files()` del indexer, así que `node_modules/`, `.venv/`
   y demás directorios vendorizados no falsean el conteo. Empates por orden
   alfabético para que la salida sea determinista.
2. **Muestra un panel con el plan y pide confirmación.** Nada se escribe antes.
3. **Indexa** a `graphify-out/graph.json` con progress bar real, alimentado por
   el callback `on_file` de `index_project()`. Si ya existe un grafo
   (auto-discovery), ofrece reutilizarlo en lugar de re-indexar.
4. **Escribe `.mcp.json`** con la ruta absoluta del binario, detectada vía
   `shutil.which("slurp")` y clasificada como `uv tool install` o `pip install`.
5. **Resumen final** con nodos, edges, ruta del grafo, el JSON generado y la
   instrucción de reiniciar el asistente.

| Flag | Default | Descripción |
|---|---|---|
| `--yes`, `-y` | off | Acepta todos los prompts. Para CI y scripting. |

**Invariantes que no se deben romper:**
- `.mcp.json` existente **nunca se machaca a ciegas**: se pregunta primero y los
  demás MCP servers del fichero se preservan. Solo se reemplaza la entrada `slurp`.
- El progress bar se desactiva fuera de TTY para no meter ANSI en pipes ni en
  las capturas de CliRunner.
- La ruta del binario en `.mcp.json` es absoluta: los clientes MCP lanzan el
  servidor sin shell de login y un `slurp` pelado dependería del PATH heredado.

---

## Roadmap de versiones

### v0.1.0 — Core MVP ✅
- [x] `loader.py` con tests
- [x] `scorer.py` con tests
- [x] `budget.py` con tests
- [x] `formatter.py` con tests
- [x] `audit.py` con tests
- [x] `cli.py` — comando `slurp` principal + `slurp stats`
- [x] `conftest.py` con fixture de grafo de ejemplo realista
- [x] README con ejemplos

### v0.2.0 — Usabilidad ✅
- [x] `slurp audit` con historial y top-nodes
- [x] `--explain` flag con desglose de scores
- [x] `.slurpignore` para excluir nodos por tipo o path
- [x] Auto-discovery de graph.json
- [x] Output JSON y YAML
- [x] `--viz` flag con visualizador interactivo HTML
- [x] Badge de ahorro de tokens en viz
- [x] `--min-score` para filtrar nodos irrelevantes
- [x] Scorer mejorado: camelCase, snake_case, boost file_type=code

### v0.3.0 — Integraciones ✅
- [x] `slurp serve` — servidor MCP stdio JSON-RPC 2.0
- [x] `slurp diff old.json new.json` — impacto de cambios con viz
- [x] `slurp export` — system prompt listo para Claude/ChatGPT/CLAUDE.md
- [x] Publicación en PyPI como `slurp-graph`

### v0.4.0 — Integraciones ✅
- [x] Embeddings opcionales vía `--backend embeddings` (anthropic o openai)
- [x] `slurp benchmark` — mide token savings vs inyectar grafo completo con métricas reales
- [x] Compatibilidad con otros formatos de grafo (neo4j export, GraphML)

### v0.5.0 — Integraciones ✅
- [x] `--inject-code` flag — lee el `file_path` de cada nodo seleccionado,
      localiza la función por `source_location` (línea), extrae el bloque
      de código real y lo añade debajo del nodo en el output markdown
- [x] Soporta Python, TypeScript/TSX, JavaScript, Go como parsers iniciales
- [x] `--inject-code` solo disponible cuando el subgrafo tiene ≤30 nodos
      para evitar explosión de tokens
- [x] Combina contexto semántico del grafo + código real en un único output
      listo para inyectar en Claude Code

### v0.6.0 — Autonomous Indexing ✅
- [x] `slurp index .` — genera graph.json propio sin depender de graphify
- [x] Soporte Python (stdlib ast), TypeScript/JS (tree-sitter o regex), Go (regex)
- [x] Java, Rust, C# y Ruby (tree-sitter o regex) con metadatos por lenguaje
- [x] Mismo formato graph.json que graphify — compatible con todo lo existente
- [x] `slurp index . --watch` — modo watch que re-indexa archivos modificados
- [x] `.slurpignore` aplicado también al indexador
- [x] Sin dependencias de LLM para indexar — análisis estático puro

---

## Decisiones de implementación relevantes

| Decisión | Motivo |
|---|---|
| Power-iteration PageRank sin numpy | Evita dependencia pesada, mismo algoritmo |
| TF-IDF sin sklearn | Mantiene deps mínimas, suficiente para v1 |
| YAML serializer propio | Sin PyYAML, stdlib pura |
| `lru_cache` en tiktoken | Evita recarga de vocab en cada nodo |
| `links` tiene prioridad sobre `edges` | Compatibilidad con formato real de graphify |
| Boost +0.3 para file_type=code | Docs compiten injustamente con código en queries técnicas |
| `min_score=0.15` default | Evita seleccionar 400+ nodos en grafos grandes |
| Freeze física tras stabilize | Nodos quietos = grafo legible |
| `max_nodes=50` en viz | Mantiene visualizador legible independiente del budget |

---

## Convenciones de código

- Type hints en todas las funciones públicas
- Docstrings en Google style
- Código y comentarios en inglés
- Commits: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`
- `ruff check .` debe pasar sin errores antes de cada commit
- `uv run pytest tests/ -q` debe pasar antes de cada commit
- Ambos se ejecutan desde `~/Desktop/Slurp/slurp/`, no desde la carpeta contenedora

---

## Lo que NO hacer

- ❌ No crear formato de grafo propio rompiendo compatibilidad con graphify — graphify es ahora opcional, pero el formato `{nodes, links}` debe seguir siendo el mismo
- ❌ No usar async — el CLI es síncrono
- ❌ No añadir UI web propia — slurp es capa de query, no visualizador standalone
- ❌ No over-engineer el scorer

---

## Cómo trabajar con Claude Code

Al inicio de cada sesión, Claude Code debe:

1. Leer este CLAUDE.md completo antes de escribir una sola línea de código.
2. Situarse en `~/Desktop/Slurp/slurp/` — es la raíz del repo. Lanzado desde
   `~/Desktop/Slurp/`, `pytest tests/` falla con "file or directory not found".
3. Ejecutar `uv run pytest tests/ -q` para ver el estado actual.
4. Preguntar en qué módulo o feature trabajamos hoy si no está claro.
5. Ejecutar `uv run pytest tests/ -q` después de cada módulo implementado.
6. No pasar al siguiente módulo hasta que los tests del actual pasen.
7. Hacer `ruff check slurp/` antes de cada commit.

## Git commits
NUNCA añadir "Co-authored-by: Claude" ni firmas 
similares en los mensajes de commit.