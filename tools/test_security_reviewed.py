"""Tests for tools/security_reviewed.py. Run: py -3.13 -m pytest -q tools/test_security_reviewed.py"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import security_reviewed as sr

SOURCE = "import subprocess\n\nsubprocess.Popen(cmd, env=env)\nsubprocess.Popen(user_cmd, shell=True)\n"
DESC = "CommandInjection: ShellExec with unsanitized input from Environment variable"


def finding(line, category="taint", desc=DESC, file="pkg\\launch.py"):
    return {"category": category, "description": desc, "file": file, "line": line}


class Project(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "launch.py").write_text(SOURCE, encoding="utf-8")
        self.entry = [
            ("pkg/launch.py", "CommandInjection", "subprocess.Popen(cmd, env=env)")
        ]

    def test_reviewed_only_on_the_named_line(self):
        data = {"findings": [finding(3)]}
        self.assertEqual(sr.count(data, self.entry, self.root), (0, 1))

    def test_new_finding_same_kind_same_file_still_counts(self):
        data = {"findings": [finding(3), finding(4)]}
        self.assertEqual(sr.count(data, self.entry, self.root), (1, 1))

    def test_changed_line_counts_again(self):
        (self.root / "pkg" / "launch.py").write_text(
            SOURCE.replace(
                "Popen(cmd, env=env)", "Popen(cmd, env=env, shell=True)x"
            ).replace("env=env, shell=True)x", "shell=True)"),
            encoding="utf-8",
        )
        self.assertEqual(
            sr.count({"findings": [finding(3)]}, self.entry, self.root), (1, 0)
        )

    def test_wrong_file_text_or_unreadable_line_counts(self):
        for entry in (
            [("other.py", "CommandInjection", "Popen")],
            [("launch.py", "PathTraversal", "Popen")],
        ):
            with self.subTest(entry=entry):
                self.assertEqual(
                    sr.count({"findings": [finding(3)]}, entry, self.root), (1, 0)
                )
        self.assertEqual(
            sr.count({"findings": [finding(99)]}, self.entry, self.root), (1, 0)
        )

    def test_other_categories_not_counted_and_summary_fallback(self):
        self.assertEqual(
            sr.count({"findings": [finding(3, "bounds")]}, [], self.root), (0, 0)
        )
        data = {"summary": {"taint_count": 2, "leak_count": 1}}
        self.assertEqual(sr.count(data, self.entry, self.root), (3, 0))

    def test_load_needs_all_four_fields(self):
        p = self.root / ".security-reviewed"
        p.write_text(
            "# comment\n\n./PKG\\launch.py | CommandInjection | Popen(cmd | argv, no shell\n"
            "pkg/a.py | Resource | open( |\n"
            "pkg/b.py | Resource | reason only\n",
            encoding="utf-8",
        )
        self.assertEqual(
            sr.load_reviewed(p), [("pkg/launch.py", "CommandInjection", "Popen(cmd")]
        )
        self.assertEqual(sr.load_reviewed(self.root / "missing"), [])

    def test_cli_reads_lines_relative_to_the_reviewed_file(self):
        sec = self.root / "secure.json"
        sec.write_text(
            json.dumps({"findings": [finding(3), finding(4)]}), encoding="utf-8"
        )
        rev = self.root / ".security-reviewed"
        rev.write_text(
            "pkg/launch.py | CommandInjection | Popen(cmd, env=env) | argv list\n",
            encoding="utf-8",
        )
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(sr.main([str(sec), str(rev)]), 0)
        self.assertEqual(out.getvalue().split(), ["1", "1"])
        sec.write_text("not json", encoding="utf-8")
        self.assertEqual(sr.main([str(sec)]), 1)


if __name__ == "__main__":
    unittest.main()
