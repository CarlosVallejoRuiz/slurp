# Usage guide — slurp

> Two ways to use slurp: fully automatically in the background (recommended), or directly from the terminal for exploration and analysis.
> Primary flow: install → `slurp index .` → configure MCP → use Claude Code normally.

---

## Zero dependencies mode

**As of v0.6.0, graphify is optional.** Use `slurp index` to build `graph.json` directly from your source code — no LLM, no external tool, no API key required.

```bash
slurp index .                              # index current directory → graphify-out/graph.json
slurp index /path/to/project              # specific path
slurp index . --output out/graph.json     # custom output path
slurp index . --watch                     # re-index on every file save
```

**Supported languages:** Python (stdlib `ast`) · TypeScript/JS (tree-sitter if installed, regex otherwise) · Go (regex)

After indexing, everything else works exactly the same:

```bash
slurp "auth flow" --graph graphify-out/graph.json --budget 4000
```

The generated `graph.json` is fully compatible with graphify's format — you can mix both: index with `slurp index` today and switch to graphify later (or vice versa) without changing any other command.

> **Install tree-sitter for better TypeScript coverage:**
> `pip install "slurp-graph[ts]"` — adds tree-sitter and tree-sitter-typescript for precise TypeScript/TSX parsing. Without it, slurp falls back to regex (works for most common patterns).

---

## Automatic Mode (recommended)

Once configured, **slurp requires no manual intervention**. It runs in the background as an MCP server. Every time Claude Code needs codebase context, it calls slurp automatically, receives the optimal subgraph, and uses it as context. The user works with Claude Code exactly as they always have.

### Setup — 4 steps, one time only

**1. Install slurp-graph**

```bash
pip install slurp-graph
# or with uv
uv add slurp-graph
```

**2. Index the project**

```bash
cd /path/to/your-project
slurp index .
# → generates graphify-out/graph.json
```

Supports Python, TypeScript/JS, and Go out of the box — no LLM, no API key, no external tools.
Re-run whenever the codebase changes significantly (or use `slurp index . --watch` for live updates).

> **Already using graphify?** Slurp works with your existing `graph.json` out of the box — skip this step and point `.mcp.json` at your graphify output directly.

**3. Configure `.mcp.json` at the project root**

```json
{
  "mcpServers": {
    "slurp": {
      "command": "slurp",
      "args": ["serve", "--graph", "graphify-out/graph.json"]
    }
  }
}
```

**4. Restart Claude Code**

Save `.mcp.json` at your project root and restart Claude Code. Done.

---

### How it works internally

```
┌─────────────────────────────────────────────────────────┐
│                                                         │
│   User types in Claude Code:                            │
│   "how does authentication work in PrismaStats?"        │
│                                                         │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│   Claude Code detects it needs codebase context         │
│   to answer accurately                                  │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│   Automatically calls:                                  │
│   slurp_query("auth flow", budget=4000)                 │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│   Slurp scores all 2111 graph nodes,                    │
│   selects the 5 most relevant in 847 tokens             │
│   (97.1% fewer tokens than injecting the full graph)    │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│   Returns to Claude: authenticate_user, JWTMiddleware,  │
│   hash_password, UserModel, their relationships/scores  │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│   Claude responds with precise codebase context,        │
│   no hallucinations, no wasted tokens                   │
└─────────────────────────────────────────────────────────┘
```

### What the user experiences

Nothing different. Claude Code answers with greater precision about the codebase because it has real context instead of guessing. No manual commands. No additional configuration per query.

---

## Manual Mode (for exploration and benchmarking)

Use it to:
- Explore which nodes slurp selects for a specific query
- Generate formatted context to paste manually into ChatGPT or other tools
- Measure real token savings before relying on automatic mode
- Debug the graph (which nodes exist? what changed between versions?)
- Visualize the subgraph with `--viz`

---

### `slurp QUERY`

Selects the optimal subgraph for a query and prints it.

