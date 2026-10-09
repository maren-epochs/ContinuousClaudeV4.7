"""Fleet launcher: open project sessions as named tabs in one Windows Terminal window.

Candidates come from ~/.claude/projects: each transcript folder's newest transcript
names its cwd; a project is offered when that folder still exists, is not home or a
temp dir, and has a handoff to resume. Each tab runs a fresh ``claude '/resume-handoff'``
in PowerShell, titled with the live session's name, else the project folder name
(``--suppressApplicationTitle`` keeps Claude Code from retitling it).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import collect, model
from .model import FleetState

TAB_PROMPT = "claude '/resume-handoff'"
CWD_HEAD_BYTES = 64 * 1024
_CWD = re.compile(r'"cwd":\s*("(?:[^"\\]|\\.)*")')
_SLUG = re.compile(r"[^A-Za-z0-9]")


class LaunchError(Exception):
    """The launcher refuses; the message says why."""


@dataclass
class Target:
    """One project to open as a tab."""

    title: str
    cwd: str
    last: float = 0.0


def path_key(path: str) -> str:
    """Comparison key for a folder path (case and separators normalized)."""
    return os.path.normcase(os.path.normpath(path))


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def transcript_cwd(path: Path) -> str | None:
    """The first ``cwd`` recorded in a transcript's head."""
    match = _CWD.search(collect.read_text(path, CWD_HEAD_BYTES))
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except ValueError:
        return None
    return value if isinstance(value, str) and value else None


def _excluded(cwd: str) -> bool:
    key = path_key(cwd)
    if key == path_key(str(model.home_dir())):
        return True
    temp = path_key(tempfile.gettempdir())
    return key == temp or key.startswith(temp + os.sep)


def renamed_cwd(slug: str, cwd: str) -> str | None:
    """The renamed project folder: a sibling of the missing ``cwd`` whose slug is ``slug``.

    Claude Code names a transcript folder after its cwd with every non-alphanumeric
    character replaced by '-'; when the project folder was renamed and the transcript
    folder renamed to match, the transcripts still record the old cwd.
    """
    parent = Path(cwd).parent
    if not parent.is_dir():
        return None
    for child in parent.iterdir():
        if child.is_dir() and _SLUG.sub("-", str(child)) == slug:
            return str(child)
    return None


def candidates(state: FleetState | None) -> list[Target]:
    """Openable projects, by title: live session name, else the folder name."""
    names = {
        path_key(s.cwd): s.name
        for s in (state.sessions if state else [])
        if s.alive and s.cwd and s.name
    }
    claude = model.claude_dir()
    projects = claude / "projects"
    found: dict[str, Target] = {}
    for folder in projects.iterdir() if projects.is_dir() else []:
        transcripts = list(folder.glob("*.jsonl")) if folder.is_dir() else []
        if not transcripts:
            continue
        newest = max(transcripts, key=_mtime)
        cwd = transcript_cwd(newest)
        if cwd and not Path(cwd).is_dir():
            cwd = renamed_cwd(folder.name, cwd)
        if not cwd or _excluded(cwd):
            continue
        if collect.newest_handoff_file(collect.handoff_root(cwd, claude)) is None:
            continue
        key, last = path_key(cwd), _mtime(newest)
        if key not in found or last > found[key].last:
            title = names.get(key) or Path(os.path.normpath(cwd)).name
            found[key] = Target(title=title, cwd=os.path.normpath(cwd), last=last)
    return sorted(found.values(), key=lambda t: t.title.lower())


def select(found: list[Target], picks: list[str]) -> list[Target]:
    """Targets for each pick: a list number, a title or folder name, or a directory."""
    chosen: dict[str, Target] = {}
    for pick in picks:
        target = _resolve(found, pick)
        chosen.setdefault(path_key(target.cwd), target)
    return list(chosen.values())


def _resolve(found: list[Target], pick: str) -> Target:
    if pick.isdigit() and 1 <= int(pick) <= len(found):
        return found[int(pick) - 1]
    lowered = pick.lower()
    for t in found:
        if lowered in (t.title.lower(), Path(t.cwd).name.lower()):
            return t
    if os.sep in pick or "/" in pick:
        if Path(pick).is_dir():
            cwd = os.path.normpath(os.path.abspath(pick))
            return Target(title=Path(cwd).name, cwd=cwd)
        raise LaunchError(f"not a directory: {pick}")
    raise LaunchError(f"no project named {pick!r} (fleet.py open lists them)")


def choose(listed: list[Target], answer: str) -> list[Target]:
    """Targets an interactive answer picks from ``listed``; [] when it is empty.

    Tokens split on spaces or commas: a list number, a range ``5-7``, a title or folder
    name, or ``a``/``all`` for every listed project.
    """
    chosen: dict[str, Target] = {}
    for token in answer.replace(",", " ").split():
        if token.lower() in ("a", "all"):
            picked = listed
        elif re.fullmatch(r"\d+-\d+", token):
            lo, hi = (int(n) for n in token.split("-"))
            if not (1 <= lo <= hi <= len(listed)):
                raise LaunchError(f"range out of 1-{len(listed)}: {token}")
            picked = listed[lo - 1 : hi]
        else:
            picked = [_resolve(listed, token)]
        for t in picked:
            chosen.setdefault(path_key(t.cwd), t)
    return list(chosen.values())


def wt_args(targets: list[Target], shell: str = "pwsh") -> list[str]:
    """wt.exe arguments: one new window, one titled tab per target."""
    args = ["-w", "new"]
    for i, t in enumerate(targets):
        if i:
            args.append(";")
        args += ["new-tab", "--title", t.title, "--suppressApplicationTitle"]
        args += ["-d", t.cwd, shell, "-NoExit", "-Command", TAB_PROMPT]
    return args


def launch(targets: list[Target]) -> list[str]:
    """Start Windows Terminal with the tabs; returns the full command."""
    if not targets:
        raise LaunchError("nothing to open")
    wt = shutil.which("wt")
    if wt is None:
        raise LaunchError("Windows Terminal (wt.exe) not found on PATH")
    shell = "pwsh" if shutil.which("pwsh") else "powershell"
    command = [wt, *wt_args(targets, shell)]
    subprocess.Popen(command, close_fds=True)
    return command
