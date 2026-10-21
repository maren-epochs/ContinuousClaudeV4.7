"""Tests for the fleet.py inbox/show/apply/reject/lessons/dashboard commands and report.

Run from the repo root:  py -3.13 -m pytest -q tools/fleet
Every test runs on a temp harness repo plus a temp HOME/USERPROFILE (``~/.claude``
with a manifest and inbox); the real ~/.claude and this repo are never touched.
"""

import contextlib
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools._testing import home_path
from tools.fleet import digest, fleet, lessons, model
from tools.fleet.model import (
    Alert,
    AuditEvent,
    Collision,
    DriftEntry,
    FleetState,
    Handoff,
    Harness,
    Machine,
    Proposal,
    Session,
)

FLEET_PY = Path(REPO_ROOT) / "tools" / "fleet" / "fleet.py"
HOOK_REL = ".claude/hooks/status.mjs"
HOOK_TEXT = "const a = 1;\nconst b = 2;\nexport default a + b;\n"
LESSON = (
    "Ask for decisions with lettered options A to D and mark the recommended "
    "option first, with a one line reason for the recommendation."
)


class CliHome(unittest.TestCase):
    """Temp HOME (with ~/.claude, manifest, inbox) and a temp harness repo."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.claude = self.home / ".claude"
        self.repo = root / "repo"
        (self.repo / ".git").mkdir(parents=True)
        (self.claude / "harness-inbox").mkdir(parents=True)
        env = mock.patch.dict(
            os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        )
        env.start()
        self.addCleanup(env.stop)
        self.repo_file = self.repo / HOOK_REL
        self.repo_file.parent.mkdir(parents=True)
        self.repo_file.write_bytes(HOOK_TEXT.encode())
        self.installed = self.claude / "hooks" / "status.mjs"
        self.installed.parent.mkdir(parents=True)
        self.installed.write_bytes(HOOK_TEXT.encode())
        self.manifest(
            {"hooks/status.mjs": {"repo_path": HOOK_REL, "sha256": None, "kept": False}}
        )

    def manifest(self, files):
        data = {"schema_version": 1, "repo": str(self.repo), "files": files}
        model.manifest_path().write_text(json.dumps(data), encoding="utf-8")

    def proposal(self, pid="20261007T120000Z-0000aaaa", change=None, **fields):
        data = {
            "schema_version": 1,
            "id": pid,
            "created_at": "2026-10-07T12:00:00Z",
            "kind": "edit",
            "source": {"project": "project-A", "session_id": "sess-a1", "cwd": None},
            "target": {
                "installed_path": str(self.installed),
                "repo_path": HOOK_REL,
                "repo": str(self.repo),
            },
            "change": change
            or {"tool": "Edit", "old_string": "b = 2", "new_string": "b = 3"},
            "reason": "redirected harness write",
            "status": "pending",
        }
        data.update(fields)
        data = {k: v for k, v in data.items() if v is not None or k == "id"}
        path = self.claude / "harness-inbox" / f"{pid}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = fleet.main(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def stored(self, path):
        return json.loads(path.read_text(encoding="utf-8"))


class ApplyEditTests(CliHome):
    def test_edit_applies_to_repo_file_and_marks_applied(self):
        path = self.proposal()
        code, out, err = self.run_main("apply", "20261007T120000Z-0000aaaa")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            self.repo_file.read_text(), HOOK_TEXT.replace("b = 2", "b = 3")
        )
        self.assertEqual(self.installed.read_text(), HOOK_TEXT)
        data = self.stored(path)
        self.assertEqual(data["status"], "applied")
        self.assertEqual(data["source"]["project"], "project-A")
        self.assertEqual(data["target"]["repo"], str(self.repo))
        self.assertIn("sync_global.py --apply", out)
        self.assertIn("not committed", out)

    def test_write_replaces_content(self):
        self.proposal(change={"tool": "Write", "content": "export default 7;\n"})
        code, _, err = self.run_main("apply", "20261007T120000Z-0000aaaa")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.repo_file.read_bytes(), b"export default 7;\n")
        self.assertEqual(self.installed.read_text(), HOOK_TEXT)

    def test_write_creates_missing_repo_file(self):
        self.repo_file.unlink()
        self.proposal(change={"tool": "Write", "content": "new\n"})
        code, _, err = self.run_main("apply", "20261007T120000Z-0000aaaa")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.repo_file.read_text(), "new\n")

    def test_old_string_not_found_refuses(self):
        path = self.proposal(
            change={"tool": "Edit", "old_string": "zzz", "new_string": "y"}
        )
        code, _, err = self.run_main("apply", "20261007T120000Z-0000aaaa")
        self.assertEqual(code, 1)
        self.assertIn("not found", err)
        self.assertEqual(self.repo_file.read_text(), HOOK_TEXT)
        self.assertEqual(self.stored(path)["status"], "pending")

    def test_ambiguous_old_string_needs_replace_all(self):
        change = {"tool": "Edit", "old_string": "const", "new_string": "let"}
        self.proposal(change=change)
        code, _, err = self.run_main("apply", "20261007T120000Z-0000aaaa")
        self.assertEqual(code, 1)
        self.assertIn("2 times", err)
        self.assertEqual(self.repo_file.read_text(), HOOK_TEXT)

        self.proposal(change={**change, "replace_all": True})
        code, _, err = self.run_main("apply", "20261007T120000Z-0000aaaa")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.repo_file.read_text(), HOOK_TEXT.replace("const", "let"))

    def test_crlf_repo_file_keeps_crlf(self):
        self.repo_file.write_bytes(HOOK_TEXT.replace("\n", "\r\n").encode())
        change = {
            "tool": "Edit",
            "old_string": "a = 1;\nconst b",
            "new_string": "a = 5;\nconst b",
        }
        self.proposal(change=change)
        code, _, err = self.run_main("apply", "20261007T120000Z-0000aaaa")
        self.assertEqual(code, 0, err)
        expected = HOOK_TEXT.replace("a = 1", "a = 5").replace("\n", "\r\n")
        self.assertEqual(self.repo_file.read_bytes(), expected.encode())

    def test_multiedit_is_all_or_nothing(self):
        edits = [
            {"old_string": "a = 1", "new_string": "a = 10"},
            {"old_string": "missing", "new_string": "x"},
        ]
        self.proposal(change={"tool": "MultiEdit", "edits": edits})
        code, _, err = self.run_main("apply", "20261007T120000Z-0000aaaa")
        self.assertEqual(code, 1)
        self.assertIn("edit 2", err)
        self.assertEqual(self.repo_file.read_text(), HOOK_TEXT)

        edits[1] = {"old_string": "a = 10", "new_string": "a = 11"}
        self.proposal(change={"tool": "MultiEdit", "edits": edits})
        code, _, err = self.run_main("apply", "20261007T120000Z-0000aaaa")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            self.repo_file.read_text(), HOOK_TEXT.replace("a = 1", "a = 11")
        )

    def test_notebook_replace_insert_delete(self):
        rel = "tools/demo.ipynb"
        nb = {
            "cells": [
                {
                    "cell_type": "code",
                    "id": "c1",
                    "metadata": {},
                    "source": ["x = 1\n"],
                    "outputs": [],
                    "execution_count": None,
                },
                {
                    "cell_type": "markdown",
                    "id": "c2",
                    "metadata": {},
                    "source": ["# t"],
                },
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
        nb_path = self.repo / rel
        nb_path.parent.mkdir(parents=True)
        nb_path.write_text(json.dumps(nb), encoding="utf-8")

        def run(notebook, content):
            change = {"tool": "NotebookEdit", "content": content, "notebook": notebook}
            target = {
                "installed_path": str(self.claude / "tools" / "demo.ipynb"),
                "repo_path": rel,
            }
            self.proposal(change=change, target=target)
            return self.run_main("apply", "20261007T120000Z-0000aaaa")

        code, _, err = run({"cell_id": "c1", "edit_mode": "replace"}, "x = 2\ny = 3")
        self.assertEqual(code, 0, err)
        cells = json.loads(nb_path.read_text("utf-8"))["cells"]
        self.assertEqual("".join(cells[0]["source"]), "x = 2\ny = 3")

        code, _, err = run(
            {"cell_id": "c1", "edit_mode": "insert", "cell_type": "markdown"}, "note"
        )
        self.assertEqual(code, 0, err)
        cells = json.loads(nb_path.read_text("utf-8"))["cells"]
        self.assertEqual(
            [c["cell_type"] for c in cells], ["code", "markdown", "markdown"]
        )
        self.assertEqual("".join(cells[1]["source"]), "note")

        code, _, err = run({"cell_id": "c2", "edit_mode": "delete"}, None)
        self.assertEqual(code, 0, err)
        cells = json.loads(nb_path.read_text("utf-8"))["cells"]
        self.assertNotIn("c2", [c.get("id") for c in cells])

        code, _, err = run({"cell_id": "nope", "edit_mode": "replace"}, "z")
        self.assertEqual(code, 1)
        self.assertIn("cell", err)

    def test_dry_run_writes_nothing(self):
        path = self.proposal()
        code, out, err = self.run_main(
            "apply", "20261007T120000Z-0000aaaa", "--dry-run"
        )
        self.assertEqual(code, 0, err)
        self.assertIn("-const b = 2;", out)
        self.assertIn("+const b = 3;", out)
        self.assertEqual(self.repo_file.read_text(), HOOK_TEXT)
        self.assertEqual(self.stored(path)["status"], "pending")

    def test_repo_path_from_manifest_when_proposal_lacks_it(self):
        self.proposal(target={"installed_path": str(self.installed)})
        code, _, err = self.run_main("apply", "20261007T120000Z-0000aaaa")
        self.assertEqual(code, 0, err)
        self.assertIn("b = 3", self.repo_file.read_text())

    def test_repo_override_flag(self):
        other = Path(self._tmp.name) / "other"
        (other / ".git").mkdir(parents=True)
        (other / HOOK_REL).parent.mkdir(parents=True)
        (other / HOOK_REL).write_text(HOOK_TEXT, encoding="utf-8")
        self.proposal()
        code, _, err = self.run_main(
            "apply", "20261007T120000Z-0000aaaa", "--repo", str(other)
        )
        self.assertEqual(code, 0, err)
        self.assertIn("b = 3", (other / HOOK_REL).read_text())
        self.assertEqual(self.repo_file.read_text(), HOOK_TEXT)

    def test_never_commits(self):
        if not shutil.which("git"):
            self.skipTest("git not on PATH")
        shutil.rmtree(self.repo / ".git")
        git = ["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "init"], check=True)
        self.proposal()
        code, _, err = self.run_main("apply", "20261007T120000Z-0000aaaa")
        self.assertEqual(code, 0, err)
        log = subprocess.run(
            [*git, "rev-list", "--count", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(log.stdout.strip(), "1")
        status = subprocess.run(
            [*git, "status", "--porcelain"], capture_output=True, text=True, check=True
        )
        self.assertIn("status.mjs", status.stdout)


class ApplyRefusalTests(CliHome):
    def assert_refused(self, pid="20261007T120000Z-0000aaaa", needle="", *extra):
        code, _, err = self.run_main("apply", pid, *extra)
        self.assertEqual(code, 1, err)
        self.assertIn(needle, err)
        self.assertEqual(self.repo_file.read_text(), HOOK_TEXT)
        self.assertEqual(self.installed.read_text(), HOOK_TEXT)
        return err

    def test_unknown_kind_and_status(self):
        self.proposal(kind="patch")
        self.assert_refused(needle="kind")
        self.proposal(kind=5)
        self.assert_refused(needle="kind")
        self.proposal(status="weird")
        self.assert_refused(needle="status")

    def test_not_pending(self):
        for status in ("applied", "rejected"):
            self.proposal(status=status)
            self.assert_refused(needle=status)

    def test_stored_id_differs_from_filename(self):
        path = self.proposal()
        data = self.stored(path)
        data["id"] = "20261007T120000Z-ffffffff"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assert_refused(needle="id")

    def test_unsafe_or_missing_id(self):
        self.assert_refused("..", "id")
        self.assert_refused("CON", "id")
        self.assert_refused("20261007T120000Z-00000000", "no proposal")

    def test_shell_proposals_are_not_applied(self):
        self.proposal(change={"tool": "Bash", "command": "cp a ~/.claude/hooks/x"})
        self.assert_refused(needle="Bash")

    def test_repo_path_escaping_the_repo(self):
        outside = Path(self._tmp.name) / "outside.mjs"
        outside.write_text(HOOK_TEXT, encoding="utf-8")
        for rel in ("../outside.mjs", str(outside), "/etc/x", "C:/x"):
            self.proposal(
                target={"installed_path": str(self.installed), "repo_path": rel}
            )
            self.assert_refused(needle="repo_path")
        self.assertEqual(outside.read_text(), HOOK_TEXT)

    def test_target_inside_claude_dir_is_refused(self):
        # harness repo == HOME: the repo file would be ~/.claude/... (installed copy)
        (self.home / ".git").mkdir()
        rel = ".claude/hooks/status.mjs"
        self.proposal(target={"installed_path": str(self.installed), "repo_path": rel})
        self.assert_refused(
            "20261007T120000Z-0000aaaa", "installed", "--repo", str(self.home)
        )

    def test_key_file_target_is_refused(self):
        key = self.repo / "sessions" / "1.abc.key"
        key.parent.mkdir()
        key.write_text("SECRET", encoding="utf-8")
        self.proposal(
            target={"installed_path": "x", "repo_path": "sessions/1.abc.key"},
            change={"tool": "Edit", "old_string": "SECRET", "new_string": "x"},
        )
        self.assert_refused(needle=".key")
        self.assertEqual(key.read_text(), "SECRET")

    def test_repo_without_git_is_refused(self):
        shutil.rmtree(self.repo / ".git")
        self.proposal()
        self.assert_refused(needle="git")

    def test_no_repo_known(self):
        model.manifest_path().unlink()
        self.proposal(
            target={"installed_path": str(self.installed), "repo_path": HOOK_REL}
        )
        self.assert_refused(needle="repo")

    def test_manifest_and_proposal_repo_path_disagree(self):
        self.proposal(
            target={
                "installed_path": str(self.installed),
                "repo_path": "harness/skills/x/SKILL.md",
            }
        )
        self.assert_refused(needle="manifest")


class ApplyLessonTests(CliHome):
    def lesson(self, **fields):
        return self.proposal(
            kind="lesson",
            target={"installed_path": "CLAUDE.md", "repo_path": None},
            change={"tool": "lesson", "content": LESSON},
            content_hash="ab" * 32,
            **fields,
        )

    def test_appends_to_chosen_repo_doc(self):
        doc = self.repo / "CLAUDE.md"
        doc.write_bytes(b"# Rules\r\n\r\n- one\r\n")
        path = self.lesson()
        code, out, err = self.run_main(
            "apply", "20261007T120000Z-0000aaaa", "--doc", "CLAUDE.md"
        )
        self.assertEqual(code, 0, err)
        text = doc.read_bytes().decode()
        self.assertTrue(text.startswith("# Rules\r\n\r\n- one\r\n"))
        self.assertIn(LESSON, text)
        self.assertNotIn("\n", text.replace("\r\n", ""))  # CRLF kept, no bare LF
        self.assertTrue(text.endswith(LESSON + "\r\n"))
        data = self.stored(path)
        self.assertEqual(data["status"], "applied")
        self.assertEqual(data["content_hash"], "ab" * 32)
        self.assertIn("sync_global.py --apply", out)

    def test_requires_doc(self):
        self.lesson()
        code, _, err = self.run_main("apply", "20261007T120000Z-0000aaaa")
        self.assertEqual(code, 1)
        self.assertIn("--doc", err)

    def test_doc_outside_repo_or_in_claude_dir_refused(self):
        (self.claude / "CLAUDE.md").write_text("# global\n", encoding="utf-8")
        outside = Path(self._tmp.name) / "notes.md"
        outside.write_text("x\n", encoding="utf-8")
        self.lesson()
        for doc in (str(outside), str(self.claude / "CLAUDE.md"), "../notes.md"):
            code, _, _ = self.run_main(
                "apply", "20261007T120000Z-0000aaaa", "--doc", doc
            )
            self.assertEqual(code, 1, doc)
        self.assertEqual((self.claude / "CLAUDE.md").read_text(), "# global\n")
        self.assertEqual(outside.read_text(), "x\n")

    def test_missing_doc_refused(self):
        self.lesson()
        code, _, err = self.run_main(
            "apply", "20261007T120000Z-0000aaaa", "--doc", "NOPE.md"
        )
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)


class RejectTests(CliHome):
    def test_reject_sets_status_in_place(self):
        path = self.proposal(content_hash="cd" * 32)
        code, _, err = self.run_main("reject", "20261007T120000Z-0000aaaa")
        self.assertEqual(code, 0, err)
        self.assertTrue(path.exists())
        data = self.stored(path)
        self.assertEqual(data["status"], "rejected")
        self.assertEqual(data["content_hash"], "cd" * 32)
        self.assertEqual(data["id"], "20261007T120000Z-0000aaaa")
        self.assertEqual(data["change"]["old_string"], "b = 2")
        self.assertEqual(self.repo_file.read_text(), HOOK_TEXT)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_rejected_lesson_blocks_reproposal(self):
        path = self.proposal(
            kind="lesson",
            change={"tool": "lesson", "content": LESSON},
            content_hash=lessons.content_hash(LESSON),
        )
        self.run_main("reject", "20261007T120000Z-0000aaaa")
        self.assertIn(lessons.content_hash(LESSON), lessons.seen_hashes(path.parent))

    def test_reject_applied_is_allowed_rejected_twice_is_not(self):
        path = self.proposal(status="applied")
        code, _, err = self.run_main("reject", "20261007T120000Z-0000aaaa")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored(path)["status"], "rejected")
        code, _, err = self.run_main("reject", "20261007T120000Z-0000aaaa")
        self.assertEqual(code, 1)
        self.assertIn("already rejected", err)

    def test_reject_validates_kind_status_and_id(self):
        path = self.proposal(kind="bogus")
        code, _, _ = self.run_main("reject", "20261007T120000Z-0000aaaa")
        self.assertEqual(code, 1)
        self.assertEqual(self.stored(path)["kind"], "bogus")
        self.assertEqual(self.stored(path)["status"], "pending")
        self.proposal(status="done")
        self.assertEqual(self.run_main("reject", "20261007T120000Z-0000aaaa")[0], 1)
        data = self.stored(self.proposal())
        data["id"] = "other-id"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.run_main("reject", "20261007T120000Z-0000aaaa")[0], 1)
        self.assertEqual(self.run_main("reject", "../x")[0], 1)


class InboxShowTests(CliHome):
    def test_inbox_lists_pending_by_default(self):
        self.proposal("20261007T120000Z-0000aaaa")
        self.proposal("20261007T120001Z-0000bbbb", status="rejected")
        self.proposal(
            "20261007T120002Z-0000cccc",
            kind="lesson",
            change={"tool": "lesson", "content": LESSON},
        )
        code, out, err = self.run_main("inbox")
        self.assertEqual(code, 0, err)
        self.assertIn("0000aaaa", out)
        self.assertIn("0000cccc", out)
        self.assertNotIn("0000bbbb", out)
        self.assertIn(HOOK_REL, out)
        code, out, _ = self.run_main("inbox", "--all")
        self.assertIn("0000bbbb", out)
        self.assertIn("rejected", out)

    def test_inbox_empty(self):
        code, out, _ = self.run_main("inbox")
        self.assertEqual(code, 0)
        self.assertIn("no pending proposals", out)

    def test_show_prints_change(self):
        self.proposal(
            change={
                "tool": "Edit",
                "old_string": "b = 2",
                "new_string": "b = 3",
                "replace_all": False,
            }
        )
        code, out, err = self.run_main("show", "20261007T120000Z-0000aaaa")
        self.assertEqual(code, 0, err)
        for needle in (
            "Edit",
            "b = 2",
            "b = 3",
            HOOK_REL,
            "project-A",
            "pending",
            "redirected harness write",
            "replace_all",
        ):
            self.assertIn(needle, out)

    def test_show_refuses_mismatch_and_missing(self):
        path = self.proposal()
        data = self.stored(path)
        data["id"] = "other"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.run_main("show", "20261007T120000Z-0000aaaa")[0], 1)
        self.assertEqual(self.run_main("show", "nope")[0], 1)


class LessonsCommandTests(CliHome):
    def memory(self, name, body):
        mem = self.claude / "projects" / "slug-a" / "memory"
        mem.mkdir(parents=True, exist_ok=True)
        text = f"---\nname: {name}\ndescription: {name}\nmetadata:\n  type: feedback\n---\n{body}\n"
        (mem / f"{name}.md").write_text(text, encoding="utf-8")

    def bloks_json(self):
        rule = {
            "title": "Pin the bokeh release",
            "kind": "rule",
            "tags": [],
            "body": "Pin the plotting release when the dashboard layer breaks on upgrade.",
        }
        return json.dumps({"rules": [rule], "tastes": [], "project_cards": []})

    def inbox(self):
        return [p for p in model.list_proposals() if p.kind == "lesson"]

    def test_default_is_memory_only(self):
        self.memory("ask-options", LESSON)
        runner = mock.Mock(side_effect=AssertionError("bloks must not run"))
        with mock.patch.object(lessons, "bloks_context", runner):
            code, out, err = self.run_main("lessons")
        self.assertEqual(code, 0, err)
        runner.assert_not_called()
        props = self.inbox()
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0].extra["origin"], "memory")
        self.assertIn(props[0].id, out)

    def test_bloks_and_all_sources(self):
        self.memory("ask-options", LESSON)
        with mock.patch.object(
            lessons, "bloks_context", return_value=self.bloks_json()
        ):
            code, _, err = self.run_main("lessons", "--source", "bloks")
        self.assertEqual(code, 0, err)
        self.assertEqual([p.extra["origin"] for p in self.inbox()], ["bloks"])
        with mock.patch.object(
            lessons, "bloks_context", return_value=self.bloks_json()
        ):
            code, _, err = self.run_main("lessons", "--source", "all")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            sorted(p.extra["origin"] for p in self.inbox()), ["bloks", "memory"]
        )

    def test_max_caps_proposals_per_run(self):
        bodies = [
            "Run the formatter before every commit so the hook never rewrites staged files.",
            "Prefer table output for data heavy answers and keep prose for explanations.",
            "Never pipe test output through tail because pipes mask the exit code value.",
        ]
        for i, body in enumerate(bodies):
            self.memory(f"m{i}", body)
        code, _, err = self.run_main("lessons", "--max", "2")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.inbox()), 2)
        code, _, _ = self.run_main("lessons", "--max", "2")
        self.assertEqual(len(self.inbox()), 3)

    def test_default_cap_is_ten(self):
        self.assertEqual(fleet.LESSONS_MAX, 10)
        words = (
            "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima"
        )
        for i in range(12):
            body = f"Lesson {words.split()[i]} says keep {words.split()[i]} steps short and clear."
            self.memory(f"m{i}", body)
        code, _, err = self.run_main("lessons")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.inbox()), 10)

    def test_dry_run_writes_nothing(self):
        self.memory("ask-options", LESSON)
        code, out, err = self.run_main("lessons", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.inbox(), [])
        self.assertIn("would propose 1", out)

    def test_bad_args(self):
        self.assertEqual(self.run_main("lessons", "--source", "web")[0], 2)
        self.assertEqual(self.run_main("lessons", "--max", "0")[0], 2)


def _state():
    alive = Session(
        pid=1,
        session_id="sess-a1",
        project="project-A",
        status="busy",
        alive=True,
        model="claude-opus-5-5",
        context_pct=42.0,
        alerts=[
            Alert(
                kind="collision",
                severity="error",
                session="sess-a1",
                detail="2 sessions wrote x.py",
                evidence="t.jsonl:7",
            ),
            Alert(
                kind="stuck",
                severity="warn",
                session="sess-a1",
                detail="AskUserQuestion unanswered 25 min",
                evidence="t.jsonl:9",
            ),
        ],
    )
    other = Session(pid=2, session_id="sess-b1", project="project-B", alive=True)
    dead = Session(pid=3, session_id="sess-c1", project="project-C", alive=False)
    stale = DriftEntry(
        installed_path="m.json",
        status="stale",
        extra={"detail": "manifest stale: marker newer"},
    )
    return FleetState(
        generated_at="2026-10-07T12:00:00Z",
        harness=Harness(
            drift=[
                DriftEntry(
                    installed_path="hooks/status.mjs",
                    repo_path=HOOK_REL,
                    expected_sha="a" * 64,
                    actual_sha="b" * 64,
                    status="modified",
                ),
                stale,
            ]
        ),
        sessions=[alive, other, dead],
        collisions=[
            Collision(
                kind="path",
                target="work/x.py",
                sessions=["sess-a1", "sess-b1"],
                detail="both wrote x.py",
            )
        ],
        audit_recent=[
            AuditEvent(
                ts="2026-10-07T11:59:00Z",
                project="project-B",
                category="force-push",
                command="git push --force",
                tool="Bash",
            )
        ],
    )


class ReportTests(CliHome):
    def test_report_sections(self):
        pending = Proposal(
            id="20261007T120000Z-0000aaaa",
            kind="edit",
            reason="redirected harness write",
        )
        pending.target.repo_path = HOOK_REL
        pending.source.project = "project-A"
        text = fleet.report(_state(), [pending])
        self.assertIn("sessions: 3 (2 alive)", text)
        self.assertIn("project-A", text)
        self.assertIn("project-B", text)
        self.assertNotIn("project-C", text.split("alerts")[0].split("live sessions")[1])
        self.assertIn("1 not alive", text)
        for needle in (
            "2 sessions wrote x.py",
            "t.jsonl:7",
            "AskUserQuestion unanswered",
            "collisions (1)",
            "work/x.py",
            "sess-a1, sess-b1",
            "drift (2)",
            "modified",
            HOOK_REL,
            "manifest stale: marker newer",
            "inbox (1 pending)",
            "0000aaaa",
            "audit (1 recent)",
            "force-push",
            "git push --force",
        ):
            self.assertIn(needle, text)
        self.assertLess(text.index("[error]"), text.index("[warn]"))

    def test_report_sync_labels(self):
        cases = [
            ({"in_sync": True, "behind_files": 0}, "harness: in sync ("),
            (
                {"in_sync": False, "behind_files": 3},
                "harness: differs from repo (3 files)",
            ),
            (
                {"in_sync": False, "behind_files": 1},
                "harness: differs from repo (1 file)",
            ),
            ({"in_sync": False}, "harness: differs from repo ("),
            ({"in_sync": None, "behind_files": None}, "harness: unknown ("),
        ]
        for extra, needle in cases:
            with self.subTest(extra=extra):
                state = FleetState(harness=Harness(extra=extra))
                text = fleet.report(state, [])
                self.assertIn(needle, text)
                self.assertNotIn("behind HEAD", text)

    def test_report_machine_alerts(self):
        state = _state()
        state.machine_alerts = [
            Alert(
                kind="memory",
                severity="warn",
                detail="free memory 2.1 GB of 15.7 GB (below 2.4 GB)",
            )
        ]
        text = fleet.report(state, [])
        self.assertIn("alerts (3):", text)
        self.assertIn(
            "[warn] memory machine: free memory 2.1 GB of 15.7 GB (below 2.4 GB)", text
        )
        self.assertLess(text.index("[error]"), text.index("memory machine"))
        only = FleetState(machine_alerts=state.machine_alerts)
        self.assertIn("alerts (1):", fleet.report(only, []))

    def test_json_includes_machine_alerts(self):
        state = _state()
        state.machine_alerts = [Alert(kind="memory", severity="warn", detail="low")]
        model.save_state(state)
        code, out, err = self.run_main("json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["machine_alerts"][0]["kind"], "memory")
        self.assertIsNone(data["machine_alerts"][0]["session"])

    def test_report_empty_sections(self):
        text = fleet.report(FleetState(), [])
        self.assertIn("no live sessions", text)
        self.assertIn("alerts: none", text)
        self.assertIn("inbox: empty", text)

    def test_report_command_reads_inbox(self):
        model.save_state(_state())
        self.proposal()
        code, out, err = self.run_main("report")
        self.assertEqual(code, 0, err)
        self.assertIn("inbox (1 pending)", out)
        self.assertIn("0000aaaa", out)


class CollectErrorTests(CliHome):
    """fleet.py collect logs a failed collect to ~/.claude/fleet/collect.err."""

    def err_path(self):
        return model.fleet_dir() / "collect.err"

    def fail_with(self, exc):
        with mock.patch.object(fleet.collect, "collect", side_effect=exc):
            return self.run_main("collect")

    def test_collect_timeout_is_logged_and_exits_nonzero(self):
        model.save_state(_state())
        before = model.state_path().read_bytes()
        code, out, err = self.fail_with(
            fleet.collect.CollectTimeout("collect exceeded 20.0 s")
        )
        self.assertEqual(code, fleet.COLLECT_FAILED)
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", err)
        self.assertIn("CollectTimeout", err)
        lines = self.err_path().read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        ts, cls, msg = lines[0].split("\t")
        self.assertRegex(ts, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(cls, "CollectTimeout")
        self.assertEqual(msg, "collect exceeded 20.0 s")
        self.assertEqual(model.state_path().read_bytes(), before)

    def test_any_exception_is_logged_one_line_per_failure(self):
        self.fail_with(RuntimeError("first\nsecond line"))
        code, _, _ = self.fail_with(ValueError("again"))
        self.assertNotEqual(code, 0)
        lines = self.err_path().read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            [line.split("\t")[1] for line in lines], ["RuntimeError", "ValueError"]
        )
        self.assertEqual(lines[0].split("\t")[2], "first second line")

    def test_save_failure_is_logged(self):
        with mock.patch.object(model, "save_state", side_effect=OSError("disk full")):
            code, _, _ = self.run_main("collect")
        self.assertEqual(code, fleet.COLLECT_FAILED)
        self.assertIn("OSError\tdisk full", self.err_path().read_text(encoding="utf-8"))

    def test_paths_are_home_relative_or_redacted(self):
        home = str(self.home)
        msg = (
            f"cannot read {home}\\.claude\\projects\\x.jsonl and "
            f"{home.replace(os.sep, '/')}/.claude/sessions/1.json, "
            "also C:\\Windows\\Temp\\other.txt, \\\\server\\share\\f and /var/tmp/z"
        )
        self.fail_with(OSError(msg))
        line = self.err_path().read_text(encoding="utf-8")
        self.assertNotIn(home, line)
        self.assertNotIn(home.replace(os.sep, "/"), line)
        self.assertIn("~\\.claude\\projects\\x.jsonl", line)
        self.assertIn("~/.claude/sessions/1.json", line)
        for gone in ("Windows", "server", "/var/tmp"):
            self.assertNotIn(gone, line)
        self.assertEqual(line.count("<path>"), 3)

    def test_log_is_capped_by_dropping_the_oldest_half(self):
        path = self.err_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        old = "".join(f"old-{i:06d}\t{'x' * 100}\n" for i in range(3000))
        path.write_bytes(old.encode())
        self.assertGreater(path.stat().st_size, fleet.COLLECT_ERR_MAX)
        self.fail_with(RuntimeError("newest"))
        data = path.read_bytes()
        self.assertLessEqual(len(data), fleet.COLLECT_ERR_MAX // 2 + 200)
        self.assertGreater(len(data), fleet.COLLECT_ERR_MAX // 2 - 200)
        text = data.decode()
        self.assertTrue(text.startswith("old-"), text[:40])
        self.assertNotIn("old-000000", text)
        self.assertIn("old-002999", text)
        self.assertTrue(text.endswith("RuntimeError\tnewest\n"))

    def test_log_write_failure_still_exits_cleanly(self):
        with mock.patch.object(fleet, "_append_err", side_effect=OSError("ro")):
            code, _, err = self.fail_with(RuntimeError("boom"))
        self.assertEqual(code, fleet.COLLECT_FAILED)
        self.assertNotIn("Traceback", err)

    def test_success_writes_no_log(self):
        with mock.patch.object(fleet.collect, "collect", return_value=_state()):
            code, out, err = self.run_main("collect")
        self.assertEqual(code, 0, err)
        self.assertIn("collected 3 sessions", out)
        self.assertFalse(self.err_path().exists())


class HandoffsTests(CliHome):
    def setUp(self):
        super().setUp()
        self.proj = self.home / "proj"
        self.root = self.proj / "thoughts" / "shared" / "handoffs" / "general"
        self.root.mkdir(parents=True)
        self.other = self.home / "other"  # no thoughts/ -> ~/.claude/handoffs/other
        self.other.mkdir()
        model.save_state(
            FleetState(
                sessions=[
                    self.session("proj-1", "proj", self.proj, "sess-p1"),
                    self.session("other-2", "other", self.other, "sess-o2"),
                    self.session("proj-4", "proj", self.proj, "sess-p4"),
                    Session(
                        name="gone-3", project="proj", cwd=str(self.proj), alive=False
                    ),
                ]
            )
        )

    @staticmethod
    def session(name, project, cwd, sid):
        return Session(
            name=name, project=project, cwd=str(cwd), session_id=sid, alive=True
        )

    def transcript(self, cwd, sid, *records):
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
        path = self.claude / "projects" / slug / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
        )

    @staticmethod
    def write_record(file_path):
        tool = {"type": "tool_use", "name": "Write", "input": {"file_path": file_path}}
        return {"type": "assistant", "message": {"content": [tool]}}

    @staticmethod
    def shell_record(command, tool="Bash"):
        use = {"type": "tool_use", "name": tool, "input": {"command": command}}
        return {"type": "assistant", "message": {"content": [use]}}

    def handoff(self, root, name, mtime):
        root.mkdir(parents=True, exist_ok=True)
        path = root / name
        path.write_text("goal: x\n", encoding="utf-8")
        os.utime(path, (mtime, mtime))

    def test_baseline_without_since_prints_since_and_no_marks(self):
        self.handoff(self.root, "a.yaml", 1000)
        code, out, _ = self.run_main("handoffs")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("since: "))
        self.assertIn("proj-1", out)
        self.assertNotIn("gone-3", out)
        self.assertNotIn("landed", out)
        self.assertNotIn("waiting", out)

    def rows(self, out):
        return {line.split()[0]: line for line in out.splitlines()[3:-1]}

    def test_since_credits_only_the_session_that_wrote_the_file(self):
        self.handoff(self.root, "old.yaml", 1000)
        self.handoff(self.root, "new.md", 3000)
        self.handoff(self.claude / "handoffs" / "other", "h.yaml", 1500)
        self.transcript(
            self.proj, "sess-p1", self.write_record(str(self.root / "new.md"))
        )
        mention = {"type": "assistant", "message": {"content": "read new.md"}}
        self.transcript(self.proj, "sess-p4", mention)  # same root, did not write it
        code, out, _ = self.run_main("handoffs", "--since", "2000")
        self.assertEqual(code, 0)
        rows = self.rows(out)
        self.assertTrue(rows["proj-1"].endswith("landed"), out)
        self.assertTrue(rows["proj-4"].endswith("waiting"), out)
        self.assertTrue(rows["other-2"].endswith("waiting"), out)
        self.assertIn("1/3 landed", out)

    def test_new_file_without_transcript_is_shared(self):
        self.handoff(self.root, "new.md", 3000)
        code, out, _ = self.run_main("handoffs", "--since", "2000")
        self.assertEqual(code, 0)
        self.assertTrue(self.rows(out)["proj-1"].endswith("shared"), out)
        self.assertIn("0/3 landed", out)

    def test_handoff_by_ended_session_of_same_project_is_prior(self):
        self.handoff(self.root, "a.md", 3000)
        self.handoff(self.root, "b.md", 3500)
        self.transcript(self.proj, "sess-p1", {"type": "assistant", "message": {}})
        self.transcript(self.proj, "sess-p4", {"type": "assistant", "message": {}})
        # sess-old ended (not live) and wrote both; newest wins
        self.transcript(
            self.proj,
            "sess-old",
            self.write_record(str(self.root / "a.md")),
            self.write_record(str(self.root / "b.md")),
        )
        code, out, _ = self.run_main("handoffs", "--since", "2000")
        self.assertEqual(code, 0)
        rows = self.rows(out)
        self.assertTrue(rows["proj-1"].endswith("prior"), out)
        self.assertIn(fleet._when(3500), rows["proj-1"])
        self.assertIn("0/3 landed, 2 by an ended earlier session", out)

    def test_handoff_by_another_live_session_is_not_prior(self):
        self.handoff(self.root, "a.md", 3000)
        self.transcript(
            self.proj, "sess-p1", self.write_record(str(self.root / "a.md"))
        )
        self.transcript(self.proj, "sess-p4", {"type": "assistant", "message": {}})
        _, out, _ = self.run_main("handoffs", "--since", "2000")
        rows = self.rows(out)
        self.assertTrue(rows["proj-1"].endswith("landed"), out)
        self.assertTrue(rows["proj-4"].endswith("waiting"), out)

    def test_handoff_written_by_shell_heredoc_counts_reads_do_not(self):
        self.handoff(self.root, "a.yaml", 3000)
        heredoc = (
            "f=thoughts/shared/handoffs/general/a.yaml && cat > $f <<'EOF'\n"
            'goal: "x"\nEOF'
        )
        self.transcript(self.proj, "sess-old", self.shell_record(heredoc))
        read = "cat thoughts/shared/handoffs/general/a.yaml 2>/dev/null; ls 2>&1"
        self.transcript(self.proj, "sess-p1", self.shell_record(read))
        self.transcript(self.proj, "sess-p4", {"type": "assistant", "message": {}})
        _, out, _ = self.run_main("handoffs", "--since", "2000")
        rows = self.rows(out)
        self.assertTrue(rows["proj-1"].endswith("prior"), out)
        self.assertIn("0/3 landed, 2 by an ended earlier session", out)

    def test_handoff_written_by_powershell_cmdlet_is_landed(self):
        self.handoff(self.root, "b.md", 3000)
        write = "Set-Content -Path thoughts\\shared\\handoffs\\general\\b.md -Value $t"
        self.transcript(
            self.proj, "sess-p1", self.shell_record(write, tool="PowerShell")
        )
        self.transcript(self.proj, "sess-p4", {"type": "assistant", "message": {}})
        _, out, _ = self.run_main("handoffs", "--since", "2000")
        rows = self.rows(out)
        self.assertTrue(rows["proj-1"].endswith("landed"), out)
        self.assertTrue(rows["proj-4"].endswith("waiting"), out)

    def targets(self, command):
        record = json.dumps(self.shell_record(command))
        return fleet._shell_targets(record)

    def test_shell_write_targets(self):
        writes = {
            "cat > d/h.yaml <<'EOF'\nx\nEOF": "h.yaml",
            "f=d/h.yaml && cat > $f <<'EOF'\nx\nEOF": "h.yaml",
            "echo x >> 'd\\h.yaml'": "h.yaml",
            "echo x | tee -a d/h.yaml": "h.yaml",
            "Set-Content -Path d\\h.yaml -Value $t": "h.yaml",
            '$t | Out-File "d\\h.yaml"': "h.yaml",
        }
        for command, name in writes.items():
            self.assertEqual(self.targets(command), {name}, command)

    def test_reads_copies_and_commits_are_not_writes(self):
        reads = [
            "cat d/h.yaml",
            "cat d/h.yaml 2>/dev/null; ls 2>&1",
            "cat d/h.yaml > copy.txt",
            "git add d/h.yaml && git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\"",
            "echo 'a => d/h.yaml'",
        ]
        for command in reads:
            self.assertNotIn("h.yaml", self.targets(command), command)

    def test_name_must_match_whole_base_name(self):
        self.handoff(self.root, "a.yaml", 3000)
        self.transcript(
            self.proj,
            "sess-p1",
            self.shell_record("cat > d/data.yaml <<'EOF'\nx\nEOF"),
            self.write_record(str(self.root / "old_a.yaml")),
        )
        self.transcript(self.proj, "sess-p4", {"type": "assistant", "message": {}})
        _, out, _ = self.run_main("handoffs", "--since", "2000")
        self.assertTrue(self.rows(out)["proj-1"].endswith("waiting"), out)

    def test_since_accepts_iso_and_rejects_garbage(self):
        code, out, _ = self.run_main("handoffs", "--since", "2026-10-08T08:00:00")
        self.assertEqual(code, 0)
        self.assertIn("0/3 landed", out)
        code, _, err = self.run_main("handoffs", "--since", "soon")
        self.assertEqual(code, 2)
        self.assertIn("epoch seconds or ISO", err)

    def test_no_live_sessions(self):
        model.save_state(FleetState())
        code, out, _ = self.run_main("handoffs", "--since", "0")
        self.assertEqual(code, 0)
        self.assertIn("no live sessions", out)


class DashboardTests(CliHome):
    def test_missing_dashboard_module_exits_2(self):
        model.save_state(_state())
        with mock.patch.dict(sys.modules, {"tools.fleet.dashboard": None}):
            code, _, err = self.run_main("dashboard", str(self.home / "d.html"))
        self.assertEqual(code, 2)
        self.assertIn("dashboard.py", err)
        self.assertFalse((self.home / "d.html").exists())

    def test_dashboard_calls_write_page(self):
        model.save_state(_state())
        calls = []

        def write_page(state, path):
            calls.append((state, path))
            return path

        fake = types.ModuleType("tools.fleet.dashboard")
        fake.write_page = write_page
        with mock.patch.dict(sys.modules, {"tools.fleet.dashboard": fake}):
            code, out, err = self.run_main("dashboard", str(self.home / "d.html"))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(calls[0][0], FleetState)
        self.assertEqual(len(calls[0][0].sessions), 3)
        self.assertEqual(Path(calls[0][1]), self.home / "d.html")
        self.assertIn("d.html", out)

    def test_dashboard_defaults_to_fleet_dir(self):
        model.save_state(_state())
        calls = []
        fake = types.ModuleType("tools.fleet.dashboard")
        fake.write_page = lambda state, path: calls.append(path) or Path(path)
        with mock.patch.dict(sys.modules, {"tools.fleet.dashboard": fake}):
            code, _, err = self.run_main("dashboard")
        self.assertEqual(code, 0, err)
        self.assertEqual(Path(calls[0]), model.fleet_dir() / "dashboard.html")

    @unittest.skipUnless((FLEET_PY.parent / "dashboard.py").exists(), "no dashboard.py")
    def test_real_dashboard_writes_html(self):
        model.save_state(_state())
        code, _, err = self.run_main("dashboard")
        self.assertEqual(code, 0, err)
        page = (model.fleet_dir() / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("project-A", page)


class ScriptModeTests(CliHome):
    def run_cli(self, *args):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, str(FLEET_PY), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=60,
        )

    def test_apply_and_inbox_as_script(self):
        self.proposal()
        out = self.run_cli("inbox")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("0000aaaa", out.stdout)
        out = self.run_cli("apply", "20261007T120000Z-0000aaaa")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("b = 3", self.repo_file.read_text())

    def test_lessons_dry_run_as_script(self):
        out = self.run_cli("lessons", "--dry-run")
        self.assertEqual(out.returncode, 0, out.stderr)

    @unittest.skipIf(
        (FLEET_PY.parent / "dashboard.py").exists(), "dashboard.py present"
    )
    def test_dashboard_absent_as_script(self):
        model.save_state(FleetState())
        out = self.run_cli("dashboard", str(self.home / "d.html"))
        self.assertEqual(out.returncode, 2)
        self.assertIn("dashboard.py", out.stderr)


class OpenStaggerParseTests(unittest.TestCase):
    def test_stagger_defaults_to_three_seconds_and_accepts_zero(self):
        parse = fleet._parser().parse_args
        self.assertEqual(parse(["open"]).stagger, 3.0)
        self.assertEqual(parse(["open", "--stagger", "0"]).stagger, 0.0)
        self.assertEqual(parse(["open", "--stagger", "0.5"]).stagger, 0.5)

    def test_bad_stagger_is_a_usage_error(self):
        for bad in ("-1", "soon", "nan"):
            with (
                self.subTest(bad=bad),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                fleet._parser().parse_args(["open", "--stagger", bad])

    def test_open_command_passes_the_stagger_through(self):
        args = fleet._parser().parse_args(["open", "amber", "--stagger", "2"])
        with mock.patch.object(fleet, "open_sessions", return_value="") as opened:
            fleet._COMMANDS["open"](args)
        self.assertEqual(opened.call_args.kwargs["stagger"], 2.0)


DIGEST_NOW = datetime.datetime(2026, 10, 14, 12, 0, tzinfo=datetime.UTC)


def _iso_ago(**delta):
    return (DIGEST_NOW - datetime.timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest_state():
    asking = Session(
        pid=1,
        session_id="sess-a1",
        project="project-A",
        status="waiting",
        alive=True,
        last_activity=_iso_ago(minutes=25),
        handoff=Handoff(age_h=26.0),
        alerts=[
            Alert(kind="stuck", severity="warn", detail="AskUserQuestion unanswered")
        ],
        extra={"pending_ask": {"since": _iso_ago(minutes=25), "line": 9}},
    )
    quiet = Session(
        pid=2,
        session_id="sess-b1",
        project="project-B",
        status="busy",
        alive=True,
        last_activity=_iso_ago(minutes=3),
        handoff=Handoff(age_h=2.5),
        alerts=[Alert(kind="context", severity="info", detail="ctx 70%")],
    )
    broken = Session(
        pid=3,
        session_id="sess-d1",
        project="project-D",
        status="idle",
        alive=True,
        last_activity=_iso_ago(hours=5),
        alerts=[
            Alert(
                kind="collision",
                severity="error",
                detail=f"2 sessions wrote {home_path('alice', 'posix')}/work/x.py",
            )
        ],
    )
    dead = Session(pid=4, session_id="sess-c1", project="project-C", alive=False)
    return FleetState(
        generated_at=_iso_ago(minutes=2),
        harness=Harness(
            drift=[DriftEntry(installed_path="hooks/a.mjs", status="modified")],
            extra={"in_sync": False, "behind_files": 3},
        ),
        sessions=[quiet, broken, dead, asking],
        inbox_count=2,
        machine=Machine(mem_total_gb=15.7, mem_free_gb=2.1),
        machine_alerts=[
            Alert(kind="memory", severity="warn", detail="free memory 2.1 GB")
        ],
    )


class DigestTests(CliHome):
    """fleet.py digest: markdown/JSON summary, --write, private-term masking."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"CCV_PRIVACY_TERMS": "zebracorp"})
        env.start()
        self.addCleanup(env.stop)
        # The clone's own .git/info/privacy-terms is real data; keep fixtures isolated.
        for target, name, kw in (
            (lessons.privacy_guard, "default_terms_file", {"return_value": None}),
            (lessons, "HARNESS_DIR", {"new": None}),
            (digest, "_now", {"return_value": DIGEST_NOW}),
        ):
            patcher = mock.patch.object(target, name, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)
        audit = [
            {"ts": _iso_ago(hours=1), "category": "force-push", "command": "git"},
            {"ts": _iso_ago(hours=23), "category": "hard-reset", "command": "git"},
            {"ts": _iso_ago(hours=30), "category": "rm-rf", "command": "rm"},
        ]
        model.audit_path().parent.mkdir(parents=True, exist_ok=True)
        model.audit_path().write_text(
            "".join(json.dumps(e) + "\n" for e in audit), encoding="utf-8"
        )

    def text(self, state=None):
        model.save_state(state or _digest_state())
        code, out, err = self.run_main("digest")
        self.assertEqual(code, 0, err)
        return out

    def test_header_lines(self):
        out = self.text()
        self.assertIn("# Fleet digest 2026-10-14", out)
        self.assertIn("2 min ago", out)
        self.assertIn("Machine: 2.1 GB free of 15.7 GB", out)
        self.assertIn("[warn] memory: free memory 2.1 GB", out)
        self.assertIn("Harness: differs from repo (3 files)", out)
        self.assertIn("1 drifted file", out)
        self.assertIn("Inbox: 2 pending", out)
        self.assertIn("Audit: 2 risky commands in the last 24 h", out)

    def test_sync_wording_matches_report(self):
        cases = [
            ({"in_sync": True, "behind_files": 0}, "Harness: in sync"),
            (
                {"in_sync": False, "behind_files": 1},
                "Harness: differs from repo (1 file)",
            ),
            ({}, "Harness: unknown"),
        ]
        for extra, needle in cases:
            with self.subTest(extra=extra):
                out = self.text(FleetState(harness=Harness(extra=extra)))
                self.assertIn(needle, out)

    def test_session_table_sorted_by_attention(self):
        out = self.text()
        self.assertIn(
            "| project | status | last activity | handoff | waiting question | alerts |",
            out,
        )
        rows = [ln for ln in out.splitlines() if ln.startswith("| project-")]
        self.assertEqual(
            [r.split("|")[1].strip() for r in rows],
            ["project-A", "project-D", "project-B"],
        )
        a, d, b = rows
        self.assertIn("25 min ago", a)
        self.assertIn("26.0 h ago", a)
        self.assertIn("25 min", a.split("|")[5])
        self.assertIn("stuck (warn)", a)
        self.assertIn("collision (error)", d)
        self.assertIn("5.0 h ago", d)
        self.assertIn("3 min ago", b)
        self.assertIn("2.5 h ago", b)
        self.assertIn("context (info)", b)
        self.assertNotIn("project-C", out)
        self.assertIn("1 session not running", out)

    def test_needs_you_list(self):
        out = self.text()
        needs = out.split("## Needs you", 1)[1]
        self.assertIn("project-A: question waiting 25 min", needs)
        self.assertIn("project-A: [warn] stuck", needs)
        self.assertIn("project-D: [error] collision", needs)
        self.assertIn("machine: [warn] memory", needs)
        self.assertNotIn("project-B", needs)
        self.assertLess(needs.index("question waiting"), needs.index("[error]"))
        self.assertLess(needs.index("[error]"), needs.index("[warn]"))
        empty = self.text(FleetState()).split("## Needs you", 1)[1]
        self.assertIn("nothing", empty.lower())

    def test_private_terms_and_home_paths_masked(self):
        state = _digest_state()
        state.sessions[0].project = "zebracorp-app"
        out = self.text(state)
        self.assertNotIn("zebracorp", out.lower())
        self.assertNotIn("alice", out)
        self.assertIn(lessons.MASK, out)
        code, js, err = self.run_main("digest", "--json")
        self.assertEqual(code, 0, err)
        self.assertNotIn("zebracorp", js.lower())
        self.assertNotIn("alice", js)
        json.loads(js)

    def test_json_same_data(self):
        model.save_state(_digest_state())
        code, out, err = self.run_main("digest", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["date"], "2026-10-14")
        self.assertEqual(data["machine"]["mem_free_gb"], 2.1)
        self.assertEqual(data["machine"]["alerts"][0]["kind"], "memory")
        self.assertEqual(data["harness"]["sync"], "differs from repo (3 files)")
        self.assertEqual(data["harness"]["behind_files"], 3)
        self.assertEqual(data["harness"]["drift"], 1)
        self.assertEqual(data["inbox_count"], 2)
        self.assertEqual(data["audit_24h"], 2)
        self.assertEqual(data["not_running"], 1)
        projects = [s["project"] for s in data["sessions"]]
        self.assertEqual(projects, ["project-A", "project-D", "project-B"])
        first = data["sessions"][0]
        self.assertEqual(first["waiting_age_s"], 25 * 60)
        self.assertEqual(first["last_activity_age_s"], 25 * 60)
        self.assertEqual(first["handoff_age_s"], 26 * 3600)
        self.assertTrue(first["needs_attention"])
        self.assertFalse(data["sessions"][2]["needs_attention"])
        kinds = [(n["who"], n["kind"]) for n in data["needs_you"]]
        self.assertEqual(
            kinds,
            [
                ("project-A", "question"),
                ("project-D", "alert"),
                ("machine", "alert"),
                ("project-A", "alert"),
            ],
        )

    def test_write_saves_dated_file_atomically(self):
        model.save_state(_digest_state())
        code, out, err = self.run_main("digest", "--write")
        self.assertEqual(code, 0, err)
        path = model.fleet_dir() / "digests" / "2026-10-14.md"
        self.assertEqual(out.strip(), str(path))
        self.assertTrue(path.is_file())
        self.assertEqual(
            path.read_text(encoding="utf-8").rstrip("\n"), self.text().rstrip("\n")
        )
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), [path.name])
        with mock.patch.object(model, "write_atomic", wraps=model.write_atomic) as wa:
            self.run_main("digest", "--write")
        wa.assert_called_once()

    def test_fresh_collects_first(self):
        with mock.patch.object(
            fleet.collect,
            "collect_and_save",
            side_effect=lambda: model.save_state(_digest_state()),
        ) as collected:
            code, out, err = self.run_main("digest", "--fresh")
        self.assertEqual(code, 0, err)
        collected.assert_called_once()
        self.assertIn("project-A", out)

    def test_json_and_write_are_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            fleet._parser().parse_args(["digest", "--json", "--write"])

    def test_age_wording(self):
        cases = [
            (None, "-"),
            (-5, "<1 min"),
            (30, "<1 min"),
            (25 * 60, "25 min"),
            (2.5 * 3600, "2.5 h"),
            (72 * 3600, "3.0 d"),
        ]
        for seconds, text in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(digest.age_text(seconds), text)

    def test_digest_as_script(self):
        model.save_state(_digest_state())
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        out = subprocess.run(
            [sys.executable, str(FLEET_PY), "digest"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=60,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("# Fleet digest", out.stdout)


class CollectDigestCase(CliHome):
    """Fixed digest clock, isolated privacy terms, collect() with patched digest parts."""

    def setUp(self):
        super().setUp()
        for target, name, kw in (
            (lessons.privacy_guard, "default_terms_file", {"return_value": None}),
            (lessons, "HARNESS_DIR", {"new": None}),
            (digest, "_now", {"return_value": DIGEST_NOW}),
        ):
            patcher = mock.patch.object(target, name, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.today = DIGEST_NOW.astimezone().date().isoformat()
        self.path = model.fleet_dir() / "digests" / f"{self.today}.md"

    def collect(self, **patches):
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    fleet.collect, "collect", return_value=_digest_state()
                )
            )
            for name, kw in patches.items():
                stack.enter_context(mock.patch.object(digest, name, **kw))
            return self.run_main("collect")


class CollectDigestTests(CollectDigestCase):
    """fleet.py collect writes today's digest once per local day; never fails collect."""

    def test_writes_todays_digest_when_absent(self):
        code, out, err = self.collect()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, fleet.summary(_digest_state()) + "\n")
        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(f"# Fleet digest {self.today}"), text[:60])
        self.assertIn("project-A", text)

    def test_existing_digest_is_kept(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("earlier digest\n", encoding="utf-8")
        with mock.patch.object(digest, "write") as write:
            code, _, _ = self.collect()
        self.assertEqual(code, 0)
        write.assert_not_called()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "earlier digest\n")

    def test_digest_failure_never_fails_collect(self):
        expected = fleet.summary(_digest_state()) + "\n"
        for name in ("build", "write", "digest_path", "_now"):
            with self.subTest(failing=name):
                code, out, err = self.collect(**{name: {"side_effect": OSError("ro")}})
                self.assertEqual((code, out, err), (0, expected, ""))
                self.assertTrue(model.state_path().exists())
                self.assertFalse(self.path.exists())
                self.assertFalse((model.fleet_dir() / "collect.err").exists())

    def test_failed_collect_writes_no_digest(self):
        with mock.patch.object(fleet.collect, "collect", side_effect=OSError("boom")):
            code, _, _ = self.run_main("collect")
        self.assertEqual(code, fleet.COLLECT_FAILED)
        self.assertFalse(self.path.exists())


