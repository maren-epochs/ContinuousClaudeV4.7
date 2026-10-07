"""Tests for tools/privacy_guard.py (pre-commit privacy hook).

Covers:
  (a) absolute user-home paths: drive + /Users/<name>, both slash forms, the
      escaped-backslash form, the Git Bash /c/Users form and the transcript-dir
      C--Users-<name>- form; placeholders (<...>, x, user, name, you) pass
  (b) the current OS username as a whole word, case-insensitive, min length 3
  (c) session-UUID-shaped ids
  (d) private terms from the union of CCV_PRIVACY_TERMS (newline/comma list),
      ~/.claude/privacy-terms (via Path.home()) and the untracked per-clone
      `git rev-parse --git-path info/privacy-terms` file (or --terms-file);
      a stderr warning, not a failure, when no source supplies any term
  (e) .privacy-allow regexes suppress reviewed hits
  (f) binary files are skipped; output is file:line: reason, private text redacted

Every name, term and id below is synthetic and assembled at runtime, so this
file never trips the guard it tests. The real OS username is derived at runtime.

Run: py -3.13 tools/test_privacy_guard.py
"""

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parent.parent
GUARD = PROJECT / "tools" / "privacy_guard.py"

SYNTH_NAME = "zorb" + "laxian"
SYNTH_TERM = "quux" + "-person"
ENV_TERMS = "CCV_PRIVACY_TERMS"
NO_TERMS_WARNING = (
    "privacy-guard: no private-terms list found (CCV_PRIVACY_TERMS, "
    "~/.claude/privacy-terms, .git/info/privacy-terms) - only paths/username/"
    "session ids are checked"
)
# Session-UUID shape built from parts so this file holds no literal id.
SYNTH_UUID = "0f1e2d3c" + "-4b5a-6978-8a9b-" + "0c1d2e3f4a5b"


USER_RE = re.compile(r"[A-Za-z0-9._-]{3,64}")


