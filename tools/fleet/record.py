"""Project record: everything needed to return to a project lives in its own folder.

Handoffs go to ``<project>/thoughts/shared/handoffs/`` (created when missing) and a copy of the
project's auto-memory to ``<project>/thoughts/shared/memory/``. Claude Code still reads memory from
``~/.claude/projects/<slug>/memory/``; the copy is the record that travels with the folder.

``thoughts/`` stays on this PC: in a git repo it is added to ``.git/info/exclude`` (local, never
committed). A repo that already tracks files under ``thoughts/`` chose to commit its handoffs, so
it is left alone. The home folder, ``Documents`` itself and drive roots are not projects and get
nothing. Stdlib only.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

from . import launch, model

HANDOFFS = Path("thoughts") / "shared" / "handoffs"
MEMORY = Path("thoughts") / "shared" / "memory"
EXCLUDE_LINE = "/thoughts/"
MEMORY_LINE = "/thoughts/shared/memory/"
EVERY_S = 30 * 60  # collect refreshes every record at most this often
GIT_TIMEOUT = 10


def is_project(folder: Path) -> bool:
    """False for the home folder, ``Documents`` itself, drive roots and temp folders."""
    try:
        path = folder.resolve()
    except OSError:
        return False
    home = model.home_dir().resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    if path == temp or temp in path.parents:
        return False
    return path.parent != path and path not in (home, home / "Documents")


def _git(folder: Path, *args: str) -> tuple[int, str]:
    try:
        done = subprocess.run(
            ["git", "-C", str(folder), *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=GIT_TIMEOUT,
            creationflags=model.NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return done.returncode, done.stdout.strip()


def keep_local(folder: Path) -> str:
    """Make sure git never picks up ``thoughts/``; returns what was found or done."""
    code, top = _git(folder, "rev-parse", "--show-toplevel")
    if code != 0 or not top:
        return "not a git repo"
    if _git(folder, "ls-files", "thoughts")[1]:
        # The repo commits its handoffs on purpose; only the memory copy stays local.
        if _ignored(folder, MEMORY / "x.md"):
            return "repo commits handoffs; memory copy already excluded"
        return _exclude(
            folder, MEMORY_LINE, "repo commits handoffs; memory copy excluded"
        )
    if _ignored(folder, HANDOFFS / "x.yaml"):
        return "already ignored"
    return _exclude(folder, EXCLUDE_LINE, "excluded from git")


def _ignored(folder: Path, path: Path) -> bool:
    return _git(folder, "check-ignore", "-q", path.as_posix())[0] == 0


def _exclude(folder: Path, line: str, done: str) -> str:
    """Append ``line`` to the repo's local ``.git/info/exclude``."""
    code, path = _git(folder, "rev-parse", "--git-path", "info/exclude")
    if code != 0 or not path:
        return "exclude file not found"
    exclude = Path(path) if Path(path).is_absolute() else folder / path
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    exclude.write_text(text + line + "\n", encoding="utf-8")
    return done


def mirror_memory(source: Path, target: Path) -> int:
    """Copy ``source/*.md`` to ``target``, dropping copies whose original is gone; files changed."""
    originals = {p.name: p for p in source.glob("*.md")} if source.is_dir() else {}
    if not originals and not target.is_dir():
        return 0
    target.mkdir(parents=True, exist_ok=True)
    changed = 0
    for name, path in originals.items():
        data = path.read_bytes()
        copy = target / name
        if not copy.exists() or copy.read_bytes() != data:
            copy.write_bytes(data)
            changed += 1
    for copy in target.glob("*.md"):
        if copy.name not in originals:
            copy.unlink()
            changed += 1
    return changed


def slug_dir(folder: Path) -> Path:
    """``~/.claude/projects/<slug>`` for a project folder."""
    return model.claude_dir() / "projects" / launch._SLUG.sub("-", str(folder))


def save(folder: Path) -> str:
    """Create the handoff folder, keep ``thoughts/`` out of git, refresh the memory copy."""
    folder = Path(folder)
    if not folder.is_dir():
        return f"{folder}: not a folder"
    if not is_project(folder):
        return f"{folder}: not a project folder, nothing saved"
    (folder / HANDOFFS).mkdir(parents=True, exist_ok=True)
    git = keep_local(folder)
    changed = mirror_memory(slug_dir(folder) / "memory", folder / MEMORY)
    return f"{folder.name}: handoffs in {HANDOFFS.as_posix()}, {changed} memory file(s) updated, git: {git}"


def save_all() -> list[str]:
    """``save`` for every project folder Claude Code has a record of."""
    out = []
    for entry in sorted((model.claude_dir() / "projects").glob("*")):
        if not entry.is_dir():
            continue
        cwds = launch.slug_cwds(entry.name)
        if len(cwds) == 1 and is_project(Path(cwds[0])):
            out.append(save(Path(cwds[0])))
    return out


def state_path() -> Path:
    """``~/.claude/fleet/record-state.json``: when ``save_all`` last ran."""
    return model.fleet_dir() / "record-state.json"


def save_all_if_due(now: float | None = None) -> list[str] | None:
    """``save_all`` at most every EVERY_S seconds; None when not due."""
    now = time.time() if now is None else now
    raw = model.read_json_dict(state_path()) or {}
    last = raw.get("at")
    if isinstance(last, (int, float)) and now - last < EVERY_S:
        return None
    lines = save_all()
    model.write_atomic(state_path(), json.dumps({"at": now, "lines": lines}) + "\n")
    return lines
