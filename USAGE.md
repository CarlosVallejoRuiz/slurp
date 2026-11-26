# Usage guide — slurp

> Two ways to use slurp: fully automatically in the background (recommended), or directly from the terminal for exploration and analysis.
> Primary flow: install → `slurp init` → restart Claude Code → use it normally. The graph keeps up with your edits by itself.

---

## Zero dependencies mode

**As of v0.6.0, graphify is optional.** Use `slurp index` to build `graph.json` directly from your source code — no LLM, no external tool, no API key required.

```bash
slurp index .                              # index current directory → graphify-out/graph.json
slurp index /path/to/project              # specific path
slurp index . --output out/graph.json     # custom output path
slurp index . --watch                     # keep graph.json current until Ctrl+C
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

Once configured, **slurp requires no manual intervention**. It runs in the background as an MCP server. Every time Claude Code needs codebase context, it calls slurp automatically, receives the optimal subgraph, and uses it as context — from a graph refreshed before each answer if your files changed, without asking you to approve anything. The user works with Claude Code exactly as they always have.

### Setup — one command

```bash
cd /path/to/your-project
slurp init
```

`slurp init` builds slurp's graph of the project, writes `.mcp.json`, approves the server
and its tools for Claude Code in `.claude/settings.local.json`, and adds a short paragraph
to `CLAUDE.local.md` telling the agent when to use slurp. It shows the plan and asks
before writing anything; see [`slurp init`](README.md#slurp-init) for every option. Then
restart Claude Code.

The four steps below are what `init` does, for doing it by hand.

### Setup by hand — 4 steps, one time only

**1. Install slurp-graph**

Choose the command that matches your setup:

| Setup | Command |
|---|---|
| Any project — recommended | `uv tool install slurp-graph` |
| Non-Python project (pipx) | `pipx install slurp-graph` |
| Simple global install | `pip install slurp-graph` |
| Python project (adds to `pyproject.toml`) | `uv add slurp-graph` |

**Don't have Python?** Install [uv](https://docs.astral.sh/uv/) first — it bundles a Python 3.12+ runtime:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then run `uv tool install slurp-graph`.

> **Windows note:** if you use `pip install` and `slurp` is not found afterwards, Python's `Scripts` folder may not be on your PATH. Use `uv tool install` or `pipx install` instead — they handle PATH automatically.

**2. Index the project**

```bash
cd /path/to/your-project
slurp index .
# generates graphify-out/graph.json
```

Supports Python, TypeScript/JS, and Go out of the box — no LLM, no API key, no external tools.
No need to re-run it: the server and the query commands refresh slurp's graph before
answering whenever files changed.

> **Already using graphify?** Slurp serves your existing `graph.json` as it is — but it cannot refresh a graph graphify built. For a graph that follows your edits, index into `graphify-out/slurp-graph.json` (`slurp index . -o graphify-out/slurp-graph.json`, which is what `slurp init` does) and point `.mcp.json` there; slurp finds that file before graphify's.

**3. Configure `.mcp.json` at the project root**

For global installs (`uv tool install`, `pipx`, `pip`):

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

If you installed with `uv add` inside a Python project, use `uv run` instead so slurp runs in the project's virtual environment:

```json
{
  "mcpServers": {
    "slurp": {
      "command": "uv",
      "args": ["run", "slurp", "serve", "--graph", "graphify-out/graph.json"]
    }
  }
}
```

> **Windows:** Use forward slashes in the graph path (`"graphify-out/graph.json"` works). If `slurp` is not found by Claude Code, use the full absolute path: find it with `where slurp` (CMD) or `(Get-Command slurp).Source` (PowerShell).

**4. Restart Claude Code**

Save `.mcp.json` at your project root and restart Claude Code. Claude Code asks once to use
the project's server and before each tool call; answer "don't ask again", or add
`"enabledMcpjsonServers": ["slurp"]` and `"mcp__slurp__*"` in `permissions.allow` to
`.claude/settings.local.json` — what `slurp init` writes.

---

### How it works internally

The figures below come from a real `slurp_query("auth flow", budget=4000)` call to
`slurp serve` on PrismaStats with v1.2.0, and the saving from `slurp benchmark` on the
same graph:

<!-- example: usage-diagram · slurp v1.2.0 · slurp_query("auth flow", budget=4000) over slurp serve; the saving from slurp benchmark -->
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
│   selects the 96 most relevant in 3,927 tokens          │
│   (96.7% fewer tokens than injecting the full graph)    │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│   Returns to Claude: createAdminClient(), AuthResult,   │
│   lib/auth/actions.ts, requireAdmin() and the rest,     │
│   with their relationships and scores                   │
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

**`--explain` on PrismaStats** — real output with v1.2.0 of `slurp "auth flow" --explain`;
the score breakdown prints after the subgraph, first and last rows shown:

<!-- example: usage-explain · slurp v1.2.0 · slurp "auth flow" --explain -->
```
                                          Score Breakdown
