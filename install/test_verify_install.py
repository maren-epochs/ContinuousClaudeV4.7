"""Tests for install/verify_install.py against a fake ~/.claude."""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
vi = importlib.import_module("verify_install")


def run(claude: Path, *extra: str) -> tuple[int, str]:
    """Exit code and output of verify_install on claude."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = vi.main(["--claude-dir", str(claude), *extra])
    return rc, out.getvalue()


class VerifyInstall(unittest.TestCase):
    def test_empty_dir_fails_with_send_back_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = run(Path(tmp))
        self.assertEqual(rc, 1)
        self.assertIn("FAIL install manifest", out)
        self.assertIn("INSTALL FAILED", out)
        self.assertIn("send it back", out)

    def test_changed_file_and_missing_hook_are_named(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            claude = Path(tmp)
            skill = claude / "skills" / "demo" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("x", encoding="utf-8")
            manifest = {
                "files": {
                    "skills/demo/SKILL.md": {
                        "sha256": hashlib.sha256(b"other").hexdigest(),
                        "kept": False,
                    }
                }
            }
            (claude / ".ccv47-manifest.json").write_text(json.dumps(manifest))
            hook = f'node "{claude.as_posix()}/.claude/hooks/gone.mjs"'
            settings = {"hooks": {"Stop": [{"hooks": [{"command": hook}]}]}}
            (claude / "settings.json").write_text(json.dumps(settings))
            rc, out = run(claude, "--skip-profile")
        self.assertEqual(rc, 1)
        self.assertIn("skills/demo/SKILL.md differs", out)
        self.assertIn("gone.mjs", out)
        self.assertIn("FAIL status line registered", out)


if __name__ == "__main__":
    unittest.main()
