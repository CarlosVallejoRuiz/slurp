"""What `slurp init` writes for coding agents (v1.3, docs/design/mcp-autonomo.md §5.2, §5.4).

Claude Code needs two approvals that `.mcp.json` alone does not give: the
project server's (``enabledMcpjsonServers``) and each tool's
(``permissions.allow``). Both go to ``.claude/settings.local.json`` — local,
never versioned, merged without ever overwriting what is there. A short
paragraph tells the agent when to reach for slurp, by default in
``CLAUDE.local.md``. Other clients get a fragment printed, not written: they
have not been tested end to end yet.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

SERVER = "slurp"
ALLOW_RULE = "mcp__slurp__*"
# Rules that already approve every slurp tool (the server-wide forms).
_COVERING_RULES = frozenset({"mcp__slurp", "mcp__slurp__*"})

BLOCK_BEGIN = "<!-- slurp:begin — managed by `slurp init`; re-running it replaces this block -->"
BLOCK_END = "<!-- slurp:end -->"

_PARAGRAPH = (
    "{first} Before searching or reading files to find where something lives, call "
    "`slurp_query` with a plain-language description of the behaviour. Before editing a "
    "file, call `slurp_impact` with its path; before changing a function or class, call "
    "`slurp_explain` with its name. Use grep for exact strings, and open the files slurp "
    "points to before editing them."
)
_FIRST_SENTENCE = {
    "own": "This project has a slurp MCP server: a code graph that stays in sync with the "
           "working tree.",
    "graphify": "This project has a slurp MCP server over a graph built by graphify "
                "(rebuild it after large changes; slurp does not refresh it).",
}

_GIT_TIMEOUT_S = 10


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                              timeout=_GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def git_root(cwd: Path) -> Path | None:
    """The work tree *cwd* belongs to, or None outside git."""
    proc = _git(cwd, "rev-parse", "--show-toplevel")
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip())


def git_ignored(path: Path) -> bool | None:
    """Whether git ignores *path* (None outside git)."""
    proc = _git(path.parent if path.parent.exists() else Path.cwd(), "check-ignore", "-q",
                str(path))
    if proc is None or proc.returncode not in (0, 1):
        return None
    return proc.returncode == 0


def git_tracked(path: Path) -> bool:
    """Whether *path* is already in the index — ignoring it then changes nothing."""
    if not path.exists():
        return False
    proc = _git(path.parent, "ls-files", "--error-unmatch", path.name)
    return proc is not None and proc.returncode == 0


def git_tracks_any(directory: Path) -> bool:
    """Whether git tracks at least one file under *directory* (it is versioned)."""
    if not directory.is_dir():
        return False
    proc = _git(directory, "ls-files", "--", ".")
    return proc is not None and proc.returncode == 0 and bool(proc.stdout.strip())


# ---------------------------------------------------------------------------
# .claude/settings.local.json
# ---------------------------------------------------------------------------

@dataclass
class SettingsResult:
    """What merging slurp's approvals into a settings file did."""

    path: Path
    written: bool = False
    invalid: bool = False
    warnings: list[str] = field(default_factory=list)
    fragment: str = ""


def settings_fragment() -> str:
    return json.dumps({"enabledMcpjsonServers": [SERVER],
                       "permissions": {"allow": [ALLOW_RULE]}}, indent=2)


def merge_claude_settings(path: Path) -> SettingsResult:
    """Approve the slurp server and its tools in *path*, merging, never overwriting.

    DECISION (design §5.2): this is what Claude Code itself writes when the
    user approves the project server and answers "don't ask again" for a tool.
    An invalid file is left untouched and the fragment is handed back to be
    added by hand. A rejection already there wins: a server listed in
    disabledMcpjsonServers is not enabled, and deny/ask rules about slurp are
    kept (Claude Code gives them precedence over allow) and reported.

    Returns:
        A SettingsResult; `written` is False when nothing needed adding.
    """
    result = SettingsResult(path=path, fragment=settings_fragment())
    data: object = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            result.invalid = True
            result.warnings.append(f"{path} is not a valid JSON object — left untouched; add "
                                   "this by hand:")
            return result
    assert isinstance(data, dict)
    changed = False

    disabled = data.get("disabledMcpjsonServers") or []
    enabled = list(data.get("enabledMcpjsonServers") or [])
    if isinstance(disabled, list) and SERVER in disabled:
        result.warnings.append(f"{path}: \"{SERVER}\" is in disabledMcpjsonServers — kept; "
                               "Claude Code will not start the server until you remove it.")
    elif SERVER not in enabled:
        data["enabledMcpjsonServers"] = [*enabled, SERVER]
        changed = True

    permissions = data.get("permissions")
    permissions = dict(permissions) if isinstance(permissions, dict) else {}
    for kind in ("deny", "ask"):
        rules = [r for r in permissions.get(kind) or [] if isinstance(r, str)
                 and r.startswith("mcp__slurp")]
        if rules:
            result.warnings.append(f"{path}: kept your {kind} rule(s) {', '.join(rules)} — "
                                   f"they take precedence over {ALLOW_RULE}.")
    allow = list(permissions.get("allow") or [])
    if not _COVERING_RULES & set(allow):
        permissions["allow"] = [*allow, ALLOW_RULE]
        data["permissions"] = permissions
        changed = True

    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        result.written = True
    return result