def _guard_module():
    """tools/privacy_guard.py imported by path (registered so dataclasses resolve)."""
    spec = importlib.util.spec_from_file_location("privacy_guard_under_test", GUARD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


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
        # Hermetic: no real env terms, a fake home, no git repo discovery above tmp.
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k != ENV_TERMS}
        self.env.update(
            HOME=str(self.home),
            USERPROFILE=str(self.home),
            GIT_CEILING_DIRECTORIES=str(self.tmp),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return p

    def run_guard(
        self,
        *paths: Path,
        username: str | None = SYNTH_NAME,
        extra=(),
        cwd=None,
        terms_file: bool = True,
    ):
        cmd = [sys.executable, str(GUARD)]
        if terms_file:
            cmd += ["--terms-file", str(self.terms)]
        cmd += ["--allow-file", str(self.allow)]
        if username is not None:
            cmd += ["--username", username]
        # Relative paths, as pre-commit passes them (the temp dir itself may sit under a home dir).
        cmd += list(extra) + [os.path.relpath(p, cwd or self.tmp) for p in paths]
        return subprocess.run(
            cmd, capture_output=True, text=True, cwd=cwd or self.tmp, env=self.env
        )


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
        # Same detection the guard runs (getlogin, USERNAME, USER); only a plain
        # account name is written into the fixture file.
        names = [n for n in _guard_module().detect_usernames() if USER_RE.fullmatch(n)]
        if not names:
            self.skipTest("no OS username of length >= 3 available")
        p = self.write("a.txt", "by " + names[0] + " today\n")
        r = self.run_guard(p, username=None)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("OS username", r.stdout)

    def test_service_accounts_not_detected(self):
        # GitHub runners log in as runner / runneradmin; those are not personal.
        mod = _guard_module()
        env = {"USERNAME": "runneradmin", "USER": "Runner"}
        with (
            mock.patch.object(mod.os, "getlogin", return_value="root"),
            mock.patch.dict(mod.os.environ, env),
        ):
            self.assertEqual(mod.detect_usernames(), [])
        with (
            mock.patch.object(mod.os, "getlogin", side_effect=OSError),
            mock.patch.dict(mod.os.environ, {"USERNAME": SYNTH_NAME, "USER": ""}),
        ):
            self.assertEqual(mod.detect_usernames(), [SYNTH_NAME, ""])


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
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=repo, env=self.env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("private term", r.stdout)
        self.assertNotIn(SYNTH_TERM, r.stdout + r.stderr)
        self.assertNotIn("no private-terms list", r.stderr)

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


# Synthetic per-source terms, assembled at runtime.
ENV_A = "glim" + "-env-one"
ENV_B = "glim" + "-env-two"
ENV_C = "glim" + "-env-three"
USER_T = "frob" + "-user-term"
CLONE_T = "wib" + "-clone-term"
SHARED_T = "zed" + "-shared-term"


class TestTermSources(GuardCase):
    """VAL-704: env var + ~/.claude/privacy-terms + per-clone file, unioned."""

    def user_file(self, text: str) -> Path:
        d = self.home / ".claude"
        d.mkdir(exist_ok=True)
        f = d / "privacy-terms"
        f.write_text(text, encoding="utf-8")
        return f

    def test_env_var_newline_and_comma_separated(self):
        self.terms.unlink()
        self.env[ENV_TERMS] = ENV_A + ", " + ENV_B + "\n# a comment\n\n" + ENV_C
        for i, t in enumerate((ENV_A, ENV_B, ENV_C)):
            p = self.write(f"e{i}.md", "x " + t.upper() + " y\n")
            r = self.run_guard(p)
            self.assertEqual(r.returncode, 1, (t, r.stdout, r.stderr))
            self.assertIn("private term", r.stdout)
            self.assertNotIn(t.upper(), r.stdout + r.stderr)
            self.assertNotIn("no private-terms list", r.stderr)

    def test_env_var_comment_entry_is_not_a_term(self):
        self.terms.unlink()
        self.env[ENV_TERMS] = "# a comment," + ENV_A
        p = self.write("a.md", "a comment here\n")
        self.assertEqual(self.run_guard(p).returncode, 0)

    def test_user_file_via_home(self):
        self.terms.unlink()
        self.user_file("# shared by every clone\n\n" + USER_T + "\n")
        p = self.write("a.md", "by " + USER_T + "\n")
        r = self.run_guard(p)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("private term", r.stdout)
        self.assertNotIn(USER_T, r.stdout + r.stderr)
        self.assertNotIn("no private-terms list", r.stderr)

    def test_user_file_resolved_via_path_home(self):
        mod = _guard_module()
        self.user_file(USER_T + "\n")
        with mock.patch.object(mod.Path, "home", return_value=self.home):
            want = self.home / ".claude" / "privacy-terms"
            self.assertEqual(mod.user_terms_file(), want)
            terms = mod.load_terms(None, env={})
        self.assertEqual([t for t, _ in terms], [USER_T])

    def test_per_clone_file_still_read(self):
        self.terms.write_text(CLONE_T + "\n", encoding="utf-8")
        p = self.write("a.md", CLONE_T + "\n")
        r = self.run_guard(p)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn(CLONE_T, r.stdout + r.stderr)

    def test_union_of_all_sources_deduplicated(self):
        self.env[ENV_TERMS] = ENV_A + "," + SHARED_T
        self.user_file(USER_T + "\n" + SHARED_T.upper() + "\n")
        self.terms.write_text(CLONE_T + "\n" + SHARED_T + "\n", encoding="utf-8")
        mod = _guard_module()
        with mock.patch.object(mod.Path, "home", return_value=self.home):
            terms = mod.load_terms(self.terms, env=self.env)
        got = sorted(t.lower() for t, _ in terms)
        self.assertEqual(got, sorted([ENV_A, SHARED_T, USER_T, CLONE_T]))
        # end to end: one hit per term occurrence, never one per source
        p = self.write("a.md", f"{ENV_A}\n{USER_T}\n{CLONE_T}\n{SHARED_T}\n")
        r = self.run_guard(p)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(r.stdout.strip().splitlines()), 4, r.stdout)
        for t in (ENV_A, USER_T, CLONE_T, SHARED_T):
            self.assertNotIn(t, r.stdout + r.stderr)

    def test_no_source_warns_once_but_passes(self):
        self.terms.unlink()
        p = self.write("a.md", "plain text\n")
        r = self.run_guard(p)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stderr.strip().splitlines(), [NO_TERMS_WARNING])
        self.assertEqual(r.stdout, "")

    def test_no_source_without_terms_file_flag(self):
        # no --terms-file, no repo (ceiling at tmp), empty home, no env var
        p = self.write("a.md", "plain text\n")
        r = self.run_guard(p, terms_file=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stderr.strip().splitlines(), [NO_TERMS_WARNING])

    def test_no_source_still_checks_other_rules(self):
        self.terms.unlink()
        p = self.write("a.yaml", "id " + SYNTH_UUID + "\n")
        r = self.run_guard(p)
        self.assertEqual(r.returncode, 1)
        self.assertIn(NO_TERMS_WARNING, r.stderr)

    def test_empty_sources_count_as_none(self):
        self.terms.write_text("# only a comment\n\n", encoding="utf-8")
        self.user_file("\n# nothing\n")
        self.env[ENV_TERMS] = " , \n"
        p = self.write("a.md", "plain\n")
        r = self.run_guard(p)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr.strip().splitlines(), [NO_TERMS_WARNING])

    def test_reason_never_names_the_term(self):
        self.env[ENV_TERMS] = ENV_A
        self.user_file(USER_T + "\n")
        for t in (ENV_A, USER_T, SYNTH_TERM):
            p = self.write("n-" + t + ".md", "x " + t + "\n")
            r = self.run_guard(p)
            self.assertEqual(r.returncode, 1)
            self.assertIn("file path", r.stdout)
            # the path is echoed (pre-commit names the file); the reason never is the term
            reasons = [ln.rsplit(": ", 1)[1] for ln in r.stdout.splitlines()]
            self.assertEqual(len(reasons), 2, r.stdout)
            for reason in reasons:
                self.assertNotIn(t, reason)
            self.assertNotIn(t, r.stderr)


if __name__ == "__main__":
    unittest.main()
