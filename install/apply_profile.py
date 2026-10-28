#!/usr/bin/env python3
"""Apply the shared epoch harness profile: how Claude Code is set up and how it works.

1. Settings (install/profile/settings.profile.json) go into ~/.claude/settings.json, but
   only keys the file does not already set: a value the user chose is never overridden.
   Nested objects (permissions, env) merge key by key. The file is backed up first
   (settings.json.bak-<timestamp>-profile) and written atomically; EOL and indent kept.
2. The working-style rules (install/profile/CLAUDE.profile.md) go into ~/.claude/CLAUDE.md
   between BEGIN/END markers. A rerun replaces only that block; the rest of the file stays.

A machine with ~/.claude/.ccv47-no-profile (the maintainer's own) is skipped entirely.
Personal parts of the maintainer's setup (permission rules, email, theme, plugins) are not
in the profile. Idempotent: a second run changes nothing. --dry-run prints what would change.
Exit 0 done / nothing to do, 1 refused (unreadable settings), 2 usage.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from register_hooks import Refused, dump, read_settings
from sync_global import _harness_now, write_atomic

PROFILE_SETTINGS = HERE / "profile" / "settings.profile.json"
PROFILE_RULES = HERE / "profile" / "CLAUDE.profile.md"
BEGIN = "<!-- BEGIN epoch harness profile (install/apply_profile.py; edits here are replaced) -->"
END = "<!-- END epoch harness profile -->"


def merge_missing(cfg: dict, profile: dict, path: str = "") -> list[str]:
    """Add profile keys absent from cfg (recursing into objects); the dotted keys added."""
    added = []
    for key, value in profile.items():
        name = f"{path}{key}"
        if key not in cfg:
            cfg[key] = json.loads(json.dumps(value))
            added.append(name)
        elif isinstance(value, dict) and isinstance(cfg[key], dict):
            added += merge_missing(cfg[key], value, name + ".")
    return added


def with_rules(text: str, rules: str) -> str:
    """text with the profile block inserted or replaced between the markers."""
    eol = "\r\n" if "\r\n" in text else "\n"
    block = BEGIN + "\n" + rules.strip().replace("\r\n", "\n") + "\n" + END
    block = block.replace("\n", eol)
    start, end = text.find(BEGIN), text.find(END)
    if start != -1 and end > start:
        return text[:start] + block + text[end + len(END) :]
    if not text.strip():
        return block + eol
    if not text.endswith("\n"):
        text += eol
    return text + eol + block + eol


def main(argv: list[str] | None = None) -> int:
    """CLI entry; see the module docstring."""
    claude = Path.home() / ".claude"
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--settings", default=str(claude / "settings.json"))
    ap.add_argument("--claude-md", default=str(claude / "CLAUDE.md"))
    ap.add_argument("--dry-run", action="store_true", help="print what would change")
    args = ap.parse_args(argv)
    opt_out = Path(args.settings).parent / ".ccv47-no-profile"
    if opt_out.exists():
        print(f"profile skipped: {opt_out} exists (this machine keeps its own setup)")
        return 0

    settings = Path(args.settings)
    try:
        cfg, eol, indent, trailing = read_settings(settings)
    except Refused as exc:
        print(f"apply_profile: {exc}", file=sys.stderr)
        return 1
    profile = json.loads(PROFILE_SETTINGS.read_text(encoding="utf-8"))
    added = merge_missing(cfg, profile)

    md = Path(args.claude_md)
    old_md = md.read_bytes().decode("utf-8-sig") if md.exists() else ""
    new_md = with_rules(old_md, PROFILE_RULES.read_text(encoding="utf-8"))

    if added:
        print("settings: adding " + ", ".join(added))
    else:
        print("settings: profile keys already set, nothing to add")
    print(
        "CLAUDE.md: "
        + (
            "working-style rules up to date"
            if new_md == old_md
            else "adding/updating working-style rules"
        )
    )
    if args.dry_run:
        print("dry run - nothing written")
        return 0
    stamp = _harness_now().strftime("%Y%m%d-%H%M%S")
    if added:
        if settings.exists():
            shutil.copy2(
                settings, settings.with_name(f"{settings.name}.bak-{stamp}-profile")
            )
        settings.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(settings, dump(cfg, eol, indent, trailing))
    if new_md != old_md:
        if md.exists():
            shutil.copy2(md, md.with_name(f"{md.name}.bak-{stamp}-profile"))
        md.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(md, new_md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
