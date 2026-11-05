"""Project record: handoff folder, memory copy, git exclusion, what is not a project."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from tools.fleet import record

HAS_GIT = shutil.which("git") is not None


def git(folder: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(folder), *args], capture_output=True, text=True, check=True
    ).stdout


class RecordHome(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        env = mock.patch.dict(
            os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        )
        env.start()
        self.addCleanup(env.stop)
        temp = mock.patch.object(
            record.tempfile, "gettempdir", return_value=str(self.home / "systemp")
        )
        temp.start()
        self.addCleanup(temp.stop)
        self.project = self.home / "Documents" / "proj"
        self.project.mkdir(parents=True)
        self.memory = record.slug_dir(self.project) / "memory"
        self.memory.mkdir(parents=True)

    def remember(self, name: str, text: str) -> None:
        (self.memory / name).write_text(text, encoding="utf-8")


class SaveTests(RecordHome):
    def test_creates_the_handoff_folder_and_copies_memory(self):
        self.remember("a.md", "one")
        self.remember("MEMORY.md", "- [a](a.md)")
        line = record.save(self.project)
        self.assertTrue((self.project / record.HANDOFFS).is_dir())
        copy = self.project / record.MEMORY
        self.assertEqual((copy / "a.md").read_text(encoding="utf-8"), "one")
        self.assertTrue((copy / "MEMORY.md").exists())
        self.assertIn("2 memory file(s) updated", line)
        self.assertIn("not a git repo", line)

    def test_copy_follows_changes_and_removals(self):
        self.remember("a.md", "one")
        self.remember("b.md", "two")
        record.save(self.project)
        self.remember("a.md", "changed")
        (self.memory / "b.md").unlink()
        self.assertIn("2 memory file(s)", record.save(self.project))
        copy = self.project / record.MEMORY
        self.assertEqual(sorted(p.name for p in copy.iterdir()), ["a.md"])
        self.assertIn("0 memory file(s)", record.save(self.project))

    def test_home_documents_and_drive_roots_are_not_projects(self):
        for folder in (self.home, self.home / "Documents", Path(self.home.anchor)):
            with self.subTest(str(folder)):
                self.assertFalse(record.is_project(folder))
        self.assertIn("not a project folder", record.save(self.home / "Documents"))
        self.assertFalse((self.home / "Documents" / "thoughts").exists())
        self.assertTrue(record.is_project(self.project))
        with mock.patch.object(
            record.tempfile, "gettempdir", return_value=str(self.home)
        ):
            self.assertFalse(record.is_project(self.project))


@unittest.skipUnless(HAS_GIT, "git not installed")
class GitTests(RecordHome):
    def setUp(self):
        super().setUp()
        git(self.project, "init", "-q")

    def test_thoughts_is_excluded_locally_and_never_shows_as_untracked(self):
        self.remember("a.md", "one")
        self.assertIn("excluded from git", record.save(self.project))
        (self.project / record.HANDOFFS / "h.yaml").write_text("x", encoding="utf-8")
        self.assertEqual(git(self.project, "status", "--porcelain"), "")
        self.assertFalse((self.project / ".gitignore").exists())
        self.assertIn("already ignored", record.save(self.project))

    def test_a_repo_that_commits_its_handoffs_is_left_alone(self):
        handoffs = self.project / record.HANDOFFS
        handoffs.mkdir(parents=True)
        (handoffs / "h.yaml").write_text("x", encoding="utf-8")
        git(self.project, "add", ".")
        self.remember("a.md", "one")
        self.assertIn("memory copy excluded", record.save(self.project))
        exclude = self.project / ".git" / "info" / "exclude"
        self.assertEqual(
            exclude.read_text(encoding="utf-8").splitlines()[-1], record.MEMORY_LINE
        )
        new = self.project / record.HANDOFFS / "new.yaml"
        new.write_text("y", encoding="utf-8")
        status = git(self.project, "status", "--porcelain", "--untracked-files=all")
        self.assertIn("new.yaml", status)
        self.assertNotIn("memory", status)


class DueTests(RecordHome):
    def test_save_all_runs_at_most_every_thirty_minutes(self):
        with mock.patch.object(record, "save_all", return_value=["x"]) as save_all:
            self.assertEqual(record.save_all_if_due(1000.0), ["x"])
            self.assertIsNone(record.save_all_if_due(1000.0 + record.EVERY_S - 1))
            self.assertEqual(record.save_all_if_due(1000.0 + record.EVERY_S), ["x"])
        self.assertEqual(save_all.call_count, 2)


if __name__ == "__main__":
    unittest.main()
