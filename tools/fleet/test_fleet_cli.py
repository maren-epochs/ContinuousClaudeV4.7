"""Tests for the fleet.py inbox/show/apply/reject/lessons/dashboard commands and report.

Run from the repo root:  py -3.13 -m pytest -q tools/fleet
Every test runs on a temp harness repo plus a temp HOME/USERPROFILE (``~/.claude``
with a manifest and inbox); the real ~/.claude and this repo are never touched.
"""

import contextlib
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

from tools.fleet import fleet, lessons, model
from tools.fleet.model import (
    Alert,
    AuditEvent,
    Collision,
    DriftEntry,
    FleetState,
    Harness,
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


if __name__ == "__main__":
    unittest.main()
