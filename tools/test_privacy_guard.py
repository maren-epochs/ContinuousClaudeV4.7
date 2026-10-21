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
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from _testing import home_path

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


def escaped(path: str) -> str:
    """path with each backslash doubled (the JSON / shell-escaped spelling)."""
    return path.replace("\\", "\\\\")


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
        p = self.write("a.md", "see " + home_path("alice", "posix") + "/proj\n")
        r = self.run_guard(p)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"{p.name}:1:", r.stdout)
        self.assertIn("user-home path", r.stdout)

    def test_backslash_real_name(self):
        p = self.write("a.txt", "ok\npath " + home_path("alice", "win") + "\\x\n")
        r = self.run_guard(p)
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"{p.name}:2:", r.stdout)

    def test_escaped_backslash_real_name(self):
        p = self.write(
            "a.json", '{"p": "' + escaped(home_path("alice", "win")) + '\\\\x"}\n'
        )
        self.assertEqual(self.run_guard(p).returncode, 1)

    def test_lowercase_drive_and_users(self):
        p = self.write("a.txt", "c:" + "/users/" + "alice/x\n")
        self.assertEqual(self.run_guard(p).returncode, 1)

    def test_git_bash_form(self):
        p = self.write("a.sh", "cd " + home_path("alice", "gitbash") + "/x\n")
        self.assertEqual(self.run_guard(p).returncode, 1)

    def test_transcript_dir_form(self):
        p = self.write(
            "a.txt",
            "projects/" + home_path("alice", "transcript") + "-Documents-proj\n",
        )
        r = self.run_guard(p)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("user-home path", r.stdout)

    def test_placeholders_pass(self):
        lines = [
            home_path(n, "posix") + "/.claude"
            for n in ("<name>", "<you>", "x", "user", "name", "you")
        ]
        lines.append(home_path("X", "win") + "\\proj")
        lines.append(home_path("<name>", "transcript") + "-Documents")
        lines.append(home_path("x", "transcript") + "-proj")
        p = self.write("ok.md", "\n".join(lines) + "\n")
        r = self.run_guard(p)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_redacts_real_name(self):
        p = self.write("a.md", home_path("alice", "posix") + "\n")
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
        b = self.write("b.txt", home_path("alice", "posix") + "\n")
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


# Synthetic redaction fixtures, assembled at runtime.
SHORT_T = "quux"
TERM_WITH_USER = SYNTH_NAME + "-labs"
FIXED_KEY = bytes(range(32))
TOKEN_RE = re.compile(r"<private:[0-9a-f]{8}>")


def _token(key: bytes, term: str) -> str:
    """Expected placeholder: first 8 hex of HMAC-SHA256(key, casefolded term)."""
    import hashlib
    import hmac

    digest = hmac.new(key, term.casefold().encode("utf-8"), hashlib.sha256)
    return "<private:" + digest.hexdigest()[:8] + ">"


class RedactCase(GuardCase):
    """VAL-802: reversible redaction (redact / restore / key / placeholder_token)."""

    def setUp(self) -> None:
        super().setUp()
        self.mod = _guard_module()
        self.key_file = self.tmp / "key" / "privacy-key"
        self.key_file.parent.mkdir()
        self.key_file.write_bytes(FIXED_KEY)
        terms = [(SYNTH_TERM, "t1"), (SHORT_T, "t2"), (TERM_WITH_USER, "t3")]
        self.guard = self.mod.Guard([SYNTH_NAME], terms, [])

    def redact(self, text: str, guard=None) -> str:
        return self.mod.redact(text, guard or self.guard, key=FIXED_KEY)


