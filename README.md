# slurp

![tests](https://img.shields.io/badge/tests-243%20passed-brightgreen)
![python](https://img.shields.io/badge/python-3.12%2B-blue)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

> *graphify builds the bowl. slurp serves exactly the noodles your LLM needs.*

Knowledge graphs can have thousands of nodes. Injecting the whole graph into an LLM context is wasteful and expensive. Slurp selects the most relevant subgraph that fits within a token budget — and tells you exactly what it picked and why.

---

## Install

```bash
# with uv (recommended)
uv add slurp-graph

# with pip
pip install slurp-graph
```

> The PyPI package is `slurp-graph`. The CLI command is `slurp`.

---

## Quickstart

```bash
slurp "auth flow" --graph graph.json --budget 4000
```

```
╭─ Slurp — Subgraph for: "auth flow" (budget: 4,000 tokens) ──────────────╮
│ Selected 5/23 nodes · 847/4,000 tokens used (21.7%)                      │
╰───────────────────────────────────────────────────────────────────────────╯

## Relevant Nodes

### authenticate_user (function) · score: 0.94
Validates user credentials and returns JWT token.
→ File: src/auth/service.py

### JWTMiddleware (class) · score: 0.87
Intercepts HTTP requests and validates Authorization header.
→ File: src/middleware/jwt.py

### hash_password (function) · score: 0.71
Hashes password using bcrypt.
→ File: src/auth/utils.py

### UserModel (class) · score: 0.58
Database model for user accounts.
→ File: src/models/user.py

### DatabasePool (class) · score: 0.41
Manages PostgreSQL connection pool.
→ File: src/db/pool.py

## Key Relationships
- authenticate_user → calls → hash_password
- authenticate_user → depends_on → UserModel
- JWTMiddleware → calls → authenticate_user
- UserModel → depends_on → DatabasePool

---
💡 18 additional connected nodes available — increase --budget to include them
```

Pipe the output directly into your LLM prompt, save it to a file, or use it as a system prompt section. It's just text.

---

## Usage

### `slurp QUERY`

The main command. Scores every node in the graph against your query, selects the highest-scoring nodes that fit within the token budget, and prints the result.

```bash
slurp "payment processing"
slurp "auth flow" --graph graph.json --budget 4000
slurp "database pool" --format json --no-audit
slurp "JWT validation" --explain
```

| Flag | Short | Default | Description |
|---|---|---|---|
| `--graph` | `-g` | auto-discover | Path to `graph.json`. Searched in `./`, `graphify-out/`, `.graphify/` if omitted. |
| `--budget` | `-b` | `4000` | Token budget for subgraph selection. |
| `--format` | `-f` | `markdown` | Output format: `markdown`, `json`, or `yaml`. |
| `--model` | `-m` | `cl100k_base` | Tiktoken encoding for token counting. |
| `--explain` | | off | Show relevance score for each selected node. |
| `--no-audit` | | off | Skip writing this query to `.slurp/audit.jsonl`. |
| `--neighbor-decay` | | `0.7` | Score multiplier applied to a node's neighbors after it is selected. Controls how aggressively the algorithm expands context around high-scoring nodes. |

**Auto-discovery order** (when `--graph` is omitted):

1. `./graph.json`
2. `./graphify-out/graph.json`
3. `./.graphify/graph.json`

### `slurp stats`

Print node and edge counts for a graph file.

```bash
slurp stats --graph graph.json
```

```
Graph: graph.json
Nodes: 23
Edges: 41
```

### `slurp audit`

Show the history of queries logged to `.slurp/audit.jsonl`, with the most frequently selected nodes.

```bash
slurp audit
slurp audit --top-nodes 20
slurp audit --audit-dir /path/to/.slurp
```

```
Total queries: 12

Top 10 nodes:
  authenticate_user: 9
  JWTMiddleware: 7
  UserModel: 6
  hash_password: 5
  DatabasePool: 3
  ...
```

Every query is appended to `.slurp/audit.jsonl` (one JSON object per line) unless `--no-audit` is passed. Use `slurp audit` to review which parts of your codebase your AI agent visits most.

---

## Works with graphify

Slurp is designed to consume the `graph.json` produced by [graphify](https://github.com/juank/graphify). It also accepts any graph that follows the same structure.

**Expected format:**

```json
{
  "nodes": [
    {
      "id": "authenticate_user",
      "label": "authenticate_user",
      "type": "function",
      "description": "Validates user credentials and returns JWT token.",
      "importance": 9,
      "file_path": "src/auth/service.py"
    }
  ],
  "edges": [
    {
      "source": "authenticate_user",
      "target": "hash_password",
      "relation": "calls",
      "weight": 3
    }
  ]
}
```

The `type`, `description`, `importance`, and `file_path` fields are optional but improve scoring quality. Any graph with at minimum `id` + `label` on nodes and `source` + `target` on edges will work.

---

## How it works

### Scoring

Each node gets a score from two signals combined:

- **Structural (40%)** — PageRank of the node in the graph, normalized 0–1. Captures topological importance: nodes that many other nodes depend on score higher.
- **Semantic (60%)** — TF-IDF cosine similarity between the query and the node's `label + description`. Captures relevance to what you actually asked.

Final score: `0.4 × pagerank_normalized + 0.6 × tfidf_similarity`

### Selection

Nodes are selected greedily in descending score order. Each time a node is selected, its direct neighbors receive a score boost of `score × neighbor_decay` (default `0.7`). This keeps topologically connected clusters together without blowing the budget on unrelated nodes.

A node is added to the subgraph only if its serialized token count fits within the remaining budget. If a node is too large to fit, it is skipped and the next candidate is tried.

### Token counting

Token costs are measured with [tiktoken](https://github.com/openai/tiktoken) using the `cl100k_base` encoding. What you see in `tokens_used` is what actually arrives in your LLM's context window.

---

## Design decisions

**Power-iteration PageRank without numpy.**
NetworkX's `nx.pagerank()` requires numpy. Slurp implements a 20-line pure-Python power-iteration algorithm that produces identical results with no extra dependencies. The convergence criterion is `sum(|rank_new - rank_old|) < N × tol`.

**TF-IDF without scikit-learn.**
The scorer uses a hand-rolled TF-IDF with smoothed IDF (`log((N+1)/(df+1)) + 1`) and cosine similarity. No sklearn, no scipy, no model downloads. In v2, this can be swapped for embeddings behind the same `score_nodes()` interface without touching callers.

**YAML serializer without PyYAML.**
The `yaml` output format is generated by a custom `_yaml_scalar()` function that renders primitive Python values as valid YAML scalars, using `json.dumps()` for strings that need quoting (JSON string literals are valid YAML). No PyYAML dependency.

**`lru_cache` on the tiktoken encoder.**
`tiktoken.get_encoding()` reads tokenizer data from disk on first call. Slurp wraps it with `functools.lru_cache(maxsize=8)` so repeated token-counting calls within a single run hit memory, not disk.

---

## Roadmap

### v0.1.0 — Core MVP ✓
- `loader`, `scorer`, `budget`, `formatter`, `audit` modules with full test coverage
- CLI: `slurp QUERY`, `slurp stats`, `slurp audit`
- Append-only audit log in `.slurp/audit.jsonl`
- Output formats: markdown, JSON, YAML

### v0.2.0 — Usability
- `--explain` output with per-signal score breakdown
- `.slurpignore` to exclude nodes by type or path pattern
- Rich terminal output with tables and panels
- Improved auto-discovery

### v0.3.0 — Integrations
- `slurp serve` — MCP server mode
- `slurp diff old.json new.json` — impact analysis for graph changes
- Export to CLAUDE.md / system prompt format
- Publish to PyPI as `slurp-graph`

### v0.4.0 — Advanced semantics
- Optional embedding backend (`--backend embeddings`) via Anthropic or OpenAI
- `slurp benchmark` — measures token savings vs. full-graph injection
- GraphML and neo4j export format support

---

## License

MIT © [Juan Carlos Vallejo Ruiz](https://github.com/juank)
