"""Fleet checks (VAL-806): alerts, collisions and harness drift on a FleetState.

Runs after the collector (``collect.collect``) on the sessions it built. Inputs per
session: ``extra.transcript`` (re-read here, last ``TAIL_BYTES``, with absolute
line numbers for evidence), ``extra.status_updated_at``/``waiting_for``, ``alive``,
``status``, ``agents_running``, ``started_at``, ``last_activity``. Subagent
transcripts (``<transcript stem>/**/*.jsonl``) modified in the last 30 min add their
writes (newest ``SUBAGENT_MAX_FILES`` only, read once per session).

- collision: two alive sessions wrote the same absolute path in the last 30 min
  (Write/Edit/MultiEdit/NotebookEdit), or two alive sessions active in the last
  30 min (``last_activity``) sit in one git repo.
- stuck (alive sessions): an AskUserQuestion, a ``waiting`` status or a prose
  question unanswered for 20 min; the same tool call failing 3+ times in a row at
  the end of the transcript; a subagent stopped at its turn limit with no report.
- compliance: model not ``claude-opus-5-5`` with no user message naming another
  model; completed turns ending in a question without AskUserQuestion; writes into
  another project's folder (main and subagent transcripts).
- drift: installed files whose sha256 differs from the manifest (kept entries
  skipped); one ``stale`` entry instead when the manifest does not match the
  ``.ccv47-installed`` marker; alive sessions started before the last sync.

Evidence is ``<transcript>:<line>``. Session files and ``*.key`` are never opened.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import model
from .model import Alert, Collision, DriftEntry, FleetState, Session

TAIL_BYTES = 512 * 1024
COLLISION_WINDOW_S = 30 * 60
SUBAGENT_MAX_FILES = 16
STUCK_WAIT_S = 20 * 60
REPEAT_FAILS = 3
STALE_SLACK_S = 60
EXPECTED_MODEL = "claude-opus-5-5"
WRITE_TOOLS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
AGENT_TOOLS = frozenset({"Agent", "Task"})
ASK_TOOL = "AskUserQuestion"
INSTALLED_MARKER = ".ccv47-installed"

_NOTIFICATION = re.compile(r"<task-notification>.*?</task-notification>", re.DOTALL)
_TOOL_USE_ID = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")
_STATUS = re.compile(r"<status>([^<]+)</status>")
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_OTHER_MODEL = re.compile(
    r"(?i)(?:\b(?:sonnet|haiku|claude-(?!opus-5-5)[a-z0-9][a-z0-9.-]*"
    r"|opus[ -]?[1-4](?:[.-]\d+)?)\b|/model\b)"
)
_LIMIT_VALUE = re.compile(r"(?i)max[_ -]?turns|turn[_ -]?limit")
_LIMIT_TEXT = re.compile(
    r"(?i)\b(?:reached|hit|exceeded|stopped at)\b[^\n]{0,40}?"
    r"(?:max(?:imum)?[_ -]?(?:number of )?turns|turn[_ -]?limit)"
)
_LIMIT_KEYS = ("status", "subtype", "stop_reason", "stopReason", "reason")
_OUTPUT_KEY = re.compile(r'"output"\s*:\s*"([^"]+?\.json)"')
_REPORT_PATH = re.compile(
    r"(?:[A-Za-z]:)?[\\/][^\s\"'<>|*?]*?reports[\\/][A-Za-z0-9._-]+\.json"
)
_UNC = re.compile(r"^[\\/]{2}")
_TOKEN_STOP = frozenset(" \t\r\n\"'<>|`()[]{},;")
_QUESTION_TAIL = "*_`)\"'”’]> \t\r\n"


# --- reading ---


def tail_lines(path: Path, max_bytes: int) -> list[tuple[int, str]]:
    """Non-blank ``(line number, text)`` of the last ``max_bytes`` of a file.

    Line numbers are absolute (newlines before the tail are counted); a partial
    first line is dropped. Lines split on ``\\n`` only, so U+2028 inside JSON
    strings never shifts the numbering.
    """
    try:
        with path.open("rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            start = max(0, size - max_bytes)
            line = 1
            remaining = start
            while remaining > 0:
                chunk = fh.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                line += chunk.count(b"\n")
                remaining -= len(chunk)
            data = fh.read(max_bytes)
    except OSError:
        return []
    parts = data.split(b"\n")
    if start > 0:
        parts = parts[1:]
        line += 1
    out = []
    for offset, raw in enumerate(parts):
        text = raw.decode("utf-8", errors="replace").rstrip("\r")
        if text.strip():
            out.append((line + offset, text))
    return out


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


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"]
            for b in content
            if isinstance(b, Mapping) and isinstance(b.get("text"), str)
        )
    return ""


def _blocks(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, Mapping)]


def _record(raw: str) -> Mapping[str, Any] | None:
    """The JSON object on a transcript line; None for invalid JSON or a non-object."""
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    return record if isinstance(record, Mapping) else None


def _done_notes(raw: str) -> Iterator[tuple[str, str]]:
    """(tool use id, notification) per id of each non-running task notification."""
    if "<task-notification>" not in raw:
        return
    for note in _NOTIFICATION.findall(raw.replace("\\n", "\n")):
        status = _STATUS.search(note)
        if status and status.group(1).strip() == "running":
            continue
        for tool_id in _TOOL_USE_ID.findall(note):
            yield tool_id.strip(), note


# --- transcript scan ---


@dataclass
class _Write:
    raw: str
    ts: float | None
    evidence: str


@dataclass
class _Call:
    id: str | None
    name: str
    key: str
    line: int
    ts: float | None
    input: Mapping[str, Any]


@dataclass
class _Result:
    is_error: bool
    text: str
    meta: Mapping[str, Any] | None
    line: int


@dataclass
class _Turn:
    closed: bool = False
    asked: bool = False
    ends_with_text: bool = False
    last_text: str | None = None
    last_line: int = 0
    last_ts: float | None = None
    call_ids: list[str] = field(default_factory=list)


@dataclass
class _Scan:
    path: Path
    last_line: int = 0
    first_ts: float | None = None
    calls: list[_Call] = field(default_factory=list)
    results: dict[str, _Result] = field(default_factory=dict)
    notes: dict[str, tuple[str, int]] = field(default_factory=dict)
    writes: list[_Write] = field(default_factory=list)
    turns: list[_Turn] = field(default_factory=list)
    model: tuple[str, int] | None = None
    model_named: bool = False
    subagent_writes: list[_Write] | None = None


def _is_prompt(record: Mapping[str, Any]) -> bool:
    if record.get("isMeta") is True:
        return False
    message = record.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    kinds = {b.get("type") for b in content if isinstance(b, Mapping)}
    return "text" in kinds and "tool_result" not in kinds


def _model_name(record: Mapping[str, Any]) -> str | None:
    """``message.model`` of a record unless missing, empty or a ``<synthetic>`` name."""
    message = record.get("message")
    name = message.get("model") if isinstance(message, Mapping) else None
    if isinstance(name, str) and name and not name.startswith("<"):
        return name
    return None


def _names_model(record: Mapping[str, Any]) -> bool:
    """True when a user prompt (system reminders removed) names another model."""
    message = record.get("message")
    text = _content_text(
        message.get("content") if isinstance(message, Mapping) else None
    )
    return bool(_OTHER_MODEL.search(_REMINDER.sub("", text)))


def _scan(path: Path) -> _Scan:
    scan = _Scan(path=path)
    turn: _Turn | None = None
    for line, raw in tail_lines(path, TAIL_BYTES):
        scan.last_line = line
        for tool_id, note in _done_notes(raw):
            scan.notes.setdefault(tool_id, (note, line))
        record = _record(raw)
        if record is not None:
            turn = _scan_record(scan, record, line, turn)
    return scan


def _scan_record(
    scan: _Scan, record: Mapping[str, Any], line: int, turn: _Turn | None
) -> _Turn | None:
    """Fold one record into scan; returns the open main-chain turn afterwards."""
    ts = _epoch(record.get("timestamp"))
    if ts is not None and (scan.first_ts is None or ts < scan.first_ts):
        scan.first_ts = ts
    kind = record.get("type")
    sidechain = record.get("isSidechain") is True
    if kind == "assistant":
        if not sidechain and turn is None:
            turn = _Turn()
            scan.turns.append(turn)
        _scan_assistant(scan, record, line, ts, None if sidechain else turn)
    elif kind == "user":
        _scan_results(scan, record, line)
        if not sidechain and _is_prompt(record):
            if turn is not None:
                turn.closed = True
            turn = None
            scan.model_named = _names_model(record) or scan.model_named
    return turn


def _scan_results(scan: _Scan, record: Mapping[str, Any], line: int) -> None:
    meta = record.get("toolUseResult")
    for block in _blocks(record):
        tool_id = block.get("tool_use_id")
        if block.get("type") == "tool_result" and isinstance(tool_id, str):
            scan.results[tool_id] = _Result(
                is_error=block.get("is_error") is True,
                text=_content_text(block.get("content"))[:2000],
                meta=meta if isinstance(meta, Mapping) else None,
                line=line,
            )


def _scan_assistant(
    scan: _Scan,
    record: Mapping[str, Any],
    line: int,
    ts: float | None,
    turn: _Turn | None,
) -> None:
    sidechain = record.get("isSidechain") is True
    name = None if sidechain else _model_name(record)
    if name is not None:
        scan.model = (name, line)
    for block in _blocks(record):
        kind = block.get("type")
        if kind == "tool_use" and isinstance(block.get("name"), str):
            tool_id = _scan_tool_use(scan, block, line, ts, sidechain)
            if turn is not None:
                _turn_call(turn, block["name"], tool_id)
        elif kind == "text" and turn is not None:
            _turn_text(turn, block, line, ts)


def _scan_tool_use(
    scan: _Scan,
    block: Mapping[str, Any],
    line: int,
    ts: float | None,
    sidechain: bool,
) -> str | None:
    """Record a tool_use block's write target and (main chain) call; returns its id."""
    name = block["name"]
    inp = block.get("input")
    inp = inp if isinstance(inp, Mapping) else {}
    tool_id = block.get("id")
    tool_id = tool_id if isinstance(tool_id, str) else None
    target = inp.get(WRITE_TOOLS.get(name, ""))
    if isinstance(target, str) and target.strip():
        scan.writes.append(_Write(target, ts, f"{scan.path}:{line}"))
    if not sidechain:
        key = json.dumps(inp, sort_keys=True, default=str)
        scan.calls.append(_Call(tool_id, name, key, line, ts, inp))
    return tool_id