┏━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Rank  ┃ Label                 ┃  Final ┃ Structural ┃ Semantic ┃ Type    ┃ File                  ┃
┡━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━┩
│ 1     │ createAdminClient()   │ 0.7000 │     1.0000 │   0.0000 │ code    │ lib/supabase/admin.ts │
│ 2     │ AuthResult            │ 0.6918 │     0.0147 │   0.6431 │ code    │ lib/auth/actions.ts   │
│ 3     │ lib/auth/actions.ts   │ 0.6911 │     0.0132 │   0.6431 │ code    │ AGENTS.md             │
│ 4     │ requireAdmin()        │ 0.4338 │     0.3345 │   0.0000 │ code    │ lib/supabase/admin-a… │
│ 5     │ Supabase (PostgreSQL  │ 0.3921 │     0.0156 │   0.6431 │ concept │ docs/documento-tecni… │
│       │ + Auth + Storage +    │        │            │          │         │                       │
│       │ RLS)                  │        │            │          │         │                       │
│ 6     │ createClient()        │ 0.3691 │     0.1728 │   0.0000 │ code    │ lib/supabase/server.… │
│ 7     │ cn()                  │ 0.3527 │     0.1318 │   0.0000 │ code    │ lib/utils.ts          │
│ 8     │ computedHash          │ 0.3523 │     0.1308 │   0.0000 │ code    │ skills-lock.json      │
…
│ 96    │ page.tsx              │ 0.3053 │     0.0132 │   0.0000 │ code    │ app/dashboard/pizarr… │
└───────┴───────────────────────┴────────┴────────────┴──────────┴─────────┴───────────────────────┘
```

**Main flags:**

| Flag | Default | Description |
|---|---|---|
| `--graph`, `-g` | auto-discover | Path to `graph.json`. |
| `--budget`, `-b` | `4000` | Token budget: an upper bound on the text printed, measured. With `--format json`/`yaml` use about `6000`–`8000` for the same recall. |
| `--format`, `-f` | `markdown` | `markdown`, `json`, or `yaml`. |
| `--explain` | off | Per-node score breakdown (final / structural / semantic). |
| `--viz` | off | Interactive HTML visualizer in the browser. |
| `--min-score` | `0.15` | Minimum relevance threshold. Raise if too many nodes are selected. |
| `--inject-code` | off | Append source code block below each node (max 30 nodes). |
| `--backend` | `tfidf` | `tfidf` (no API) or `openai` for real embeddings. `anthropic` is refused: Anthropic has no embeddings endpoint. |
| `--no-audit` | off | Do not save this query to `.slurp/audit.jsonl`. |
| `--include-tests` | off | Let test code compete for a place (left out by default). |
| `--include-imports` | off | Let import nodes compete for a place (left out by default). |

Test code and the `import` nodes `slurp index` writes are left out of the subgraph by
default — the closing tip says how many — but stay in the graph, so `slurp explain`,
`slurp impact` and `slurp diff` still count the tests, apart from production callers.

---

### `slurp stats`

Graph summary.

```bash
slurp stats --graph graphify-out/graph.json
```

<!-- example: usage-stats · slurp v1.2.0 · slurp stats --graph graphify-out/graph.json -->
```
Graph: graphify-out/graph.json
Nodes: 2111
Edges: 3421
```

Useful for verifying that graphify generated the graph correctly before starting.

---

### `slurp audit`

Query history and most-selected nodes.

```bash
slurp audit
slurp audit --top-nodes 20
```

Real output with v1.2.0 on PrismaStats, after three queries at `--budget 4000`
(`auth flow`, `prisma schema validation`, `database pool connection`):

<!-- example: usage-audit · slurp v1.2.0 · slurp audit, after three queries at --budget 4000 on PrismaStats -->
```
                       Query History (last 3 of 3)
