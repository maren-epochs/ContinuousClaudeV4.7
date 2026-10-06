#!/usr/bin/env python3
"""Sync this checkout into ~/.claude as a global (all-projects) install.

Upstream assumes the harness runs from the repo root, so skills call
`python tools/ouros_harness.py` and `bash scripts/readiness.sh`. A global
install needs those rewritten to absolute paths under ~/.claude, and on
Windows the interpreter pinned to one that has ouros wheels (cp313 max).

Dry run by default: prints what would change. Pass --apply to write.
Files that would be overwritten are backed up first under
~/.claude/.ccv47-backup/<timestamp>/.

Never touches settings.json, CLAUDE.md, .env, or *.orig / *.bak-* files.
Hook registration in settings.json is a one-time manual step (see README);
the dry run only READS settings.json to warn about installed-but-unregistered
hooks, and compares .ccv47-installed against git HEAD. Those drift checks are
informational: exit codes stay 1 = file drift, 0 = in sync.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# repo subtree -> destination subtree under ~/.claude
MAPPINGS = [
    ("harness/agents", "agents"),
    (".claude/hooks", "hooks"),
    ("harness/skills", "skills"),
    ("scripts", "scripts"),
    ("tools", "tools"),
]
# .json: data files tools load at runtime (tools/viz/palette.json). Global, not tools/-only:
# no other .json exists in the mapped trees; settings.json lives outside them.
INCLUDE_SUFFIXES = {".md", ".mjs", ".js", ".sh", ".py", ".txt", ".json"}
# installed .mjs not registered in settings.json: skip registration check
# (tldr-shim is spawned by tldr-read; worker-report-check is a worker.md frontmatter hook)
NOT_EVENT_HOOKS = {"tldr-shim.mjs", "worker-report-check.mjs"}


def rewrites(dest: str, python: str) -> list[tuple[re.Pattern, str]]:
    harness = f"{dest}/tools/ouros_harness.py"
    return [
        (
            re.compile(r"\b(?:py -3\.13|python3?) tools/ouros_harness\.py"),
            f"{python} {harness}",
        ),
        (re.compile(r"(?m)^tools/ouros_harness\.py"), f"{python} {harness}"),
        (re.compile(r"`tools/ouros_harness\.py`"), f"`{harness}`"),
        (
            re.compile(
                r"\b(?:py -3\.13|python3?) tools/(validate_report\.py|viz/[\w-]+\.py)"
            ),
            rf"{python} {dest}/tools/\1",
        ),
        (
            re.compile(r"\bbash scripts/(readiness(?:-fix)?\.sh)"),
            rf"bash {dest}/scripts/\1",
        ),
        (
            re.compile(r"\bnode \.claude/hooks/([\w-]+\.mjs)"),
            rf'node "{dest}/hooks/\1"',
        ),
        (re.compile(r"/tmp/ouros/\.venv/bin/pip install"), f"{python} -m pip install"),
    ]


def render(src: Path, rules, eol: str) -> bytes:
    text = src.read_text(encoding="utf-8").replace("\r\n", "\n")
    if src.suffix == ".md":
        for pat, repl in rules:
            text = pat.sub(repl, text)
    return text.replace("\n", eol).encode("utf-8")


def registered_mjs(settings: Path) -> set[str] | None:
    """Basenames of .mjs files referenced under hooks.* / statusLine, or None if unparseable."""
    try:
        cfg = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    strings: list[str] = []

    def walk(node) -> None:
        if isinstance(node, str):
            strings.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(cfg.get("hooks"))
    walk(cfg.get("statusLine"))
    return {m for s in strings for m in re.findall(r"[\w.-]+\.mjs", s)}


def drift_checks(target: Path, sha: str, file_drift: bool) -> None:
    """Read-only drift diagnostics: never writes, never changes exit codes."""
    installed = target / ".ccv47-installed"
    if sha and installed.is_file():
        recorded = (installed.read_text(encoding="utf-8").split() or ["?"])[0]
        if recorded != sha:
            if file_drift:
                print(f"warn: installed SHA {recorded} != HEAD {sha}")
            else:
                print(
                    f"note: recorded SHA {recorded} != HEAD {sha} (file contents in sync; record is stale)"
                )

    settings = target / "settings.json"
    if not settings.is_file():
        print(f"note: {settings} not found - skipping hook registration check")
        return
    refs = registered_mjs(settings)
    if refs is None:
        print(f"note: {settings} is not valid JSON - skipping hook registration check")
        return
    for hook in sorted((REPO / ".claude/hooks").glob("*.mjs")):
        if hook.name.startswith("test") or hook.name in NOT_EVENT_HOOKS:
            continue
        if hook.name not in refs:
            print(
                f"warn: hooks/{hook.name} is installed but not registered in {settings}"
            )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--apply", action="store_true", help="write changes (default: dry run)"
    )
    ap.add_argument(
        "--target",
        default=str(Path.home() / ".claude"),
        help="install root (default: ~/.claude)",
    )
    ap.add_argument(
        "--python",
        default="py -3.13" if sys.platform == "win32" else "python3",
        help="interpreter command written into skills",
    )
    ap.add_argument(
        "--eol",
        choices=["crlf", "lf"],
        default="crlf" if sys.platform == "win32" else "lf",
    )
    ap.add_argument(
        "--diff", action="store_true", help="show unified diffs for changed files"
    )
    args = ap.parse_args()

    target = Path(args.target)
    rules = rewrites(target.as_posix(), args.python)
    eol = "\r\n" if args.eol == "crlf" else "\n"

    plan: list[tuple[Path, Path, bytes, str]] = []
    for src_rel, dst_rel in MAPPINGS:
        for src in sorted((REPO / src_rel).rglob("*")):
            if (
                not src.is_file()
                or src.suffix not in INCLUDE_SUFFIXES
                or "__pycache__" in src.parts
            ):
                continue
            dst = target / dst_rel / src.relative_to(REPO / src_rel)
            new = render(src, rules, eol)
            if not dst.exists():
                plan.append((src, dst, new, "new"))
            elif dst.read_bytes() != new:
                plan.append((src, dst, new, "update"))

    if not plan:
        print(f"{target}: in sync with {REPO.name}")
    for src, dst, new, kind in plan:
        print(f"{kind:7} {dst.relative_to(target).as_posix()}")
        if args.diff and kind == "update":
            old = dst.read_text(encoding="utf-8").replace("\r\n", "\n").splitlines()
            sys.stdout.writelines(
                l + "\n"
                for l in difflib.unified_diff(
                    old,
                    new.decode("utf-8").replace("\r\n", "\n").splitlines(),
                    "installed",
                    "repo",
                    lineterm="",
                    n=1,
                )
            )

    sha = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()

    if not args.apply:
        drift_checks(target, sha, file_drift=bool(plan))
        if plan:
            print("\ndry run - pass --apply to write (exit 1: out of sync)")
        return 1 if plan else 0

    if plan:
        backup = (
            target
            / ".ccv47-backup"
            / datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        )
        for _, dst, new, kind in plan:
            if kind == "update":
                b = backup / dst.relative_to(target)
                b.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, b)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(new)
        print(f"\nwrote {len(plan)} file(s); backups in {backup}")
    (target / ".ccv47-installed").write_text(
        f"{sha}{' (dirty)' if dirty else ''}\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
