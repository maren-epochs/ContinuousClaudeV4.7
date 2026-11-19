"""fleet.py close <tab>: end a session cleanly once its handoff is written.

Writing a handoff does not end a session: the Claude process keeps its memory until
the tab closes. Killing it works but leaves the tab open with an error icon (Windows
Terminal keeps a tab whose program exited with an error code). So this types
``/exit`` into the tab, then ``exit`` into the PowerShell that ``fleet open`` starts
the tab with, and the tab closes on its own. Only if Claude is still running after
that does it stop the process and close the tab through its close button.

Refused: no live session for the tab, this session itself, a session still working
(its screen shows "esc to interrupt"), or no handoff written since the session's last
activity minus ``HANDOFF_SLACK_S`` (auto-handoffs from compaction do not count).
``--force`` skips the handoff and busy checks, never the self check.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import boxes, collect, model
from .model import FleetState, Session

HANDOFF_SLACK_S = (
    15 * 60
)  # the handoff may be written up to this long before the last activity
BUSY_MARK = "esc to interrupt"  # Claude Code shows this while it works
EXIT_WAIT_MS = 20000  # how long /exit may take to return to the shell prompt
GONE_WAIT_S = 10.0


class CloseError(Exception):
    """Refused or failed; the message says why."""


def _ts(value: str | None) -> float | None:
    return model.parse_epoch(value)


def written_handoff(cwd: str, since: float) -> tuple[float, Path] | None:
    """Newest hand-written handoff (not auto-handoff-*) modified at or after since."""
    root = collect.handoff_root(cwd, Path.home() / ".claude")
    best: tuple[float, Path] | None = None
    if not root.is_dir():
        return None
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.startswith("auto-handoff") or not name.endswith(
                collect.HANDOFF_SUFFIXES
            ):
                continue
            path = Path(dirpath) / name
            mtime = model.mtime(path)
            if (
                mtime is not None
                and mtime >= since
                and (best is None or mtime > best[0])
            ):
                best = (mtime, path)
    return best


def pick(state: FleetState, tab: str) -> Session:
    """The one live session whose project is tab."""
    names = {n.lower() for n in boxes.tab_names(tab)}
    live = [
        s
        for s in state.sessions
        if s.alive and s.pid and (s.project or "").lower() in names
    ]
    if not live:
        raise CloseError(f"no live session for tab '{tab}'")
    if len(live) > 1:
        pids = ", ".join(str(s.pid) for s in live)
        raise CloseError(
            f"{len(live)} live sessions for '{tab}' (pids {pids}); close one by hand"
        )
    return live[0]


def check(session: Session, force: bool, env: dict[str, str]) -> str:
    """Why closing is refused, '' when allowed; returns the handoff note when found."""
    if str(session.pid) == env.get("CLAUDE_PID"):
        raise CloseError(
            "that is this session; write its handoff and close it from another tab"
        )
    if force:
        return "handoff check skipped (--force)"
    last = _ts(session.last_activity) or _ts(session.updated_at)
    if last is None:
        raise CloseError(
            "session has no recorded activity; add --force to close anyway"
        )
    found = written_handoff(session.cwd or "", last - HANDOFF_SLACK_S)
    if not found:
        raise CloseError(
            "no handoff written since its last activity; ask it for one (/create-handoff)"
            " or add --force"
        )
    return f"handoff {found[1].name}"


def _wait_gone(pid: int, wait_s: float, sleep: Callable[[float], None]) -> bool:
    for _ in range(max(1, int(wait_s / 0.5))):
        if not collect.process_alive(pid, None):
            return True
        sleep(0.5)
    return not collect.process_alive(pid, None)


def _kill(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False
        )
    else:  # pragma: no cover - fleet close is for Windows Terminal
        os.kill(pid, 9)


def close(
    state: FleetState,
    tab: str,
    force: bool = False,
    dry_run: bool = False,
    run: Callable[[dict[str, object]], dict[str, object]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    env: dict[str, str] | None = None,
) -> str:
    """Close tab's session cleanly; the message says what happened."""
    run = run or boxes._run
    env = dict(os.environ) if env is None else env
    session = pick(state, tab)
    note = check(session, force, env)
    pid = int(session.pid or 0)
    screen = str(run({"tab": tab, "mode": "read"}).get("screen") or "")
    tail = "\n".join(ln for ln in screen.splitlines() if ln.strip())[-2000:]
    if BUSY_MARK in tail and not force:
        raise CloseError(
            "session is still working; wait for it to finish or add --force"
        )
    if dry_run:
        return f"would close '{tab}' (pid {pid}; {note})"
    # An open question box would take /exit as its typed answer: cancel it first.
    cancel = (
        [{"keys": "{ESC}", "wait_ms": 1500, "expect": ""}]
        if boxes.BOX_FOOTER in tail[-600:]
        else []
    )
    out = run(
        {
            "tab": tab,
            "mode": "keys",
            "steps": [
                *cancel,
                {"keys": "/exit", "wait_ms": 800, "expect": ""},
                {
                    "keys": "{ENTER}",
                    "wait_ms": EXIT_WAIT_MS,
                    "expect": "PS ",
                    "last": True,
                },
                {"keys": "exit{ENTER}", "wait_ms": 1500, "expect": ""},
            ],
        }
    )
    if _wait_gone(pid, GONE_WAIT_S, sleep):
        how = "typed /exit" if out.get("ok") else "it exited"
        return f"closed '{tab}': {how}, the tab closed itself ({note})"
    _kill(pid)
    if not _wait_gone(pid, GONE_WAIT_S, sleep):
        raise CloseError(f"pid {pid} is still running after /exit and taskkill")
    closed = run({"tab": tab, "mode": "closetab"})
    tab_note = (
        "tab closed" if closed.get("ok") else f"tab left open ({closed.get('error')})"
    )
    return f"closed '{tab}': /exit did not end it, process stopped; {tab_note} ({note})"