┏━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━┓
┃ #    ┃ Timestamp           ┃ Query                    ┃ Nodes ┃ Tokens ┃
┡━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━┩
│ 1    │ 2026-11-25T20:24:15 │ auth flow                │    96 │  3,927 │
│ 2    │ 2026-11-25T20:24:15 │ prisma schema validation │   108 │  3,924 │
│ 3    │ 2026-11-25T20:24:15 │ database pool connection │   104 │  3,927 │
└──────┴─────────────────────┴──────────────────────────┴───────┴────────┘
                   Top 10 Most Selected Nodes
┏━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┓
┃ Rank  ┃ Node                                ┃ Times Selected ┃
┡━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━┩
│ 1     │ supabase_admin_createadminclient    │              3 │
│ 2     │ supabase_admin_actions_requireadmin │              3 │
│ 3     │ supabase_server_createclient        │              3 │
│ 4     │ lib_utils_cn                        │              3 │
│ 5     │ skills_lock_seo_computedhash        │              3 │
│ 6     │ skills_lock_seo_source              │              3 │
│ 7     │ skills_lock_seo_sourcetype          │              3 │
│ 8     │ ndarray                             │              3 │
│ 9     │ supabase_server                     │              3 │
│ 10    │ supabase_admin                      │              3 │
└───────┴─────────────────────────────────────┴────────────────┘
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

**Typical use case in PrismaStats:** before refactoring `createAdminClient` (`lib/supabase/admin.ts`), keep a copy of `graph.json`; after the change, rebuild the graph (`graphify .` or `slurp index .`) and run `slurp diff` on the old and the new one to see which nodes changed and which of the functions that call it are at risk. To ask before editing, from the one graph you have, use `slurp impact lib/supabase/admin.ts`.

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

**`--format claudemd` output** — real, v1.2.0, on PrismaStats, first nodes shown:

<!-- example: usage-export · slurp v1.2.0 · slurp export "auth flow" --format claudemd -->
```markdown
## Codebase Context

<!-- slurp-graph · query: "auth flow" | nodes: 97/2111 | tokens: 3,927/4,000 | coverage: 4.6% -->

## Relevant Nodes

### createAdminClient() · score: 0.70
→ File: lib/supabase/admin.ts

### AuthResult · score: 0.69
→ File: lib/auth/actions.ts

…
```

---

### `slurp benchmark`

Measure real token savings comparing slurp vs injecting the full graph.

```bash
# macOS / Linux / PowerShell
slurp benchmark \
  --graph graphify-out/graph.json \
  --queries "auth flow" \
  --queries "prisma schema" \
  --queries "database pool" \
  --budget 2000 --budget 4000 --budget 8000
```

```bat
REM Windows CMD — no line continuation, write it on one line
slurp benchmark --graph graphify-out/graph.json --queries "auth flow" --queries "prisma schema" --budget 2000 --budget 4000
```

**Summary output** (the first command, v1.2.0, on a 2,111-node graphify graph of a
Next.js codebase; a per-run table with tokens, full graph, savings, cost, nodes and
coverage for each query × budget comes first):

<!-- example: usage-benchmark · slurp v1.2.0 · slurp benchmark (the first command above) -->
```
                         Summary
┏━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Metric             ┃                             Value ┃
┡━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ Mean savings       │                             96.1% │
│ p50 savings        │                             96.7% │
│ p90 savings        │                             98.3% │
│ p95 savings        │                             98.3% │
│ Best case          │ 98.3% ('auth flow', budget 2,000) │
│ Worst case         │ 93.3% ('auth flow', budget 8,000) │
│ Cost with slurp    │                           $0.1236 │
│ Cost without slurp │                           $3.1813 │
│ Money saved        │                   $3.0577 (96.1%) │
└────────────────────┴───────────────────────────────────┘
Retrieval quality is not measured here: see `slurp eval --retrieval`.
```