# ---------------------------------------------------------------------------
# The paragraph for the agent
# ---------------------------------------------------------------------------

def instruction_block(variant: str) -> str:
    """The managed block, markers included; *variant* is "own" or "graphify".

    The markers are HTML comments, which Claude Code strips before loading the
    file: they cost no tokens.
    """
    first = _FIRST_SENTENCE["graphify" if variant == "graphify" else "own"]
    return (f"{BLOCK_BEGIN}\n## Code navigation (slurp)\n\n"
            f"{_PARAGRAPH.format(first=first)}\n{BLOCK_END}\n")


@dataclass
class InstructionPlan:
    """Where the paragraph goes, decided before anything is written."""

    mode: str
    variant: str
    #: Files to append the block to (created if missing).
    append: list[Path] = field(default_factory=list)
    #: Files whose existing block is replaced.
    replace: list[Path] = field(default_factory=list)
    #: Start a new CLAUDE.local.md with "@AGENTS.md" so Claude Code keeps reading it.
    import_agents: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def targets(self) -> list[Path]:
        return sorted({*self.append, *self.replace})


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _claude_md_above(root: Path) -> bool:
    """A CLAUDE.md or .claude/CLAUDE.md in *root* or a directory above it.

    The walk stops below the home directory: ~/.claude/CLAUDE.md is the user's
    own memory, loaded everywhere, not part of this project's hierarchy.
    """
    home = Path.home().resolve()
    here = root.resolve()
    while True:
        if here == home:
            return False
        if (here / "CLAUDE.md").is_file() or (here / ".claude" / "CLAUDE.md").is_file():
            return True
        if here.parent == here:
            return False
        here = here.parent


