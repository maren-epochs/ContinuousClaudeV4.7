"""Fleet launcher: open project sessions as named tabs in one Windows Terminal window.

Candidates come from ~/.claude/projects: each transcript folder's newest transcript
names its cwd; a project is offered when that folder still exists, is not home or a
temp dir, and has a handoff to resume. Each tab runs a fresh ``claude '/resume-handoff'``
in PowerShell, titled with the project folder name
(``--suppressApplicationTitle`` keeps Claude Code from retitling it).

With a stagger, each tab is its own ``wt`` call a few seconds after the last: the first
creates a window with a unique name (``wt -w <name>`` makes the window when none has
that name) and the later ones add tabs to it, so the sessions do not all start at once.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import collect, model

TAB_PROMPT = "claude '/resume-handoff'"
STAGGER_S = 3.0  # seconds between tabs for fleet.py open; 0 = one chained wt command
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


def candidates() -> list[Target]:
    """Openable projects, by title: the project folder name."""
    claude = model.claude_dir()
    projects = claude / "projects"
    found: dict[str, Target] = {}
    for folder in projects.iterdir() if projects.is_dir() else []:
        transcripts = list(folder.glob("*.jsonl")) if folder.is_dir() else []
        if not transcripts:
            continue
        transcripts.sort(key=lambda p: model.mtime(p) or 0.0, reverse=True)
        newest = transcripts[0]
        # A metadata-only stub (title/name lines, no cwd) can be the newest file.
        cwd = next(filter(None, map(transcript_cwd, transcripts)), None)
        if cwd and not Path(cwd).is_dir():
            cwd = renamed_cwd(folder.name, cwd)
        if not cwd or _excluded(cwd):
            continue
        if collect.newest_handoff_file(collect.handoff_root(cwd, claude)) is None:
            continue
        key, last = path_key(cwd), (model.mtime(newest) or 0.0)
        if key not in found or last > found[key].last:
            cwd = os.path.normpath(cwd)
            found[key] = Target(title=Path(cwd).name, cwd=cwd, last=last)
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


def _wt_literal(value: str) -> str:
    """value with ``;`` escaped: wt splits its command line on ``;`` even inside an argument."""
    return value.replace(";", "\\;")


def _tab_args(t: Target, shell: str, profile: str | None) -> list[str]:
    args = ["new-tab"]
    if profile:
        args += ["-p", _wt_literal(profile)]
    args += ["--title", _wt_literal(t.title), "--suppressApplicationTitle"]
    return args + ["-d", _wt_literal(t.cwd), shell, "-NoExit", "-Command", TAB_PROMPT]


def wt_args(
    targets: list[Target], shell: str = "pwsh", profile: str | None = None
) -> list[str]:
    """wt.exe arguments: one new window, one titled tab per target.

    ``profile`` names a Windows Terminal profile for every tab; the tab keeps its own
    command line and takes the profile's icon (which the taskbar shows) and colors.
    """
    args = ["-w", "new"]
    for i, t in enumerate(targets):
        if i:
            args.append(";")
        args += _tab_args(t, shell, profile)
    return args


def window_name() -> str:
    """A fresh Windows Terminal window name (not an integer id nor ``new``/``last``)."""
    return f"fleet-{time.time_ns() // 1_000_000}"


def wt_calls(
    targets: list[Target],
    shell: str = "pwsh",
    profile: str | None = None,
    stagger: float = 0.0,
    window: str | None = None,
) -> list[tuple[float, list[str]]]:
    """``(delay before the call, wt.exe arguments)`` per wt call.

    ``stagger`` <= 0: one call, the chained :func:`wt_args`. Otherwise one call per tab,
    each ``-w <window>`` (``window`` default: :func:`window_name`) with the same tab
    arguments; the first creates the named window, the rest wait ``stagger`` seconds.
    """
    if stagger <= 0:
        return [(0.0, wt_args(targets, shell, profile))]
    name = window or window_name()
    return [
        (float(stagger) if i else 0.0, ["-w", name, *_tab_args(t, shell, profile)])
        for i, t in enumerate(targets)
    ]


def clean_env(env: dict[str, str]) -> dict[str, str]:
    """``env`` without the calling Claude Code session's markers.

    Run from a session's shell, the tabs would inherit CLAUDE_CODE_CHILD_SESSION and
    friends, and each new claude would take itself for a child session and not save its
    transcript. User settings env (settings.json ``env``) is reapplied by claude itself.
    """
    return {
        k: v
        for k, v in env.items()
        if not (
            k.upper() in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT")
            or k.upper().startswith("CLAUDE_CODE_")
        )
    }


def launch(
    targets: list[Target],
    profile: str | None = None,
    stagger: float = 0.0,
    sleep: Callable[[float], object] | None = None,
    window: str | None = None,
) -> list[list[str]]:
    """Start Windows Terminal with the tabs; returns every wt command run.

    ``sleep`` (default :func:`time.sleep`) waits out each delay of :func:`wt_calls`.
    """
    if not targets:
        raise LaunchError("nothing to open")
    wt = shutil.which("wt")
    if wt is None:
        raise LaunchError("Windows Terminal (wt.exe) not found on PATH")
    shell = "pwsh" if shutil.which("pwsh") else "powershell"
    wait = sleep or time.sleep
    env = clean_env(dict(os.environ))
    commands = []
    for delay, args in wt_calls(targets, shell, profile, stagger, window):
        if delay > 0:
            wait(delay)
        command = [wt, *args]
        subprocess.Popen(command, close_fds=True, env=env)
        commands.append(command)
    return commands
