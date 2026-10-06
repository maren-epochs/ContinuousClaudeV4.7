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
      case-insensitive, length >= 3;
  (c) session-UUID-shaped ids (8-4-4-4-12 hex);
  (d) every non-empty, non-# line of the UNTRACKED private-terms file
      (default: `git rev-parse --git-path info/privacy-terms`), matched as a
      case-insensitive substring.

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
from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER_NAMES = {"x", "user", "name", "you"}
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


@dataclass
class Hit:
    text: str  # matched text (allowlist is checked against this)
    reason: str  # redacted reason for output


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


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
    p = _git("rev-parse", "--git-path", "info/privacy-terms")
    return Path(p) if p else None


def default_allow_file() -> Path:
    top = _git("rev-parse", "--show-toplevel")
    return Path(top or ".") / ".privacy-allow"


def detect_usernames() -> list[str]:
    names = []
    try:
        names.append(os.getlogin())
    except OSError:
        pass
    names += [os.environ.get("USERNAME", ""), os.environ.get("USER", "")]
    return names


def _is_placeholder(name: str) -> bool:
    return name.startswith("<") or name.lower() in PLACEHOLDER_NAMES


class Guard:
    def __init__(
        self, usernames: list[str], terms: list[str], allow: list[re.Pattern[str]]
    ):
        seen: dict[str, str] = {}
        for n in usernames:
            if n and len(n) >= 3:
                seen.setdefault(n.lower(), n)
        self.user_res = [
            re.compile(r"(?<!\w)" + re.escape(n) + r"(?!\w)", re.IGNORECASE)
            for n in seen.values()
        ]
        self.term_res = [
            (i, re.compile(re.escape(t), re.IGNORECASE)) for i, t in enumerate(terms, 1)
        ]
        self.allow = allow

    def _allowed(self, text: str) -> bool:
        return any(a.fullmatch(text) for a in self.allow)

    def scan(self, text: str) -> list[Hit]:
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
        for i, pat in self.term_res:
            for m in pat.finditer(text):
                hits.append(Hit(m.group(0), f"private term (privacy-terms entry {i})"))
        return [h for h in hits if not self._allowed(h.text)]

    def check_file(self, arg: str) -> list[str]:
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


def main(argv: list[str] | None = None) -> int:
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
    args = ap.parse_args(argv)

    terms_file = args.terms_file or default_terms_file()
    terms = _lines(terms_file) if terms_file else []
    allow_file = args.allow_file or default_allow_file()
    allow = []
    for i, line in enumerate(_lines(allow_file), 1):
        try:
            allow.append(re.compile(line))
        except re.error as e:
            print(
                f"privacy_guard: bad regex in allow file {allow_file} entry {i}: {e}",
                file=sys.stderr,
            )
            return 2
    usernames = args.username if args.username else detect_usernames()

    guard = Guard(usernames, terms, allow)
    problems: list[str] = []
    for f in args.files:
        problems += guard.check_file(f)
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
