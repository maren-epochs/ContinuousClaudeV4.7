"""Fleet digest: one plain-language summary of every session (``fleet.py digest``).

``build(state)`` turns a FleetState into a JSON-ready dict: machine memory and machine
alerts, harness sync (same wording as ``fleet.py report``), drift count, pending inbox
count, audit events of the last 24 h (read from the ``audit.jsonl`` tail), and the
running sessions sorted needing-attention first (a pending AskUserQuestion, then
warn/error alerts) plus a "needs you" list. ``markdown(data)`` renders it.

Every string is masked with the lessons privacy guard (private terms, home paths,
usernames, session ids) before it is printed or written. ``write(text)`` saves
``~/.claude/fleet/digests/YYYY-MM-DD.md`` (local date) atomically. Stdlib only.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any

from . import collect, lessons, model
from .model import Alert, FleetState, Harness, Session

AUDIT_WINDOW_S = 24 * 3600
ATTENTION = ("error", "warn")
SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}
ERR_MAX = 200  # chars of a digest.err reason


def _now() -> _dt.datetime:
    """Current time (aware); patched in tests."""
    return _dt.datetime.now(_dt.UTC)


def _epoch(stamp: Any) -> float | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        value = _dt.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_dt.UTC)
    return value.timestamp()


def _age_s(stamp: Any, now: float) -> int | None:
    then = _epoch(stamp)
    return None if then is None else max(0, round(now - then))


def age_text(seconds: float | None) -> str:
    """``-``, ``<1 min``, ``N min`` (under 1 h), ``N.N h`` (under 48 h), ``N.N d``."""
    if seconds is None:
        return "-"
    if seconds < 60:
        return "<1 min"
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"


def _ago(seconds: float | None) -> str:
    text = age_text(seconds)
    return text if text == "-" else f"{text} ago"


def sync_label(h: Harness) -> str:
    """``in sync``, ``differs from repo (N files)`` or ``unknown`` (report wording)."""
    sync = h.extra.get("in_sync")
    sync = sync if isinstance(sync, bool) else None
    text = {True: "in sync", False: "differs from repo", None: "unknown"}[sync]
    behind = h.extra.get("behind_files")
    if sync is False and isinstance(behind, int) and not isinstance(behind, bool):
        text += f" ({behind} file{'' if behind == 1 else 's'})"
    return text


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def audit_count(now: float, window_s: float = AUDIT_WINDOW_S) -> int:
    """Audit events (tail of ``audit.jsonl``) whose ``ts`` is within window_s of now."""
    text = collect.read_tail(model.audit_path(), collect.AUDIT_TAIL_BYTES)
    count = 0
    for event in model.parse_audit_lines(text):
        then = _epoch(event.ts)
        if then is not None and 0 <= now - then <= window_s:
            count += 1
    return count


def _alert(a: Alert) -> dict[str, Any]:
    return {"severity": a.severity, "kind": a.kind, "detail": a.detail}


def _rank(severity: Any) -> int:
    return SEVERITY_ORDER.get(severity if isinstance(severity, str) else "", 3)


def _session(s: Session, now: float) -> dict[str, Any]:
    ask = s.extra.get("pending_ask")
    waiting = isinstance(ask, dict)
    since = ask.get("since") if isinstance(ask, dict) else None
    handoff_h = s.handoff.age_h if s.handoff else None
    return {
        "project": s.project or s.name or "-",
        "status": s.status,
        "last_activity": s.last_activity,
        "last_activity_age_s": _age_s(s.last_activity, now),
        "handoff_age_s": None if handoff_h is None else round(handoff_h * 3600),
        "waiting": waiting,
        "waiting_since": since,
        "waiting_age_s": _age_s(since, now),
        "alerts": [_alert(a) for a in s.alerts],
        "needs_attention": waiting or any(a.severity in ATTENTION for a in s.alerts),
    }


def _sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    worst = min((_rank(a["severity"]) for a in row["alerts"]), default=3)
    return (
        not row["needs_attention"],
        not row["waiting"],
        -(row["waiting_age_s"] or 0),
        worst,
        -(row["last_activity_age_s"] is not None),
        row["last_activity_age_s"] or 0,
        row["project"],
    )


def _needs_you(
    rows: list[dict[str, Any]], machine_alerts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Pending questions (longest waiting first), then warn/error alerts worst first."""
    out = [
        {
            "kind": "question",
            "who": r["project"],
            "severity": None,
            "age_s": r["waiting_age_s"],
            "text": f"question waiting {age_text(r['waiting_age_s'])}",
        }
        for r in rows
        if r["waiting"]
    ]
    found = [("machine", a) for a in machine_alerts]
    found += [(r["project"], a) for r in rows for a in r["alerts"]]
    found = [wa for wa in found if wa[1]["severity"] in ATTENTION]
    found.sort(key=lambda wa: _rank(wa[1]["severity"]))
    out += [
        {
            "kind": "alert",
            "who": who,
            "severity": a["severity"],
            "age_s": None,
            "text": f"[{a['severity']}] {a['kind'] or '-'}: {a['detail'] or '-'}",
        }
        for who, a in found
    ]
    return out


