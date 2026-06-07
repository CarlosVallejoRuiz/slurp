# Guía de uso — slurp

> Dos formas de usar slurp: en segundo plano de forma completamente automática (recomendado), o directamente desde el terminal para exploración y análisis.

---

## Modo Automático (recomendado)

Una vez configurado, **slurp no requiere ninguna intervención manual**. Vive en segundo plano como servidor MCP. Cada vez que Claude Code necesita contexto del codebase, llama a slurp automáticamente, recibe el subgrafo óptimo y lo usa como contexto. El usuario usa Claude Code exactamente igual que siempre.

### Setup — 3 pasos, una sola vez

**1. Instalar slurp-graph**

```bash
pip install slurp-graph
# o con uv
uv add slurp-graph
```

**2. Generar el grafo con graphify**

```bash
cd /path/to/tu-proyecto
graphify .
# → genera graphify-out/graph.json
```

> Si no tienes graphify instalado: `pip install graphify-py` o `uv add graphify-py`.
> Vuelve a ejecutar `graphify .` cuando el codebase cambie significativamente.

**3. Configurar `.mcp.json` en la raíz del proyecto**

```json
{
  "mcpServers": {
    "slurp": {
      "command": "/ruta/a/.venv/bin/slurp",
      "args": ["serve", "--graph", "/ruta/a/tu-proyecto/graphify-out/graph.json"]
    }
  }
}
```

Para encontrar la ruta exacta del binario:

```bash
which slurp
# /Users/juancarlos/.venv/bin/slurp
```

Guarda el `.mcp.json` en la raíz de tu proyecto y reinicia Claude Code. Listo.

---

### Cómo funciona internamente

```
┌─────────────────────────────────────────────────────────┐
│                                                         │
│   Usuario escribe en Claude Code:                       │
│   "¿cómo funciona la autenticación en PrismaStats?"     │
│                                                         │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│   Claude Code detecta que necesita contexto             │
│   del codebase para responder con precisión             │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│   Llama automáticamente a:                              │
│   slurp_query("auth flow", budget=4000)                 │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│   Slurp puntúa los 2111 nodos del grafo,                │
│   selecciona los 5 más relevantes en 847 tokens         │
│   (97.1% menos que inyectar el grafo completo)          │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│   Devuelve a Claude: authenticate_user, JWTMiddleware,  │
│   hash_password, UserModel, sus relaciones y scores     │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│   Claude responde con contexto preciso del codebase,    │
│   sin alucinaciones, sin tokens desperdiciados          │
└─────────────────────────────────────────────────────────┘
```

### Lo que el usuario experimenta

Nada diferente. Claude Code responde con más precisión sobre el codebase porque tiene contexto real en lugar de inventar. Ningún comando manual. Ninguna configuración adicional por query.

---

## Modo Manual (para exploración y benchmarking)

Úsalo para:
- Explorar qué nodos selecciona slurp para una query concreta
- Generar contexto formateado para pegar manualmente en ChatGPT u otras herramientas
- Medir el ahorro real de tokens antes de confiar en el modo automático
- Depurar el grafo (¿qué nodos existen? ¿qué cambió entre versiones?)
- Visualizar el subgrafo con `--viz`

---

### `slurp QUERY`

Selecciona el subgrafo óptimo para una query y lo imprime.

```bash
# Básico
slurp "auth flow" --graph graphify-out/graph.json

# Con presupuesto y formato JSON
slurp "prisma schema" --budget 8000 --format json

# Ver desglose de scores por nodo
slurp "database pool" --explain

# Abrir visualizador interactivo en el navegador
slurp "user model" --viz

# Inyectar el código fuente real de cada función seleccionada
slurp "auth flow" --inject-code --budget 4000

# Usar embeddings reales en lugar de TF-IDF
slurp "auth flow" --backend openai

# Excluir nodos por tipo o path
slurp "auth flow" --ignore-file .slurpignore
```

**Salida de `--explain` en PrismaStats:**

```
 Score Breakdown
 ┌──────┬───────────────────────┬────────┬────────────┬──────────┬──────────┬──────────────────────┐
 │ Rank │ Label                 │  Final │ Structural │ Semantic │ Type     │ File                 │
 ├──────┼───────────────────────┼────────┼────────────┼──────────┼──────────┼──────────────────────┤
 │    1 │ authenticate_user     │ 0.9412 │     0.3821 │   0.9127 │ function │ src/auth/service.py  │
 │    2 │ JWTMiddleware         │ 0.8731 │     0.4112 │   0.8250 │ class    │ src/middleware/jwt   │
 │    3 │ hash_password         │ 0.7144 │     0.2934 │   0.7012 │ function │ src/auth/utils.py    │
 │    4 │ UserModel             │ 0.5823 │     0.5102 │   0.4811 │ class    │ src/models/user.py   │
 │    5 │ DatabasePool          │ 0.4102 │     0.6341 │   0.1822 │ class    │ src/db/pool.py       │
 └──────┴───────────────────────┴────────┴────────────┴──────────┴──────────┴──────────────────────┘
```

