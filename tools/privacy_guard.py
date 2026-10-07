#!/usr/bin/env python3
"""Pre-commit privacy guard: keep private identifiers out of tracked files.

Usage (pre-commit passes the staged file paths):
    py -3.13 tools/privacy_guard.py [--terms-file F] [--allow-file F] [--username N ...] FILE...

Rejects, per line of each text file (and in the file path itself):
  (a) absolute user-home paths: <drive>:/Users/<name>, <drive>:\\Users\\<name>
      (any number of backslashes), Git Bash /<drive>/Users/<name> and the
      transcript-dir form <drive>--Users-<name>-, unless <name> is a placeholder
      (<...>, x, user, name, you);
  (b) the current OS username (os.getlogin(), USERNAME, USER) as a whole word,
      case-insensitive, length >= 3; shared service accounts (GitHub runners'
      runner/runneradmin, root) are not personal and are never treated as one;
  (c) session-UUID-shaped ids (8-4-4-4-12 hex);
  (d) private terms, matched as case-insensitive substrings: the union
      (de-duplicated, case-insensitive) of
        1. env var CCV_PRIVACY_TERMS (newline- or comma-separated; CI/cloud),
        2. ~/.claude/privacy-terms (user-level, shared by every clone),
        3. the UNTRACKED per-clone file (--terms-file, default
           `git rev-parse --git-path info/privacy-terms`);
      blank and #-comment entries are ignored. When no source yields a term,
      one warning line goes to stderr; that alone never fails the run.

A hit is suppressed when its matched text fully matches a regex (one per
non-empty, non-# line) in the tracked, reviewed allowlist `.privacy-allow` at
the repo root. Binary files (NUL byte in the first 8 KiB) are skipped.

Output: `file:line: reason` per hit, with the private text redacted.
Exit: 0 clean, 1 hits found, 2 usage error (bad allowlist regex).
Stdlib only.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER_NAMES = {"x", "user", "name", "you"}
# Shared service accounts (GitHub-hosted runners: runner on Linux/macOS, runneradmin
# on Windows; root in containers). Not personal, and as whole words they hit ordinary
# text ("runner.os", "repo root"), so CI must not flag them.
SERVICE_ACCOUNTS = {"runner", "runneradmin", "root"}
_NAME = r"(?P<name><[^>\s]*>|[^/\\\s\"'`<>:;,|*?()\[\]{}]+)"
HOME_PATTERNS = [
    # C:/Users/<name>, C:\Users\<name>, C:\\Users\\<name> (escaped in JSON/shell)
    re.compile(
        r"(?<![A-Za-z])(?P<pre>[A-Za-z]:(?:/|\\+)Users(?:/|\\+))" + _NAME, re.IGNORECASE
    ),
    # Git Bash: /c/Users/<name>
    re.compile(r"(?<![\w.])(?P<pre>/[A-Za-z]/Users/)" + _NAME, re.IGNORECASE),
    # Claude Code transcript dir: C--Users-<name>-...
    re.compile(
        r"(?<![A-Za-z])(?P<pre>[A-Za-z]--Users-)(?P<name><[^>\s]*>|[A-Za-z0-9_.]+)",
        re.IGNORECASE,
    ),
]
UUID_RE = re.compile(
    r"(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])",
    re.IGNORECASE,
)
BINARY_SNIFF = 8192
ENV_TERMS = "CCV_PRIVACY_TERMS"
NO_TERMS_WARNING = (
    "privacy-guard: no private-terms list found (CCV_PRIVACY_TERMS, "
    "~/.claude/privacy-terms, .git/info/privacy-terms) - only paths/username/"
    "session ids are checked"
)


@dataclass
class Hit:
    """One private-identifier match: the raw text and its redacted output reason."""

    text: str  # matched text (allowlist is checked against this)
    reason: str  # redacted reason for output


def _git(*args: str) -> str | None:
    """Stripped stdout of `git ARGS`; None on failure, timeout or empty output."""
    try:
        out = subprocess.check_output(
            ["git", *args], stderr=subprocess.DEVNULL, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):  # incl. CalledProcessError
        return None
    return out.strip() or None


def _lines(path: Path) -> list[str]:
    """Non-empty, non-# lines of a text file; [] when it does not exist."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (FileNotFoundError, NotADirectoryError):
        return []
    out = []
    for line in raw.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def default_terms_file() -> Path | None:
    """Path of the untracked .git/info/privacy-terms file; None outside a repo."""
    p = _git("rev-parse", "--git-path", "info/privacy-terms")
    return Path(p) if p else None


def user_terms_file() -> Path:
    """User-level ~/.claude/privacy-terms, shared by every clone on this machine."""
    return Path.home() / ".claude" / "privacy-terms"


def _env_terms(env: Mapping[str, str]) -> list[str]:
    """Entries of CCV_PRIVACY_TERMS split on newlines and commas; blanks/# dropped."""
    out = []
    for part in re.split(r"[,\r\n]", env.get(ENV_TERMS, "")):
        s = part.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def load_terms(
    terms_file: Path | None, env: Mapping[str, str] | None = None
) -> list[tuple[str, str]]:
    """(term, source label) from env var, user file and per-clone file, de-duplicated."""
    env = os.environ if env is None else env
    sources = [
        (ENV_TERMS, _env_terms(env)),
        ("~/.claude/privacy-terms", _lines(user_terms_file())),
        (".git/info/privacy-terms", _lines(terms_file) if terms_file else []),
    ]
    seen: set[str] = set()
    out = []
    for label, terms in sources:
        for i, t in enumerate(terms, 1):
            if t.lower() not in seen:
                seen.add(t.lower())
                out.append((t, f"{label} entry {i}"))
    return out


