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
User pins: <target>/.ccv47-keep lists install-relative paths (one per line,
# comments) that are never written; they print as `keep    <path>` and do not
count as drift.
--apply also writes <target>/.ccv47-manifest.json (atomic replace): every
installed path -> its repo path, sha256 of the installed bytes, and a kept flag
(shape: tools/fleet/schema.md). The manifest itself is never a synced file.
Hook registration in settings.json is a one-time manual step (see README);
the dry run only READS settings.json to warn about installed-but-unregistered
hooks, and compares .ccv47-installed against git HEAD. Those drift checks are
informational: exit codes stay 1 = file drift, 0 = in sync.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
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
    """Regex rewrites that point repo-relative tool paths in .md files at dest."""
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
    """Source file bytes as they should appear in the target (rewritten .md, chosen EOL)."""
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


def _parse_args() -> argparse.Namespace:
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
    return ap.parse_args()


def _sources(src_root: Path):
    """Installable files under one mapped repo subtree, sorted."""
    for src in sorted(src_root.rglob("*")):
        if (
            src.is_file()
            and src.suffix in INCLUDE_SUFFIXES
            and "__pycache__" not in src.parts
        ):
            yield src


def installables(target: Path):
    """(src, dst) for every repo file the sync maps into target."""
    for src_rel, dst_rel in MAPPINGS:
        for src in _sources(REPO / src_rel):
            yield src, target / dst_rel / src.relative_to(REPO / src_rel)


KEEP_FILE = ".ccv47-keep"


def load_keep(target: Path) -> set[str]:
    """Install-relative posix paths pinned in <target>/.ccv47-keep (missing file = none)."""
    try:
        text = (target / KEEP_FILE).read_text(encoding="utf-8")
    except OSError:
        return set()
    keep: set[str] = set()
    for line in text.splitlines():
        entry = line.strip().replace("\\", "/")
        if entry and not entry.startswith("#"):
            keep.add(entry.removeprefix("./"))
    return keep


def build_plan(
    target: Path, rules, eol: str, keep: frozenset[str] | set[str] = frozenset()
) -> list[tuple[Path, Path, bytes, str]]:
    """(src, dst, rendered bytes, 'new'|'update') for every file that differs;
    kept paths are always listed as (src, dst, b'', 'keep') and never rendered."""
    plan: list[tuple[Path, Path, bytes, str]] = []
    for src, dst in installables(target):
        if dst.relative_to(target).as_posix() in keep:
            plan.append((src, dst, b"", "keep"))
            continue
        new = render(src, rules, eol)
        if not dst.exists():
            plan.append((src, dst, new, "new"))
        elif dst.read_bytes() != new:
            plan.append((src, dst, new, "update"))
    return plan


MANIFEST_FILE = ".ccv47-manifest.json"  # tools/fleet/model.py manifest_path()


def build_manifest(
    target: Path, keep: frozenset[str] | set[str], sha: str, dirty: bool, eol: str
) -> dict:
    """Manifest of the installed tree as it is on disk (call after writing)."""
    files: dict[str, dict] = {}
    for src, dst in installables(target):
        rel = dst.relative_to(target).as_posix()
        digest = hashlib.sha256(dst.read_bytes()).hexdigest() if dst.is_file() else None
        files[rel] = {
            "repo_path": src.relative_to(REPO).as_posix(),
            "sha256": digest,
            "kept": rel in keep,
        }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": str(REPO),
        "head_sha": sha or None,
        "dirty": dirty,
        "eol": eol,
        "files": files,
    }


def manifest_text(manifest: dict) -> str:
    """Manifest as strict JSON text (NaN/Infinity raise ValueError: Node rejects them)."""
    return json.dumps(manifest, indent=2, allow_nan=False) + "\n"


def write_atomic(path: Path, text: str) -> None:
    """Temp file in the same dir + os.replace (retried: Windows readers block renames)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _print_diff(dst: Path, new: bytes) -> None:
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


def print_plan(target: Path, plan, show_diff: bool) -> None:
    """Print each planned create/update/keep (optionally with a unified diff), or in-sync."""
    if not actionable(plan):
        print(f"{target}: in sync with {REPO.name}")
    for _, dst, new, kind in plan:
        print(f"{kind:7} {dst.relative_to(target).as_posix()}")
        if show_diff and kind == "update":
            _print_diff(dst, new)


def actionable(plan):
    """Plan entries that write (kept paths excluded)."""
    return [p for p in plan if p[3] != "keep"]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


def apply_plan(target: Path, plan) -> None:
    """Write the plan; files being updated are backed up first."""
    backup = (
        target / ".ccv47-backup" / datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    )
    for _, dst, new, kind in plan:
        if kind == "update":
            b = backup / dst.relative_to(target)
            b.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, b)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(new)
    print(f"\nwrote {len(plan)} file(s); backups in {backup}")


def main() -> int:
    """CLI entry: plan and print the sync to --target; dry run exits 1 if out of sync, --apply writes."""
    args = _parse_args()
    target = Path(args.target)
    rules = rewrites(target.as_posix(), args.python)
    eol = "\r\n" if args.eol == "crlf" else "\n"

    keep = load_keep(target)
    full = build_plan(target, rules, eol, keep)
    print_plan(target, full, args.diff)
    kept = {dst.relative_to(target).as_posix() for _, dst, _, k in full if k == "keep"}
    for entry in sorted(keep - kept):
        print(f"note: {KEEP_FILE} entry {entry} matches no installable file")
    plan = actionable(full)

    sha = _git("rev-parse", "HEAD")
    dirty = _git("status", "--porcelain")

    if not args.apply:
        drift_checks(target, sha, file_drift=bool(plan))
        if plan:
            print("\ndry run - pass --apply to write (exit 1: out of sync)")
        return 1 if plan else 0

    if plan:
        apply_plan(target, plan)
    (target / ".ccv47-installed").write_text(
        f"{sha}{' (dirty)' if dirty else ''}\n", encoding="utf-8"
    )
    manifest = build_manifest(target, keep, sha, bool(dirty), args.eol)
    write_atomic(target / MANIFEST_FILE, manifest_text(manifest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