**Flags principales:**

| Flag | Default | Descripción |
|---|---|---|
| `--graph`, `-g` | auto-discover | Ruta al `graph.json`. |
| `--budget`, `-b` | `4000` | Presupuesto en tokens. |
| `--format`, `-f` | `markdown` | `markdown`, `json`, o `yaml`. |
| `--explain` | off | Desglose de score por nodo (final / estructural / semántico). |
| `--viz` | off | Visualizador interactivo HTML en el navegador. |
| `--min-score` | `0.15` | Umbral mínimo de relevancia. Aumentar si hay demasiados nodos seleccionados. |
| `--inject-code` | off | Añade el bloque de código fuente debajo de cada nodo (máx. 30 nodos). |
| `--backend` | `tfidf` | `tfidf` (sin API), `openai`, o `anthropic` para embeddings reales. |
| `--no-audit` | off | No guardar esta query en `.slurp/audit.jsonl`. |

---

### `slurp stats`

Resumen del grafo.

```bash
slurp stats --graph graphify-out/graph.json
```

```
Graph: graphify-out/graph.json
Nodes: 2111
Edges: 4823
```

Útil para verificar que graphify generó el grafo correctamente antes de empezar.

---

### `slurp audit`

Historial de queries y nodos más seleccionados.

```bash
slurp audit
slurp audit --top-nodes 20
```

```
 Query History (last 20 of 47)
 ┌───┬─────────────────────┬──────────────────────────┬───────┬────────┬─────────┐
 │ # │ Timestamp           │ Query                    │ Nodes │ Tokens │ Savings │
 ├───┼─────────────────────┼──────────────────────────┼───────┼────────┼─────────┤
 │ 1 │ 2026-06-07 09:14:22 │ auth flow                │ 5     │    847 │     97% │
 │ 2 │ 2026-06-07 09:31:05 │ prisma schema validation │ 7     │   1124 │     96% │
 │ 3 │ 2026-06-07 10:02:41 │ database pool connection │ 9     │   1893 │     93% │
 └───┴─────────────────────┴──────────────────────────┴───────┴────────┴─────────┘

 Top 10 Most Selected Nodes
 ┌──────┬──────────────────────┬────────────────┐
 │ Rank │ Node                 │ Times Selected │
 ├──────┼──────────────────────┼────────────────┤
 │    1 │ authenticate_user    │             31 │
 │    2 │ UserModel            │             28 │
 │    3 │ DatabasePool         │             19 │
 └──────┴──────────────────────┴────────────────┘
```

Cada query se registra en `.slurp/audit.jsonl` (append-only). Útil para saber qué partes del codebase visita más el agente de AI.

---

### `slurp diff`

Compara dos versiones del grafo y muestra el impacto de los cambios.

```bash
# Ver qué cambió entre ayer y hoy
slurp diff graph-v1.json graph-v2.json

# Con visualizador (verde=nuevo, rojo=eliminado, amarillo=modificado)
slurp diff graph-v1.json graph-v2.json --viz

# Seleccionar solo los nodos afectados más relevantes dentro de un presupuesto
slurp diff graph-v1.json graph-v2.json --budget 4000
```

**Caso de uso típico en PrismaStats:** después de refactorizar `UserModel`, ejecutar `graphify .` para regenerar el grafo, y luego `slurp diff` para saber qué otros nodos se vieron afectados y revisar el impacto en las funciones que lo llaman.

---

### `slurp export`

Genera un bloque de contexto listo para pegar en un prompt de AI.

```bash
# Para pegar en Claude (dentro de <context> XML)
slurp export "auth flow" --format claude

# Para pegar en ChatGPT
slurp export "auth flow" --format chatgpt

# Para añadir al CLAUDE.md del proyecto
slurp export "auth flow" --format claudemd --output context.md
```

**Salida de `--format claudemd`:**

```markdown
## Codebase Context

<!-- slurp-graph · query: "auth flow" | nodes: 5/2111 | tokens: 847/4,000 | coverage: 21.2% -->

## Relevant Nodes

### authenticate_user (function) · score: 0.94
...
```

---

### `slurp benchmark`

Mide el ahorro real de tokens comparando slurp vs inyectar el grafo completo.

```bash
slurp benchmark \
  --graph graphify-out/graph.json \
  --queries "auth flow" \
  --queries "prisma schema" \
  --queries "database pool" \
  --budget 2000 --budget 4000 --budget 8000
```

