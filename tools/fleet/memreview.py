"""Memory review, stage 1: a local check of every project's auto-memory.

Flags index mismatches, broken ``[[name]]`` links, stale path references, near duplicates and
memories whose project folder is gone. It uses no model, sends nothing anywhere and changes no
memory: it only writes ``~/.claude/fleet/memory-review/YYYY-MM-DD.md`` (masked), about once a
week from ``collect``. Stale references read backticked paths only, so a flag is a prompt to
look, not a verdict. Judging meaning or merging text is left to a session and the user.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import launch, lessons, model

REVIEW_DAYS = 7
DUPLICATE_AT = (
    0.13  # share of distinctive words; set from the real memories, 2026-11-05
)
COMMON_SHARE = 0.15  # a word in more memories than this says nothing about overlap
KINDS = (
    ("index", "Index mismatch"),
    ("stale", "Stale reference"),
    ("duplicate", "Near duplicate or overlap"),
    ("old_project", "Old project"),
    ("link", "Broken link (low priority: links may name a memory not written yet)"),
)
_INDEX_LINK = re.compile(r"\]\(([^)\s]+\.md)\)")
_WIKI_LINK = re.compile(r"\[\[([^\]\n]+)\]\]")
_CODE = re.compile(r"`([^`\n]+)`")
_FILE_END = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,5}$")
_LINE_SUFFIX = re.compile(r":\d+(?:[-:]\d+)?$")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_WORD = re.compile(r"[A-Za-z][\w-]{3,}")
_HAS_WORD = re.compile(r"\w")
_NOT_A_PATH = re.compile(r"[\s<>*?$%{}|\"']|://")
_FLAGS_LINE = re.compile(r"^Flags: (\d+)$", re.MULTILINE)


@dataclass(frozen=True)
class Memory:
    """One memory file."""

    project: str  # project title
    path: Path
    name: str
    body: str


@dataclass(frozen=True)
class Flag:
    """One finding."""

    kind: str
    project: str
    detail: str


def _split_frontmatter(text: str) -> tuple[str, str]:
    """(frontmatter, body); frontmatter is empty when the file has none."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :])
    return "", text


def read_memory(project: str, path: Path) -> Memory:
    """Parse one memory file; its name falls back to the file stem."""
    text = path.read_text(encoding="utf-8", errors="replace")
    front, body = _split_frontmatter(text)
    found = re.search(r"^name:\s*(.+?)\s*$", front, re.MULTILINE)
    name = found.group(1).strip("\"'") if found else path.stem
    return Memory(project, path, name, body)


def project_title(slug: str, cwds: list[str], home: Path) -> str:
    """The project folder name; for a gone folder, the slug minus the home prefix."""
    if cwds:
        return Path(cwds[0]).name
    prefix = re.sub(r"[^A-Za-z0-9]", "-", str(home)) + "-"
    return slug.removeprefix(prefix)


def project_memories(folder: Path, title: str) -> list[Memory]:
    """The project's memories: top-level files, so ``.archive`` and the index are skipped."""
    return [
        read_memory(title, p)
        for p in sorted(folder.glob("*.md"))
        if p.name != "MEMORY.md"
    ]


def index_flags(folder: Path, memories: list[Memory], project: str) -> list[Flag]:
    """MEMORY.md lines without a file, and files without a line."""
    index = folder / "MEMORY.md"
    text = index.read_text(encoding="utf-8", errors="replace") if index.exists() else ""
    linked = {m.group(1).replace("\\", "/") for m in _INDEX_LINK.finditer(text)}
    flags = [
        Flag("index", project, f"MEMORY.md points to a missing file: {name}")
        for name in sorted(linked)
        if not (folder / name).exists()
    ]
    flags += [
        Flag("index", project, f"no MEMORY.md line for {m.path.name}")
        for m in memories
        if m.path.name not in linked
    ]
    return flags


def link_flags(memories: list[Memory]) -> list[Flag]:
    """``[[name]]`` links that match no memory name or file stem in the project."""
    known = {m.name for m in memories} | {m.path.stem for m in memories}
    return [
        Flag("link", m.project, f"{m.path.name}: [[{target}]] matches no memory")
        for m in memories
        for target in sorted({t.strip() for t in _WIKI_LINK.findall(m.body)})
        if target not in known
    ]


def path_reference(token: str) -> str | None:
    """The file path a backticked token names, else None.

    A path has a separator and either a file extension, a trailing separator, or a home or
    drive start. Slash commands, owner/repo names, URLs and placeholders are not paths.
    """
    token = _LINE_SUFFIX.sub("", token.strip())
    if not _HAS_WORD.search(token) or _NOT_A_PATH.search(token) or token[0] in "/-":
        return None
    if "/" not in token and "\\" not in token:
        return None
    rooted = token.startswith("~") or _DRIVE.match(token) is not None
    if rooted or token[-1] in "/\\" or _FILE_END.search(token):
        return token
    return None


def stale_flags(memory: Memory, cwds: list[str], home: Path) -> list[Flag]:
    """Backticked paths found neither where they point, in the project folder nor beside it."""
    flags = []
    for token in sorted(set(_CODE.findall(memory.body))):
        ref = path_reference(token)
        if ref is None:
            continue
        if ref.startswith("~"):
            places = [home / ref[1:].lstrip("/\\")]
        elif _DRIVE.match(ref):
            places = [Path(ref)]
        elif cwds:
            # written from the project folder, or from the folder holding the projects
            places = [
                base / ref for cwd in cwds for base in (Path(cwd), Path(cwd).parent)
            ]
        else:
            continue  # no folder to look in: reported as an old project
        if not any(p.exists() for p in places):
            flags.append(
                Flag("stale", memory.project, f"{memory.path.name}: `{ref}` not found")
            )
    return flags