def _nested_agents_md(root: Path) -> list[Path]:
    from slurp.indexer import _is_skipped_dir

    found: list[Path] = []
    stack = [root]
    while stack:
        here = stack.pop()
        try:
            entries = list(here.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir() and not entry.is_symlink() and not entry.name.startswith(".") \
                    and not _is_skipped_dir(entry.name, entry):
                stack.append(entry)
            elif entry.name == "AGENTS.md" and here != root:
                found.append(entry)
    return sorted(found)


def plan_instructions(root: Path, mode: str, variant: str) -> InstructionPlan:
    """Decide where the paragraph goes (mode "local", "versioned" or "none").

    DECISION (design §5.4): never twice. Every file Claude would load is read
    first; if one holds the markers, the block there is replaced and nothing
    else is written; if one already mentions slurp_query or mcp__slurp without
    markers, nothing is written and the user is told. Locally the block goes to
    CLAUDE.local.md, which Claude Code still reads — and which, under the
    default "Project instructions" setting, makes it stop reading a lone
    AGENTS.md, hence the "@AGENTS.md" import on a new file.
    """
    plan = InstructionPlan(mode=mode, variant=variant)
    if mode == "none":
        return plan
    candidates = [root / "CLAUDE.md", root / ".claude" / "CLAUDE.md", root / "CLAUDE.local.md",
                  root / "AGENTS.md"]
    existing = [p for p in candidates if p.is_file()]
    marked = [p for p in existing if BLOCK_BEGIN.split(" —")[0] in _read(p)]
    if marked:
        plan.replace = marked
        return plan
    mentioning = [p for p in existing if "slurp_query" in _read(p) or "mcp__slurp" in _read(p)]
    if mentioning:
        plan.warnings.append(
            f"{', '.join(p.name for p in mentioning)} already mention slurp_query or "
            "mcp__slurp — no paragraph written, to avoid saying it twice.")
        return plan

    agents = root / "AGENTS.md"
    claude = root / "CLAUDE.md"
    if mode == "local":
        local = root / "CLAUDE.local.md"
        plan.append = [local]
        if agents.is_file() and not local.exists() and not _claude_md_above(root):
            plan.import_agents = True
            nested = _nested_agents_md(root)
            if nested:
                plan.warnings.append(
                    f"{len(nested)} AGENTS.md file(s) below {root.name}/ (e.g. "
                    f"{nested[0].relative_to(root)}): with CLAUDE.local.md present Claude Code "
                    "stops loading them on demand. To keep them, set \"Project instructions\" "
                    "to claude-md-and-agents-md in your user settings (slurp does not write "
                    "it).")
        return plan

    # versioned (design §5.4 table)
    claude_text = _read(claude) if claude.is_file() else ""
    if claude.is_file() and agents.is_file():
        imports = any(line.strip() == "@AGENTS.md" for line in claude_text.splitlines())
        plan.append = [agents] if imports else [agents, claude]
    elif agents.is_file():
        plan.append = [agents]
    else:
        plan.append = [claude]
    return plan


def write_instructions(plan: InstructionPlan) -> list[Path]:
    """Carry out *plan*; returns the files written."""
    block = instruction_block(plan.variant)
    written: list[Path] = []
    for path in plan.replace:
        text = _read(path)
        start = text.index(BLOCK_BEGIN.split(" —")[0])
        end_at = text.find(BLOCK_END, start)
        end = len(text) if end_at < 0 else end_at + len(BLOCK_END)
        if text[end:end + 1] == "\n":
            end += 1
        path.write_text(text[:start] + block + text[end:], encoding="utf-8")
        written.append(path)
    for path in plan.append:
        if path.exists():
            text = _read(path)
            # One blank line between what was there and the block.
            if not text or text.endswith("\n\n"):
                separator = ""
            else:
                separator = "\n" if text.endswith("\n") else "\n\n"
            path.write_text(text + separator + block, encoding="utf-8")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            head = "@AGENTS.md\n\n" if plan.import_agents else ""
            path.write_text(head + block, encoding="utf-8")
        written.append(path)
    return written


# ---------------------------------------------------------------------------
# .gitignore
# ---------------------------------------------------------------------------

def _mentions(line: str, entry: str) -> bool:
    """Whether a .gitignore line already decides what happens to *entry*.

    A negation (`!entry`) counts too: it is the user deliberately choosing to
    track it, and appending the entry after it would override them.
    """
    pattern = line.strip().lstrip("!").removeprefix("**/").removeprefix("/")
    wanted = entry.rstrip("/")
    return pattern.rstrip("*").rstrip("/") == wanted


def ensure_ignored(gitignore: Path, entry: str) -> bool:
    """Append *entry* to an existing .gitignore that does not decide it yet.

    A missing .gitignore is left missing: creating one is a project decision,
    not slurp's.

    Returns:
        True if the entry was appended.
    """
    if not gitignore.is_file():
        return False
    text = gitignore.read_text(encoding="utf-8")
    if any(_mentions(line, entry) for line in text.splitlines()):
        return False
    separator = "" if not text or text.endswith("\n") else "\n"
    gitignore.write_text(f"{text}{separator}{entry}\n", encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Other clients — printed, not written (design §5.2, phase 1)
# ---------------------------------------------------------------------------

def client_fragments(command: str, graph_path: Path) -> dict[str, str]:
    """The configuration each other client needs, to paste by hand."""
    args = ["serve", "--graph", str(graph_path)]
    server = {"command": command, "args": args}
    codex = (
        "# .codex/config.toml (Codex reads it in trusted projects)\n"
        "[mcp_servers.slurp]\n"
        f"command = {json.dumps(command)}\n"
        f"args = {json.dumps(args)}\n"
        "# Every slurp tool is read-only: Codex asks only about tools that write.\n"
        'default_tools_approval_mode = "writes"'
    )
    gemini = ("// .gemini/settings.json — trust: true skips the confirmation for its tools\n"
              + json.dumps({"mcpServers": {"slurp": {**server, "trust": True}}}, indent=2))
    cursor = ("// .cursor/mcp.json\n"
              + json.dumps({"mcpServers": {"slurp": server}}, indent=2)
              + "\n// Cursor CLI: add \"Mcp(slurp:*)\" to permissions.allow in .cursor/cli.json;"
              "\n// in the editor, allow the tools from the MCP settings.")
    return {"Codex": codex, "Gemini CLI": gemini, "Cursor": cursor}