```bash
# Basic
slurp "auth flow" --graph graphify-out/graph.json

# With budget and JSON format
slurp "prisma schema" --budget 8000 --format json

# Show per-node score breakdown
slurp "database pool" --explain

# Open interactive visualizer in the browser
slurp "user model" --viz

# Inject the real source code of each selected function
slurp "auth flow" --inject-code --budget 4000

# Use real embeddings instead of TF-IDF
slurp "auth flow" --backend openai

# Exclude nodes by type or path
slurp "auth flow" --ignore-file .slurpignore
```

**`--explain` output on PrismaStats:**

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

**Main flags:**

| Flag | Default | Description |
|---|---|---|
| `--graph`, `-g` | auto-discover | Path to `graph.json`. |
| `--budget`, `-b` | `4000` | Token budget. |
| `--format`, `-f` | `markdown` | `markdown`, `json`, or `yaml`. |
| `--explain` | off | Per-node score breakdown (final / structural / semantic). |
| `--viz` | off | Interactive HTML visualizer in the browser. |
| `--min-score` | `0.15` | Minimum relevance threshold. Raise if too many nodes are selected. |
| `--inject-code` | off | Append source code block below each node (max 30 nodes). |
| `--backend` | `tfidf` | `tfidf` (no API), `openai`, or `anthropic` for real embeddings. |
| `--no-audit` | off | Do not save this query to `.slurp/audit.jsonl`. |

---

### `slurp stats`

Graph summary.

```bash
slurp stats --graph graphify-out/graph.json
```

```
Graph: graphify-out/graph.json
Nodes: 2111
Edges: 4823
```

Useful for verifying that graphify generated the graph correctly before starting.

---

### `slurp audit`

Query history and most-selected nodes.

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

Every query is recorded in `.slurp/audit.jsonl` (append-only). Useful for understanding which parts of the codebase the AI agent visits most.

---

### `slurp diff`

Compare two graph versions and show the impact of changes.

```bash
# See what changed between yesterday and today
slurp diff graph-v1.json graph-v2.json

# With visualizer (green=added, red=removed, yellow=modified)
slurp diff graph-v1.json graph-v2.json --viz

# Select only the most relevant affected nodes within a budget
slurp diff graph-v1.json graph-v2.json --budget 4000
```

**Typical use case in PrismaStats:** after refactoring `UserModel`, run `graphify .` to regenerate the graph, then `slurp diff` to find which other nodes were affected and review the impact on functions that call it.

---

### `slurp export`

Generate a context block ready to paste into an AI prompt.

```bash
# To paste into Claude (inside <context> XML tags)
slurp export "auth flow" --format claude

# To paste into ChatGPT
slurp export "auth flow" --format chatgpt

# To add to the project's CLAUDE.md
slurp export "auth flow" --format claudemd --output context.md
```

**`--format claudemd` output:**

```markdown
## Codebase Context

<!-- slurp-graph · query: "auth flow" | nodes: 5/2111 | tokens: 847/4,000 | coverage: 21.2% -->

## Relevant Nodes

### authenticate_user (function) · score: 0.94
...
```

---

### `slurp benchmark`

Measure real token savings comparing slurp vs injecting the full graph.

```bash
slurp benchmark \
  --graph graphify-out/graph.json \
  --queries "auth flow" \
  --queries "prisma schema" \
  --queries "database pool" \
  --budget 2000 --budget 4000 --budget 8000
```

**Summary output:**

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

Run this before relying on automatic mode to confirm slurp performs well on your specific codebase.

---

### `slurp index`

Index source code and generate a graphify-compatible `graph.json` without any external tool.

```bash
# Index current directory (default output: graphify-out/graph.json)
slurp index .

# Custom output path
slurp index . --output graph.json

# Specific project root
slurp index /path/to/project

# Live re-indexing on file changes
slurp index . --watch
```

**Example output on PrismaStats:**

