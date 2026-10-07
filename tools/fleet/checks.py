"""Fleet checks (VAL-806): alerts, collisions and harness drift on a FleetState.

Runs after the collector (``collect.collect``) on the sessions it built. Inputs per
session: ``extra.transcript`` (re-read here, last ``TAIL_BYTES``, with absolute
line numbers for evidence), ``extra.status_updated_at``/``waiting_for``, ``alive``,
``status``, ``agents_running``, ``started_at``. Subagent transcripts
(``<transcript stem>/**/*.jsonl``) modified since the tail began add their writes.

- collision: two alive sessions wrote the same absolute path in the last 30 min
  (Write/Edit/MultiEdit/NotebookEdit), or two alive sessions sit in one git repo.
- stuck (alive sessions): an AskUserQuestion, a ``waiting`` status or a prose
  question unanswered for 20 min; the same tool call failing 3+ times in a row at
  the end of the transcript; a subagent stopped at its turn limit with no report.
- compliance: model not ``claude-opus-5-5`` with no user message naming another
  model; completed turns ending in a question without AskUserQuestion; writes into
  another project's folder.
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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import model
from .model import Alert, Collision, DriftEntry, FleetState, Session

TAIL_BYTES = 512 * 1024
COLLISION_WINDOW_S = 30 * 60
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


def _scan(path: Path) -> _Scan:
    scan = _Scan(path=path)
    turn: _Turn | None = None
    for line, raw in tail_lines(path, TAIL_BYTES):
        scan.last_line = line
        if "<task-notification>" in raw:
            for note in _NOTIFICATION.findall(raw.replace("\\n", "\n")):
                status = _STATUS.search(note)
                if status and status.group(1).strip() == "running":
                    continue
                for tool_id in _TOOL_USE_ID.findall(note):
                    scan.notes.setdefault(tool_id.strip(), (note, line))
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(record, Mapping):
            continue
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
            for block in _blocks(record):
                tool_id = block.get("tool_use_id")
                if block.get("type") == "tool_result" and isinstance(tool_id, str):
                    meta = record.get("toolUseResult")
                    scan.results[tool_id] = _Result(
                        is_error=block.get("is_error") is True,
                        text=_content_text(block.get("content"))[:2000],
                        meta=meta if isinstance(meta, Mapping) else None,
                        line=line,
                    )
            if not sidechain and _is_prompt(record):
                if turn is not None:
                    turn.closed = True
                turn = None
                message = record.get("message")
                text = _content_text(
                    message.get("content") if isinstance(message, Mapping) else None
                )
                if _OTHER_MODEL.search(_REMINDER.sub("", text)):
                    scan.model_named = True
    return scan


def _scan_assistant(
    scan: _Scan,
    record: Mapping[str, Any],
    line: int,
    ts: float | None,
    turn: _Turn | None,
) -> None:
    sidechain = record.get("isSidechain") is True
    if not sidechain:
        message = record.get("message")
        name = message.get("model") if isinstance(message, Mapping) else None
        if isinstance(name, str) and name and not name.startswith("<"):
            scan.model = (name, line)
    for block in _blocks(record):
        kind = block.get("type")
        name = block.get("name")
        if kind == "tool_use" and isinstance(name, str):
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
            if turn is not None:
                turn.ends_with_text = False
                turn.asked = turn.asked or name == ASK_TOOL
                if tool_id:
                    turn.call_ids.append(tool_id)
        elif kind == "text" and turn is not None:
            body = block.get("text")
            if isinstance(body, str) and body.strip():
                turn.ends_with_text = True
                turn.last_text = body
                turn.last_line = line
                turn.last_ts = ts


def _subagent_writes(scan: _Scan, now: float) -> list[_Write]:
    folder = scan.path.with_suffix("")
    if not folder.is_dir():
        return []
    since = now - COLLISION_WINDOW_S
    if scan.first_ts is not None:
        since = min(since, scan.first_ts)
    writes = []
    for path in sorted(folder.rglob("*.jsonl")):
        try:
            if path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        writes.extend(_scan(path).writes)
    return writes


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


def check_collisions(
    sessions: list[Session],
    scans: list[_Scan | None],
    roots: _Roots,
    now: float,
) -> list[Collision]:
    """Same path written by 2+ alive sessions in the window; 2+ alive in one repo."""
    collisions: list[Collision] = []
    by_path: dict[str, dict[int, _Write]] = {}
    for i, (s, scan) in enumerate(zip(sessions, scans, strict=True)):
        if not s.alive or scan is None:
            continue
        for w in scan.writes + _subagent_writes(scan, now):
            path = _norm(w.raw, s.cwd)
            if path is None or w.ts is None or now - w.ts > COLLISION_WINDOW_S:
                continue
            slot = by_path.setdefault(_key(path), {})
            if i not in slot or (slot[i].ts or 0) <= w.ts:
                slot[i] = _Write(path, w.ts, w.evidence)
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
            others = ", ".join(_label(s) for s in members if s is not sessions[i])
            _alert(
                sessions[i],
                "collision",
                "error",
                f"wrote {latest.raw} within 30 min of {others}",
                slot[i].evidence,
            )
    by_repo: dict[str, list[int]] = {}
    repo_path: dict[str, str] = {}
    for i, s in enumerate(sessions):
        if not s.alive or not s.cwd:
            continue
        cwd = _norm(s.cwd, None)
        root = roots.repo(cwd) if cwd else None
        if root:
            by_repo.setdefault(_key(root), []).append(i)
            repo_path.setdefault(_key(root), root)
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
                detail=f"{len(members)} alive sessions work in this repo",
            )
        )
        for i in idx:
            others = ", ".join(_label(s) for s in members if s is not sessions[i])
            _alert(
                sessions[i],
                "collision",
                "warn",
                f"same repo {root} as {others}",
                _fallback(sessions[i], scans[i]),
            )
    return collisions


def _turn_limited(result: _Result | None, note: tuple[str, int] | None) -> int | None:
    """Evidence line when a subagent result says it stopped at its turn limit."""
    if result is not None and result.meta is not None:
        status = result.meta.get("status")
        if status != "async_launched" and result.meta.get("isAsync") is not True:
            for key in _LIMIT_KEYS:
                value = result.meta.get(key)
                if isinstance(value, str) and _LIMIT_VALUE.search(value):
                    return result.line
    if result is not None and _LIMIT_TEXT.search(result.text[:500]):
        return result.line
    if note is not None:
        body, line = note
        status = _STATUS.search(body)
        if status and _LIMIT_VALUE.search(status.group(1)):
            return line
        if _LIMIT_TEXT.search(body[:1000]):
            return line
    return None


def _report_path(prompt: Any) -> str | None:
    if not isinstance(prompt, str):
        return None
    match = _OUTPUT_KEY.search(prompt) or _REPORT_PATH.search(prompt)
    if not match:
        return None
    return (match.group(1) if match.groups() else match.group(0)).replace("\\\\", "\\")


def check_stuck(session: Session, scan: _Scan | None, now: float) -> None:
    """Waiting on the user > 20 min, repeated failing call, turn-limited subagent."""
    if not session.alive:
        return
    pending_ask = None
    if scan is not None:
        for call in reversed(scan.calls):
            if call.name == ASK_TOOL:
                if call.id is None or call.id not in scan.results:
                    pending_ask = call
                break
    status_at = _epoch(session.extra.get("status_updated_at")) or _epoch(
        session.updated_at
    )
    final = scan.turns[-1] if scan is not None and scan.turns else None
    if pending_ask and pending_ask.ts and now - pending_ask.ts > STUCK_WAIT_S:
        minutes = int((now - pending_ask.ts) // 60)
        _alert(
            session,
            "stuck",
            "warn",
            f"AskUserQuestion unanswered for {minutes} min",
            _fallback(session, scan, pending_ask.line),
        )
    elif (
        session.status == "waiting"
        and status_at is not None
        and now - status_at > STUCK_WAIT_S
    ):
        waiting = session.extra.get("waiting_for")
        why = f" for {waiting}" if isinstance(waiting, str) and waiting else ""
        line = pending_ask.line if pending_ask else None
        _alert(
            session,
            "stuck",
            "warn",
            f"waiting{why} for {int((now - status_at) // 60)} min",
            _fallback(session, scan, line),
        )
    elif (
        scan is not None
        and final is not None
        and session.status != "busy"
        and session.agents_running == 0
        and final.ends_with_text
        and final.last_text is not None
        and _is_question(final.last_text)
        and final.last_ts is not None
        and now - final.last_ts > STUCK_WAIT_S
    ):
        minutes = int((now - final.last_ts) // 60)
        _alert(
            session,
            "stuck",
            "warn",
            f"question to the user unanswered for {minutes} min",
            _fallback(session, scan, final.last_line),
        )
    if scan is None:
        return
    if scan.calls:
        last = scan.calls[-1]
        run: list[_Result] = []
        for call in reversed(scan.calls):
            res = scan.results.get(call.id) if call.id else None
            if call.key != last.key or call.name != last.name or not res:
                break
            if not res.is_error:
                break
            run.append(res)
        if len(run) >= REPEAT_FAILS:
            _alert(
                session,
                "stuck",
                "warn",
                f"{last.name} failed {len(run)} times in a row with the same input",
                _fallback(session, scan, run[0].line),
            )
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


def _is_question(text: str) -> bool:
    return text.rstrip(_QUESTION_TAIL).endswith("?")


def check_compliance(
    session: Session,
    scan: _Scan | None,
    roots: _Roots,
    project_roots: Iterable[str],
    claude: str,
) -> None:
    """Model preference, question turns without AskUserQuestion, foreign writes."""
    if scan is None:
        return
    if scan.model is not None:
        name, line = scan.model
        if not name.startswith(EXPECTED_MODEL) and not scan.model_named:
            _alert(
                session,
                "compliance",
                "warn",
                f"model {name} is not {EXPECTED_MODEL} and no user message named it",
                _fallback(session, scan, line),
            )
    questions = []
    for n, turn in enumerate(scan.turns):
        final = n == len(scan.turns) - 1
        pending = any(i not in scan.results for i in turn.call_ids)
        complete = turn.closed or (final and session.status != "busy" and not pending)
        if (
            complete
            and turn.ends_with_text
            and turn.last_text
            and not turn.asked
            and _is_question(turn.last_text)
        ):
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
    if not session.cwd:
        return
    cwd = _norm(session.cwd, None)
    if cwd is None:
        return
    own = roots.repo(cwd) or cwd
    known = sorted(project_roots, key=len, reverse=True)
    foreign: dict[str, tuple[str, str]] = {}
    for w in scan.writes:
        path = _norm(w.raw, session.cwd)
        if path is None or _inside(path, claude) or _inside(path, own):
            continue
        root = roots.repo(path) or next((r for r in known if _inside(path, r)), None)
        if root is None or _key(root) == _key(own) or _inside(own, root):
            continue
        foreign[_key(root)] = (root, w.evidence)
    for root, evidence in foreign.values():
        _alert(
            session,
            "compliance",
            "warn",
            f"wrote into another project's folder ({os.path.basename(root)}: {root})",
            evidence,
        )


def _sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _safe_rel(key: str) -> PurePosixPath | None:
    rel = PurePosixPath(key)
    if (
        not key
        or "\\" in key
        or ":" in key
        or rel.is_absolute()
        or any(part in ("", ".", "..") for part in rel.parts)
        or rel.parts[0] == "sessions"
        or rel.name.endswith(".key")
    ):
        return None
    return rel


def check_drift(state: FleetState, now: float) -> float | None:
    """Fill ``harness.drift``; returns the last sync time (epoch) when known."""
    manifest_file = model.manifest_path()
    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state.harness.drift = []
        return None
    if not isinstance(data, Mapping):
        state.harness.drift = []
        return None
    target = manifest_file.parent
    generated_raw = data.get("generated_at")
    generated = _epoch(generated_raw)
    head = data.get("head_sha") if isinstance(data.get("head_sha"), str) else None
    marker_file = target / INSTALLED_MARKER
    marker_sha: str | None = None
    marker_at: float | None = None
    try:
        tokens = marker_file.read_text(encoding="utf-8").split()
        marker_sha = tokens[0] if tokens else None
        marker_at = marker_file.stat().st_mtime
    except OSError:
        pass
    last_sync = max((t for t in (generated, marker_at) if t is not None), default=None)
    stale_reason = None
    if marker_sha is not None and marker_sha != head:
        stale_reason = f"head_sha {head} != installed marker {marker_sha}"
    elif (
        marker_at is not None
        and generated is not None
        and marker_at > generated + STALE_SLACK_S
    ):
        stale_reason = f"installed marker is newer than generated_at {generated_raw}"
    if stale_reason:
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
    drift = []
    for key, entry in sorted(files.items()) if isinstance(files, Mapping) else []:
        if not isinstance(key, str) or not isinstance(entry, Mapping):
            continue
        if entry.get("kept") is True:
            continue
        expected = entry.get("sha256")
        rel = _safe_rel(key)
        if not isinstance(expected, str) or rel is None:
            continue
        path = target.joinpath(*rel.parts)
        repo = entry.get("repo_path")
        actual = _sha256(path) if path.is_file() else None
        if actual is not None and actual == expected.lower():
            continue
        drift.append(
            DriftEntry(
                installed_path=str(path),
                repo_path=repo if isinstance(repo, str) else None,
                expected_sha=expected,
                actual_sha=actual,
                status="modified" if actual is not None else "missing",
            )
        )
    state.harness.drift = drift
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


def run_checks(state: FleetState, now: float | None = None) -> FleetState:
    """Add check alerts, ``collisions`` and ``harness.drift`` to ``state``."""
    now = time.time() if now is None else now
    claude = os.path.normpath(str(model.claude_dir()))
    roots = _Roots(str(model.home_dir()))
    sessions = state.sessions
    scans: list[_Scan | None] = []
    for s in sessions:
        transcript = s.extra.get("transcript")
        scans.append(_scan(Path(transcript)) if isinstance(transcript, str) else None)
    project_roots = set()
    for s in sessions:
        cwd = _norm(s.cwd, None) if s.cwd else None
        if cwd is None:
            continue
        root = roots.repo(cwd) or cwd
        if _key(root) != roots.home and not _inside(root, claude):
            project_roots.add(root)
    state.collisions = check_collisions(sessions, scans, roots, now)
    last_sync = check_drift(state, now)
    for s, scan in zip(sessions, scans, strict=True):
        check_stuck(s, scan, now)
        check_compliance(s, scan, roots, project_roots, claude)
        check_session_sync(s, scan, last_sync)
    return state
