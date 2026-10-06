"""Tests for tools/privacy_guard.py (pre-commit privacy hook).

Covers:
  (a) absolute user-home paths: drive + /Users/<name>, both slash forms, the
      escaped-backslash form, the Git Bash /c/Users form and the transcript-dir
      C--Users-<name>- form; placeholders (<...>, x, user, name, you) pass
  (b) the current OS username as a whole word, case-insensitive, min length 3
  (c) session-UUID-shaped ids
  (d) terms from the untracked privacy-terms file (explicit path and the
      `git rev-parse --git-path info/privacy-terms` default)
  (e) .privacy-allow regexes suppress reviewed hits
  (f) binary files are skipped; output is file:line: reason, private text redacted

Every name, term and id below is synthetic and assembled at runtime, so this
file never trips the guard it tests. The real OS username is derived at runtime.

Run: py -3.13 tools/test_privacy_guard.py
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
GUARD = PROJECT / "tools" / "privacy_guard.py"

SYNTH_NAME = "zorb" + "laxian"
SYNTH_TERM = "quux" + "-person"
# Session-UUID shape built from parts so this file holds no literal id.
SYNTH_UUID = "0f1e2d3c" + "-4b5a-6978-8a9b-" + "0c1d2e3f4a5b"


def home(drive_sep: str, name: str) -> str:
    """C:<sep>Users<sep><name> assembled at runtime."""
    return "C:" + drive_sep + "Users" + drive_sep + name


class GuardCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.terms = self.tmp / "terms"
        self.terms.write_text(
            "# comment line\n\n" + SYNTH_TERM + "\n", encoding="utf-8"
        )
        self.allow = self.tmp / "allow"
        self.allow.write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return p

    def run_guard(
        self, *paths: Path, username: str | None = SYNTH_NAME, extra=(), cwd=None
    ):
        cmd = [sys.executable, str(GUARD), "--terms-file", str(self.terms)]
        cmd += ["--allow-file", str(self.allow)]
        if username is not None:
            cmd += ["--username", username]
        # Relative paths, as pre-commit passes them (the temp dir itself may sit under a home dir).
        cmd += list(extra) + [os.path.relpath(p, cwd or self.tmp) for p in paths]
        return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd or self.tmp)


class TestHomePaths(GuardCase):
    def test_forward_slash_real_name(self):
        p = self.write("a.md", "see " + home("/", "alice") + "/proj\n")
        r = self.run_guard(p)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"{p.name}:1:", r.stdout)
        self.assertIn("user-home path", r.stdout)

    def test_backslash_real_name(self):
        p = self.write("a.txt", "ok\npath " + home("\\", "alice") + "\\x\n")
        r = self.run_guard(p)
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"{p.name}:2:", r.stdout)

    def test_escaped_backslash_real_name(self):
        p = self.write("a.json", '{"p": "' + home("\\\\", "alice") + '\\\\x"}\n')
        self.assertEqual(self.run_guard(p).returncode, 1)

    def test_lowercase_drive_and_users(self):
        p = self.write("a.txt", "c:" + "/users/" + "alice/x\n")
        self.assertEqual(self.run_guard(p).returncode, 1)

    def test_git_bash_form(self):
        p = self.write("a.sh", "cd /c" + "/Users/" + "alice/x\n")
        self.assertEqual(self.run_guard(p).returncode, 1)

    def test_transcript_dir_form(self):
        p = self.write("a.txt", "projects/C" + "--Users-" + "alice-Documents-proj\n")
        r = self.run_guard(p)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("user-home path", r.stdout)

    def test_placeholders_pass(self):
        lines = [
            home("/", n) + "/.claude"
            for n in ("<name>", "<you>", "x", "user", "name", "you")
        ]
        lines.append(home("\\", "X") + "\\proj")
        lines.append("C" + "--Users-" + "<name>-Documents")
        lines.append("C" + "--Users-" + "x-proj")
        p = self.write("ok.md", "\n".join(lines) + "\n")
        r = self.run_guard(p)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_redacts_real_name(self):
        p = self.write("a.md", home("/", "alice") + "\n")
        r = self.run_guard(p)
        self.assertNotIn("alice", r.stdout)


class TestUsername(GuardCase):
    def test_whole_word_case_insensitive(self):
        p = self.write("a.txt", "owner: " + SYNTH_NAME.upper() + "\n")
        r = self.run_guard(p)
        self.assertEqual(r.returncode, 1)
        self.assertIn("OS username", r.stdout)
        self.assertNotIn(SYNTH_NAME.upper(), r.stdout)

    def test_not_inside_longer_word(self):
        p = self.write("a.txt", "x" + SYNTH_NAME + "y and " + SYNTH_NAME + "_2\n")
        self.assertEqual(self.run_guard(p).returncode, 0)

    def test_short_username_ignored(self):
        p = self.write("a.txt", "ab is a word\n")
        self.assertEqual(self.run_guard(p, username="ab").returncode, 0)

    def test_detected_username_flagged(self):
        real = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        try:
            real = os.getlogin() or real
        except OSError:
            pass
        if len(real) < 3:
            self.skipTest("no OS username of length >= 3 available")
        p = self.write("a.txt", "by " + real + " today\n")
        r = self.run_guard(p, username=None)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("OS username", r.stdout)


class TestUuid(GuardCase):
    def test_uuid_flagged(self):
        p = self.write("a.yaml", "session: " + SYNTH_UUID + "\n")
        r = self.run_guard(p)
        self.assertEqual(r.returncode, 1)
        self.assertIn("session-UUID", r.stdout)
        self.assertNotIn(SYNTH_UUID, r.stdout)

    def test_uppercase_uuid_flagged(self):
        p = self.write("a.yaml", SYNTH_UUID.upper() + "\n")
        self.assertEqual(self.run_guard(p).returncode, 1)

    def test_short_id_passes(self):
        p = self.write("a.yaml", "session " + SYNTH_UUID[:8] + "\n")
        self.assertEqual(self.run_guard(p).returncode, 0)


class TestPrivateTerms(GuardCase):
    def test_term_flagged_and_redacted(self):
        p = self.write("a.md", "hello\nfrom " + SYNTH_TERM.title() + "\n")
        r = self.run_guard(p)
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"{p.name}:2:", r.stdout)
        self.assertIn("private term", r.stdout)
        self.assertNotIn(SYNTH_TERM.title(), r.stdout)

    def test_comment_and_blank_lines_are_not_terms(self):
        p = self.write("a.md", "a comment line here\n\n")
        self.assertEqual(self.run_guard(p).returncode, 0)

    def test_missing_terms_file_is_not_an_error(self):
        self.terms.unlink()
        p = self.write("a.md", "plain text\n")
        self.assertEqual(self.run_guard(p).returncode, 0)

    def test_default_terms_path_via_git(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        info = repo / ".git" / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "privacy-terms").write_text(SYNTH_TERM + "\n", encoding="utf-8")
        f = repo / "doc.md"
        f.write_text("by " + SYNTH_TERM + "\n", encoding="utf-8")
        cmd = [sys.executable, str(GUARD), "--username", SYNTH_NAME, "doc.md"]
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=repo)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("private term", r.stdout)

    def test_term_in_file_path(self):
        p = self.write("notes-" + SYNTH_TERM + ".md", "clean\n")
        r = self.run_guard(p)
        self.assertEqual(r.returncode, 1)
        self.assertIn("file path", r.stdout)


class TestAllowAndBinary(GuardCase):
    def test_allowlist_suppresses_hit(self):
        self.allow.write_text("# reviewed\n0f1e2d3c-4b5a-.*\n", encoding="utf-8")
        p = self.write("a.yaml", "id " + SYNTH_UUID + "\n")
        self.assertEqual(self.run_guard(p).returncode, 0)

    def test_allowlist_is_fullmatch(self):
        self.allow.write_text("0f1e2d3c\n", encoding="utf-8")
        p = self.write("a.yaml", "id " + SYNTH_UUID + "\n")
        self.assertEqual(self.run_guard(p).returncode, 1)

    def test_bad_allow_regex_is_usage_error(self):
        self.allow.write_text("(unclosed\n", encoding="utf-8")
        p = self.write("a.txt", "x\n")
        r = self.run_guard(p)
        self.assertEqual(r.returncode, 2)
        self.assertIn("allow", r.stderr)

    def test_binary_skipped(self):
        p = self.tmp / "blob.bin"
        p.write_bytes(b"\x00\x01" + SYNTH_TERM.encode() + b"\x00")
        self.assertEqual(self.run_guard(p).returncode, 0)

    def test_clean_files_exit_zero_silently(self):
        a = self.write("a.py", "print('hi')\n")
        b = self.write("b.md", "# title\n")
        r = self.run_guard(a, b)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_multiple_hits_all_reported(self):
        a = self.write("a.txt", SYNTH_UUID + "\n" + SYNTH_TERM + "\n")
        b = self.write("b.txt", home("/", "alice") + "\n")
        r = self.run_guard(a, b)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(r.stdout.strip().splitlines()), 3, r.stdout)

    def test_missing_file_skipped(self):
        r = self.run_guard(self.tmp / "gone.txt")
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