def build(state: FleetState, now: _dt.datetime | None = None) -> dict[str, Any]:
    """Digest data (JSON-ready, unmasked) of state at now (default: the current time)."""
    now = now or _now()
    ts = now.timestamp()
    h = state.harness
    behind = h.extra.get("behind_files")
    in_sync = h.extra.get("in_sync")
    rows = sorted((_session(s, ts) for s in state.sessions if s.alive), key=_sort_key)
    machine_alerts = [_alert(a) for a in state.machine_alerts]
    return {
        "date": now.astimezone().date().isoformat(),
        "generated_at": now.astimezone(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "state_at": state.generated_at,
        "state_age_s": _age_s(state.generated_at, ts),
        "machine": {
            "mem_free_gb": state.machine.mem_free_gb,
            "mem_total_gb": state.machine.mem_total_gb,
            "alerts": machine_alerts,
        },
        "harness": {
            "sync": sync_label(h),
            "in_sync": in_sync if isinstance(in_sync, bool) else None,
            "behind_files": (
                behind
                if isinstance(behind, int) and not isinstance(behind, bool)
                else None
            ),
            "drift": len(h.drift),
        },
        "inbox_count": state.inbox_count,
        "audit_24h": audit_count(ts),
        "sessions": rows,
        "not_running": sum(1 for s in state.sessions if not s.alive),
        "needs_you": _needs_you(rows, machine_alerts),
    }


def _cell(value: Any) -> str:
    text = "-" if value is None or value == "" else " ".join(str(value).split())
    return text.replace("|", "\\|")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def markdown(data: dict[str, Any]) -> str:
    """Plain-language markdown of build() data: header, session table, needs-you list."""
    m, h = data["machine"], data["harness"]
    state_at = data["state_at"] or "-"
    if data["state_age_s"] is not None:
        state_at += f" ({_ago(data['state_age_s'])})"
    drift = f"; {_plural(h['drift'], 'drifted file')}" if h["drift"] else ""
    lines = [
        f"# Fleet digest {data['date']}",
        "",
        f"Fleet state collected {state_at}.",
        "",
        f"- Machine: {_num(m['mem_free_gb'])} GB free of {_num(m['mem_total_gb'])} GB",
    ]
    lines += [
        f"  - [{a['severity'] or '-'}] {a['kind'] or '-'}: {a['detail'] or '-'}"
        for a in m["alerts"]
    ]
    lines += [
        f"- Harness: {h['sync']}{drift}",
        f"- Inbox: {data['inbox_count']} pending",
        f"- Audit: {_plural(data['audit_24h'], 'risky command')} in the last 24 h",
        "",
        "## Sessions",
        "",
    ]
    rows = data["sessions"]
    if rows:
        lines += [
            "| project | status | last activity | handoff | waiting question | alerts |",
            "|---|---|---|---|---|---|",
        ]
        for r in rows:
            alerts = ", ".join(
                f"{a['kind'] or '-'} ({a['severity'] or '-'})" for a in r["alerts"]
            )
            waiting = age_text(r["waiting_age_s"]) if r["waiting"] else None
            cells = (
                r["project"],
                r["status"],
                _ago(r["last_activity_age_s"]),
                _ago(r["handoff_age_s"]),
                waiting,
                alerts,
            )
            lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    else:
        lines.append("No sessions running.")
    if data["not_running"]:
        lines += [
            "",
            f"({_plural(data['not_running'], 'session')} not running, omitted)",
        ]
    lines += ["", "## Needs you", ""]
    needs = data["needs_you"]
    lines += [f"- {n['who']}: {n['text']}" for n in needs] or ["Nothing right now."]
    return "\n".join(lines) + "\n"


def _mask_all(value: Any, guard: Any) -> Any:
    if isinstance(value, str):
        return lessons.mask(value, guard)
    if isinstance(value, list):
        return [_mask_all(v, guard) for v in value]
    if isinstance(value, dict):
        return {k: _mask_all(v, guard) for k, v in value.items()}
    return value


def masked(data: dict[str, Any]) -> dict[str, Any]:
    """data with every string value masked by the lessons default privacy guard."""
    out: dict[str, Any] = _mask_all(data, lessons.default_guard(model.home_dir()))
    return out


def digest_path(date: str) -> Path:
    """``~/.claude/fleet/digests/<date>.md``."""
    return model.fleet_dir() / "digests" / f"{date}.md"


def err_path() -> Path:
    """``~/.claude/fleet/digest.err``: reason of the last failed daily digest write."""
    return model.fleet_dir() / "digest.err"


def notice() -> str | None:
    """``digest: last daily write failed (<reason>)`` while digest.err exists."""
    path = err_path()
    if not os.path.lexists(path):
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    reason = " ".join(text.split())[:ERR_MAX] or "reason unknown"
    return f"digest: last daily write failed ({reason})"


def write(data: dict[str, Any]) -> Path:
    """Save the masked markdown of data to its dated digest file (atomic); the path."""
    return model.write_atomic(digest_path(data["date"]), markdown(masked(data)))


def render(state: FleetState, as_json: bool = False, save: bool = False) -> str:
    """CLI text: the written path (save), masked JSON (as_json) or masked markdown."""
    data = build(state)
    if save:
        return str(write(data))
    clean = masked(data)
    if as_json:
        return json.dumps(clean, indent=2, ensure_ascii=False, allow_nan=False)
    return markdown(clean).rstrip("\n")
