#!/usr/bin/env bash
# The agent measurement of docs/design/mcp-autonomo.md §7 — condition B.
#
# Usage:  docs/design/mcp-autonomo/run_tasks.sh WORK_DIR [TASK_ID...]
#
# WORK_DIR must lie outside the real repositories (e.g. /tmp/slurp-medicion). For
# each task: a fresh clone of the project in WORK_DIR/<id>/repo, with its remote
# removed (nothing an agent does can be pushed, and nothing is merged back);
# `slurp init --yes` with this checkout's slurp; `.slurp/` created so the session
# log is written; one new `claude -p` session with the task's prompt — which never
# mentions slurp, the graph or MCP — then the project's checks.
#
# Raw artifacts stay in WORK_DIR/<id>/ (claude.json, session.log, diff, checks).
# Into docs/design/mcp-autonomo/resultados/<id>/ go only `slurp session --sessions`
# and resultado.json (how the session ended, the diff stat, the checks): the
# agents' answers and diffs of PrismaStats never enter this repository.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
SLURP_BIN="$REPO/.venv/bin"
PRISMASTATS="${SLURP_PRISMASTATS:-$HOME/Desktop/prismastats}"
TASKS="$REPO/docs/design/mcp-autonomo/tareas.json"
OUT="$REPO/docs/design/mcp-autonomo/resultados"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || ls -d "$HOME"/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude 2>/dev/null | tail -1)}"
# Fixed for all ten tasks; must not allow mcp__slurp tools (their approval is the
# point: it comes from the .claude/settings.local.json `slurp init` writes).
CLAUDE_FLAGS="${CLAUDE_FLAGS:-}"
TIMEOUT_S="${TIMEOUT_S:-1800}"

WORK="${1:?WORK_DIR}"; shift
case "$(cd "$(dirname "$WORK")" 2>/dev/null && pwd)/" in
  "$REPO"/*|"$PRISMASTATS"/*) echo "WORK_DIR must be outside the real repositories" >&2; exit 2 ;;
esac
case "$CLAUDE_FLAGS" in *mcp__slurp*) echo "CLAUDE_FLAGS must not allow slurp's tools" >&2; exit 2 ;; esac
[ -x "$CLAUDE_BIN" ] || { echo "claude not found (set CLAUDE_BIN)" >&2; exit 2; }
mkdir -p "$WORK" "$OUT"
ids=("$@")
if [ ${#ids[@]} -eq 0 ]; then
  while IFS= read -r line; do ids+=("$line"); done < <(python3 -c "import json,sys; [print(t['id']) for t in json.load(open(sys.argv[1]))['tasks']]" "$TASKS")
fi

for id in "${ids[@]}"; do
  project="$(python3 -c "import json,sys; print(next(t['project'] for t in json.load(open(sys.argv[1]))['tasks'] if t['id']==sys.argv[2]))" "$TASKS" "$id")"
  prompt="$(python3 -c "import json,sys; print(next(t['prompt'] for t in json.load(open(sys.argv[1]))['tasks'] if t['id']==sys.argv[2]))" "$TASKS" "$id")"
  dir="$WORK/$id"
  rm -rf "$dir" && mkdir -p "$dir"
  case "$project" in
    prismastats)
      git clone -q --no-hardlinks "$PRISMASTATS" "$dir/repo"
      # tsc needs the dependencies: a copy-on-write clone (APFS), never a symlink —
      # an `npm install` through a symlink would write into the real project.
      cp -cR "$PRISMASTATS/node_modules" "$dir/repo/node_modules" ;;
    slurp)
      git clone -q --no-hardlinks "$REPO" "$dir/repo"
      (cd "$dir/repo" && uv sync -q --frozen --all-extras) ;;
  esac
  git -C "$dir/repo" remote remove origin
  (
    cd "$dir/repo"
    PATH="$SLURP_BIN:$PATH" slurp init --yes > "$dir/init.txt" 2>&1
    mkdir -p .slurp
    echo "[$id] claude -p …"
    start=$(date +%s)
    # shellcheck disable=SC2086
    PATH="$SLURP_BIN:$PATH" timeout "$TIMEOUT_S" "$CLAUDE_BIN" -p "$prompt" --output-format json \
      $CLAUDE_FLAGS > "$dir/claude.json" 2> "$dir/claude.stderr" || true
    echo $(( $(date +%s) - start )) > "$dir/seconds.txt"
    cp .slurp/session.log "$dir/session.log" 2>/dev/null || : > "$dir/session.log"
    git add -A >/dev/null 2>&1 || true
    git diff --cached --stat -- . ':!.slurp' ':!graphify-out' ':!.mcp.json' ':!CLAUDE.local.md' \
      ':!.gitignore' ':!.claude' > "$dir/diff-stat.txt" || true
    git diff --cached -- . ':!.slurp' ':!graphify-out' ':!.mcp.json' ':!CLAUDE.local.md' \
      ':!.gitignore' ':!.claude' > "$dir/diff.patch" || true
    case "$project" in
      slurp)
        (uv run pytest -q -p no:cacheprovider > "$dir/check-tests.txt" 2>&1; echo $? > "$dir/check-tests.rc") || true
        (uv run ruff check . > "$dir/check-lint.txt" 2>&1; echo $? > "$dir/check-lint.rc") || true ;;
      prismastats)
        (npx tsc --noEmit > "$dir/check-tests.txt" 2>&1; echo $? > "$dir/check-tests.rc") || true ;;
    esac
    mkdir -p "$OUT/$id"
    PATH="$SLURP_BIN:$PATH" slurp session --sessions --log-dir "$dir/repo/.slurp" > "$OUT/$id/sessions.txt" 2>&1 || true
    python3 - "$dir" "$OUT/$id/resultado.json" "$id" "$project" <<'PY'
import json, sys
from pathlib import Path
d, out, task, project = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
def read(name, default=""):
    p = d / name
    return p.read_text(encoding="utf-8", errors="replace").strip() if p.exists() else default
try:
    claude = json.loads(read("claude.json", "{}") or "{}")
except ValueError:
    claude = {}
denials = claude.get("permission_denials") or []
result = {
    "task": task, "project": project,
    "claude": {k: claude.get(k) for k in ("subtype", "is_error", "num_turns", "duration_ms",
                                          "total_cost_usd")},
    "permission_denials": [x.get("tool_name") if isinstance(x, dict) else x for x in denials],
    "seconds": int(read("seconds.txt", "0") or 0),
    "diff_stat": read("diff-stat.txt").splitlines()[-1:] or [""],
    "checks": {name: read(f"check-{name}.rc") for name in ("tests", "lint")
               if (d / f"check-{name}.rc").exists()},
    "finished": None,   # filled in by hand after reading the diff and the checks
}
out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY
  )
done

echo "Done. Analyse with: uv run python $REPO/docs/design/mcp-autonomo/analyze_sessions.py $WORK"