class TestKeyAndToken(RedactCase):
    def test_placeholder_token_is_keyed_hmac_of_casefolded_term(self):
        tok = self.mod.placeholder_token(SYNTH_TERM, FIXED_KEY)
        self.assertEqual(tok, _token(FIXED_KEY, SYNTH_TERM))
        self.assertRegex(tok, r"^<private:[0-9a-f]{8}>$")
        self.assertEqual(self.mod.placeholder_token(SYNTH_TERM.upper(), FIXED_KEY), tok)
        other = self.mod.placeholder_token(SYNTH_TERM, bytes(32))
        self.assertNotEqual(other, tok)

    def test_key_created_once_with_32_random_bytes(self):
        path = self.tmp / "fresh" / "nested" / "privacy-key"
        key = self.mod.load_or_create_key(path)
        self.assertEqual(len(key), 32)
        self.assertEqual(path.read_bytes(), key)
        self.assertEqual(self.mod.load_or_create_key(path), key)
        other = self.mod.load_or_create_key(self.tmp / "second-key")
        self.assertNotEqual(other, key)

    def test_existing_key_is_read_not_replaced(self):
        self.assertEqual(self.mod.load_or_create_key(self.key_file), FIXED_KEY)
        self.assertEqual(self.key_file.read_bytes(), FIXED_KEY)

    def test_short_key_file_is_an_error(self):
        bad = self.tmp / "bad-key"
        bad.write_bytes(b"short")
        with self.assertRaises(ValueError) as cm:
            self.mod.load_or_create_key(bad)
        self.assertIn("corrupt", str(cm.exception))
        self.assertEqual(
            bad.read_bytes(), b"short"
        )  # never replaced: tokens may use it

    def _race(self, path: Path, n: int = 12) -> list[object]:
        """load_or_create_key(path) from n threads released together; keys or errors."""
        barrier = threading.Barrier(n)
        got: list[object] = []

        def worker() -> None:
            barrier.wait()
            try:
                got.append(self.mod.load_or_create_key(path))
            except Exception as e:  # noqa: BLE001 - collected for the assertion
                got.append(e)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(got), n)
        return got

    def test_concurrent_creators_converge_on_one_key(self):
        """F1: no reader ever sees a half-written key; every creator gets the same key."""
        for round_no in range(5):
            path = self.tmp / f"race-{round_no}" / "privacy-key"
            got = self._race(path)
            self.assertEqual({type(k) for k in got}, {bytes}, got)
            self.assertEqual(len(set(got)), 1)
            self.assertEqual(path.read_bytes(), got[0])
            self.assertEqual([p.name for p in path.parent.iterdir()], ["privacy-key"])

    def test_creation_never_exposes_an_empty_key(self):
        """F1: the key appears with all 32 bytes at once (temp file + link)."""
        path = self.tmp / "atomic" / "privacy-key"
        seen: list[int] = []
        real_link = self.mod.os.link

        def spy_link(src, dst, *a, **kw):
            self.assertFalse(Path(dst).exists())
            self.assertEqual(len(Path(src).read_bytes()), 32)
            real_link(src, dst, *a, **kw)
            seen.append(len(Path(dst).read_bytes()))

        with mock.patch.object(self.mod.os, "link", spy_link):
            key = self.mod.load_or_create_key(path)
        self.assertEqual(seen, [32])
        self.assertEqual(path.read_bytes(), key)

    def _empty_key(self, name: str, age_s: float) -> Path:
        path = self.tmp / name
        path.write_bytes(b"")
        t = time.time() - age_s
        os.utime(path, (t, t))
        return path

    def test_stale_empty_key_is_replaced(self):
        """F1: a 0-byte key (creator killed before writing) never made a token."""
        path = self._empty_key("empty-key", 60)
        key = self.mod.load_or_create_key(path)
        self.assertEqual(len(key), 32)
        self.assertEqual(path.read_bytes(), key)
        self.assertEqual(self.mod.load_or_create_key(path), key)
        self.assertEqual(
            [p.name for p in self.tmp.iterdir() if "empty-key" in p.name], ["empty-key"]
        )

    def test_fresh_empty_key_waits_for_its_writer(self):
        """F1: a just-created 0-byte key may be mid-write by another process."""
        path = self._empty_key("young-key", 0)
        writer = threading.Timer(0.3, lambda: path.write_bytes(FIXED_KEY))
        writer.start()
        try:
            self.assertEqual(self.mod.load_or_create_key(path), FIXED_KEY)
        finally:
            writer.cancel()

    def test_fresh_empty_key_replaced_once_stale(self):
        path = self._empty_key("stuck-key", 0)
        with mock.patch.object(self.mod, "KEY_STALE_S", 0.3):
            key = self.mod.load_or_create_key(path)
        self.assertEqual(len(key), 32)
        self.assertEqual(path.read_bytes(), key)

    def test_future_mtime_empty_key_is_replaced_at_once(self):
        """N1: a negative age (clock moved back) counts as stale, not as fresh."""
        path = self._empty_key("future-key", -3600)
        start = time.monotonic()
        key = self.mod.load_or_create_key(path)
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(len(key), 32)
        self.assertEqual(path.read_bytes(), key)

    def test_future_mtime_lock_is_removed(self):
        """N1: a lock dated in the future does not block repair of a stale key."""
        path = self._empty_key("locked-key", 60)
        lock = path.with_name(path.name + ".lock")
        lock.write_bytes(b"")
        t = time.time() + 3600
        os.utime(lock, (t, t))
        start = time.monotonic()
        key = self.mod.load_or_create_key(path)
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(len(key), 32)
        self.assertFalse(lock.exists())

    def test_concurrent_repair_of_empty_key_converges(self):
        for round_no in range(5):
            path = self._empty_key(f"stale-race-{round_no}", 60)
            got = self._race(path)
            self.assertEqual({type(k) for k in got}, {bytes}, got)
            self.assertEqual(len(set(got)), 1)
            self.assertEqual(path.read_bytes(), got[0])


