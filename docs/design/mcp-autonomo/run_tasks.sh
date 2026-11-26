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
# Fixed for all ten tasks, agreed before launching (option a): edits accepted and
# a fixed list of shell commands; no rule for slurp — its approval is the point,
# and must come from the .claude/settings.local.json `slurp init` writes. The
# criterion's "0 approvals" counts denials of mcp__slurp__* tools only; any other
# denial is reported apart.
ALLOWED="Bash(git status:*),Bash(git diff:*),Bash(git log:*),Bash(ls:*),Bash(cat:*),Bash(grep:*),Bash(find:*),Bash(uv run pytest:*),Bash(uv run ruff:*),Bash(npx tsc:*),Bash(npm test:*)"
TIMEOUT_S="${TIMEOUT_S:-1800}"
# PREPARE_ONLY=1: build and initialise the clones, run the checks, stop before claude
# (the clone for the author's interactive check of the approvals).
PREPARE_ONLY="${PREPARE_ONLY:-0}"
CONTAIN="python3 $REPO/docs/design/mcp-autonomo/contain_settings.py"

WORK="${1:?WORK_DIR}"; shift
case "$(cd "$(dirname "$WORK")" 2>/dev/null && pwd)/" in
  "$REPO"/*|"$PRISMASTATS"/*) echo "WORK_DIR must be outside the real repositories" >&2; exit 2 ;;
esac
case "$ALLOWED" in *mcp__slurp*) echo "the fixed list must not allow slurp's tools" >&2; exit 2 ;; esac
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
    # Agreed with the author after the aborted first run: Claude Code ignores a
    # .claude/settings.local.json git tracks (PrismaStats versions it), so do what
    # `slurp init` advises before it runs; and point every path of the real
    # checkout in the project's settings into the clone. Both are committed in the
    # clone, so they stay out of the agent's diff.
    real="$PRISMASTATS"; [ "$project" = slurp ] && real="$REPO"
    if git ls-files --error-unmatch .claude/settings.local.json > /dev/null 2>&1; then
      git rm -q --cached .claude/settings.local.json
    fi
    $CONTAIN rewrite "$dir/repo" "$real" > "$dir/contain.txt"
    # Only the shared file: re-adding the whole .claude/ would track settings.local.json again.
    git add .claude/settings.json 2>/dev/null || true
    git -c user.name=measurement -c user.email=measurement@localhost commit -q \
      -m "measurement: settings.local.json out of git, settings paths inside the clone" \
      > /dev/null 2>&1 || true
    PATH="$SLURP_BIN:$PATH" slurp init --yes > "$dir/init.txt" 2>&1
    mkdir -p .slurp
    # Before every session: no permission rule or additional directory may point
    # outside the clone. A failure stops the run.
    if ! $CONTAIN check "$dir/repo" 2>> "$dir/contain.txt"; then
      echo "[$id] a permission points outside the clone — see $dir/contain.txt" >&2
      exit 3
    fi
    echo "settings: nothing points outside the clone" >> "$dir/contain.txt"
    if [ "$PREPARE_ONLY" = 1 ]; then
      echo "[$id] prepared: $dir/repo"
      exit 0
    fi
    # Is the project server approved before the session starts? (no model call)
    "$CLAUDE_BIN" mcp get slurp > "$dir/mcp-get.txt" 2>&1 || true
    echo "[$id] claude -p …  $(date '+%H:%M:%S')"
    start=$(date +%s)
    PATH="$SLURP_BIN:$PATH" timeout "$TIMEOUT_S" "$CLAUDE_BIN" -p "$prompt" \
      --output-format stream-json --verbose --permission-mode acceptEdits \
      --allowedTools "$ALLOWED" < /dev/null > "$dir/stream.jsonl" 2> "$dir/claude.stderr" || true
    echo $(( $(date +%s) - start )) > "$dir/seconds.txt"
    # The final result event (subtype, turns, cost, permission_denials) as claude.json.
    python3 -c "
import json, sys
last = {}
for line in open(sys.argv[1], encoding='utf-8', errors='replace'):
    try:
        event = json.loads(line)
    except ValueError:
        continue
    if isinstance(event, dict) and event.get('type') == 'result':
        last = event
json.dump(last, open(sys.argv[2], 'w'), indent=2)
" "$dir/stream.jsonl" "$dir/claude.json"
    cp .slurp/session.log "$dir/session.log" 2>/dev/null || : > "$dir/session.log"
    git add -A >/dev/null 2>&1 || true
    git diff --cached --stat -- . ':!.slurp' ':!graphify-out' ':!.mcp.json' ':!CLAUDE.local.md' \
      ':!.gitignore' ':!.claude' > "$dir/diff-stat.txt" || true
    git diff --cached -- . ':!.slurp' ':!graphify-out' ':!.mcp.json' ':!CLAUDE.local.md' \
      ':!.gitignore' ':!.claude' > "$dir/diff.patch" || true
    git diff --cached --name-only -- . ':!.slurp' ':!graphify-out' ':!.mcp.json' \
      ':!CLAUDE.local.md' ':!.gitignore' ':!.claude' > "$dir/changed-files.txt" || true
    case "$project" in
      slurp)
        (uv run pytest -q -p no:cacheprovider > "$dir/check-tests.txt" 2>&1; echo $? > "$dir/check-tests.rc") || true
        (uv run ruff check . > "$dir/check-lint.txt" 2>&1; echo $? > "$dir/check-lint.rc") || true ;;
      prismastats)
        (npx tsc --noEmit > "$dir/check-tests.txt" 2>&1; echo $? > "$dir/check-tests.rc") || true ;;
    esac
    mkdir -p "$OUT/$id"
    PATH="$SLURP_BIN:$PATH" slurp session --sessions --log-dir "$dir/repo/.slurp" > "$OUT/$id/sessions.txt" 2>&1 || true
    python3 - "$dir" "$OUT/$id/resultado.json" "$id" "$project" \
        "$REPO/docs/design/mcp-autonomo/analyze_sessions.py" <<'PY'
import collections, json, runpy, sys
from pathlib import Path
d, out, task, project = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
runs_graphify = runpy.run_path(sys.argv[5])["_RUNS_GRAPHIFY"]  # the analyzer's own rule
init, tools, graphify = {}, collections.Counter(), []
for line in (d / "stream.jsonl").read_text(encoding="utf-8", errors="replace").splitlines() \
        if (d / "stream.jsonl").exists() else []:
    try:
        event = json.loads(line)
    except ValueError:
        continue
    if event.get("type") == "system" and event.get("subtype") == "init":
        init = event
    for block in (event.get("message") or {}).get("content") or [] \
            if event.get("type") == "assistant" else []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name, args = block.get("name"), block.get("input") or {}
            tools[name] += 1
            if name == "Skill" and "graphify" in str(args.get("skill", "")).lower():
                graphify.append(f"Skill {args.get('skill')}")
            elif name == "Bash" and runs_graphify.search(str(args.get("command", ""))):
                graphify.append(f"Bash: {str(args.get('command'))[:60]}")
def read(name, default=""):
    p = d / name
    return p.read_text(encoding="utf-8", errors="replace").strip() if p.exists() else default
try:
    claude = json.loads(read("claude.json", "{}") or "{}")
except ValueError:
    claude = {}
denials = [x.get("tool_name") if isinstance(x, dict) else x
           for x in claude.get("permission_denials") or []]
servers = {s.get("name"): s.get("status") for s in init.get("mcp_servers") or []}
result = {
    "task": task, "project": project,
    "model": init.get("model"),
    "slurp_server": servers.get("slurp"),
    "claude": {k: claude.get(k) for k in ("subtype", "is_error", "num_turns", "duration_ms",
                                          "total_cost_usd")},
    "slurp_denials": [x for x in denials if str(x).startswith("mcp__slurp__")],
    "other_denials": [x for x in denials if not str(x).startswith("mcp__slurp__")],
    "agent_slurp_calls": {k: v for k, v in tools.items() if str(k).startswith("mcp__slurp__")},
    "agent_tools": dict(tools.most_common()),
    "graphify": graphify,
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
