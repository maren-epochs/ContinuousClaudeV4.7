#!/usr/bin/env python3
"""Register the fleet hooks in a Claude Code settings file (user-run, idempotent).

Adds, only when the hook is not referenced under that event already:

  PreToolUse   Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell -> harness-guard.mjs
  PostToolUse  Bash|PowerShell                                   -> fleet-audit.mjs
  PreToolUse   AskUserQuestion                                   -> ask-queue.mjs

ask-queue.mjs queues every question instead of opening a box, so a waiting session
can still read cross-session messages (tools/fleet/questions.py).

The guard and audit hooks are unfiltered (no `if`): a live check against Claude Code 2.1.293 showed a
`Write(~/...)` filter does not fire for an 8.3 short-name spelling of the same path,
so the guard does its own path normalization on every call (~60-80 ms node spawn).
The fleet collect trigger rides the existing Stop hook (auto-handoff-stop.mjs); no
entry is added for it, only a note when that hook is not registered.

Commands use the same style as the existing entries: node "<home>/.claude/hooks/<x>.mjs"
with forward slashes. Every other key, value, key order, indent and EOL is kept.
A changed file is backed up to <settings>.bak-<timestamp>-register-hooks first and
replaced atomically. --dry-run prints the unified diff and writes nothing.

Requires Node 18+ on PATH and the hook files installed (`py -3.13 install/sync_global.py
--apply` first). Exit codes: 0 registered or already registered, 1 refused, 2 usage.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from sync_global import write_atomic  # sibling script: install/ is sys.path[0]

MIN_NODE = 18
GUARD, AUDIT, STOP = "harness-guard.mjs", "fleet-audit.mjs", "auto-handoff-stop.mjs"
ASK = "ask-queue.mjs"
HOOK_FILES = (GUARD, AUDIT, ASK)
FILE_TOOLS = "Write|Edit|MultiEdit|NotebookEdit"
SHELL_TOOLS = "Bash|PowerShell"
# (event, matcher, hook file); file tools unfiltered per the live `if` check (8.3 miss)
ENTRIES = (
    ("PreToolUse", f"{FILE_TOOLS}|{SHELL_TOOLS}", GUARD),
    ("PostToolUse", SHELL_TOOLS, AUDIT),
    ("PreToolUse", "AskUserQuestion", ASK),
)
TIMEOUT = 15


class Refused(Exception):
    """Registration refused; message goes to stderr, nothing is written."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI options; defaults point at ~/.claude/settings.json and ~/.claude/hooks."""
    claude = Path.home() / ".claude"
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--settings",
        default=str(claude / "settings.json"),
        help="settings file (default: ~/.claude/settings.json)",
    )
    ap.add_argument(
        "--hooks-dir",
        default=str(claude / "hooks"),
        help="installed hooks dir used in the commands (default: ~/.claude/hooks)",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="print the diff, write nothing"
    )
    return ap.parse_args(argv)


def node_major(node: str = "node") -> int | None:
    """Major version of `node --version`, or None when node is missing or unparseable."""
    try:
        out = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=30, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"\s*v?(\d+)\.", out)
    return int(m.group(1)) if m else None


def read_settings(path: Path) -> tuple[dict, str, str | int, bool]:
    """(config, eol, indent, trailing newline); a missing file is an empty config."""
    if not path.exists():
        return {}, "\n", 2, True
    raw = path.read_bytes().decode("utf-8-sig")
    try:
        cfg = json.loads(raw)
    except ValueError as exc:
        raise Refused(f"{path} is not valid JSON ({exc}); fix it first") from None
    if not isinstance(cfg, dict):
        raise Refused(f"{path}: top level is not a JSON object")
    eol = "\r\n" if "\r\n" in raw else "\n"
    m = re.search(r"\n([ \t]+)\S", raw)
    indent: str | int = 2
    if m:
        ws = m.group(1)
        indent = len(ws) if set(ws) == {" "} else ws
    return cfg, eol, indent, raw.endswith("\n")


def dump(cfg: dict, eol: str, indent: str | int, trailing: bool) -> str:
    """Config as JSON text in the file's own indent, EOL and trailing-newline style."""
    text = json.dumps(cfg, indent=indent, ensure_ascii=False)
    return text.replace("\n", eol) + (eol if trailing else "")


