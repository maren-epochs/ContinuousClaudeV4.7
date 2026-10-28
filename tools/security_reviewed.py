#!/usr/bin/env python3
"""Count `tldr secure` findings, minus the ones a project marked as reviewed.

Used by scripts/readiness.sh (security_scan). ``.security-reviewed`` in the project root
lists reviewed false positives, one per line, ``#`` comments allowed::

    <path> | <text the finding's description contains> | <code on the flagged line> | <why it is safe>

A finding is reviewed only when all three match: its file ends with <path> (slashes and
case normalized), its description contains the text, and the source line it points at
(read from the project, relative to the .security-reviewed folder) contains the code. A
new finding of the same kind elsewhere in the file, or the reviewed line being changed,
counts again. Only taint and resource_leak findings count, matching the summary fields
readiness has always scored. Without a ``findings`` list (older tldr) the summary counts
are used and nothing is discounted.

Usage: security_reviewed.py <secure.json> [<.security-reviewed>]
Prints ``<unreviewed> <reviewed>``; exit 1 when secure.json is unreadable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

COUNTED = ("taint", "resource_leak")

Entry = tuple[str, str, str]  # (path suffix, description text, code on the line)


def load_reviewed(path: Path | None) -> list[Entry]:
    """Reviewed entries; missing file and lines without all four fields skipped."""
    if path is None or not path.is_file():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 4 and all(parts[:4]):
            out.append((_norm(parts[0]), parts[1], parts[2]))
    return out


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./").lower()


def _source_line(root: Path, finding: dict[str, Any]) -> str | None:
    """The source line a finding points at, or None when it cannot be read."""
    try:
        n = int(finding.get("line", 0))
        rel = str(finding.get("file", "")).replace("\\", "/")
        lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[n - 1] if 1 <= n <= len(lines) else None
    except (OSError, ValueError):
        return None


def is_reviewed(finding: dict[str, Any], reviewed: list[Entry], root: Path) -> bool:
    """Whether an entry matches the finding's file, description and flagged line."""
    file = _norm(str(finding.get("file", "")))
    desc = str(finding.get("description", ""))
    candidates = [
        code for p, text, code in reviewed if file.endswith(p) and text in desc
    ]
    if not candidates:
        return False
    line = _source_line(root, finding)
    return line is not None and any(code in line for code in candidates)


def count(
    data: dict[str, Any], reviewed: list[Entry], root: Path = Path(".")
) -> tuple[int, int]:
    """(unreviewed, reviewed) counted findings; root is where finding paths start."""
    findings = data.get("findings")
    if not isinstance(findings, list):
        s = data.get("summary") or {}
        return int(s.get("taint_count", 0)) + int(s.get("leak_count", 0)), 0
    counted = [
        f for f in findings if isinstance(f, dict) and f.get("category") in COUNTED
    ]
    hits = sum(1 for f in counted if is_reviewed(f, reviewed, root))
    return len(counted) - hits, hits


def main(argv: list[str]) -> int:
    """CLI entry; see the module docstring."""
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        data = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 1
    if not isinstance(data, dict):
        return 1
    reviewed_file = Path(argv[1]) if len(argv) > 1 else None
    reviewed = load_reviewed(reviewed_file)
    root = reviewed_file.parent if reviewed_file else Path(".")
    unreviewed, hits = count(data, reviewed, root)
    print(unreviewed, hits)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
