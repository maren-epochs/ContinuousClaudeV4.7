#!/usr/bin/env python3
"""Exit-code contract tests for install/sync_global.py (VAL-001, VAL-104).

Runs the sync script against a temporary --target directory (never the real
~/.claude) and asserts:

  1. dry run against a fresh/stale target -> exit 1 (drift detected)
  2. --apply against that target         -> exit 0
  3. dry run immediately after apply     -> exit 0, output contains "in sync"
  4. dry-run drift checks (VAL-104): stale .ccv47-installed SHA notes/warnings
     and read-only hook-registration warnings against settings.json, none of
     which change the exit-code contract.
  5. tools/ data files (.json, e.g. tools/viz/palette.json) are installed, and
     `py -3.13 tools/viz/<x>.py` commands in skills are rewritten to absolute
     paths exactly like the existing `py -3.13 tools/validate_report.py` rule
     (VAL-301).

Stdlib only. Run as: py -3.13 install/test_sync_global.py
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "sync_global.py"
sys.path.insert(0, str(SCRIPT.parent))
# importlib (not a late `import`) keeps ruff quiet under both default (E402) and RUF100 configs
_sync = importlib.import_module("sync_global")
NOT_EVENT_HOOKS, render, rewrites = _sync.NOT_EVENT_HOOKS, _sync.render, _sync.rewrites

TIMEOUT = 120
HOOKS = sorted(
    p.name
    for p in (SCRIPT.parent.parent / ".claude" / "hooks").glob("*.mjs")
    if not p.name.startswith("test") and p.name not in NOT_EVENT_HOOKS
)
FAKE_SHA = "0" * 40


def run_sync(target: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--target", str(target), *extra],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )


class ExitCodeContract(unittest.TestCase):
    def test_exit_code_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-sync-test-") as td:
            target = Path(td)

            # (a) fresh target has drift: dry run must exit 1
            dry = run_sync(target)
            self.assertEqual(
                dry.returncode,
                1,
                f"dry run with drift should exit 1, got {dry.returncode}\n"
                f"stdout:\n{dry.stdout}\nstderr:\n{dry.stderr}",
            )
            self.assertNotIn("in sync", dry.stdout)

            # (b) --apply succeeds: exit 0
            applied = run_sync(target, "--apply")
            self.assertEqual(
                applied.returncode,
                0,
                f"--apply should exit 0, got {applied.returncode}\n"
                f"stdout:\n{applied.stdout}\nstderr:\n{applied.stderr}",
            )

            # (c) dry run after apply: in sync, exit 0
            clean = run_sync(target)
            self.assertEqual(
                clean.returncode,
                0,
                f"dry run when in sync should exit 0, got {clean.returncode}\n"
                f"stdout:\n{clean.stdout}\nstderr:\n{clean.stderr}",
            )
            self.assertIn("in sync", clean.stdout)


def warn_lines(out: str) -> list[str]:
    return [l for l in out.splitlines() if l.startswith("warn:")]


def note_lines(out: str) -> list[str]:
    return [l for l in out.splitlines() if l.startswith("note:")]


class DriftChecks(unittest.TestCase):
    def test_partial_registration_warns_unregistered_hooks(self) -> None:
        self.assertGreaterEqual(len(HOOKS), 3, "need several repo hooks for this test")
        with tempfile.TemporaryDirectory(prefix="ccv47-sync-test-") as td:
            target = Path(td)
            self.assertEqual(run_sync(target, "--apply").returncode, 0)

            registered, missing = HOOKS[:2], HOOKS[2:]
            settings = {
                "statusLine": {
                    "type": "command",
                    "command": f'node "{target.as_posix()}/hooks/{registered[0]}"',
                },
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Read",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f'node "{target.as_posix()}/hooks/{registered[1]}"',
                                }
                            ],
                        }
                    ]
                },
            }
            (target / "settings.json").write_text(
                json.dumps(settings), encoding="utf-8"
            )

            dry = run_sync(target)
            self.assertEqual(
                dry.returncode,
                0,
                f"registration warnings must not change exit code\n"
                f"stdout:\n{dry.stdout}\nstderr:\n{dry.stderr}",
            )
            self.assertIn("in sync", dry.stdout)
            warns = warn_lines(dry.stdout)
            for name in missing:
                self.assertTrue(
                    any(name in w for w in warns),
                    f"expected warn naming {name}, got: {warns}",
                )
            for name in registered:
                self.assertFalse(
                    any(name in w for w in warns),
                    f"registered hook {name} must not be warned: {warns}",
                )

    def test_non_event_hooks_never_warned(self) -> None:
        self.assertIn("tldr-shim.mjs", NOT_EVENT_HOOKS)
        with tempfile.TemporaryDirectory(prefix="ccv47-sync-test-") as td:
            target = Path(td)
            self.assertEqual(run_sync(target, "--apply").returncode, 0)
            # every event hook registered; non-event hooks deliberately absent
            settings = {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Read",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f'node "{target.as_posix()}/hooks/{name}"',
                                }
                                for name in HOOKS
                            ],
                        }
                    ]
                }
            }
            (target / "settings.json").write_text(
                json.dumps(settings), encoding="utf-8"
            )

            dry = run_sync(target)
            self.assertEqual(
                dry.returncode, 0, f"stdout:\n{dry.stdout}\nstderr:\n{dry.stderr}"
            )
            self.assertTrue(
                (target / "hooks" / "tldr-shim.mjs").is_file(),
                "non-event hooks must still be installed",
            )
            self.assertEqual(
                warn_lines(dry.stdout),
                [],
                f"no registration warnings expected: {dry.stdout}",
            )

    def test_missing_settings_json_is_informational(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-sync-test-") as td:
            target = Path(td)
            self.assertEqual(run_sync(target, "--apply").returncode, 0)

            dry = run_sync(target)  # no settings.json in target
            self.assertEqual(
                dry.returncode,
                0,
                f"missing settings.json must not change exit code or crash\n"
                f"stdout:\n{dry.stdout}\nstderr:\n{dry.stderr}",
            )
            self.assertEqual(warn_lines(dry.stdout), [])
            notes = note_lines(dry.stdout)
            self.assertEqual(
                len(notes), 1, f"expected one informational line, got: {notes}"
            )
            self.assertIn("settings.json", notes[0])

    def test_stale_sha_note_when_contents_in_sync(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-sync-test-") as td:
            target = Path(td)
            self.assertEqual(run_sync(target, "--apply").returncode, 0)
            (target / ".ccv47-installed").write_text(FAKE_SHA + "\n", encoding="utf-8")

            dry = run_sync(target)
            self.assertEqual(
                dry.returncode,
                0,
                f"stale SHA record with contents in sync must stay exit 0\n"
                f"stdout:\n{dry.stdout}\nstderr:\n{dry.stderr}",
            )
            self.assertIn("in sync", dry.stdout)
            sha_notes = [n for n in note_lines(dry.stdout) if FAKE_SHA in n]
            self.assertEqual(
                len(sha_notes), 1, f"expected one SHA-mismatch note, got: {dry.stdout}"
            )
            self.assertIn("HEAD", sha_notes[0])

    def test_stale_sha_warn_with_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-sync-test-") as td:
            target = Path(td)
            (target / ".ccv47-installed").write_text(FAKE_SHA + "\n", encoding="utf-8")

            dry = run_sync(target)  # fresh target: file drift -> exit 1
            self.assertEqual(
                dry.returncode,
                1,
                f"file drift must still exit 1\nstdout:\n{dry.stdout}\nstderr:\n{dry.stderr}",
            )
            sha_warns = [w for w in warn_lines(dry.stdout) if FAKE_SHA in w]
            self.assertEqual(
                len(sha_warns),
                1,
                f"expected one SHA-mismatch warning, got: {dry.stdout}",
            )
            self.assertIn("HEAD", sha_warns[0])


class ToolsInstall(unittest.TestCase):
    """VAL-301: the globally installed tools.viz package must be complete and reachable."""

    def test_tools_json_installed(self) -> None:
        src = SCRIPT.parent.parent / "tools" / "viz" / "palette.json"
        self.assertTrue(src.is_file(), f"fixture missing: {src}")
        with tempfile.TemporaryDirectory(prefix="ccv47-sync-test-") as td:
            target = Path(td)
            applied = run_sync(target, "--apply")
            self.assertEqual(
                applied.returncode,
                0,
                f"stdout:\n{applied.stdout}\nstderr:\n{applied.stderr}",
            )
            dst = target / "tools" / "viz" / "palette.json"
            self.assertTrue(dst.is_file(), "tools/viz/palette.json must be installed")
            self.assertEqual(
                json.loads(dst.read_text(encoding="utf-8")),
                json.loads(src.read_text(encoding="utf-8")),
            )
            # installed skill no longer carries repo-relative viz commands
            skill = (target / "skills" / "visualize" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("py -3.13 tools/viz/", skill)
            self.assertIn(f"{target.as_posix()}/tools/viz/recommend.py", skill)

    def test_viz_command_rewritten_like_other_tools(self) -> None:
        dest, python = "C:/Users/x/.claude", "py -3.13"
        rules = rewrites(dest, python)
        with tempfile.TemporaryDirectory(prefix="ccv47-sync-test-") as td:
            md = Path(td) / "SKILL.md"
            md.write_text(
                "py -3.13 tools/validate_report.py r.json\n"
                "py -3.13 tools/viz/recommend.py agg.parquet --json\n"
                "python3 tools/viz/export.py render out.html out.png\n",
                encoding="utf-8",
            )
            out = render(md, rules, "\n").decode("utf-8").splitlines()
        self.assertEqual(
            out,
            [
                f"{python} {dest}/tools/validate_report.py r.json",
                f"{python} {dest}/tools/viz/recommend.py agg.parquet --json",
                f"{python} {dest}/tools/viz/export.py render out.html out.png",
            ],
        )


if __name__ == "__main__":
    unittest.main()