def referenced(groups: list, name: str) -> bool:
    """True when any hook command in the event's groups references the hook basename."""
    for group in groups:
        hooks = group.get("hooks") if isinstance(group, dict) else None
        for hook in hooks if isinstance(hooks, list) else []:
            cmd = hook.get("command") if isinstance(hook, dict) else None
            if isinstance(cmd, str) and name in re.findall(r"[\w.-]+\.mjs", cmd):
                return True
    return False


def register(cfg: dict, hooks_dir: str) -> tuple[dict, list[str]]:
    """Copy of cfg with missing fleet entries appended, plus one message per entry."""
    new = json.loads(json.dumps(cfg))
    hooks = new.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise Refused("settings key 'hooks' is not an object")
    notes: list[str] = []
    for event, matcher, name in ENTRIES:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise Refused(f"settings hooks.{event} is not a list")
        if referenced(groups, name):
            notes.append(f"{name} already registered under {event}")
            continue
        groups.append(
            {
                "matcher": matcher,
                "hooks": [
                    {
                        "type": "command",
                        "command": f'node "{hooks_dir}/{name}"',
                        "timeout": TIMEOUT,
                    }
                ],
            }
        )
        notes.append(f"added {event} {matcher} -> {name}")
    stop = hooks.get("Stop")
    if not (isinstance(stop, list) and referenced(stop, STOP)):
        notes.append(
            f"note: {STOP} is not registered under Stop; fleet collect rides that hook,"
            " so state.json only refreshes on `fleet.py collect` / `report --fresh`"
        )
    return new, notes


def backup_path(path: Path) -> Path:
    """Unused <name>.bak-<timestamp>[-N]-register-hooks sibling of path."""
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    cand = path.with_name(f"{path.name}.bak-{stamp}-register-hooks")
    n = 1
    while cand.exists():
        n += 1
        cand = path.with_name(f"{path.name}.bak-{stamp}-{n}-register-hooks")
    return cand


def run(args: argparse.Namespace) -> int:
    """Check Node and hook files, then register (or diff); raises Refused."""
    major = node_major()
    if major is None:
        raise Refused(
            f"Node.js not found on PATH; the hooks need Node {MIN_NODE}+ (install it, then rerun)"
        )
    if major < MIN_NODE:
        raise Refused(
            f"Node {major} found; the hooks need Node {MIN_NODE}+ (upgrade, then rerun)"
        )
    hooks_dir = Path(os.path.abspath(args.hooks_dir))
    missing = [n for n in HOOK_FILES if not (hooks_dir / n).is_file()]
    if missing:
        raise Refused(
            f"{', '.join(missing)} not found in {hooks_dir}; install first:"
            " py -3.13 install/sync_global.py --apply"
        )
    settings = Path(args.settings)
    cfg, eol, indent, trailing = read_settings(settings)
    new, notes = register(cfg, hooks_dir.as_posix())
    for note in notes:
        print(note)
    if new == cfg:
        print(f"{settings}: already registered, nothing to do")
        return 0
    new_text = dump(new, eol, indent, trailing)
    if args.dry_run:
        old = settings.read_bytes().decode("utf-8-sig") if settings.exists() else ""
        sys.stdout.writelines(
            ln + "\n"
            for ln in difflib.unified_diff(
                old.replace("\r\n", "\n").splitlines(),
                new_text.replace("\r\n", "\n").splitlines(),
                str(settings),
                f"{settings} (registered)",
                lineterm="",
            )
        )
        print("dry run - nothing written")
        return 0
    if settings.exists():
        bak = backup_path(settings)
        shutil.copy2(settings, bak)
        print(f"backup: {bak}")
    # sync_global.write_atomic writes text with newline="\n": EOLs are kept as-is.
    write_atomic(settings, new_text)
    print(f"wrote {settings}; restart Claude Code sessions to load the hooks")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry: 0 done or already registered, 1 refused, 2 usage."""
    args = parse_args(argv)
    try:
        return run(args)
    except Refused as exc:
        print(f"register_hooks: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
