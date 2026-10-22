"""Tests for permissions.py (denials scan, grant queue) and fleet.py denials/grant.
Run from the repo root:  py -3.13 -m pytest -q tools/fleet
Every test uses a temp HOME/USERPROFILE; the real ~/.claude is never touched.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.fleet import fleet, permissions

CLASSIFIER = (
    "Permission for this action was denied by the Claude Code auto mode classifier. "
    "Reason: [Self-Modification]. If you have other tasks..."
)
REJECTED = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected"
)


def _use(uid, name, command, ts="2026-10-20T10:00:00Z"):
    return {
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": uid,
                    "name": name,
                    "input": {"command": command},
                }
            ],
        },
    }


def _result(uid, text, ts="2026-10-20T10:00:01Z"):
    return {
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": uid,
                    "is_error": True,
                    "content": text,
                }
            ],
        },
    }


class Home(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        env = mock.patch.dict(
            os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        )
        env.start()
        self.addCleanup(env.stop)
        self.claude = self.home / ".claude"
        self.claude.mkdir()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fleet.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class DenialScan(Home):
    def write_transcript(self, slug, name, entries):
        d = self.claude / "projects" / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(
            "\n".join(json.dumps(e) for e in entries), encoding="utf-8"
        )

    def test_classifier_denial_found_rejection_skipped(self):
        self.write_transcript(
            "C--Users-x-Documents-amber",
            "aaaaaaaa-1.jsonl",
            [
                _use("t1", "Bash", "git push origin main"),
                _result("t1", CLASSIFIER),
                _use("t2", "Bash", "ls"),
                _result("t2", REJECTED),
            ],
        )
        rows = permissions.scan()
        self.assertEqual(len(rows), 1)
        d = rows[0]
        self.assertEqual(
            (d.project, d.tool, d.reason), ("amber", "Bash", "[Self-Modification]")
        )
        self.assertEqual(d.command, "git push origin main")

    def test_subagent_transcripts_and_project_filter(self):
        self.write_transcript(
            "C--Users-x-Documents-amber/sess/subagents",
            "agent-1.jsonl",
            [
                _use("s1", "PowerShell", "Stop-ScheduledTask x"),
                _result("s1", CLASSIFIER),
            ],
        )
        self.write_transcript(
            "C--Users-x-Documents-other",
            "b.jsonl",
            [_use("o1", "Bash", "git push"), _result("o1", CLASSIFIER)],
        )
        self.assertEqual(len(permissions.scan()), 2)
        self.assertEqual([d.project for d in permissions.scan("amber")], ["amber"])

    def test_since_drops_old_rows(self):
        self.write_transcript(
            "C--Users-x-Documents-amber",
            "a.jsonl",
            [
                _use("t1", "Bash", "old", ts="2020-01-01T00:00:00Z"),
                _result("t1", CLASSIFIER, ts="2020-01-01T00:00:01Z"),
            ],
        )
        self.assertEqual(permissions.scan(since_days=30), [])
        self.assertEqual(len(permissions.scan()), 1)

    def test_report_masks_secrets(self):
        self.write_transcript(
            "C--Users-x-Documents-amber",
            "a.jsonl",
            [
                _use(
                    "t1", "Bash", "curl 'https://x/?api_key=sk_live_abcdefghijklmnop'"
                ),
                _result("t1", CLASSIFIER),
            ],
        )
        code, out, _ = self.cli("denials")
        self.assertEqual(code, 0)
        self.assertIn("amber (1)", out)
        self.assertNotIn("sk_live_abcdefghijklmnop", out)
        code, out, _ = self.cli("denials", "--json")
        self.assertNotIn("sk_live_abcdefghijklmnop", out)
        self.assertEqual(json.loads(out)[0]["reason"], "[Self-Modification]")


class RuleCheck(unittest.TestCase):
    def test_accepts_exact_rules(self):
        for rule in (
            "Bash(git push)",
            "Bash(py -3.13 install/sync_global.py --apply)",
            "Bash(py -3.13 tools/fleet/fleet.py handoffs --fresh *)",
            "mcp__render__list_services",
            "Edit(**/install/settings.template.json)",
            "Edit(**/.claude/settings.local.json)",
            'PowerShell(Start-ScheduledTask "Amber Gateway watchdog")',
        ):
            self.assertEqual(permissions.check_rule(f"  {rule} "), rule)

    def test_refuses_blanket_interpreter_and_escalation(self):
        for rule in (
            "Bash(*)",
            "Bash(* --push)",
            "Edit(**)",
            "Bash(py *)",
            "Bash(python -c *)",
            "Bash(node -e *)",
            r"Bash(C:\Python\python.exe *)",
            "Bash(pwsh -Command *)",
            "Bash(git push --force)",
            "Edit(~/.claude/settings.json)",
            "Bash(py -3.13 ~/.claude/tools/fleet/fleet.py grant apply)",
            "Bash(rm -rf build)",
            "not a rule",
        ):
            with self.subTest(rule=rule), self.assertRaises(permissions.GrantError):
                permissions.check_rule(rule)


class GrantQueue(Home):
    def setUp(self):
        super().setUp()
        self.settings = self.claude / "settings.json"
        self.settings.write_text(
            json.dumps(
                {"permissions": {"defaultMode": "auto"}, "model": "opus"}, indent=2
            )
            + "\n",
            encoding="utf-8",
        )

    def test_add_list_apply_roundtrip(self):
        code, out, _ = self.cli(
            "grant",
            "add",
            "Bash(git push)",
            "--reason",
            "push denied",
            "--project",
            "p",
        )
        self.assertEqual(code, 0)
        self.assertIn("queued g-", out)
        self.assertIn("grant apply", out)
        code, out, _ = self.cli("grant", "add", "Bash(git push)", "--reason", "again")
        self.assertIn("already queued", out)
        self.cli("grant", "add", "--note", "may restart task X", "--reason", "restart")
        code, out, _ = self.cli("grant", "list")
        self.assertEqual(out.count("pending"), 2)

        code, out, _ = self.cli("grant", "apply", "--dry-run")
        self.assertIn("would add", out)
        self.assertNotIn("allow", self.settings.read_text(encoding="utf-8"))

        code, out, _ = self.cli("grant", "apply")
        self.assertEqual(code, 0, out)
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["permissions"]["allow"], ["Bash(git push)"])
        self.assertEqual(data["permissions"]["defaultMode"], "auto")
        self.assertEqual(data["model"], "opus")
        self.assertTrue(
            data["autoMode"]["environment"][0].endswith("may restart task X")
        )
        self.assertEqual(len(list(self.claude.glob("settings.json.bak-*-grants"))), 1)
        self.assertEqual(self.cli("grant", "apply")[1].strip(), "no pending grants")

    def test_refused_rule_exit_1_nothing_queued(self):
        code, _, err = self.cli("grant", "add", "Bash(*)", "--reason", "x")
        self.assertEqual(code, 1)
        self.assertIn("refused", err)
        self.assertFalse(permissions.grants_path().exists())

    def test_drop(self):
        out = self.cli("grant", "add", "Bash(git push)", "--reason", "x")[1]
        gid = out.split()[1]
        self.assertEqual(self.cli("grant", "drop", gid)[1].strip(), f"dropped {gid}")
        self.assertEqual(self.cli("grant", "list")[1].strip(), "no pending grants")
        self.assertEqual(self.cli("grant", "drop", gid)[0], 1)

    def test_apply_rechecks_a_tampered_queue(self):
        self.cli("grant", "add", "Bash(git push)", "--reason", "x")
        data = json.loads(permissions.grants_path().read_text(encoding="utf-8"))
        data["grants"][0]["value"] = "Bash(*)"
        permissions.grants_path().write_text(json.dumps(data), encoding="utf-8")
        code, _, _ = self.cli("grant", "apply")
        self.assertEqual(code, 1)
        self.assertNotIn("allow", self.settings.read_text(encoding="utf-8"))

    def test_crlf_settings_keep_crlf(self):
        self.settings.write_bytes(b'{\r\n  "model": "opus"\r\n}\r\n')
        self.cli("grant", "add", "Bash(git push)", "--reason", "x")
        self.cli("grant", "apply")
        raw = self.settings.read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

    def test_report_shows_pending_grants(self):
        from tools.fleet.model import FleetState

        self.assertNotIn("grants:", fleet.report(FleetState(), []))
        self.cli("grant", "add", "Bash(git push)", "--reason", "x")
        text = fleet.report(FleetState(), [])
        self.assertIn("grants: 1 pending, user runs: ", text)
        self.assertIn("grant apply", text)
        permissions.grants_path().write_text("not json", encoding="utf-8")
        self.assertEqual(permissions.pending_count(), 0)


if __name__ == "__main__":
    unittest.main()