def _turn_call(turn: _Turn, name: str, tool_id: str | None) -> None:
    turn.ends_with_text = False
    turn.asked = turn.asked or name == ASK_TOOL
    if tool_id:
        turn.call_ids.append(tool_id)


def _turn_text(
    turn: _Turn, block: Mapping[str, Any], line: int, ts: float | None
) -> None:
    body = block.get("text")
    if isinstance(body, str) and body.strip():
        turn.ends_with_text = True
        turn.last_text = body
        turn.last_line = line
        turn.last_ts = ts


def _subagent_writes(scan: _Scan, now: float) -> list[_Write]:
    """Writes of the newest SUBAGENT_MAX_FILES subagent transcripts touched in the
    collision window; computed once per scan."""
    if scan.subagent_writes is not None:
        return scan.subagent_writes
    scan.subagent_writes = []
    folder = scan.path.with_suffix("")
    if not folder.is_dir():
        return scan.subagent_writes
    since = now - COLLISION_WINDOW_S
    recent: list[tuple[float, Path]] = []
    for path in folder.rglob("*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= since:
            recent.append((mtime, path))
    recent.sort(key=lambda item: (item[0], str(item[1])), reverse=True)
    for _mtime, path in sorted(recent[:SUBAGENT_MAX_FILES], key=lambda i: str(i[1])):
        scan.subagent_writes.extend(_scan(path).writes)
    return scan.subagent_writes


# --- paths and repos ---


def _norm(path: str, cwd: str | None) -> str | None:
    text = path.strip()
    if not text:
        return None
    if sys.platform == "win32":
        match = re.match(r"^/([A-Za-z])(?=/|$)", text)
        if match:
            text = f"{match.group(1)}:" + text[2:]
    if not os.path.isabs(text):
        if not cwd:
            return None
        text = os.path.join(cwd, text)
    return os.path.normpath(text)


def _key(path: str) -> str:
    return os.path.normcase(path)


def _inside(path: str, root: str) -> bool:
    p, r = _key(path), _key(root).rstrip("\\/")
    return p == r or p.startswith(r + os.sep)


class _Roots:
    """Nearest git root (``.git`` dir or file) per path, cached per directory."""

    def __init__(self, home: str) -> None:
        self.home = _key(os.path.normpath(home))
        self.cache: dict[str, str | None] = {}

    def repo(self, path: str) -> str | None:
        seen = []
        current = path
        found: str | None = None
        while True:
            key = _key(current)
            if key in self.cache:
                found = self.cache[key]
                break
            seen.append(key)
            if os.path.exists(os.path.join(current, ".git")):
                found = None if key == self.home else current
                break
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        for key in seen:
            self.cache[key] = found
        return found


def _label(session: Session) -> str:
    return session.session_id or f"pid-{session.pid}"


def _fallback(session: Session, scan: _Scan | None, line: int | None = None) -> str:
    if scan is not None:
        return f"{scan.path}:{line if line is not None else max(scan.last_line, 1)}"
    return f"{session.pid}.json"


def _alert(session: Session, kind: str, severity: str, detail: str, ev: str) -> None:
    session.alerts.append(
        Alert(
            kind=kind,
            severity=severity,
            session=session.session_id,
            detail=detail,
            evidence=ev,
        )
    )


# --- checks ---


def _others(members: list[Session], me: Session) -> str:
    return ", ".join(_label(s) for s in members if s is not me)


def _windowed(session: Session, scan: _Scan, now: float) -> Iterator[_Write]:
    """Main + subagent writes inside the collision window, paths normalized."""
    for w in scan.writes + _subagent_writes(scan, now):
        path = _norm(w.raw, session.cwd)
        if path is not None and w.ts is not None and now - w.ts <= COLLISION_WINDOW_S:
            yield _Write(path, w.ts, w.evidence)


def _recent_writes(
    sessions: list[Session], scans: list[_Scan | None], now: float
) -> dict[str, dict[int, _Write]]:
    """Path key -> session index -> that session's newest windowed write."""
    by_path: dict[str, dict[int, _Write]] = {}
    for i, (s, scan) in enumerate(zip(sessions, scans, strict=True)):
        if not s.alive or scan is None:
            continue
        for w in _windowed(s, scan, now):
            slot = by_path.setdefault(_key(w.raw), {})
            if i not in slot or (slot[i].ts or 0) <= (w.ts or 0):
                slot[i] = w
    return by_path


def _path_collisions(
    sessions: list[Session], scans: list[_Scan | None], now: float
) -> list[Collision]:
    collisions: list[Collision] = []
    by_path = _recent_writes(sessions, scans, now)
    for key in sorted(by_path):
        slot = by_path[key]
        if len(slot) < 2:
            continue
        latest = max(slot.values(), key=lambda w: w.ts or 0)
        members = [sessions[i] for i in sorted(slot)]
        collisions.append(
            Collision(
                kind="path",
                target=latest.raw,
                sessions=[_label(s) for s in members],
                detail=f"{len(members)} alive sessions wrote this path within 30 min",
            )
        )
        for i in sorted(slot):
            _alert(
                sessions[i],
                "collision",
                "error",
                f"wrote {latest.raw} within 30 min of {_others(members, sessions[i])}",
                slot[i].evidence,
            )
    return collisions


def _active_root(session: Session, roots: _Roots, now: float) -> str | None:
    """Git root of an alive session active inside the collision window."""
    if not session.alive or not session.cwd:
        return None
    active = _epoch(session.last_activity)
    if active is None or now - active > COLLISION_WINDOW_S:
        return None
    cwd = _norm(session.cwd, None)
    return roots.repo(cwd) if cwd else None


def _repo_collisions(
    sessions: list[Session], scans: list[_Scan | None], roots: _Roots, now: float
) -> list[Collision]:
    by_repo: dict[str, list[int]] = {}
    repo_path: dict[str, str] = {}
    for i, s in enumerate(sessions):
        root = _active_root(s, roots, now)
        if root:
            by_repo.setdefault(_key(root), []).append(i)
            repo_path.setdefault(_key(root), root)
    collisions: list[Collision] = []
    for key in sorted(by_repo):
        idx = by_repo[key]
        if len(idx) < 2:
            continue
        root = repo_path[key]
        members = [sessions[i] for i in idx]
        collisions.append(
            Collision(
                kind="repo",
                target=root,
                sessions=[_label(s) for s in members],
                detail=(
                    f"{len(members)} alive sessions active within 30 min work in"
                    " this repo"
                ),
            )
        )
        for i in idx:
            _alert(
                sessions[i],
                "collision",
                "warn",
                f"same repo {root} as {_others(members, sessions[i])}",
                _fallback(sessions[i], scans[i]),
            )
    return collisions


def check_collisions(
    sessions: list[Session],
    scans: list[_Scan | None],
    roots: _Roots,
    now: float,
) -> list[Collision]:
    """Same path written by 2+ alive sessions in the window; 2+ alive in one repo."""
    paths = _path_collisions(sessions, scans, now)
    return paths + _repo_collisions(sessions, scans, roots, now)


def _meta_limited(meta: Mapping[str, Any] | None) -> bool:
    """A finished (not async-launched) result whose metadata names the turn limit."""
    if meta is None or meta.get("status") == "async_launched":
        return False
    if meta.get("isAsync") is True:
        return False
    return any(
        isinstance(meta.get(key), str) and bool(_LIMIT_VALUE.search(meta[key]))
        for key in _LIMIT_KEYS
    )


def _note_limited(note: tuple[str, int] | None) -> int | None:
    if note is None:
        return None
    body, line = note
    status = _STATUS.search(body)
    if status and _LIMIT_VALUE.search(status.group(1)):
        return line
    return line if _LIMIT_TEXT.search(body[:1000]) else None


def _turn_limited(result: _Result | None, note: tuple[str, int] | None) -> int | None:
    """Evidence line when a subagent result says it stopped at its turn limit."""
    if result is not None and _meta_limited(result.meta):
        return result.line
    if result is not None and _LIMIT_TEXT.search(result.text[:500]):
        return result.line
    return _note_limited(note)


def _report_path(prompt: Any) -> str | None:
    if not isinstance(prompt, str):
        return None
    match = _OUTPUT_KEY.search(prompt) or _REPORT_PATH.search(prompt)
    if not match:
        return None
    if match.groups():
        raw = token = match.group(1)
    else:
        raw, start = match.group(0), match.start()
        while start > 0 and prompt[start - 1] not in _TOKEN_STOP:
            start -= 1
        token = prompt[start : match.end()]
    if _UNC.match(token):
        return None  # UNC / device path: a stat would be network I/O
    return raw.replace("\\\\", "\\")


# A stuck finding: (alert detail, evidence line or None for the fallback).
_Finding = tuple[str, int | None]


def _pending_ask(scan: _Scan | None) -> _Call | None:
    """The last AskUserQuestion call when it has no result yet."""
    if scan is None:
        return None
    for call in reversed(scan.calls):
        if call.name == ASK_TOOL:
            return call if call.id is None or call.id not in scan.results else None
    return None


def _ask_wait(ask: _Call | None, now: float) -> _Finding | None:
    if ask and ask.ts and now - ask.ts > STUCK_WAIT_S:
        return (
            f"AskUserQuestion unanswered for {int((now - ask.ts) // 60)} min",
            ask.line,
        )
    return None


def _status_wait(session: Session, ask: _Call | None, now: float) -> _Finding | None:
    status_at = _epoch(session.extra.get("status_updated_at")) or _epoch(
        session.updated_at
    )
    if session.status != "waiting" or status_at is None:
        return None
    if now - status_at <= STUCK_WAIT_S:
        return None
    waiting = session.extra.get("waiting_for")
    why = f" for {waiting}" if isinstance(waiting, str) and waiting else ""
    line = ask.line if ask else None
    return f"waiting{why} for {int((now - status_at) // 60)} min", line


def _open_question(turn: _Turn) -> bool:
    """The turn's final block is text ending in a question."""
    return (
        turn.ends_with_text
        and turn.last_text is not None
        and _is_question(turn.last_text)
    )


def _question_wait(session: Session, scan: _Scan | None, now: float) -> _Finding | None:
    final = scan.turns[-1] if scan is not None and scan.turns else None
    if final is None or session.status == "busy" or session.agents_running != 0:
        return None
    if not _open_question(final) or final.last_ts is None:
        return None
    if now - final.last_ts <= STUCK_WAIT_S:
        return None
    minutes = int((now - final.last_ts) // 60)
    return f"question to the user unanswered for {minutes} min", final.last_line


def _stuck_waiting(session: Session, scan: _Scan | None, now: float) -> None:
    ask = _pending_ask(scan)
    found = (
        _ask_wait(ask, now)
        or _status_wait(session, ask, now)
        or _question_wait(session, scan, now)
    )
    if found:
        _alert(session, "stuck", "warn", found[0], _fallback(session, scan, found[1]))


def _failing_run(scan: _Scan) -> list[_Result]:
    """Error results of the trailing run of identical calls (newest first)."""
    last = scan.calls[-1]
    run: list[_Result] = []
    for call in reversed(scan.calls):
        res = scan.results.get(call.id) if call.id else None
        if call.key != last.key or call.name != last.name or not res:
            break
        if not res.is_error:
            break
        run.append(res)
    return run


def _stuck_repeats(session: Session, scan: _Scan) -> None:
    run = _failing_run(scan) if scan.calls else []
    if len(run) >= REPEAT_FAILS:
        _alert(
            session,
            "stuck",
            "warn",
            f"{scan.calls[-1].name} failed {len(run)} times in a row with the same input",
            _fallback(session, scan, run[0].line),
        )


def _stuck_subagents(session: Session, scan: _Scan) -> None:
    for call in scan.calls:
        if call.name not in AGENT_TOOLS or call.id is None:
            continue
        line = _turn_limited(scan.results.get(call.id), scan.notes.get(call.id))
        if line is None:
            continue
        report = _report_path(call.input.get("prompt"))
        if report and Path(report).is_file():
            continue
        missing = f" ({report} missing)" if report else ""
        _alert(
            session,
            "stuck",
            "warn",
            f"subagent stopped at its turn limit without a report{missing}",
            _fallback(session, scan, line),
        )


def check_stuck(session: Session, scan: _Scan | None, now: float) -> None:
    """Waiting on the user > 20 min, repeated failing call, turn-limited subagent."""
    if not session.alive:
        return
    _stuck_waiting(session, scan, now)
    if scan is None:
        return
    _stuck_repeats(session, scan)
    _stuck_subagents(session, scan)


def _is_question(text: str) -> bool:
    return text.rstrip(_QUESTION_TAIL).endswith("?")


@dataclass
class _Context:
    """Fleet-wide inputs of the per-session compliance check."""

    roots: _Roots
    project_roots: Iterable[str]
    claude: str
    now: float | None = None


def _compliance_model(session: Session, scan: _Scan) -> None:
    if scan.model is None:
        return
    name, line = scan.model
    if not name.startswith(EXPECTED_MODEL) and not scan.model_named:
        _alert(
            session,
            "compliance",
            "warn",
            f"model {name} is not {EXPECTED_MODEL} and no user message named it",
            _fallback(session, scan, line),
        )


def _unasked_question(turn: _Turn, complete: bool) -> bool:
    """A complete turn ending in a question without an AskUserQuestion call."""
    return complete and not turn.asked and bool(turn.last_text) and _open_question(turn)


def _compliance_questions(session: Session, scan: _Scan) -> None:
    questions = []
    for n, turn in enumerate(scan.turns):
        final = n == len(scan.turns) - 1
        pending = any(i not in scan.results for i in turn.call_ids)
        complete = turn.closed or (final and session.status != "busy" and not pending)
        if _unasked_question(turn, complete):
            questions.append(turn)
    if questions:
        plural = "turn ends" if len(questions) == 1 else "turns end"
        _alert(
            session,
            "compliance",
            "info",
            f"{len(questions)} {plural} in a question without AskUserQuestion",
            _fallback(session, scan, questions[-1].last_line),
        )


def _foreign_root(path: str, own: str, known: list[str], ctx: _Context) -> str | None:
    """Root of another project that path lies in; None for own/claude/unknown paths."""
    if _inside(path, ctx.claude) or _inside(path, own):
        return None
    root = ctx.roots.repo(path) or next((r for r in known if _inside(path, r)), None)
    if root is None or _key(root) == _key(own) or _inside(own, root):
        return None
    return root


def _compliance_foreign(session: Session, scan: _Scan, ctx: _Context) -> None:
    cwd = _norm(session.cwd, None) if session.cwd else None
    if cwd is None:
        return
    own = ctx.roots.repo(cwd) or cwd
    known = sorted(ctx.project_roots, key=len, reverse=True)
    foreign: dict[str, tuple[str, str]] = {}
    subagent = _subagent_writes(scan, ctx.now) if ctx.now is not None else []
    for w in scan.writes + subagent:
        path = _norm(w.raw, session.cwd)
        root = _foreign_root(path, own, known, ctx) if path is not None else None
        if root is not None:
            foreign[_key(root)] = (root, w.evidence)
    for root, evidence in foreign.values():
        _alert(
            session,
            "compliance",
            "warn",
            f"wrote into another project's folder ({os.path.basename(root)}: {root})",
            evidence,
        )


def check_compliance(session: Session, scan: _Scan | None, ctx: _Context) -> None:
    """Model preference, question turns without AskUserQuestion, foreign writes."""
    if scan is None:
        return
    _compliance_model(session, scan)
    _compliance_questions(session, scan)
    _compliance_foreign(session, scan, ctx)


def _sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


_BAD_PARTS = frozenset({"", ".", ".."})


def _safe_rel(key: str) -> PurePosixPath | None:
    if not key or "\\" in key or ":" in key:
        return None
    rel = PurePosixPath(key)
    # No parts: "." (and "./") normalize to an empty path; rel.parts[0] would raise.
    if not rel.parts or rel.is_absolute() or _BAD_PARTS.intersection(rel.parts):
        return None
    if rel.parts[0] == "sessions" or rel.name.endswith(".key"):
        return None
    return rel


def _load_manifest(path: Path) -> Mapping[str, Any] | None:
    data = model.load_json(path)
    return data if isinstance(data, Mapping) else None


def _read_marker(marker_file: Path) -> tuple[str | None, float | None]:
    """(sha, mtime) of the ``.ccv47-installed`` marker; Nones when unreadable."""
    marker_sha: str | None = None
    marker_at: float | None = None
    try:
        tokens = marker_file.read_text(encoding="utf-8").split()
        marker_sha = tokens[0] if tokens else None
        marker_at = marker_file.stat().st_mtime
    except OSError:
        pass
    return marker_sha, marker_at


def _stale_reason(
    data: Mapping[str, Any], marker_sha: str | None, marker_at: float | None
) -> str | None:
    """Why the manifest does not describe the installed tree; None when it does."""
    head = data.get("head_sha") if isinstance(data.get("head_sha"), str) else None
    if marker_sha is not None and marker_sha != head:
        return f"head_sha {head} != installed marker {marker_sha}"
    generated = _epoch(data.get("generated_at"))
    if marker_at is None or generated is None:
        return None
    if marker_at > generated + STALE_SLACK_S:
        return f"installed marker is newer than generated_at {data.get('generated_at')}"
    return None


def _file_drift(target: Path, key: Any, entry: Any) -> DriftEntry | None:
    """DriftEntry for one manifest ``files`` entry; None when it matches or is skipped."""
    if not isinstance(key, str) or not isinstance(entry, Mapping):
        return None
    if entry.get("kept") is True:
        return None
    expected = entry.get("sha256")
    rel = _safe_rel(key)
    if not isinstance(expected, str) or rel is None:
        return None
    path = target.joinpath(*rel.parts)
    actual = _sha256(path) if path.is_file() else None
    if actual is not None and actual == expected.lower():
        return None
    repo = entry.get("repo_path")
    return DriftEntry(
        installed_path=str(path),
        repo_path=repo if isinstance(repo, str) else None,
        expected_sha=expected,
        actual_sha=actual,
        status="modified" if actual is not None else "missing",
    )


def check_drift(state: FleetState, now: float) -> float | None:
    """Fill ``harness.drift``; returns the last sync time (epoch) when known."""
    manifest_file = model.manifest_path()
    data = _load_manifest(manifest_file)
    if data is None:
        state.harness.drift = []
        return None
    target = manifest_file.parent
    marker_sha, marker_at = _read_marker(target / INSTALLED_MARKER)
    generated = _epoch(data.get("generated_at"))
    last_sync = max((t for t in (generated, marker_at) if t is not None), default=None)
    stale_reason = _stale_reason(data, marker_sha, marker_at)
    if stale_reason:
        head = data.get("head_sha") if isinstance(data.get("head_sha"), str) else None
        stale = DriftEntry(
            installed_path=str(manifest_file),
            expected_sha=marker_sha,
            actual_sha=head,
            status="stale",
        )
        stale.extra["detail"] = f"manifest stale: {stale_reason}; rerun sync --apply"
        state.harness.drift = [stale]
        return last_sync
    files = data.get("files")
    items = sorted(files.items()) if isinstance(files, Mapping) else []
    drift = [_file_drift(target, key, entry) for key, entry in items]
    state.harness.drift = [d for d in drift if d is not None]
    return last_sync


def check_session_sync(
    session: Session, scan: _Scan | None, last_sync: float | None
) -> None:
    """Alive session started before the last harness sync runs older harness files."""
    started = _epoch(session.started_at)
    if not session.alive or last_sync is None or started is None:
        return
    if started < last_sync:
        stamp = _dt.datetime.fromtimestamp(last_sync, _dt.UTC)
        _alert(
            session,
            "drift",
            "info",
            "session started before the last harness sync "
            f"({stamp.strftime('%Y-%m-%dT%H:%M:%SZ')}); restart to load it",
            _fallback(session, scan, 1),
        )


def _project_roots(sessions: list[Session], roots: _Roots, claude: str) -> set[str]:
    """Repo root (else cwd) of every session outside home and ~/.claude."""
    found = set()
    for s in sessions:
        cwd = _norm(s.cwd, None) if s.cwd else None
        if cwd is None:
            continue
        root = roots.repo(cwd) or cwd
        if _key(root) != roots.home and not _inside(root, claude):
            found.add(root)
    return found


def run_checks(
    state: FleetState,
    now: float | None = None,
    tick: Callable[[], None] | None = None,
) -> FleetState:
    """Add check alerts, ``collisions`` and ``harness.drift`` to ``state``.

    ``tick`` runs before each per-session step (the collector's deadline check).
    """
    now = time.time() if now is None else now
    tick = tick or (lambda: None)
    claude = os.path.normpath(str(model.claude_dir()))
    roots = _Roots(str(model.home_dir()))
    sessions = state.sessions
    scans: list[_Scan | None] = []
    for s in sessions:
        tick()
        transcript = s.extra.get("transcript")
        scans.append(_scan(Path(transcript)) if isinstance(transcript, str) else None)
    ctx = _Context(roots, _project_roots(sessions, roots, claude), claude, now)
    state.collisions = check_collisions(sessions, scans, roots, now)
    last_sync = check_drift(state, now)
    for s, scan in zip(sessions, scans, strict=True):
        tick()
        check_stuck(s, scan, now)
        check_compliance(s, scan, ctx)
        check_session_sync(s, scan, last_sync)
    return state