def default_allow_file() -> Path:
    """`.privacy-allow` at the repo root (cwd when git cannot report the root)."""
    top = _git("rev-parse", "--show-toplevel")
    return Path(top or ".") / ".privacy-allow"


def detect_usernames() -> list[str]:
    """Candidate OS usernames: os.getlogin(), $USERNAME, $USER (may be empty).

    Shared service accounts (SERVICE_ACCOUNTS) are dropped.
    """
    names = []
    try:
        names.append(os.getlogin())
    except OSError:
        pass
    names += [os.environ.get("USERNAME", ""), os.environ.get("USER", "")]
    return [n for n in names if n.lower() not in SERVICE_ACCOUNTS]


def _is_placeholder(name: str) -> bool:
    """True for a documentation stand-in name such as <you>, x or user."""
    return name.startswith("<") or name.lower() in PLACEHOLDER_NAMES


class Guard:
    """Compiled privacy rules: home paths, usernames, UUIDs, terms, allowlist."""

    def __init__(
        self,
        usernames: list[str],
        terms: list[tuple[str, str]],
        allow: list[re.Pattern[str]],
    ):
        """Compile whole-word username and substring term patterns (names >= 3 chars)."""
        seen: dict[str, str] = {}
        for n in usernames:
            if n and len(n) >= 3:
                seen.setdefault(n.lower(), n)
        self.user_res = [
            re.compile(r"(?<!\w)" + re.escape(n) + r"(?!\w)", re.IGNORECASE)
            for n in seen.values()
        ]
        self.term_res = [
            (label, re.compile(re.escape(t), re.IGNORECASE)) for t, label in terms
        ]
        self.allow = allow

    def _allowed(self, text: str) -> bool:
        """True when some allowlist regex fully matches the hit text."""
        return any(a.fullmatch(text) for a in self.allow)

    def scan(self, text: str) -> list[Hit]:
        """Every non-allowlisted hit in one line of text, in rule order."""
        hits: list[Hit] = []
        for pat in HOME_PATTERNS:
            for m in pat.finditer(text):
                if not _is_placeholder(m.group("name")):
                    redacted = m.group("pre") + "<redacted>"
                    hits.append(
                        Hit(m.group(0), f"absolute user-home path ({redacted})")
                    )
        for pat in self.user_res:
            for m in pat.finditer(text):
                hits.append(Hit(m.group(0), "current OS username"))
        for m in UUID_RE.finditer(text):
            hits.append(
                Hit(m.group(0), f"session-UUID-shaped id ({m.group(0)[:4]}...)")
            )
        for label, pat in self.term_res:
            for m in pat.finditer(text):
                hits.append(Hit(m.group(0), f"private term ({label})"))
        return [h for h in hits if not self._allowed(h.text)]

    def check_file(self, arg: str) -> list[str]:
        """`file[:line]: reason` for hits in the path and each line; binaries skipped."""
        out = []
        shown = arg
        for h in self.scan(shown.replace("\\", "/")):
            out.append(f"{shown}: {h.reason} in file path")
        try:
            data = Path(arg).read_bytes()
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            return out
        if b"\x00" in data[:BINARY_SNIFF]:
            return out
        text = data.decode("utf-8", errors="replace")
        for no, line in enumerate(text.splitlines(), 1):
            for h in self.scan(line):
                out.append(f"{shown}:{no}: {h.reason}")
        return out


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse FILE... plus the --terms-file/--allow-file/--username overrides."""
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None
    )
    ap.add_argument("files", nargs="*")
    ap.add_argument("--terms-file", type=Path, help="default: .git/info/privacy-terms")
    ap.add_argument(
        "--allow-file", type=Path, help="default: <repo root>/.privacy-allow"
    )
    ap.add_argument(
        "--username",
        action="append",
        help="override OS username detection (repeatable)",
    )
    return ap.parse_args(argv)


def _load_allow(allow_file: Path) -> list[re.Pattern[str]] | None:
    """Compiled allowlist regexes; None (reason on stderr) on the first bad one."""
    allow = []
    for i, line in enumerate(_lines(allow_file), 1):
        try:
            allow.append(re.compile(line))
        except re.error as e:
            print(
                f"privacy_guard: bad regex in allow file {allow_file} entry {i}: {e}",
                file=sys.stderr,
            )
            return None
    return allow


def _build_guard(args: argparse.Namespace) -> Guard | None:
    """Guard from the CLI overrides or the git/OS defaults; None on a bad allowlist."""
    terms = load_terms(args.terms_file or default_terms_file())
    if not terms:
        print(NO_TERMS_WARNING, file=sys.stderr)
    allow = _load_allow(args.allow_file or default_allow_file())
    if allow is None:
        return None
    return Guard(args.username or detect_usernames(), terms, allow)


def main(argv: list[str] | None = None) -> int:
    """Scan FILE... and print one line per hit; exit 0 clean, 1 hits, 2 usage error."""
    args = _parse_args(argv)
    guard = _build_guard(args)
    if guard is None:
        return 2
    problems = [p for f in args.files for p in guard.check_file(f)]
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