class DigestErrTests(CollectDigestCase):
    """A failed daily digest write leaves a masked reason in fleet/digest.err."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"CCV_PRIVACY_TERMS": "zebracorp"})
        env.start()
        self.addCleanup(env.stop)
        self.err = model.fleet_dir() / "digest.err"
        self.expected = fleet.summary(_digest_state()) + "\n"

    def fail_write(self, exc=None):
        exc = exc or OSError(
            13,
            "Permission denied for zebracorp",
            str(self.path),
        )
        code, out, err = self.collect(write={"side_effect": exc})
        self.assertEqual((code, out, err), (0, self.expected, ""))
        return exc

    def test_failure_records_short_masked_reason_without_paths(self):
        other = home_path("alice", "posix") + "/notes and /var/tmp/z"
        self.fail_write(OSError(f"cannot write {self.path} for ZebraCorp near {other}"))
        text = self.err.read_text(encoding="utf-8")
        self.assertEqual(text.count("\n"), 1, text)
        self.assertTrue(text.startswith("OSError: cannot write "), text)
        self.assertLessEqual(len(text.strip()), fleet.DIGEST_ERR_MAX)
        for leak in (str(self.home), "alice", "zebracorp", "/var/tmp", ".md"):
            self.assertNotIn(leak.lower(), text.lower())
        self.assertIn("<path>", text)
        self.assertIn(lessons.MASK, text)

    def test_errno_repr_path_and_private_term_hidden(self):
        self.fail_write()
        text = self.err.read_text(encoding="utf-8")
        self.assertIn("Permission denied", text)
        self.assertNotIn("zebracorp", text.lower())
        self.assertNotIn(self.home.name, text)
        self.assertNotIn("digests", text)

    def test_unc_and_spaced_home_paths_hidden(self):
        unc = "\\\\" + "fileserver\\share\\f.md"
        for exc in (OSError(13, "denied", unc), OSError(f"cannot open {unc}")):
            self.fail_write(exc)
            text = self.err.read_text(encoding="utf-8")
            self.assertNotIn("fileserver", text)
            self.assertNotIn("share", text)
        spaced = self.home.parent / "Quentin Smythe"
        spaced.mkdir()
        env = {"HOME": str(spaced), "USERPROFILE": str(spaced)}
        with mock.patch.dict(os.environ, env):
            self.fail_write(OSError(13, "denied", str(spaced / ".claude" / "x.md")))
            text = (model.fleet_dir() / "digest.err").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("PermissionError: [Errno 13] denied"), text)
        self.assertNotIn("Smythe", text)
        self.assertNotIn("Quentin", text)

    def test_reason_is_clipped(self):
        self.fail_write(RuntimeError("x" * 1000))
        text = self.err.read_text(encoding="utf-8").strip()
        self.assertEqual(len(text), fleet.DIGEST_ERR_MAX)

    def test_overwritten_by_the_next_failure(self):
        self.fail_write(OSError("first"))
        self.fail_write(ValueError("second"))
        self.assertEqual(self.err.read_text(encoding="utf-8"), "ValueError: second\n")

    def test_removed_on_the_next_success(self):
        self.fail_write()
        self.assertTrue(self.err.exists())
        code, out, err = self.collect()
        self.assertEqual((code, out, err), (0, self.expected, ""))
        self.assertTrue(self.path.exists())
        self.assertFalse(self.err.exists())

    def test_removed_when_todays_digest_already_exists(self):
        self.fail_write()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("earlier digest\n", encoding="utf-8")
        self.assertEqual(self.collect()[0], 0)
        self.assertFalse(self.err.exists())

    def test_masking_failure_falls_back_to_the_class_name(self):
        with mock.patch.object(
            lessons, "default_guard", side_effect=RuntimeError("no guard")
        ):
            self.fail_write(OSError("zebracorp"))
        self.assertEqual(self.err.read_text(encoding="utf-8"), "OSError\n")

    def test_unwritable_err_file_never_fails_collect(self):
        self.err.mkdir(parents=True)  # a directory where the file should go
        self.fail_write()
        self.assertTrue(self.err.is_dir())

    def test_report_and_digest_print_one_notice_line(self):
        self.fail_write(OSError("disk full"))
        notice = "digest: last daily write failed (OSError: disk full)"
        code, out, err = self.run_main("report")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines().count(notice), 1, out)
        code, out, err = self.run_main("digest")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines().count(notice), 1, out)
        for flag in ("--json", "--write"):
            with self.subTest(flag=flag):
                code, out, err = self.run_main("digest", flag)
                self.assertEqual(code, 0)
                self.assertNotIn("digest:", out)
                self.assertEqual(err, notice + "\n")
        json.loads(self.run_main("digest", "--json")[1])

    def test_no_notice_without_err_file(self):
        model.save_state(_digest_state())
        for args in (("report",), ("digest",), ("digest", "--json")):
            with self.subTest(args=args):
                code, out, err = self.run_main(*args)
                self.assertEqual((code, err), (0, ""))
                self.assertNotIn("last daily write failed", out)

    def test_unreadable_err_file_still_noticed(self):
        model.save_state(_digest_state())
        self.err.mkdir(parents=True)
        _, out, _ = self.run_main("report")
        self.assertIn("digest: last daily write failed (reason unknown)", out)


if __name__ == "__main__":
    unittest.main()
