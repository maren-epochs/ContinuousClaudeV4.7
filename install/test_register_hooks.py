#!/usr/bin/env python3
"""Tests for install/register_hooks.py (VAL-811).

Every test works on a temp settings file and a temp hooks dir (never the real
~/.claude). Covered: both fleet entries added with absolute node commands in the
existing style, other content and key order kept, idempotent re-runs, no
duplicates when one entry is already present, backup before write, --dry-run
diff without writing, EOL/indent kept, invalid JSON / missing hook files /
missing or old Node refused, Stop-hook collect trigger noted but never added.

Stdlib only. Run as: py -3.13 -m pytest -q install/test_register_hooks.py
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent / "register_hooks.py"
sys.path.insert(0, str(SCRIPT.parent))
_reg = importlib.import_module("register_hooks")

EXISTING = {
    "env": {"MAX_THINKING_TOKENS": "31999"},
    "permissions": {"allow": ["Bash(git status)"], "deny": []},
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Read",
                "hooks": [
                    {
                        "type": "command",
                        "if": "Read(*.py)",
                        "command": 'node "C:/x/.claude/hooks/tldr-read.mjs"',
                        "timeout": 15,
                    }
                ],
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Edit|Write|MultiEdit|Update",
                "hooks": [
                    {
                        "type": "command",
                        "command": 'node "C:/x/.claude/hooks/worker-report-check.mjs"',
                        "timeout": 25,
                    }
                ],
            }
        ],
        "Stop": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": 'node "C:/x/.claude/hooks/auto-handoff-stop.mjs"',
                    }
                ],
            }
        ],
    },
    "statusLine": {
        "type": "command",
        "command": 'node "C:/x/.claude/hooks/status.mjs"',
    },
    "model": "opus",
}


class Env:
    """Temp dir with settings.json + hooks/{harness-guard,fleet-audit}.mjs."""

    def __init__(self, td: str, data: object = EXISTING, text: str | None = None):
        self.root = Path(td)
        self.hooks = self.root / "hooks"
        self.hooks.mkdir()
        for name in _reg.HOOK_FILES:
            (self.hooks / name).write_text("// stub\n", encoding="utf-8")
        self.settings = self.root / "settings.json"
        if text is None and data is not None:
            text = json.dumps(data, indent=2) + "\n"
        if text is not None:
            self.settings.write_bytes(text.encode("utf-8"))

    def run(self, *extra: str, node: int | None = 24) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = [
            "--settings",
            str(self.settings),
            "--hooks-dir",
            str(self.hooks),
            *extra,
        ]
        with (
            mock.patch.object(_reg, "node_major", return_value=node),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = _reg.main(argv)
        return code, out.getvalue(), err.getvalue()

    def load(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def backups(self) -> list[Path]:
        return sorted(self.root.glob("settings.json.bak-*"))

    def cmd(self, name: str) -> str:
        return f'node "{self.hooks.as_posix()}/{name}"'


def commands(cfg: dict, event: str) -> list[str]:
    return [h["command"] for g in cfg["hooks"].get(event, []) for h in g["hooks"]]


class Register(unittest.TestCase):
    def test_adds_guard_and_audit_with_absolute_node_commands(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td)
            code, out, err = env.run()
            self.assertEqual(code, 0, err)
            cfg = env.load()
            pre = cfg["hooks"]["PreToolUse"]
            post = cfg["hooks"]["PostToolUse"]
            self.assertEqual(pre[0], EXISTING["hooks"]["PreToolUse"][0])  # type: ignore[index]
            self.assertEqual(post[0], EXISTING["hooks"]["PostToolUse"][0])  # type: ignore[index]
            guard = pre[-1]
            self.assertEqual(
                set(guard["matcher"].split("|")),
                {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "PowerShell"},
            )
            self.assertEqual(
                guard["hooks"],
                [
                    {
                        "type": "command",
                        "command": env.cmd("harness-guard.mjs"),
                        "timeout": 15,
                    }
                ],
            )
            audit = post[-1]
            self.assertEqual(audit["matcher"], "Bash|PowerShell")
            self.assertEqual(
                audit["hooks"],
                [
                    {
                        "type": "command",
                        "command": env.cmd("fleet-audit.mjs"),
                        "timeout": 15,
                    }
                ],
            )
            # unfiltered: the live check showed `if` globs miss 8.3 short names
            self.assertNotIn("if", guard["hooks"][0])
            self.assertNotIn("if", audit["hooks"][0])
            self.assertIn("added", out)

    def test_other_content_and_key_order_kept(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td)
            env.run()
            cfg = env.load()
            self.assertEqual(list(cfg), list(EXISTING))
            self.assertEqual(list(cfg["hooks"]), list(EXISTING["hooks"]))  # type: ignore[arg-type]
            cfg["hooks"]["PreToolUse"].pop()
            cfg["hooks"]["PostToolUse"].pop()
            self.assertEqual(cfg, EXISTING)

    def test_idempotent_second_run_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td)
            env.run()
            first = env.settings.read_bytes()
            backups = env.backups()
            code, out, _ = env.run()
            self.assertEqual(code, 0)
            self.assertEqual(env.settings.read_bytes(), first)
            self.assertEqual(env.backups(), backups, "no backup when nothing changes")
            self.assertIn("already registered", out)
            cfg = env.load()
            self.assertEqual(
                commands(cfg, "PreToolUse").count(env.cmd("harness-guard.mjs")), 1
            )
            self.assertEqual(
                commands(cfg, "PostToolUse").count(env.cmd("fleet-audit.mjs")), 1
            )

    def test_existing_entry_in_other_style_not_duplicated(self) -> None:
        data = json.loads(json.dumps(EXISTING))
        data["hooks"]["PreToolUse"].append(
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "node ~/.claude/hooks/harness-guard.mjs",
                    }
                ],
            }
        )
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td, data)
            code, out, _ = env.run()
            self.assertEqual(code, 0)
            cfg = env.load()
            guards = [
                c for c in commands(cfg, "PreToolUse") if "harness-guard.mjs" in c
            ]
            self.assertEqual(guards, ["node ~/.claude/hooks/harness-guard.mjs"])
            self.assertIn("harness-guard.mjs already registered", out)
            self.assertIn(env.cmd("fleet-audit.mjs"), commands(cfg, "PostToolUse"))

    def test_backup_holds_original_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td)
            original = env.settings.read_bytes()
            env.run()
            backups = env.backups()
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)

    def test_dry_run_prints_diff_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td)
            original = env.settings.read_bytes()
            code, out, _ = env.run("--dry-run")
            self.assertEqual(code, 0)
            self.assertEqual(env.settings.read_bytes(), original)
            self.assertEqual(env.backups(), [])
            self.assertIn("--- ", out)
            self.assertIn("+++ ", out)
            self.assertIn("@@", out)
            added = [
                ln
                for ln in out.splitlines()
                if ln.startswith("+") and not ln.startswith("+++")
            ]
            self.assertTrue(any("harness-guard.mjs" in ln for ln in added), out)
            self.assertTrue(any("fleet-audit.mjs" in ln for ln in added), out)

    def test_dry_run_when_registered_shows_no_diff(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td)
            env.run()
            code, out, _ = env.run("--dry-run")
            self.assertEqual(code, 0)
            self.assertNotIn("@@", out)
            self.assertIn("already registered", out)

    def test_crlf_and_four_space_indent_kept(self) -> None:
        text = json.dumps(EXISTING, indent=4).replace("\n", "\r\n") + "\r\n"
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td, text=text)
            env.run()
            raw = env.settings.read_bytes()
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
            self.assertTrue(raw.endswith(b"}\r\n"))
            self.assertIn(b'\r\n    "env": {', raw)

    def test_unchanged_regions_round_trip_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td)
            env.run()
            cfg = env.load()
            cfg["hooks"]["PreToolUse"].pop()
            cfg["hooks"]["PostToolUse"].pop()
            self.assertEqual(
                json.dumps(cfg, indent=2) + "\n",
                json.dumps(EXISTING, indent=2) + "\n",
            )

    def test_non_ascii_kept_unescaped(self) -> None:
        data = json.loads(json.dumps(EXISTING))
        data["env"]["NOTE"] = "caf\u00e9"
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td, text=text)
            env.run()
            self.assertIn("caf\u00e9", env.settings.read_text(encoding="utf-8"))

    def test_missing_settings_file_created(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td, data=None)
            self.assertFalse(env.settings.exists())
            code, _, err = env.run()
            self.assertEqual(code, 0, err)
            cfg = env.load()
            self.assertEqual(list(cfg), ["hooks"])
            self.assertEqual(
                commands(cfg, "PreToolUse"), [env.cmd("harness-guard.mjs")]
            )
            self.assertEqual(commands(cfg, "PostToolUse"), [env.cmd("fleet-audit.mjs")])
            self.assertEqual(env.backups(), [])

    def test_hooks_key_missing_added_last(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td, {"model": "opus", "env": {}})
            env.run()
            self.assertEqual(list(env.load()), ["model", "env", "hooks"])


class StopTrigger(unittest.TestCase):
    def test_stop_hook_never_added(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td)
            env.run()
            self.assertEqual(env.load()["hooks"]["Stop"], EXISTING["hooks"]["Stop"])  # type: ignore[index]

    def test_note_when_stop_hook_missing(self) -> None:
        data = json.loads(json.dumps(EXISTING))
        del data["hooks"]["Stop"]
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td, data)
            code, out, _ = env.run()
            self.assertEqual(code, 0)
            self.assertNotIn("Stop", env.load()["hooks"])
            self.assertIn("auto-handoff-stop.mjs", out)
            self.assertIn("collect", out)


class Refusals(unittest.TestCase):
    def assert_refused(
        self, env: Env, code: int, err: str, needle: str, before: bytes | None
    ) -> None:
        self.assertEqual(code, 1)
        self.assertIn(needle, err)
        if before is None:
            self.assertFalse(env.settings.exists())
        else:
            self.assertEqual(env.settings.read_bytes(), before)
        self.assertEqual(env.backups(), [])

    def test_node_missing_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td)
            before = env.settings.read_bytes()
            code, _, err = env.run(node=None)
            self.assert_refused(env, code, err, "Node", before)

    def test_node_older_than_18_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td)
            before = env.settings.read_bytes()
            code, _, err = env.run(node=16)
            self.assert_refused(env, code, err, "18", before)

    def test_node_18_accepted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td)
            code, _, err = env.run(node=18)
            self.assertEqual(code, 0, err)

    def test_invalid_json_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td, text='{"hooks": [}\n')
            code, _, err = env.run()
            self.assert_refused(env, code, err, "not valid JSON", b'{"hooks": [}\n')

    def test_wrong_shape_refused(self) -> None:
        text = '{"hooks": {"PreToolUse": {"matcher": "x"}}}\n'
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td, text=text)
            code, _, err = env.run()
            self.assert_refused(env, code, err, "PreToolUse", text.encode())

    def test_missing_hook_file_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td)
            (env.hooks / "fleet-audit.mjs").unlink()
            before = env.settings.read_bytes()
            code, _, err = env.run()
            self.assert_refused(env, code, err, "sync_global.py --apply", before)


class NodeMajor(unittest.TestCase):
    def test_parses_version(self) -> None:
        done = subprocess.CompletedProcess(["node"], 0, stdout="v18.19.1\n", stderr="")
        with mock.patch.object(_reg.subprocess, "run", return_value=done):
            self.assertEqual(_reg.node_major(), 18)

    def test_missing_binary_is_none(self) -> None:
        with mock.patch.object(_reg.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(_reg.node_major())

    def test_garbage_is_none(self) -> None:
        done = subprocess.CompletedProcess(["node"], 0, stdout="nope\n", stderr="")
        with mock.patch.object(_reg.subprocess, "run", return_value=done):
            self.assertIsNone(_reg.node_major())


class Cli(unittest.TestCase):
    def test_script_runs_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td)
            argv = [
                sys.executable,
                str(SCRIPT),
                "--settings",
                str(env.settings),
                "--hooks-dir",
                str(env.hooks),
            ]
            dry = subprocess.run(
                [*argv, "--dry-run"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertIn("harness-guard.mjs", dry.stdout)
            done = subprocess.run(
                argv, capture_output=True, text=True, timeout=60, check=False
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn(
                env.cmd("harness-guard.mjs"), commands(env.load(), "PreToolUse")
            )

    def test_relative_hooks_dir_written_absolute(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            env = Env(td)
            argv = [
                sys.executable,
                str(SCRIPT),
                "--settings",
                "settings.json",
                "--hooks-dir",
                "hooks",
            ]
            done = subprocess.run(
                argv,
                cwd=env.root,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            guard = [
                c for c in commands(env.load(), "PreToolUse") if "harness-guard" in c
            ]
            self.assertEqual(len(guard), 1)
            written = Path(guard[0].removeprefix('node "').removesuffix('"'))
            self.assertTrue(written.is_absolute(), written)
            self.assertNotIn("\\", guard[0])
            self.assertTrue(written.samefile(env.hooks / "harness-guard.mjs"))

    def test_default_hooks_dir_is_home_claude_hooks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ccv47-reg-") as td:
            home = Path(td)
            with mock.patch.object(_reg.Path, "home", return_value=home):
                args = _reg.parse_args([])
            self.assertEqual(Path(args.settings), home / ".claude" / "settings.json")
            self.assertEqual(Path(args.hooks_dir), home / ".claude" / "hooks")


if __name__ == "__main__":
    unittest.main()