Both sides are measured text: what slurp prints, and the whole graph printed the same
way. Before v1.1.0 this table also showed a "precision" (the scorer agreeing with
itself) and savings against an under-counted slurp output.

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

# Keep graph.json current until Ctrl+C (checks every second)
slurp index . --watch

# Re-parse only what changed since the graph was built
slurp index . --smart

# graphify-out/graph.json already holds graphify's graph? Keep both...
slurp index . -o graphify-out/slurp-graph.json
# ...or replace it (without --force, slurp asks first and defaults to no)
slurp index . --force
```

**Example output on PrismaStats** (real output, v1.2.0; the paths are placeholders):

<!-- example: index · slurp v1.2.0 · slurp index /path/to/prismastats -->
```
Indexing /path/to/prismastats ...
✓ 1582 nodes · 1852 edges · 185 files
  Parsers: Python (ast) · TypeScript (tree-sitter)
  Saved: /path/to/prismastats/graphify-out/graph.json

Next: slurp "your query" --graph /path/to/prismastats/graphify-out/graph.json
```

| Flag | Default | Description |
|---|---|---|
| `--output`, `-o` | `<path>/graphify-out/graph.json` | Output path for graph.json. |
| `--watch` | off | Keep the graph current until Ctrl+C: polls every second, refreshes after a quiet second. Only needed for tools that read `graph.json` outside slurp. |
| `--smart` | off | Re-parse only the files whose content changed (from the graph's manifest; git for graphs from slurp ≤ 1.2). |
| `--ignore-file` | `.slurpignore` | Exclusion rules (same file used for queries). |

`.slurpignore` is applied during indexing, so excluded nodes never enter the graph:

```
type:import      # drop import nodes from the graph itself (queries skip them anyway)
file:tests/**    # drop test files — explain/impact/diff then stop counting tests too
id:generated_*  # skip auto-generated code
```

---

### `slurp serve`

Start the MCP server manually (normally not needed — Claude Code launches it automatically).

```bash
slurp serve --graph graphify-out/graph.json
```

Useful for debugging the MCP integration or connecting other tools that support the JSON-RPC 2.0 protocol. Before each call it refreshes slurp's own graph if files changed (waiting at most 2 s; `--refresh-timeout`, `--no-refresh`, or `SLURP_REFRESH_TIMEOUT` / `SLURP_REFRESH=0`), and leaves graphify's graph as it is.

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
| Codebase changes very frequently | **Automatic** — the graph is refreshed before each answer; `slurp index . --watch` only for tools that read `graph.json` directly |
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

Every query is recorded in `.slurp/audit.jsonl`. Run `slurp audit` to see the full history with selected nodes and tokens used (measured savings are `slurp benchmark`'s job). To see the exact reasoning for a specific query, replay it in manual mode with `--explain`.

---

**Does slurp modify the codebase or `graph.json`?**

It never modifies your code. It does rewrite **its own** graph: before answering, the MCP server and the query commands refresh slurp's `graph.json` when files changed (turn it off with `--no-refresh` or `SLURP_REFRESH=0`). A graph slurp did not build — graphify's — is never written, only read. Besides that, slurp writes `.slurp/audit.jsonl` (append-only, disable with `--no-audit`) and, if `.slurp/` exists, the MCP session log.

---

**How do I know the graph is up to date?**

slurp checks before every answer — a few stat calls, under a millisecond — and refreshes its graph within 2 seconds when a file changed. If the refresh takes longer, the answer says so in its first line and the next one gets the new graph. In the terminal the warning goes to stderr. `slurp benchmark` and `slurp eval` never refresh: they warn when the graph is out of date.

---

**A query returns too many nodes and the budget fills up quickly. What should I do?**

Three options, from least to most aggressive:

1. **Raise `--min-score`**: `--min-score 0.3` excludes low-relevance nodes before selection. Useful for broad queries like `"database"`.
2. **Lower `--neighbor-decay`**: the default `0.7` gives neighbors of each selected node 70% of that node's score. Lowering it to `0.4` reduces context expansion around each node.
3. **Add a `.slurpignore`**: exclude node types you know are irrelevant to your usual queries (e.g., `type:document`). Tests and import nodes are already left out of queries by default.
