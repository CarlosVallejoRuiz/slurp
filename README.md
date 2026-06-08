<p align="center">
  <img src="assets/logo.png" alt="slurp logo" width="120">
</p>

# slurp

![tests](https://img.shields.io/badge/tests-794%20passed-brightgreen)
![python](https://img.shields.io/badge/python-3.12%2B-blue)
![license](https://img.shields.io/badge/license-MIT-lightgrey)
![pypi](https://img.shields.io/badge/PyPI-slurp--graph-orange)

> *graphify builds the bowl. slurp serves exactly the noodles your LLM needs.*

A knowledge graph is a bowl of ramen — thousands of nodes tangled together. Your LLM doesn't need the whole bowl. Slurp scores every node against your query, then greedily selects the highest-relevance subgraph that fits within your token budget — and tells you exactly what it picked and why.

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

## Install

```bash
pip install slurp-graph

# or with uv
uv add slurp-graph
```

> PyPI package: `slurp-graph` — CLI command: `slurp`

📖 **Full usage guide (automatic MCP mode + manual CLI):** [USAGE.md](USAGE.md)

---

## Quickstart

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

Compare two graph versions and report the impact of changes.

```bash
slurp diff old.json new.json
slurp diff old.json new.json --hops 2 --viz
slurp diff old.json new.json --budget 4000
```

Reports added/removed/modified nodes and edges, computes an impact score based on centrality, and optionally opens a diff-colored visualization (green=added, red=removed, yellow=modified, grey=unchanged). Pass `--budget` to further select the most relevant affected nodes.

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
```

See [MCP Integration](#mcp-integration) for configuration.

---

### `slurp benchmark`

Measure real token savings across queries and budgets.

```bash
slurp benchmark \
  --graph graph.json \
  --queries "auth flow" --queries "schema validation" \
  --budget 2000 --budget 4000 --budget 8000
```

Outputs a per-run table and aggregate stats: mean savings, p50/p90/p95, best/worst case, and precision (fraction of relevant nodes captured).

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

**`.mcp.json`:**

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

---

## License

MIT © [Juan Carlos Vallejo Ruiz](https://github.com/CarlosVallejoRuiz)