**Salida resumida:**

```
 Benchmark Results
 ┌──────────────────┬────────┬─────────────┬────────────┬─────────┬───────────┐
 │ Query            │ Budget │ Tokens used │ Full graph │ Savings │ Precision │
 ├──────────────────┼────────┼─────────────┼────────────┼─────────┼───────────┤
 │ auth flow        │  2,000 │         812 │     28,412 │  97.1%  │    94.3%  │
 │ auth flow        │  4,000 │       1,036 │     28,412 │  96.3%  │    97.1%  │
 │ prisma schema    │  2,000 │       1,176 │     28,412 │  95.8%  │    91.2%  │
 │ database pool    │  8,000 │       4,172 │     28,412 │  85.1%  │    88.7%  │
 └──────────────────┴────────┴─────────────┴────────────┴─────────┴───────────┘

 Summary
 ┌──────────────────┬────────┐
 │ Mean savings     │ 93.3%  │
 │ p50 savings      │ 94.2%  │
 │ p90 savings      │ 96.8%  │
 │ Mean precision   │ 92.4%  │
 │ Best case        │ 97.1%  │
 └──────────────────┴────────┘
```

Úsalo antes de confiar en el modo automático para confirmar que slurp tiene buen rendimiento en tu codebase específico.

---

### `slurp serve`

Arranca el servidor MCP manualmente (normalmente no hace falta — Claude Code lo lanza solo).

```bash
slurp serve --graph graphify-out/graph.json
```

Útil para depurar la integración MCP o para conectar otras herramientas que soporten el protocolo JSON-RPC 2.0.

---

## Cuándo usar cada modo

| Situación | Modo recomendado |
|---|---|
| Usar Claude Code en el día a día | **Automático** — configura una vez y olvídate |
| Primera vez con slurp en un proyecto nuevo | **Manual** → `slurp benchmark` para verificar rendimiento |
| Depurar por qué Claude da una respuesta inesperada | **Manual** → `slurp "tu query" --explain --viz` |
| Generar contexto para ChatGPT o Gemini | **Manual** → `slurp export --format chatgpt` |
| Revisar el impacto de un refactor grande | **Manual** → `slurp diff antes.json despues.json --viz` |
| Entender qué partes del código visita más el agente | **Manual** → `slurp audit --top-nodes 20` |
| Codebase cambia muy frecuentemente | **Automático** + regenerar `graph.json` en el pipeline de CI |
| Quieres embeddings reales en lugar de TF-IDF | **Manual** → `slurp "query" --backend openai`, luego configura `--backend` en el `.mcp.json` |

---

## Preguntas frecuentes

**¿Necesito graphify para usar slurp?**

No, pero es la forma más fácil de generar un `graph.json` compatible. Slurp acepta cualquier grafo con el formato estándar (`id` + `label` en nodos, `source` + `target` en edges). También carga GraphML (`.graphml`) y exports de Neo4j (`.csv`) de forma nativa.

---

**¿Qué hace exactamente `--min-score 0.15`?**

Filtra los nodos con score de relevancia menor a 0.15 antes de empezar la selección greedy. Sin este filtro, en grafos grandes (2000+ nodos) el algoritmo consideraría cientos de nodos casi irrelevantes, ralentizando la selección y diluyendo el resultado. Si tus queries son muy específicas y el presupuesto se llenó con nodos poco relevantes, sube el umbral a `--min-score 0.3` o más.

---

**En modo automático, ¿cómo sé qué nodos seleccionó slurp?**

Cada query se registra en `.slurp/audit.jsonl`. Ejecuta `slurp audit` para ver el historial completo con los nodos seleccionados, tokens usados y ahorro estimado. Para ver el razonamiento exacto de una query específica, réplicala en modo manual con `--explain`.

---

**¿Slurp modifica el codebase o el `graph.json`?**

No. Slurp es de solo lectura sobre el grafo. El único archivo que escribe es `.slurp/audit.jsonl` (append-only, desactivable con `--no-audit`). El `graph.json` nunca se modifica.

---

**La query devuelve demasiados nodos y el presupuesto se agota rápido. ¿Qué hago?**

Tres opciones, de menos a más agresiva:

1. **Sube `--min-score`**: `--min-score 0.3` excluye nodos con baja relevancia antes de la selección. Útil para queries amplias como `"database"`.
2. **Baja `--neighbor-decay`**: el valor por defecto `0.7` hace que los vecinos de cada nodo seleccionado reciban un 70% del score del nodo. Bajarlo a `0.4` reduce la expansión de contexto alrededor de cada nodo.
3. **Añade un `.slurpignore`**: excluye tipos de nodos que sabes que no son relevantes para tus queries habituales (e.g., `type:document`, `file:tests/**`).
