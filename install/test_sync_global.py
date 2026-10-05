#!/usr/bin/env python3
"""Exit-code contract tests for install/sync_global.py (VAL-001).

Runs the sync script against a temporary --target directory (never the real
~/.claude) and asserts:

  1. dry run against a fresh/stale target -> exit 1 (drift detected)
  2. --apply against that target         -> exit 0
  3. dry run immediately after apply     -> exit 0, output contains "in sync"

Stdlib only. Run as: py -3.13 install/test_sync_global.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "sync_global.py"
TIMEOUT = 120


def run_sync(target: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--target", str(target), *extra],
        capture_output=True, text=True, timeout=TIMEOUT,
    )


class ExitCodeContract(unittest.TestCase):
    def test_exit_code_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-sync-test-") as td:
            target = Path(td)

            # (a) fresh target has drift: dry run must exit 1
            dry = run_sync(target)
            self.assertEqual(
                dry.returncode, 1,
                f"dry run with drift should exit 1, got {dry.returncode}\n"
                f"stdout:\n{dry.stdout}\nstderr:\n{dry.stderr}",
            )
            self.assertNotIn("in sync", dry.stdout)

            # (b) --apply succeeds: exit 0
            applied = run_sync(target, "--apply")
            self.assertEqual(
                applied.returncode, 0,
                f"--apply should exit 0, got {applied.returncode}\n"
                f"stdout:\n{applied.stdout}\nstderr:\n{applied.stderr}",
            )

            # (c) dry run after apply: in sync, exit 0
            clean = run_sync(target)
            self.assertEqual(
                clean.returncode, 0,
                f"dry run when in sync should exit 0, got {clean.returncode}\n"
                f"stdout:\n{clean.stdout}\nstderr:\n{clean.stderr}",
            )
            self.assertIn("in sync", clean.stdout)


if __name__ == "__main__":
    unittest.main()
