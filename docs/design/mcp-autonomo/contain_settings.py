"""Keep a measurement clone's Claude Code permissions inside the clone. Not part of slurp.

Usage (run_tasks.sh calls it for every clone):

    python contain_settings.py rewrite CLONE REAL_ROOT   # before slurp init
    python contain_settings.py check CLONE               # before every session

A project's `.claude/settings.json` and `.claude/settings.local.json` can name
absolute paths of the real checkout — PrismaStats's list `additionalDirectories`
under ~/Desktop/prismastats, and shell rules with its path — and an agent working
with acceptEdits in the clone could then write into the real repository.
`rewrite` points every such path into the clone (a Windows path is mapped by the
project's directory name); `check` exits 1, naming each offender, if any
permission rule or additional directory still points outside the clone.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

SETTINGS = (".claude/settings.json", ".claude/settings.local.json")
# An absolute path inside a permission rule: POSIX (`//` is Claude Code's
# absolute-path prefix in Read/Edit rules) or Windows.
_POSIX = re.compile(r"(?<![\w.~-])/{1,2}(?:Users|private|tmp|var|opt|home|Volumes|etc|usr|mnt)"
                    r"(?:/[^\s\"'()*,;|&<>]*)?")
_WINDOWS = re.compile(r"[A-Za-z]:\\[^\"'()*,;|&<>]*")


def _strings(node, path=()):
    """(location, string) for every string under a settings value."""
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _strings(item, (*path, i))
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, (*path, key))


def _map(node, fn):
    if isinstance(node, str):
        return fn(node)
    if isinstance(node, list):
        return [_map(x, fn) for x in node]
    if isinstance(node, dict):
        return {k: _map(v, fn) for k, v in node.items()}
    return node


def _forms(path: Path) -> list[str]:
    """The spellings of *path* a settings file may use (as given, resolved)."""
    forms = {str(path), os.path.realpath(path)}
    return sorted(forms, key=len, reverse=True)


def rewrite(clone: Path, real_root: Path) -> list[str]:
    """Point every path of *real_root* in the clone's settings into the clone."""
    clone_s = os.path.realpath(clone)
    name = real_root.name.lower()
    win = re.compile(r"[A-Za-z]:\\(?:[^\\\"']+\\)*?" + re.escape(name) + r"((?:\\[^\\\"']*)*)",
                     re.IGNORECASE)
    changed: list[str] = []
    for rel in SETTINGS:
        path = clone / rel
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue

        def fix(text: str) -> str:
            for form in _forms(real_root):
                text = text.replace(form, clone_s)
            return win.sub(lambda m: clone_s + m.group(1).replace("\\", "/"), text)

        fixed = _map(data, fix)
        if fixed != data:
            path.write_text(json.dumps(fixed, indent=2) + "\n", encoding="utf-8")
            changed.append(rel)
    return changed


def offenders(clone: Path) -> list[str]:
    """Permission rules and additional directories that point outside the clone."""
    inside = [f.rstrip("/") for f in _forms(clone)]
    found: list[str] = []
    for rel in SETTINGS:
        path = clone / rel
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            found.append(f"{rel}: not valid JSON, cannot be checked")
            continue
        for where, text in _strings(data.get("permissions", {}) if isinstance(data, dict)
                                    else {}):
            for token in [*_POSIX.findall(text), *_WINDOWS.findall(text)]:
                normal = "/" + token.lstrip("/") if token.startswith("/") else token
                if not any(normal == root or normal.startswith(root + "/") for root in inside):
                    found.append(f"{rel}: permissions.{'.'.join(map(str, where))}: {token}")
    return found


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "rewrite":
        for rel in rewrite(Path(argv[1]), Path(argv[2])):
            print(f"rewrote paths in {rel}")
        return 0
    if len(argv) == 2 and argv[0] == "check":
        bad = offenders(Path(argv[1]))
        for line in bad:
            print(f"outside the clone: {line}", file=sys.stderr)
        return 1 if bad else 0
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
