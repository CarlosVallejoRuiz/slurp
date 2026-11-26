#!/usr/bin/env bash
# The agent measurement of docs/design/mcp-autonomo.md §7 — condition B. Not run yet:
# the author reviews the phase, confirms the tasks in tareas.json, and launches it.
#
# Usage:  docs/design/mcp-autonomo/run_tasks.sh RESULTS_DIR [TASK_ID...]
#
# For each task: a fresh clone of the project (PrismaStats or slurp) in
# RESULTS_DIR/<id>/repo, `slurp init --yes` with this checkout's slurp, `.slurp/`
# created so the session log is written, then one new `claude -p` session with
# the task's prompt — which never mentions slurp, the graph or MCP. Afterwards
# RESULTS_DIR/<id>/ holds claude.json and session.log for analyze_sessions.py.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
SLURP_BIN="$REPO/.venv/bin"
PRISMASTATS="${SLURP_PRISMASTATS:-$HOME/Desktop/prismastats}"
TASKS="$REPO/docs/design/mcp-autonomo/tareas.json"
# Anything else to pass to claude -p (model, max turns…), fixed for all ten tasks.
CLAUDE_FLAGS="${CLAUDE_FLAGS:-}"
TIMEOUT_S="${TIMEOUT_S:-1800}"

RESULTS="${1:?RESULTS_DIR}"; shift
mkdir -p "$RESULTS"
ids=("$@")
if [ ${#ids[@]} -eq 0 ]; then
  mapfile -t ids < <(python3 -c "import json,sys; [print(t['id']) for t in json.load(open(sys.argv[1]))['tasks']]" "$TASKS")
fi

for id in "${ids[@]}"; do
  read -r project prompt < <(python3 - "$TASKS" "$id" <<'PY'
import json, sys
task = next(t for t in json.load(open(sys.argv[1]))["tasks"] if t["id"] == sys.argv[2])
print(task["project"], task["prompt"].replace("\n", " "))
PY
)
  dir="$RESULTS/$id"
  rm -rf "$dir" && mkdir -p "$dir"
  case "$project" in
    prismastats)
      git clone -q --local "$PRISMASTATS" "$dir/repo"
      # tsc and the tests need the dependencies; they are read, never written.
      ln -s "$PRISMASTATS/node_modules" "$dir/repo/node_modules" ;;
    slurp)
      git clone -q --local "$REPO" "$dir/repo"
      (cd "$dir/repo" && uv sync -q --frozen --all-extras) ;;
  esac
  (
    cd "$dir/repo"
    PATH="$SLURP_BIN:$PATH" slurp init --yes > "$dir/init.txt" 2>&1
    mkdir -p .slurp
    echo "[$id] claude -p …"
    # shellcheck disable=SC2086
    PATH="$SLURP_BIN:$PATH" timeout "$TIMEOUT_S" claude -p "$prompt" --output-format json \
      $CLAUDE_FLAGS > "$dir/claude.json" 2> "$dir/claude.stderr" || true
    cp .slurp/session.log "$dir/session.log" 2>/dev/null || : > "$dir/session.log"
    git diff --stat > "$dir/diff.txt" || true
  )
done

echo "Done. Analyse with: uv run python $REPO/docs/design/mcp-autonomo/analyze_sessions.py $RESULTS"