```
Indexing /path/to/prismastats ...
✓ 312 nodes · 487 edges · 41 files
  Saved: /path/to/prismastats/graphify-out/graph.json

Next: slurp "your query" --graph /path/to/prismastats/graphify-out/graph.json
```

| Flag | Default | Description |
|---|---|---|
| `--output`, `-o` | `<path>/graphify-out/graph.json` | Output path for graph.json. |
| `--watch` | off | Re-index on file changes (watchdog included). |
| `--ignore-file` | `.slurpignore` | Exclusion rules (same file used for queries). |

`.slurpignore` is applied during indexing, so excluded nodes never enter the graph:

```
type:import      # skip all import nodes (reduces graph size significantly)
file:tests/**    # skip test files
id:generated_*  # skip auto-generated code
```

---

### `slurp serve`

Start the MCP server manually (normally not needed — Claude Code launches it automatically).

```bash
slurp serve --graph graphify-out/graph.json
```

Useful for debugging the MCP integration or connecting other tools that support the JSON-RPC 2.0 protocol.

---

## When to use each mode

| Situation | Recommended mode |
|---|---|
| Day-to-day use of Claude Code | **Automatic** — configure once and forget |
| First time using slurp on a new project | `slurp index .` then `slurp benchmark` to verify |
| No graphify installed | `slurp index .` — fully standalone, no external deps |
| Debugging why Claude gave an unexpected answer | **Manual** → `slurp "your query" --explain --viz` |
| Generating context for ChatGPT or Gemini | **Manual** → `slurp export --format chatgpt` |
| Reviewing the impact of a large refactor | **Manual** → `slurp diff before.json after.json --viz` |
| Understanding which code the agent visits most | **Manual** → `slurp audit --top-nodes 20` |
| Codebase changes very frequently | **Automatic** + `slurp index . --watch` or regenerate in CI |
| Want real embeddings instead of TF-IDF | **Manual** → `slurp "query" --backend openai`, then set `--backend` in `.mcp.json` |

---

## Frequently asked questions

**Do I need graphify to use slurp?**

No. As of v0.6.0, run `slurp index .` to build `graph.json` directly from source code — no graphify needed. Slurp also accepts any graph in the standard format (`id` + `label` on nodes, `source` + `target` on edges), and natively loads GraphML (`.graphml`) and Neo4j exports (`.csv`). Graphify remains a great option for richer semantic graphs generated with LLM assistance.

---

**What exactly does `--min-score 0.15` do?**

It filters out nodes with a relevance score below 0.15 before the greedy selection begins. Without this filter, on large graphs (2000+ nodes) the algorithm would consider hundreds of nearly-irrelevant nodes, slowing down selection and diluting the result. If your queries are very specific but the budget fills up with low-relevance nodes, raise the threshold to `--min-score 0.3` or higher.

---

**In automatic mode, how do I know which nodes slurp selected?**

Every query is recorded in `.slurp/audit.jsonl`. Run `slurp audit` to see the full history with selected nodes, tokens used, and estimated savings. To see the exact reasoning for a specific query, replay it in manual mode with `--explain`.

---

**Does slurp modify the codebase or `graph.json`?**

No. Slurp is read-only with respect to the graph. The only file it writes is `.slurp/audit.jsonl` (append-only, disable with `--no-audit`). The `graph.json` is never modified.

---

**A query returns too many nodes and the budget fills up quickly. What should I do?**

Three options, from least to most aggressive:

1. **Raise `--min-score`**: `--min-score 0.3` excludes low-relevance nodes before selection. Useful for broad queries like `"database"`.
2. **Lower `--neighbor-decay`**: the default `0.7` gives neighbors of each selected node 70% of that node's score. Lowering it to `0.4` reduces context expansion around each node.
3. **Add a `.slurpignore`**: exclude node types you know are irrelevant to your usual queries (e.g., `type:document`, `file:tests/**`).