class TestRedact(RedactCase):
    def test_home_prefix_every_spelling_keeps_separators(self):
        cases = {
            home_path("alice", "posix") + "/proj/a.py": "~/proj/a.py",
            home_path("alice", "win") + "\\proj\\a.py": "~\\proj\\a.py",
            escaped(home_path("alice", "win"))
            + "\\\\proj\\\\a.py": "~\\\\proj\\\\a.py",
            "c:" + "/users/" + "alice/x": "~/x",
            "cd " + home_path("alice", "gitbash") + "/x/y": "cd ~/x/y",
            "projects/"
            + home_path("alice", "transcript")
            + "-Documents-proj": "projects/~-Documents-proj",
            "end " + home_path("alice", "posix"): "end ~",
        }
        for src, want in cases.items():
            self.assertEqual(self.redact(src), want, src)

    def test_placeholder_home_names_untouched(self):
        for src in (
            home_path("<you>", "posix") + "/.claude",
            home_path("x", "win") + "\\p",
        ):
            self.assertEqual(self.redact(src), src)

    def test_username_elsewhere_whole_word_case_insensitive(self):
        src = "owner " + SYNTH_NAME.upper() + " and x" + SYNTH_NAME + "y"
        self.assertEqual(self.redact(src), "owner <user> and x" + SYNTH_NAME + "y")

    def test_home_name_with_space_consumed_whole(self):
        """F2: a profile folder with a space or hyphen leaves no part of the name."""
        guard = self.mod.Guard(["Alice Smith"], [], [])
        src = (
            home_path("Alice Smith", "win")
            + "\\Docs and "
            + home_path("Alice-Smith", "transcript")
            + "-Docs"
        )
        self.assertEqual(self.redact(src, guard), "~\\Docs and ~-Docs")
        src = (
            "cd "
            + home_path("alice smith", "gitbash")
            + "/x and "
            + escaped(home_path("ALICE SMITH", "win"))
            + "\\\\y"
        )
        self.assertEqual(self.redact(src, guard), "cd ~/x and ~\\\\y")

    def test_home_name_from_homes_with_hyphen_and_dot(self):
        guard = self.mod.Guard([SYNTH_NAME], [], [], homes=["a-b.c"])
        src = (
            home_path("a-b.c", "posix")
            + "/x "
            + home_path("a-b-c", "transcript")
            + "-Docs "
            + home_path("a-b.c", "transcript")
            + "-z"
        )
        self.assertEqual(self.redact(src, guard), "~/x ~-Docs ~-z")

    def test_known_home_name_needs_a_boundary(self):
        guard = self.mod.Guard(["Alice Smith"], [], [], homes=["bob"])
        src = (
            home_path("bobby", "posix")
            + "/x "
            + home_path("Alice Smithers", "posix")
            + "/y"
        )
        self.assertEqual(self.redact(src, guard), "~/x ~ Smithers/y")

    def test_home_names_from_environment(self):
        profile = home_path("Alice Smith", "win", drive="D") + "\\"
        env = {"HOME": "/home/a-b.c", "USERPROFILE": profile}
        with mock.patch.object(self.mod.Path, "home", return_value=Path("/x/qq")):
            self.assertEqual(self.mod.home_names(env), ["qq", "a-b.c", "Alice Smith"])

    def test_home_dir_name_is_not_left_as_user_token(self):
        src = home_path(SYNTH_NAME, "posix") + "/proj"
        self.assertEqual(self.redact(src), "~/proj")

    def test_terms_case_insensitive_longest_first(self):
        src = "a " + SYNTH_TERM.title() + " b " + SHORT_T.upper() + " c"
        want = (
            "a "
            + _token(FIXED_KEY, SYNTH_TERM)
            + " b "
            + _token(FIXED_KEY, SHORT_T)
            + " c"
        )
        self.assertEqual(self.redact(src), want)

    def test_term_containing_username_wins_over_username(self):
        src = "x " + TERM_WITH_USER + " y"
        self.assertEqual(
            self.redact(src), "x " + _token(FIXED_KEY, TERM_WITH_USER) + " y"
        )

    def test_uuids_left_as_is(self):
        src = "resume " + SYNTH_UUID
        self.assertEqual(self.redact(src), src)

    def test_output_passes_scan_except_uuids(self):
        src = "\n".join(
            [
                "cd " + escaped(home_path("alice", "win")) + "\\\\proj",
                "by " + SYNTH_NAME + " for " + SYNTH_TERM.upper(),
                home_path(SYNTH_NAME, "gitbash") + "/" + SHORT_T,
                "session " + SYNTH_UUID,
            ]
        )
        out = self.redact(src)
        reasons = [h.reason for ln in out.splitlines() for h in self.guard.scan(ln)]
        self.assertEqual(len(reasons), 1, out)
        self.assertIn("session-UUID", reasons[0])

    def test_idempotent(self):
        src = SYNTH_TERM + " " + SYNTH_NAME + " " + home_path("alice", "posix") + "/p"
        once = self.redact(src)
        self.assertEqual(self.redact(once), once)

    def test_allowlisted_hits_left_alone(self):
        allow = [re.compile(re.escape(SHORT_T), re.IGNORECASE)]
        guard = self.mod.Guard([SYNTH_NAME], [(SHORT_T, "t")], allow)
        self.assertEqual(self.redact("a " + SHORT_T, guard), "a " + SHORT_T)

    def test_no_terms_matched_never_loads_key(self):
        key = self.tmp / "never" / "privacy-key"
        with mock.patch.dict(self.mod.os.environ, {self.mod.ENV_KEY_FILE: str(key)}):
            out = self.mod.redact("by " + SYNTH_NAME, self.guard)
        self.assertEqual(out, "by <user>")
        self.assertFalse(key.exists())

    def test_default_key_used_when_not_given(self):
        with mock.patch.dict(
            self.mod.os.environ, {self.mod.ENV_KEY_FILE: str(self.key_file)}
        ):
            out = self.mod.redact(SHORT_T, self.guard)
        self.assertEqual(out, _token(FIXED_KEY, SHORT_T))


