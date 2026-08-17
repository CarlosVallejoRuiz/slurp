<p align="center">
  <img src="assets/logo.png" alt="slurp logo" width="120">
</p>

# slurp

![tests](https://img.shields.io/badge/tests-1521%20passed-brightgreen)
![python](https://img.shields.io/badge/python-3.12%2B-blue)
![license](https://img.shields.io/badge/license-MIT-lightgrey)
![pypi](https://img.shields.io/badge/PyPI-slurp--graph-orange)

> *graphify builds the bowl. slurp serves exactly the noodles your LLM needs.*

A knowledge graph is a bowl of ramen — thousands of nodes tangled together. Your LLM doesn't need the whole bowl. Slurp scores every node against your query, then greedily selects the highest-relevance subgraph that fits within your token budget — and tells you exactly what it picked and why. Works standalone or as a companion to graphify.

---

## Benchmark

Tested on a real **PrismaStats** codebase: 2,111 nodes, 28,412 tokens total.

| Query | Budget 2k | Budget 4k | Budget 8k |
|---|---|---|---|
| `"auth flow"` | **97.1%** saved | 96.3% saved | 95.2% saved |
| `"prisma schema"` | 95.8% saved | 94.2% saved | 93.8% saved |
| `"database pool"` | 93.1% saved | 89.1% saved | 85.1% saved |

**Mean savings: 93.3% · p50: 94.2% · Best case: 97.1%**

Even the worst case — `"database pool"` at budget 8k — injects 85% fewer tokens than the full graph.

---

## Performance

Measured on the same PrismaStats graph (2,111 nodes, 3,421 edges), 10 hot queries
through the real MCP handler with `time.perf_counter`:

| Optimization | Before | After | Improvement |
|---|---|---|---|
| PageRank cache | 8.32 ms | 0.002 ms | **4000×** |
| Token count cache | ~50 ms | 0.174 ms | **280×** |
| Greedy heap (O(n log n)) | 63.78 ms | 10.69 ms | **6×** |
| End-to-end latency | 86.41 ms | 10.69 ms | **8.1×** |
| Graph staleness | silent | auto-reload | ✅ |

**PageRank cache** — PageRank depends only on the graph, never on the query, so
it is computed once per graph object instead of once per query.

**Token count cache** — per-node token costs are memoised across queries, keyed
by `(node_id, encoding)`. The graph's text does not change between queries, so
neither does its token cost.

**Greedy heap** — the subgraph selector used to scan every remaining candidate on
each iteration, which is `O(n²)`. It now uses a lazy max-heap: scores still mutate
as neighbors get boosted, so a boost pushes a fresh entry and superseded entries
are discarded when they surface. Ties break by node id, which also makes the
selection **deterministic** — the previous scan broke ties by set iteration order,
so the same query could return a different subgraph on every process start.

**Graph staleness** — the MCP server records the graph file's mtime and reloads it
when it changes on disk, invalidating both caches. Before, a server started before
a re-index would keep answering from the graph it read at boot, with no way for the
client to know. Responses that triggered a reload carry `"graph_reloaded": true`.

---

## Install

**Quick reference:**

| Situation | Command |
|---|---|
| Any project, fastest setup | `uv tool install slurp-graph` |
| Non-Python project (pipx user) | `pipx install slurp-graph` |
| Simple global install | `pip install slurp-graph` |
| Python project (adds to `pyproject.toml`) | `uv add slurp-graph` |
| Better TypeScript/TSX indexing | `pip install "slurp-graph[ts]"` |

**Requires Python 3.12+.** Don't have Python? [Install uv](https://docs.astral.sh/uv/) — it bundles a Python runtime and is the fastest path.

---

**Install uv (if you don't have Python yet):**

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then: `uv tool install slurp-graph`

---

**Non-Python projects** (JS, TypeScript, Go — install slurp globally so it's on your PATH):

```bash
uv tool install slurp-graph     # recommended: isolated environment, no conflicts
pipx install slurp-graph        # same idea if you already have pipx
pip install slurp-graph         # works if Python's bin/Scripts dir is on your PATH
```

> **Windows:** `pip install` places `slurp.exe` in Python's `Scripts` folder, which may not be on PATH by default. If `slurp` is not found after install, see [adding Python Scripts to PATH](https://docs.python.org/3/using/windows.html#excursus-setting-environment-variables), or use `uv tool install` instead (it handles PATH automatically).

---

**Python projects** (adds slurp as a project dependency):

```bash
uv add slurp-graph
# or
pip install slurp-graph
```

> When using `uv add`, run slurp as `uv run slurp` or activate the virtual environment first. For the MCP server with `uv add`, see the note in [MCP Integration](#mcp-integration).

---

> PyPI package: `slurp-graph` — CLI command: `slurp`

📖 **Full usage guide (automatic MCP mode + manual CLI):** [USAGE.md](USAGE.md)

---

## Quickstart

**New to slurp?** Run `slurp init` in your project root — it detects your language, indexes your codebase, and configures the MCP server in one step.

```bash
slurp init
```

Then query it:

```bash
slurp "auth flow" --graph graph.json --budget 4000
```

```
╭─ Slurp — Subgraph for: "auth flow" (budget: 4,000 tokens) ──────────────╮
│ Selected 5/2111 nodes · 847/4,000 tokens used (21.2%)                    │
╰───────────────────────────────────────────────────────────────────────────╯

## Relevant Nodes

### authenticate_user (function) · score: 0.94
Validates user credentials and returns JWT token.
→ File: src/auth/service.py

### JWTMiddleware (class) · score: 0.87
Intercepts HTTP requests and validates Authorization header.
→ File: src/middleware/jwt.py

### hash_password (function) · score: 0.71
Hashes password using bcrypt with a cost factor of 12.
→ File: src/auth/utils.py

## Key Relationships
- JWTMiddleware → calls → authenticate_user
- authenticate_user → calls → hash_password

---
💡 2106 additional connected nodes available — increase --budget to include them
```

Add `--inject-code` to embed the actual function body next to each node:

```bash
slurp "auth flow" --graph graph.json --budget 4000 --inject-code
```

````
### authenticate_user (function) · score: 0.94
Validates user credentials and returns JWT token.
→ File: src/auth/service.py

```python
def authenticate_user(username: str, password: str) -> dict | None:
    user = db.query(User).filter_by(username=username).first()
    if not user or not bcrypt.checkpw(password.encode(), user.password_hash):
        return None
    return {"token": jwt.encode({"sub": user.id}, SECRET_KEY)}
```
````

Pipe the output directly into your LLM prompt, save it to a file, or use `slurp export` to format it as a ready-to-paste system prompt block.

---

## Commands

### `slurp init`

Guided one-command setup. **New to slurp?** Run `slurp init` in your project root — it detects your language, indexes your codebase, and configures the MCP server in one step.

```bash
slurp init          # from your project root — no flags needed
slurp init --yes    # accept every prompt (CI / scripting)
```

It shows you a plan and asks for confirmation before touching anything:

1. **Detects the dominant language** by counting `.py`, `.ts`, `.tsx`, `.js`, `.jsx` and `.go` files, ignoring vendored trees like `node_modules/` and `.venv/`.
2. **Indexes the project** into `graphify-out/graph.json` with a live progress bar. If a graph already exists, it offers to reuse it instead of re-indexing.
3. **Writes `.mcp.json`** pointing at the slurp binary it detected — works with both `uv tool install` and `pip install` layouts. An existing `.mcp.json` is never clobbered: slurp asks first, and other MCP servers in the file are preserved.

Finish by restarting your AI coding assistant to activate slurp.

| Flag | Default | Description |
|---|---|---|
| `--yes`, `-y` | off | Accept every confirmation prompt. Useful for CI and scripting. |

### `slurp QUERY`

The main command. Scores all graph nodes against your query and selects the optimal subgraph within the token budget.

```bash
slurp "auth flow" --graph graph.json --budget 4000
slurp "payment processing" --format json
slurp "JWT validation" --explain
slurp "database schema" --inject-code --min-score 0.3
slurp "prisma models" --backend openai
```

| Flag | Default | Description |
|---|---|---|
| `--graph`, `-g` | auto-discover | Path to `graph.json`. |
| `--budget`, `-b` | `4000` | Token budget for subgraph selection. |
| `--format`, `-f` | `markdown` | Output format: `markdown`, `json`, or `yaml`. |
| `--model`, `-m` | `cl100k_base` | Tiktoken encoding for token counting. |
| `--explain` | off | Print per-node score breakdown: final / structural / semantic. |
| `--no-audit` | off | Skip writing to `.slurp/audit.jsonl`. |
| `--neighbor-decay` | `0.7` | Score multiplier applied to neighbors of each selected node. |
| `--min-score` | `0.15` | Minimum relevance score; nodes below this are excluded before selection. |
| `--viz` | off | Open an interactive graph visualization in the browser. |
| `--viz-output PATH` | — | Save visualization HTML to file (without opening browser). |
| `--ignore-file` | `.slurpignore` | Path to node exclusion rules. |
| `--backend` | `tfidf` | Scoring backend: `tfidf` (default), `openai`, or `anthropic`. |
| `--inject-code` | off | Embed source code blocks for each selected node (requires ≤30 nodes). |
| `--project-root` | graph dir | Root directory for resolving `source_file` paths. |

**Auto-discovery** (when `--graph` is omitted):

1. `./graph.json`
2. `./graphify-out/graph.json`
3. `./.graphify/graph.json`

---

### `slurp stats`

Print node and edge counts for a graph file.

```bash
slurp stats --graph graph.json
```

```
Graph: graph.json
Nodes: 2111
Edges: 4823
```

---

### `slurp audit`

Show the history of queries logged to `.slurp/audit.jsonl`, plus the most frequently selected nodes.

```bash
slurp audit
slurp audit --top-nodes 20
slurp audit --audit-dir /custom/.slurp
```

Every query is appended as a JSON line (unless `--no-audit` is passed). Useful for tracking which parts of your codebase an AI agent visits most.

---

### `slurp diff`

**Know exactly what changed — and what it affects.**

A `git diff` tells you which lines moved. It cannot tell you that the helper you
just renamed is called from eleven other modules, or that the function you deleted
was the only caller keeping a service alive. `slurp diff` compares two versions of
your knowledge graph and reports the **blast radius**: what changed, what sits
downstream of it, and which nodes are central enough that a reviewer should look
at them by hand.

#### The workflow: before you merge

Index both sides of the change and diff them:

```bash
git checkout main       && slurp index . --output main.json
git checkout feature/auth-refactor && slurp index . --output feature.json

slurp diff main.json feature.json --viz
```

The `--viz` flag opens an interactive graph where the change is colour-coded:
**green** for added nodes, **red** for removed, **yellow** for modified, and
**grey** for untouched neighbors that are still connected to the change. You can
see at a glance whether your refactor touched one isolated corner or pulled on a
thread running through half the codebase.

#### Text output

Without `--viz` you get a Markdown impact report — pipe it into a PR description,
a review checklist, or your AI assistant:

```
# Slurp Diff — Impact Analysis

## Summary
- **⚠️ High impact change**
- **Impact score:** 0.7412 (🔴 high)
- 3 nodes added · 1 removed · 2 modified
- 5 edges added · 2 removed
- 9 nodes in risk neighborhood

## Added Nodes (3)

### refresh_token (function)
Issues a new JWT from a valid refresh token.
→ File: src/auth/tokens.py

## Removed Nodes (1)

### legacy_session_check

## Modified Nodes (2)

### authenticate_user (function)
### JWTMiddleware (class)

## Affected Edges

**Added (5):**
- login_handler → calls → refresh_token
- JWTMiddleware → calls → refresh_token

**Removed (2):**
- login_handler → legacy_session_check

## Nodes at Risk
Direct neighbors of changed nodes:

- login_handler (function) · centrality: 0.0841
- UserModel (class) · centrality: 0.0663
- api_router (module) · centrality: 0.0512

## What to review
Most connected nodes in the blast radius — review these first:

1. login_handler (function) · centrality: 0.0841 · src/api/login.py
2. UserModel (class) · centrality: 0.0663 · src/models.py
3. api_router (module) · centrality: 0.0512 · src/api/router.py
4. JWTMiddleware (class) · centrality: 0.0447 · src/middleware/jwt.py
5. authenticate_user (function) · centrality: 0.0391 · src/auth/service.py
```

The run finishes with a colour-coded summary panel and, if you did not pass
`--viz`, the exact command to see the full picture:

```
╭─────────────── Impact Summary ───────────────╮
│                                              │
│  ⚠️ High impact change  (impact score 0.7412) │
│                                              │
│  +3 added  -1 removed  ~2 modified           │
│                                              │
│  See the full blast radius:                  │
│    slurp diff main.json feature.json --viz   │
│                                              │
╰──────────────────────────────────────────────╯
```

#### Flags

```bash
slurp diff old.json new.json
slurp diff old.json new.json --hops 2 --viz
slurp diff old.json new.json --budget 4000
slurp diff old.json new.json --viz-output reports/impact.html
```

| Flag | Default | Description |
|---|---|---|
| `--hops` | `2` | Depth of impact neighborhood expansion. |
| `--viz` | off | Open an interactive visualizer of the affected area in the browser. |
| `--viz-output PATH` | — | Save visualization HTML to file (without opening browser). |
| `--budget`, `-b` | none | Token budget; selects the most relevant nodes from the affected area. |

Impact score is computed from the centrality of the changed nodes — changing a
leaf scores near zero, changing a hub scores high. `--hops` controls how far the
blast radius is traced; `--budget` narrows the affected area down to what fits a
token budget, so you can hand exactly the relevant slice to a reviewer or an LLM.

> **This is the only tool that shows you the blast radius of your code changes before you merge.**

---

### `slurp export`

Export a context block ready to paste into an AI system prompt.

```bash
slurp export "auth flow" --format claude     # <context> XML tags
slurp export "auth flow" --format chatgpt    # [CODEBASE CONTEXT] block
slurp export "auth flow" --format claudemd   # ## Codebase Context for CLAUDE.md
slurp export "auth flow" --output context.md
```

All three formats include query, nodes selected/total, tokens used/budget, and coverage %.

---

### `slurp serve`

Start an MCP stdio server (JSON-RPC 2.0) that exposes the `slurp_query` tool.

```bash
slurp serve --graph graph.json
slurp serve --graph graph.json --no-log
```

| Flag | Default | Description |
|---|---|---|
| `--graph`, `-g` | auto-discover | Path to `graph.json`. |
| `--log` / `--no-log` | `--log` | Append served queries to `.slurp/session.log` (only if `.slurp/` exists). |

See [MCP Integration](#mcp-integration) for configuration.

---

### `slurp session`

Shows queries processed by the MCP server in real time.

```bash
slurp session --last 10
slurp session --tail
```

| Flag | Default | Description |
|---|---|---|
| `--last N` | `20` | Number of most recent entries to show. |
| `--tail` | off | Follow the log in real time (Ctrl+C to stop). |
| `--log-dir PATH` | `.slurp` | Directory containing `session.log`. |

```
             MCP Session Log — last 2 (.slurp/session.log)
┏━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━┳━━━━━━━━┳━━━━━━━━━┓
┃ Time                ┃ Query      ┃ Budget ┃ Nodes ┃ Tokens ┃ Savings ┃
┡━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━╇━━━━━━━━╇━━━━━━━━━┩
│ 2026-08-15T12:13:58 │ auth flow  │  4,000 │   171 │  4,000 │   94.6% │
│ 2026-08-15T12:13:58 │ mcp server │  2,000 │    94 │  1,995 │   97.3% │
└─────────────────────┴────────────┴────────┴───────┴────────┴─────────┘
```

Use it to confirm your AI assistant is actually calling slurp. The log is written
only when `.slurp/` already exists — running any query manually creates it, and
`slurp serve --no-log` turns logging off entirely.

---

### `slurp benchmark`

Measure real token savings across queries and budgets.

```bash
slurp benchmark \
  --graph graph.json \
  --queries "auth flow" --queries "schema validation" \
  --budget 2000 --budget 4000 --budget 8000
# Windows CMD: remove the backslashes and write on one line
```

Outputs a per-run table and aggregate stats: mean savings, p50/p90/p95, best/worst case, and precision (fraction of relevant nodes captured).

---

### `slurp index`

Index the project's source code and generate `graph.json` without graphify or any LLM.

```bash
slurp index .                              # index current directory
slurp index /path/to/project              # specific path
slurp index . --output custom/graph.json  # custom output path
slurp index . --watch                     # re-index on file changes
slurp index . --smart                     # only re-index what changed
```

```
Indexing /path/to/project ...
✓ 312 nodes · 487 edges · 41 files
  Saved: /path/to/project/graphify-out/graph.json

Next: slurp "your query" --graph /path/to/project/graphify-out/graph.json
```

| Flag | Default | Description |
|---|---|---|
| `--output`, `-o` | `<path>/graphify-out/graph.json` | Output path for graph.json. |
| `--watch` | off | Re-index on file changes (requires watchdog, installed by default). |
| `--smart` | off | Git-aware incremental re-index — only re-indexes changed files and their dependents. |
| `--dry-run` | off | Show what `--smart` would re-index without making changes. |
| `--ignore-file` | `.slurpignore` | Path to .slurpignore rules. |

#### Incremental re-indexing with `--smart`

Re-parsing every file after a two-line edit is wasted work. `--smart` asks git which
files changed since the last commit — unstaged, staged, and untracked — widens that
set to the files that import them (2 hops), and rewrites only that slice of the graph.

```bash
slurp index . --smart
# → Detected 1 changed file (git diff)
# ✓ Updated 47 nodes · 46 edges in 0.1s
#   Full index would take: ~2.2s (21× faster)
```

The resulting graph is identical to a full index — nodes from re-parsed files are
replaced, nodes from deleted files are dropped, and no dangling edges are left behind.

`--dry-run` lists the files it would touch and exits without writing:

```bash
slurp index . --smart --dry-run
```

It falls back to a full index, with a message, when there is no git repository or no
existing graph to update. Speedup depends on what you touched: editing a leaf module
is near-instant, while editing something the whole project imports pulls in most of it.

#### Supported languages

| Language | Parser |
|---|---|
| Python | stdlib `ast` |
| TypeScript / JavaScript | tree-sitter |
| Java | tree-sitter |
| Rust | tree-sitter |
| C# | tree-sitter |
| Ruby | tree-sitter |
| PHP | tree-sitter |
| Kotlin | tree-sitter ⚠️ |
| Scala | tree-sitter |
| Swift | tree-sitter |
| C | tree-sitter |
| C++ | tree-sitter |
| Go | regex |
| Lua | regex |
| Elixir | regex |
| PowerShell | regex |

> ⚠️ **Kotlin:** the `tree-sitter-kotlin` grammar has known parsing issues — it
> fails on a class with a body followed by an `object` declaration, which is
> ordinary Kotlin. Slurp detects the broken parse tree and the regex fallback is
> active in practice. The extra is still declared so a fixed grammar release
> takes effect without a code change.

Every tree-sitter language falls back to a regex parser when its grammar is not
installed. The fallback extracts strictly less — it cannot nest inner classes or
reach into method bodies — so `slurp index` always prints which parser it used:

```
✓ 312 nodes · 487 edges · 41 files
  Parsers: Python (ast) · Java (tree-sitter) · Ruby (regex fallback)
  Install the 'ts' extra for full TypeScript support: uv sync --extra ts
```

Python needs no extra (it uses the standard library). **Go, Lua, Elixir and
PowerShell are indexed by regex as their primary parser** — no grammar is wired
up for them, so their regex parser is the design rather than a degradation and
is never reported as a fallback.

#### Optional extras

| Extra | Languages | Install |
|---|---|---|
| `ts` | TypeScript, JavaScript | `uv sync --extra ts` |
| `java` | Java | `uv sync --extra java` |
| `rust` | Rust | `uv sync --extra rust` |
| `csharp` | C# | `uv sync --extra csharp` |
| `ruby` | Ruby | `uv sync --extra ruby` |
| `php` | PHP | `uv sync --extra php` |
| `kotlin` | Kotlin | `uv sync --extra kotlin` |
| `scala` | Scala | `uv sync --extra scala` |
| `swift` | Swift | `uv sync --extra swift` |
| `cpp` | C, C++ | `uv sync --extra cpp` |
| `all-languages` | All of the above | `uv sync --extra all-languages` |

Each language records what matters for navigating that ecosystem: Java and C#
annotations/attributes (`@Service`, `[HttpGet]`) plus access modifiers, Rust
lifetimes and `pub` visibility, Ruby class methods and accessors, PHP magic
methods (`__get`, `__call`), Kotlin `data`/`sealed`/`suspend` and extension
receivers, Scala `case` classes and `implicit`, Swift computed properties and
`async`. Relationships become typed edges — `extends`, `implements` (including
Rust's `impl Trait for Type`), `with` (Scala), `conforms_to` (Swift), and
`mixin` for Ruby's `include`/`extend`/`prepend` and PHP's trait `use`, Elixir's
`use`/`import`/`alias`/`require`, and PowerShell's `requires`.

**Broken-grammar guard.** A tree-sitter grammar that returns a parse tree
containing `ERROR` nodes silently drops whole declarations. Slurp checks for that
after every parse and treats it as a parser failure: it warns on stderr and falls
back to regex, which extracts less per declaration but does not lose most of the
file.

After indexing, the full query pipeline works as usual:

```bash
slurp "auth flow" --graph graphify-out/graph.json --budget 4000
```

---

## Works with graphify

Slurp is the query layer for [`graphify`](https://github.com/CarlosVallejoRuiz/graphify). Run graphify on your codebase, point slurp at the output.

```bash
graphify .                                        # generates graphify-out/graph.json
slurp "auth flow" --budget 4000                   # auto-discovers graphify-out/graph.json
```

**Supported node fields:**

```json
{
  "id": "authenticate_user",
  "label": "authenticate_user",
  "type": "function",
  "description": "Validates credentials and returns JWT.",
  "importance": 9,
  "source_file": "src/auth/service.py",
  "source_location": "L42"
}
```

The `type`, `description`, `importance`, `source_file`, and `source_location` fields are optional but improve scoring and enable `--inject-code`. Any graph with `id` + `label` on nodes and `source`/`target` on edges will work.

Both `links` (graphify/NetworkX serialization) and `edges` are supported. Additional formats are auto-detected by extension:

| Extension | Format |
|---|---|
| `.json` | graphify or generic JSON |
| `.graphml` | GraphML (NetworkX / yEd / Gephi) |
| `.csv` | Neo4j export (nodes CSV + sibling relationships CSV) |

Use `slurp convert` or the `convert_graph()` API to export between formats.

---

## MCP Integration

Run slurp as an MCP server so Claude Code (or any MCP-compatible agent) can query the graph directly.

**`.mcp.json`** — for global installs (`uv tool install`, `pipx`, `pip`):

```json
{
  "mcpServers": {
    "slurp": {
      "command": "slurp",
      "args": ["serve", "--graph", "/absolute/path/to/graphify-out/graph.json"]
    }
  }
}
```

**`.mcp.json`** — if you installed with `uv add` inside a Python project:

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

> **Windows:** Use forward slashes or escaped backslashes in the graph path: `"C:/Users/you/project/graphify-out/graph.json"`. If `slurp` isn't found, replace `"command": "slurp"` with the full path — find it with `where slurp` (CMD) or `Get-Command slurp | Select-Object Source` (PowerShell).

**Tool exposed:** `slurp_query(query: str, budget: int = 4000) → str`

Claude Code calls this automatically when it needs codebase context. The server runs over stdio and returns the formatted markdown subgraph — no HTTP, no ports.

---

## .slurpignore

Exclude nodes by type, file path, or ID pattern. Create `.slurpignore` in your project root:

```
# Exclude documentation nodes
type:document
type:markdown

# Exclude test files
file:tests/**
file:**/*.test.ts

# Exclude generated code
id:generated_*
```

Pass a custom path with `--ignore-file path/to/.slurpignore`.

---

## Design decisions

**Power-iteration PageRank without numpy.** `nx.pagerank()` requires numpy. Slurp implements a 20-line pure-Python power-iteration algorithm (convergence: `Σ|rank_new − rank_old| < N × tol`). Same result, no heavy dependency.

**TF-IDF without scikit-learn.** Hand-rolled TF-IDF with smoothed IDF (`log((N+1)/(df+1)) + 1`) and cosine similarity. The tokenizer splits camelCase and snake_case, so `authenticate_user` scores on both `authenticate` and `user`. The `score_nodes()` interface is backend-agnostic — swap to real embeddings with `--backend openai` or `--backend anthropic` without touching any caller.

**YAML serializer without PyYAML.** `_yaml_scalar()` renders Python primitives as valid YAML scalars using `json.dumps()` for strings that need quoting (JSON string literals are valid YAML 1.1). No PyYAML dependency.

**`lru_cache` on the tiktoken encoder.** `tiktoken.get_encoding()` reads tokenizer data from disk on first call. Caching with `lru_cache(maxsize=8)` means repeated token-counting calls within a single run hit memory, not disk.

**`+0.3` score boost for `file_type == "code"` nodes (clamped to 1.0).** Documentation nodes compete unfairly with code in technical queries. The boost is bounded so it cannot override a genuinely high structural+semantic score.

**`--inject-code` capped at 30 nodes.** Code blocks are 50–200 tokens each. At 30 nodes, that's up to 6,000 extra tokens — manageable. At 200 nodes it would explode the context budget. The cap is enforced in both the CLI (warning message) and `inject_code()` (hard guard), so the formatter never receives oversized input.

---

## Roadmap

- ✅ **v0.1.0** — `loader`, `scorer`, `budget`, `formatter`, `audit` — core pipeline, full tests, `slurp QUERY` + `slurp stats`
- ✅ **v0.2.0** — `--explain`, `.slurpignore`, `--viz` interactive HTML, `--min-score`, camelCase/snake_case tokenizer, `--neighbor-decay`
- ✅ **v0.3.0** — `slurp serve` (MCP stdio), `slurp diff`, `slurp export` (claude/chatgpt/claudemd), PyPI publish as `slurp-graph`
- ✅ **v0.4.0** — `--backend openai|anthropic` (optional embeddings), `slurp benchmark`, GraphML + Neo4j CSV loader, `convert_graph()`
- ✅ **v0.5.0** — `--inject-code`: extract real function bodies from source files and embed them in the context output
- ✅ **v0.6.0** — `slurp index .`: standalone static indexer (Python ast, TypeScript/JS, Go) — graphify is now optional

---

## License

MIT © [Juan Carlos Vallejo Ruiz](https://github.com/CarlosVallejoRuiz)
