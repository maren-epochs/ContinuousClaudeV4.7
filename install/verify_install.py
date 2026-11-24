#!/usr/bin/env python3
"""Check that the harness install in ~/.claude works; the installers run it last.

    python install/verify_install.py [--claude-dir DIR]

Checks: every installed file matches the install manifest; every hook and the status
line registered in settings.json points at an installed file; Node 18+ runs the status
line on a sample input; the Python packages the tools need import; Claude Code runs; the profile block is
in CLAUDE.md (unless opted out). One line per check, then INSTALL OK or INSTALL FAILED
with what to send back. Exit 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apply_profile import BEGIN

PACKAGES = ("aiohttp", "matplotlib", "plotly", "pandas", "playwright")
SAMPLE = {
    "model": {"display_name": "Opus"},
    "workspace": {"current_dir": str(Path.home())},
    "context_window": {"used_percentage": 12},
    "session_id": "verify-install",
}


class Report:
    """Collects check results and prints them as they come."""

    def __init__(self) -> None:
        """No failures yet."""
        self.failed: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        """Print one result line; remember failures."""
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} {name}" + (f": {detail}" if detail else ""))
        if not ok:
            self.failed.append(name)


def check_files(claude: Path, r: Report) -> None:
    """Installed files exist and match the manifest's sha256."""
    manifest = claude / ".ccv47-manifest.json"
    if not manifest.exists():
        r.check(
            "install manifest", False, f"{manifest} missing: the sync step did not run"
        )
        return
    files = json.loads(manifest.read_text(encoding="utf-8"))["files"]
    bad = []
    for rel, meta in files.items():
        path = claude / rel
        if meta.get("kept"):
            continue
        if not path.is_file():
            bad.append(f"{rel} missing")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != meta["sha256"]:
            bad.append(f"{rel} differs")
    r.check(f"{len(files)} harness files installed", not bad, "; ".join(bad[:5]))
    skills = sorted(p.parent.name for p in (claude / "skills").glob("*/SKILL.md"))
    r.check("skills", bool(skills), ", ".join(skills) or "none found")


def hook_paths(command: str) -> list[Path]:
    """The .mjs files a hook command runs."""
    return [Path(m) for m in re.findall(r'"([^"]+\.mjs)"', command)]


def check_settings(claude: Path, r: Report) -> str | None:
    """Hooks and status line registered and pointing at installed files; status command."""
    settings = claude / "settings.json"
    try:
        cfg = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        r.check("settings.json", False, str(exc))
        return None
    commands = [
        h.get("command", "")
        for groups in cfg.get("hooks", {}).values()
        for g in groups
        for h in g.get("hooks", [])
    ]
    ours = [c for c in commands if "/.claude/hooks/" in c.replace("\\", "/")]
    missing = [str(p) for c in ours for p in hook_paths(c) if not p.is_file()]
    r.check(
        f"{len(ours)} hooks registered",
        bool(ours) and not missing,
        "; ".join(missing[:3]),
    )
    status = cfg.get("statusLine", {}).get("command", "")
    r.check("status line registered", "status.mjs" in status, status or "none")
    return status if "status.mjs" in status else None


def check_node(status: str | None, r: Report) -> None:
    """Node 18+ present and the status line runs on a sample input."""
    node = shutil.which("node")
    if not node:
        r.check("Node.js", False, "node not on PATH")
        return
    ver = subprocess.run(
        [node, "--version"], capture_output=True, text=True, check=False
    ).stdout.strip()
    major = int(ver.lstrip("v").split(".")[0] or 0)
    r.check("Node.js 18+", major >= 18, ver)
    if not status:
        return
    paths = hook_paths(status)
    p = subprocess.run(
        [node, *map(str, paths)],
        input=json.dumps(SAMPLE),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    ok = p.returncode == 0 and bool(p.stdout.strip())
    r.check(
        "status line runs",
        ok,
        "" if ok else (p.stderr.strip()[-200:] or f"exit {p.returncode}"),
    )


def check_python(r: Report) -> None:
    """Python 3.13 and the packages the tools import."""
    v = sys.version_info
    r.check(
        "Python 3.13",
        v[:2] == (3, 13),
        f"{v.major}.{v.minor}.{v.micro} at {sys.executable}",
    )
    missing = [m for m in PACKAGES if importlib.util.find_spec(m) is None]
    r.check(
        "Python packages",
        not missing,
        "missing " + ", ".join(missing) if missing else "",
    )


def check_claude(r: Report) -> None:
    """Claude Code (`claude`) on PATH and runs."""
    exe = shutil.which("claude") or shutil.which(
        str(Path.home() / ".local" / "bin" / "claude")
    )
    if not exe:
        r.check("Claude Code", False, "claude not on PATH")
        return
    p = subprocess.run(
        [exe, "--version"], capture_output=True, text=True, timeout=60, check=False
    )
    r.check(
        "Claude Code",
        p.returncode == 0,
        p.stdout.strip() or p.stderr.strip()[-200:],
    )


def check_profile(claude: Path, r: Report) -> None:
    """Profile rules present unless the machine opted out."""
    if (claude / ".ccv47-no-profile").exists():
        r.check("profile", True, "opted out")
        return
    md = claude / "CLAUDE.md"
    ok = md.exists() and BEGIN in md.read_text(encoding="utf-8-sig")
    r.check("profile rules in CLAUDE.md", ok)


def main(argv: list[str] | None = None) -> int:
    """CLI entry; see the module docstring."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--claude-dir", default=str(Path.home() / ".claude"))
    ap.add_argument("--skip-profile", action="store_true")
    args = ap.parse_args(argv)
    claude = Path(args.claude_dir)
    r = Report()
    print("Checking the install:")
    check_files(claude, r)
    status = check_settings(claude, r)
    check_node(status, r)
    check_python(r)
    check_claude(r)
    if not args.skip_profile:
        check_profile(claude, r)
    if r.failed:
        print(f"\nINSTALL FAILED ({len(r.failed)} check(s): {', '.join(r.failed)}).")
        print("Copy everything in this window and send it back.")
        return 1
    print(
        "\nINSTALL OK. Open a new terminal and type claude (the first time it asks you"
        " to sign in with your Claude account), then run /bootup in any project."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
