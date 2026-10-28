#!/usr/bin/env python3
"""Tests for install/apply_profile.py. Temp files only; the real ~/.claude is never touched.

Run as: py -3.13 -m pytest -q install/test_apply_profile.py
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
ap = importlib.import_module("apply_profile")


class Profile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.settings = self.dir / "settings.json"
        self.md = self.dir / "CLAUDE.md"

    def run_cli(self, *extra: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ap.main(
                ["--settings", str(self.settings), "--claude-md", str(self.md), *extra]
            )
        return code, out.getvalue()

    def test_fresh_install_gets_profile_settings_and_rules(self) -> None:
        code, out = self.run_cli()
        self.assertEqual(code, 0, out)
        cfg = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(cfg["permissions"]["defaultMode"], "auto")
        self.assertEqual(cfg["model"], "opus")
        self.assertEqual(cfg["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "90")
        text = self.md.read_text(encoding="utf-8")
        self.assertIn(ap.BEGIN, text)
        self.assertIn("AskUserQuestion", text)

    def test_user_choices_never_overridden_and_other_keys_kept(self) -> None:
        self.settings.write_text(
            json.dumps(
                {
                    "model": "sonnet",
                    "permissions": {
                        "defaultMode": "acceptEdits",
                        "allow": ["Bash(git status)"],
                    },
                    "env": {"MAX_THINKING_TOKENS": "8000"},
                    "hooks": {"Stop": []},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.run_cli()
        cfg = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(cfg["model"], "sonnet")
        self.assertEqual(cfg["permissions"]["defaultMode"], "acceptEdits")
        self.assertEqual(cfg["permissions"]["allow"], ["Bash(git status)"])
        self.assertEqual(cfg["env"]["MAX_THINKING_TOKENS"], "8000")
        self.assertEqual(cfg["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "32000")
        self.assertIn("hooks", cfg)
        self.assertEqual(len(list(self.dir.glob("settings.json.bak-*-profile"))), 1)

    def test_rules_block_replaced_not_duplicated_and_own_notes_kept(self) -> None:
        self.md.write_bytes(
            b"# My notes\r\n\r\nKeep this.\r\n"
        )  # bytes: write_text adds \r on Windows
        self.run_cli()
        text = self.md.read_bytes().decode("utf-8")
        self.assertTrue(text.startswith("# My notes\r\n\r\nKeep this.\r\n"))
        self.assertNotIn("\n\n", text.replace("\r\n", "\x00"))  # CRLF kept, no bare LF
        stale = text.replace("Be concise and direct.", "OLD RULE.")
        self.md.write_bytes(stale.encode("utf-8"))
        self.run_cli()
        again = self.md.read_bytes().decode("utf-8")
        self.assertEqual(again.count(ap.BEGIN), 1)
        self.assertNotIn("OLD RULE.", again)
        self.assertIn("Keep this.", again)

    def test_second_run_changes_nothing(self) -> None:
        self.run_cli()
        before = (self.settings.read_bytes(), self.md.read_bytes())
        backups = sorted(self.dir.glob("*.bak-*"))
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual((self.settings.read_bytes(), self.md.read_bytes()), before)
        self.assertEqual(sorted(self.dir.glob("*.bak-*")), backups)
        self.assertIn("nothing to add", out)

    def test_dry_run_writes_nothing(self) -> None:
        code, out = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse(self.settings.exists())
        self.assertFalse(self.md.exists())
        self.assertIn("dry run", out)

    def test_opt_out_marker_skips_everything(self) -> None:
        (self.dir / ".ccv47-no-profile").write_text("", encoding="utf-8")
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("profile skipped", out)
        self.assertFalse(self.settings.exists())
        self.assertFalse(self.md.exists())

    def test_invalid_settings_refused(self) -> None:
        self.settings.write_text("{not json", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _ = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{not json")


if __name__ == "__main__":
    unittest.main()