class TestRestore(RedactCase):
    def test_round_trip_terms(self):
        src = "a " + SYNTH_TERM + " b " + SHORT_T + " c " + TERM_WITH_USER
        out = self.redact(src)
        self.assertNotIn(SHORT_T, out)
        terms = [SYNTH_TERM, SHORT_T, TERM_WITH_USER]
        self.assertEqual(self.mod.restore(out, terms, key=FIXED_KEY), src)

    def test_accepts_load_terms_tuples(self):
        out = self.redact(SYNTH_TERM)
        restored = self.mod.restore(out, [(SYNTH_TERM, "label")], key=FIXED_KEY)
        self.assertEqual(restored, SYNTH_TERM)

    def test_unknown_tokens_unchanged(self):
        unknown = "<private:" + "0" * 8 + ">"
        text = "x " + unknown + " " + self.redact(SHORT_T)
        out = self.mod.restore(text, [SHORT_T], key=FIXED_KEY)
        self.assertEqual(out, "x " + unknown + " " + SHORT_T)

    def test_wrong_key_restores_nothing(self):
        out = self.redact(SHORT_T)
        self.assertEqual(self.mod.restore(out, [SHORT_T], key=bytes(32)), out)


class TestRedactCli(RedactCase):
    def cli(self, *args: str):
        cmd = [sys.executable, str(GUARD), "--terms-file", str(self.terms)]
        cmd += ["--allow-file", str(self.allow), "--username", SYNTH_NAME]
        cmd += ["--key-file", str(self.key_file), *args]
        return subprocess.run(cmd, capture_output=True, cwd=self.tmp, env=self.env)

    def test_redact_in_place_preserves_eol_and_utf8(self):
        crlf = self.tmp / "crlf.md"
        body = (
            "caf\u00e9 "
            + SYNTH_TERM
            + "\r\nhome "
            + home_path("alice", "posix")
            + "/p\r\n"
        )
        crlf.write_bytes(body.encode("utf-8"))
        lf = self.tmp / "lf.md"
        lf.write_bytes(("by " + SYNTH_NAME + "\n").encode("utf-8"))
        r = self.cli("--redact", crlf.name, lf.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        want = "caf\u00e9 " + _token(FIXED_KEY, SYNTH_TERM) + "\r\nhome ~/p\r\n"
        self.assertEqual(crlf.read_bytes(), want.encode("utf-8"))
        self.assertEqual(lf.read_bytes(), b"by <user>\n")
        # A redacted file passes the guard itself.
        self.assertEqual(self.run_guard(crlf, lf).returncode, 0)

    def test_redacted_uuid_still_flagged_by_check(self):
        f = self.write("s.md", SYNTH_TERM + " " + SYNTH_UUID + "\n")
        self.assertEqual(self.cli("--redact", f.name).returncode, 0)
        r = self.run_guard(f)
        self.assertEqual(r.returncode, 1)
        self.assertIn("session-UUID", r.stdout)
        self.assertNotIn("private term", r.stdout)

    def test_restore_prints_restored_text(self):
        src = "x " + SYNTH_TERM + "\r\ny\n"
        f = self.tmp / "r.md"
        f.write_bytes(src.encode("utf-8"))
        self.assertEqual(self.cli("--redact", f.name).returncode, 0)
        self.assertNotIn(SYNTH_TERM.encode(), f.read_bytes())
        r = self.cli("--restore", f.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, src.encode("utf-8"))
        self.assertNotIn(SYNTH_TERM.encode(), f.read_bytes())

    def test_redact_cli_consumes_the_environment_home_name(self):
        spaced = self.tmp / "Alice Smith"
        spaced.mkdir()
        self.env.update(HOME=str(spaced), USERPROFILE=str(spaced))
        f = self.write(
            "h.md",
            home_path("Alice Smith", "win")
            + "\\p "
            + home_path("Alice-Smith", "transcript")
            + "-q\n",
        )
        r = self.cli("--redact", f.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(f.read_text(encoding="utf-8"), "~\\p ~-q\n")

    def test_redact_missing_file_is_usage_error(self):
        r = self.cli("--redact", "gone.md")
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
