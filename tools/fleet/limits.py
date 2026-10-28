"""Live sessions stopped by the usage limit, and when each can resume.

A session is stopped when the last conversation entry in its transcript is Claude Code's
synthetic limit message: ``"apiError": "usage_limit_reached"`` with the reset epoch in
``apiErrorParams.rate_limit_info.resetsAt`` (or ``quotaLimits.resetsAt``). Anything the
session wrote after it (a user turn, an assistant reply) means it already resumed.
``fleet.py limits`` lists them; ``/fleet resume`` sends "resume" to the ready ones.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import collect, model

TAIL_BYTES = 256 * 1024
LIMIT_ERROR = "usage_limit_reached"
RESUME_TEXT = "resume"


@dataclass
class Stopped:
    """One live session waiting on the usage limit."""

    name: str
    project: str
    session_id: str
    resets_at: float | None
    limit_type: str | None

    def ready(self, now: float) -> bool:
        """True once the reset time has passed (unknown reset counts as ready)."""
        return self.resets_at is None or now >= self.resets_at


def _tail_entries(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    if size > TAIL_BYTES:
        lines = lines[1:]  # first line is cut
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _reset_epoch(entry: dict[str, Any]) -> tuple[float | None, str | None]:
    for holder in (
        (entry.get("apiErrorParams") or {}).get("rate_limit_info"),
        entry.get("quotaLimits"),
    ):
        if isinstance(holder, dict) and isinstance(
            holder.get("resetsAt"), (int, float)
        ):
            kind = holder.get("rateLimitType")
            return float(holder["resetsAt"]), kind if isinstance(kind, str) else None
    return None, None


def limit_entry(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The limit message when it is the last user/assistant entry, else None."""
    for entry in reversed(entries):
        if entry.get("type") not in ("user", "assistant") or entry.get("isMeta"):
            continue
        return entry if entry.get("apiError") == LIMIT_ERROR else None
    return None


def stopped(state: model.FleetState, projects: Path | None = None) -> list[Stopped]:
    """Alive sessions whose transcript ends on the usage-limit message."""
    projects = projects or model.claude_dir() / "projects"
    out = []
    for s in state.sessions:
        if not s.alive or not s.session_id:
            continue
        path = collect.transcript_path(projects, s.cwd, s.session_id)
        entry = limit_entry(_tail_entries(path)) if path else None
        if entry is None:
            continue
        resets, kind = _reset_epoch(entry)
        out.append(
            Stopped(
                s.name or s.session_id[:8], s.project or "", s.session_id, resets, kind
            )
        )
    return out


def usage_path() -> Path:
    """``~/.claude/fleet/usage.json``: the latest rate_limits reading saved by status.mjs."""
    return model.fleet_dir() / "usage.json"


def read_usage() -> dict[str, Any] | None:
    """The saved usage reading, or None when absent or unreadable."""
    try:
        data = json.loads(usage_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def usage_report(now: float, as_json: bool = False) -> str:
    """5-hour and 7-day usage with reset times (Eastern) and the reading's age."""
    u = read_usage()
    if as_json:
        return json.dumps(u or {}, indent=2)
    if not u:
        return (
            "no usage reading yet (the status line saves it after a session's first"
            " response; Pro and Max plans only)"
        )
    out = []
    for label, key in (("5-hour", "five_hour"), ("7-day", "seven_day")):
        pct, resets = u.get(f"{key}_pct"), u.get(f"{key}_resets_at")
        if isinstance(pct, (int, float)):
            when = _when(resets) if isinstance(resets, (int, float)) else "unknown"
            out.append(f"{label}: {pct:.0f}% used, resets {when}")
    at = u.get("at")
    if isinstance(at, (int, float)):
        out.append(f"reading {max(0, now - at / 1000) / 60:.0f} min old")
    return "\n".join(out) or "usage reading has no values"


def _when(epoch: float | None) -> str:
    if epoch is None:
        return "unknown"
    return model.local_time(epoch)


def report(rows: list[Stopped], now: float, as_json: bool = False) -> str:
    """Text table (or JSON) of stopped sessions with reset time and readiness."""
    if as_json:
        return json.dumps(
            [
                {
                    "name": r.name,
                    "project": r.project,
                    "session_id": r.session_id,
                    "resets_at": r.resets_at,
                    "resets": _when(r.resets_at),
                    "limit_type": r.limit_type,
                    "ready": r.ready(now),
                }
                for r in rows
            ],
            indent=2,
        )
    if not rows:
        return "no live session is stopped on the usage limit"
    out = [f"{len(rows)} session(s) stopped on the usage limit:"]
    for r in rows:
        state = (
            "ready: send 'resume'" if r.ready(now) else f"resets {_when(r.resets_at)}"
        )
        out.append(f"  {r.name:<28} {r.project:<18} {state}")
    later = [r.resets_at for r in rows if not r.ready(now) and r.resets_at]
    if later:
        out.append(f"next reset: {_when(min(later))}")
    return "\n".join(out)