def _word_sets(memories: list[Memory]) -> list[tuple[Memory, set[str]]]:
    """Each memory's distinctive words: those in at most COMMON_SHARE of all memories."""
    sets = [
        (m, {w.lower() for w in _WORD.findall(f"{m.name} {m.body}")}) for m in memories
    ]
    counts = Counter(w for _, words in sets for w in words)
    limit = max(3, COMMON_SHARE * len(memories))
    return [(m, {w for w in words if counts[w] <= limit}) for m, words in sets]


def duplicate_flags(
    memories: list[Memory], threshold: float = DUPLICATE_AT
) -> list[Flag]:
    """Pairs sharing at least ``threshold`` of their distinctive words; across projects is marked."""
    sets = _word_sets(memories)
    flags = []
    for i, (a, wa) in enumerate(sets):
        for b, wb in sets[i + 1 :]:
            union = len(wa | wb)
            share = len(wa & wb) / union if union else 0.0
            if share < threshold:
                continue
            if a.project == b.project:
                pair = f"{a.path.name} and {b.path.name}"
            else:
                pair = f"{a.path.name} and {b.path.name} in {b.project} (across projects: lesson)"
            flags.append(Flag("duplicate", a.project, f"{pair}: {share:.0%} shared"))
    return flags


def scan() -> list[Flag]:
    """Every flag across ``~/.claude/projects/*/memory``."""
    home = model.home_dir()
    flags: list[Flag] = []
    everything: list[Memory] = []
    folders = sorted((model.claude_dir() / "projects").glob("*/memory"))
    for folder in folders:
        if not folder.is_dir():
            continue
        if not any(folder.glob("*.md")):
            continue
        cwds = launch.slug_cwds(folder.parent.name)
        project = project_title(folder.parent.name, cwds, home)
        memories = project_memories(folder, project)
        everything += memories
        if not cwds:
            flags.append(
                Flag("old_project", project, "project folder no longer exists")
            )
        flags += index_flags(folder, memories, project)
        flags += link_flags(memories)
        for memory in memories:
            flags += stale_flags(memory, cwds, home)
    return flags + duplicate_flags(everything)


def markdown(flags: list[Flag], date: str) -> str:
    """The report, grouped by kind."""
    lines = [
        f"# Memory review {date}",
        "",
        f"Flags: {len(flags)}",
        "",
        "Report only: no memory was changed. Stale references read backticked paths only.",
    ]
    for kind, title in KINDS:
        rows = [f for f in flags if f.kind == kind]
        if not rows:
            continue
        lines += ["", f"## {title} ({len(rows)})", ""]
        lines += [f"- {f.project}: {f.detail}" for f in rows]
    return "\n".join(lines) + "\n"


def masked_report(flags: list[Flag], date: str) -> str:
    """The report with private terms masked."""
    return lessons.mask(markdown(flags, date), lessons.default_guard(model.home_dir()))


def review_dir() -> Path:
    """``~/.claude/fleet/memory-review``."""
    return model.fleet_dir() / "memory-review"


def err_path() -> Path:
    """``~/.claude/fleet/memory-review.err``: reason of the last failed weekly write."""
    return model.fleet_dir() / "memory-review.err"


def local_date(now: _dt.datetime) -> _dt.date:
    """The date in the harness time zone."""
    return now.astimezone(model.harness_tz()).date()


def latest() -> Path | None:
    """The newest report file."""
    reports = sorted(review_dir().glob("????-??-??.md"))
    return reports[-1] if reports else None


def is_due(now: _dt.datetime) -> bool:
    """True with no report, or when the newest is REVIEW_DAYS old."""
    path = latest()
    if path is None:
        return True
    try:
        last = _dt.date.fromisoformat(path.stem)
    except ValueError:
        return True
    return (local_date(now) - last).days >= REVIEW_DAYS


def write(now: _dt.datetime) -> Path:
    """Save today's report (atomic); the path."""
    date = local_date(now).isoformat()
    return model.write_atomic(review_dir() / f"{date}.md", masked_report(scan(), date))


def write_if_due(now: _dt.datetime) -> Path | None:
    """Save a report when one is due; the path, else None."""
    return write(now) if is_due(now) else None


def render(now: _dt.datetime, save: bool = False) -> str:
    """CLI text: the written path (save), else a fresh report, written nowhere."""
    if save:
        return str(write(now))
    return masked_report(scan(), local_date(now).isoformat()).rstrip("\n")


def notice() -> str | None:
    """One report line: a failed weekly write, else the flags in the newest report."""
    path = err_path()
    if path.exists():
        text = " ".join(path.read_text(encoding="utf-8", errors="replace").split())
        return f"memory review: last weekly write failed ({text[:200] or 'reason unknown'})"
    report = latest()
    if report is None:
        return None
    found = _FLAGS_LINE.search(report.read_text(encoding="utf-8", errors="replace"))
    if found is None or found.group(1) == "0":
        return None
    return f"memory review: {found.group(1)} flags in {report}"
