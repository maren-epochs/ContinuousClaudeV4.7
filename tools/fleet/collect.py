"""Fleet collector: builds a FleetState from ``~/.claude`` (shapes: schema.md).

Sources, all read-only except the audit rotation and state.json:

- ``sessions/<digits>.json`` only; ``<pid>.<hash>.key`` files are secrets and are
  never opened (the file-name regex admits digits + ``.json`` and nothing else).
- each session's transcript ``projects/<slug>/<sessionId>.jsonl``, last
  ``TAIL_BYTES`` only: model, context % (auto-handoff-stop.mjs transcript rule,
  window never taken from this process's env), last activity, running subagents.
- the statusline pct file ``<tmpdir>/claude-context-pct-<sessionId[:8]>.txt``
  (status.mjs, Claude Code's ``used_percentage``): preferred for context % when
  its mtime is under ``PCT_FRESH_S`` old, or older while the transcript's mtime is
  not newer than it (idle session, ``statusline-idle``).
- the handoff root (project ``thoughts/shared/handoffs`` else
  ``~/.claude/handoffs/<basename>``), the install manifest, the inbox, the tail of
  ``fleet/audit.jsonl`` (rotated to ``audit.jsonl.1`` past ``AUDIT_ROTATE_BYTES``).

Claude Code's files are undocumented: every key is optional, wrong types become
null, and vanished keys raise a ``schema_unknown`` alert instead of an error.
``collect()`` ends with ``checks.run_checks`` (alerts, collisions, per-file
drift); the harness summary here is HEAD vs the sha recorded at the last sync.
It raises ``CollectTimeout`` past ``COLLECT_DEADLINE_S`` (checked between steps) and a
watchdog thread hard-exits the process ``WATCHDOG_GRACE_S`` later when a step hangs
(e.g. a network stat), so a stuck collect never replaces the last good state.json.
"""

from __future__ import annotations

import ctypes
import datetime as _dt
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import checks, model
from .checks import _blocks, _done_notes, _record
from .model import (
    Alert,
    AuditEvent,
    FleetState,
    Handoff,
    Harness,
    Machine,
    Session,
)

TAIL_BYTES = 512 * 1024
HANDOFF_HEAD_BYTES = 64 * 1024
AUDIT_TAIL_BYTES = 256 * 1024
AUDIT_RECENT = 50
AUDIT_ROTATE_BYTES = 5 * 1024 * 1024
SESSION_FILE = re.compile(r"\d+\.json")
SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
EXPECTED_SESSION_KEYS = (
    "pid",
    "sessionId",
    "cwd",
    "startedAt",
    "status",
    "kind",
    "version",
    "updatedAt",
)
AGENT_TOOLS = frozenset({"Agent", "Task"})
HANDOFF_SUFFIXES = (".yaml", ".yml", ".md")
PCT_FRESH_S = 600
PCT_SKEW_S = 5
_PCT_TEXT = re.compile(r"\d{1,6}")
COLLECT_DEADLINE_S = 20.0
WATCHDOG_GRACE_S = 5.0
WATCHDOG_EXIT = 3
WATCHDOG_NAME = "fleet-collect-watchdog"
_hard_exit = os._exit


class CollectTimeout(Exception):
    """collect() ran past its deadline; nothing was saved."""


# --- small helpers ---


def read_text(path: Path, max_bytes: int | None = None) -> str:
    """UTF-8 text of a file (first ``max_bytes`` when given); '' when unreadable."""
    try:
        with path.open("rb") as fh:
            data = fh.read() if max_bytes is None else fh.read(max_bytes)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def read_tail(path: Path, max_bytes: int) -> str:
    """Last ``max_bytes`` of a file as text, minus a partial first line."""
    try:
        with path.open("rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            start = max(0, size - max_bytes)
            fh.seek(start)
            data = fh.read(max_bytes)
    except OSError:
        return ""
    if start > 0:
        cut = data.find(b"\n")
        data = data[cut + 1 :] if cut >= 0 else b""
    return data.decode("utf-8", errors="replace")


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _iso(value: Any) -> str | None:
    """Epoch ms/s number -> ``YYYY-MM-DDTHH:MM:SSZ``; strings kept as-is."""
    if isinstance(value, str):
        return value
    number = _finite(value)
    if number is None:
        return None
    seconds = number / 1000 if number > 1e11 else number
    try:
        stamp = _dt.datetime.fromtimestamp(seconds, _dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _basename(path: str) -> str:
    return re.split(r"[\\/]", path.rstrip("\\/"))[-1] or path


def _load_json(path: Path, warnings: list[str], label: str) -> Any:
    text = read_text(path)
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        warnings.append(f"{label}: invalid JSON")
        return None


# --- processes and machine ---

if sys.platform == "win32":
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _QUERY_LIMITED = 0x1000
    _STILL_ACTIVE = 259

    class _MemoryStatus(ctypes.Structure):
        _fields_ = (
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        )

    def _win_process(pid: int) -> tuple[bool, int | None]:
        handle = _kernel32.OpenProcess(_QUERY_LIMITED, False, pid)
        if not handle:
            return False, None
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False, None
            if code.value != _STILL_ACTIVE:
                return False, None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not _kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return True, None
            created = times[0]
            return True, (created.dwHighDateTime << 32) | created.dwLowDateTime
        finally:
            _kernel32.CloseHandle(handle)


def process_start(pid: int) -> int | None:
    """Process creation time as Claude Code records it (win32 FILETIME), else None."""
    if sys.platform == "win32":
        return _win_process(pid)[1]
    return None


def _win_alive(pid: int, proc_start: Any) -> bool:
    """Running, and its creation time equals ``procStart`` when both are known."""
    running, started = _win_process(pid)
    if not running:
        return False
    try:
        expected = int(proc_start) if isinstance(proc_start, str | int) else None
    except ValueError:
        expected = None
    return expected is None or started is None or started == expected


def process_alive(pid: Any, proc_start: Any) -> bool:
    """True when ``pid`` runs and, on win32, its creation time equals ``procStart``."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        return _win_alive(pid, proc_start)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def machine_memory() -> Machine:
    """Physical memory total/free in GB (win32 API or /proc/meminfo), else nulls."""
    gb = 1024**3
    if sys.platform == "win32":
        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        if _kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return Machine(
                mem_total_gb=round(status.ullTotalPhys / gb, 2),
                mem_free_gb=round(status.ullAvailPhys / gb, 2),
            )
        return Machine()
    info: dict[str, int] = {}
    for line in read_text(Path("/proc/meminfo")).splitlines():
        name, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            info[name] = int(fields[0]) * 1024
    if "MemTotal" not in info:
        return Machine()
    free = info.get("MemAvailable", info.get("MemFree"))
    return Machine(
        mem_total_gb=round(info["MemTotal"] / gb, 2),
        mem_free_gb=round(free / gb, 2) if free is not None else None,
    )


# --- transcripts ---


def context_pct(usage: Any, env: Mapping[str, str]) -> float | None:
    """auto-handoff-stop.mjs rule: input + cache tokens vs CLAUDE_CONTEXT_WINDOW.

    Window falls back to 200K, or 1M once tokens exceed 200K; floored, capped at 100.
    """
    if not isinstance(usage, Mapping):
        return None
    tokens = 0
    for key in (
        "input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        value = _finite(usage.get(key))
        if value is not None:
            tokens += int(value)
    try:
        window = int(float(env.get("CLAUDE_CONTEXT_WINDOW") or 0))
    except ValueError:
        window = 0
    if window <= 0:
        window = 1_000_000 if tokens > 200_000 else 200_000
    return float(min(100, tokens * 100 // window))


def pct_dir() -> Path:
    """Directory status.mjs writes its pct files to (Node ``os.tmpdir()``)."""
    return Path(tempfile.gettempdir())


def statusline_reading(
    session_id: str | None,
    now: float,
    directory: Path | None = None,
    transcript_mtime: float | None = None,
) -> tuple[float, str] | None:
    """(pct, source) from status.mjs's pct file, else None.

    ``statusline`` when the file is under PCT_FRESH_S old; ``statusline-idle`` when
    older but the transcript has not changed since it was written (no API call
    since, so the value still stands).
    """
    if not session_id or not SAFE_SESSION_ID.fullmatch(session_id):
        return None
    path = (directory or pct_dir()) / f"claude-context-pct-{session_id[:8]}.txt"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    age = now - mtime
    if age < -PCT_SKEW_S:
        return None
    if age <= PCT_FRESH_S:
        source = "statusline"
    elif transcript_mtime is not None and transcript_mtime <= mtime:
        source = "statusline-idle"
    else:
        return None
    text = read_text(path, 64).strip()
    if not _PCT_TEXT.fullmatch(text):
        return None
    return float(min(100, int(text))), source


def statusline_pct(
    session_id: str | None, now: float, directory: Path | None = None
) -> float | None:
    """Context % from status.mjs's pct file when fresh (< PCT_FRESH_S), else None."""
    reading = statusline_reading(session_id, now, directory)
    return reading[0] if reading else None


def transcript_path(
    projects: Path, cwd: str | None, session_id: str | None
) -> Path | None:
    """``projects/<slug>/<id>.jsonl``, else the newest ``projects/*/<id>.jsonl``."""
    if not session_id or not SAFE_SESSION_ID.fullmatch(session_id):
        return None
    name = f"{session_id}.jsonl"
    if cwd:
        direct = projects / re.sub(r"[^A-Za-z0-9]", "-", cwd) / name
        if direct.is_file():
            return direct
    best: tuple[float, Path] | None = None
    for candidate in projects.glob(f"*/{name}"):
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, candidate)
    return best[1] if best else None


@dataclass
class _Tally:
    """Running totals of one transcript tail (parse_transcript)."""

    model: str | None = None
    pct: float | None = None
    last: str | None = None
    spawned: set[str] = field(default_factory=set)
    finished: set[str] = field(default_factory=set)
    parsed: int = 0
    typed: int = 0
    assistants: int = 0
    with_usage: int = 0

    def result(self) -> dict[str, Any]:
        issues = []
        if self.parsed and not self.typed:
            issues.append("transcript records lack 'type'")
        if self.assistants and not self.with_usage:
            issues.append("assistant records lack message.usage")
        return {
            "model": self.model,
            "context_pct": self.pct,
            "last_activity": self.last,
            "agents_running": len(self.spawned - self.finished),
            "issues": issues,
        }


def _agent_ids(record: Mapping[str, Any]) -> list[str]:
    return [
        b["id"]
        for b in _blocks(record)
        if b.get("type") == "tool_use"
        and b.get("name") in AGENT_TOOLS
        and isinstance(b.get("id"), str)
    ]


def _result_ids(record: Mapping[str, Any]) -> list[str]:
    return [
        b["tool_use_id"]
        for b in _blocks(record)
        if b.get("type") == "tool_result" and isinstance(b.get("tool_use_id"), str)
    ]


def _async_launch(record: Mapping[str, Any]) -> bool:
    result = record.get("toolUseResult")
    return isinstance(result, Mapping) and (
        result.get("status") == "async_launched" or result.get("isAsync") is True
    )


def _tally_assistant(
    tally: _Tally, record: Mapping[str, Any], env: Mapping[str, str]
) -> None:
    tally.assistants += 1
    message = record.get("message")
    message = message if isinstance(message, Mapping) else {}
    name = message.get("model")
    if isinstance(name, str) and name and not name.startswith("<"):
        tally.model = name
    usage = message.get("usage")
    if isinstance(usage, Mapping):
        tally.with_usage += 1
        tally.pct = context_pct(usage, env)
    tally.spawned.update(_agent_ids(record))


def _tally_record(
    tally: _Tally, record: Mapping[str, Any], env: Mapping[str, str]
) -> None:
    tally.parsed += 1
    kind = record.get("type")
    if isinstance(kind, str):
        tally.typed += 1
    stamp = record.get("timestamp")
    if isinstance(stamp, str) and (tally.last is None or stamp > tally.last):
        tally.last = stamp
    if record.get("isSidechain") is True:
        return
    if kind == "assistant":
        _tally_assistant(tally, record, env)
    elif kind == "user" and not _async_launch(record):
        tally.finished.update(_result_ids(record))


def parse_transcript(path: Path, env: Mapping[str, str]) -> dict[str, Any]:
    """Model, context %, last activity, running agents and schema issues of a tail."""
    tally = _Tally()
    for line in read_tail(path, TAIL_BYTES).splitlines():
        tally.finished.update(tool_id for tool_id, _note in _done_notes(line))
        record = _record(line)
        if record is not None:
            _tally_record(tally, record, env)
    return tally.result()


# --- handoffs ---


def handoff_root(cwd: str, claude: Path) -> Path:
    """Project thoughts/shared/handoffs if it exists, else ~/.claude/handoffs/<base>."""
    local = Path(cwd) / "thoughts" / "shared" / "handoffs"
    return local if local.is_dir() else claude / "handoffs" / _basename(cwd)


def _handoff_field(text: str, name: str) -> str | None:
    match = re.search(rf"^{name}:\s*(.+?)\s*$", text, re.MULTILINE)
    if not match:
        return None
    value = match.group(1).strip().strip("\"'").strip()
    return value[:200] or None


def newest_handoff(root: Path, now: float) -> Handoff | None:
    """Newest .yaml/.yml/.md under root by mtime, with goal/now (status.mjs rules)."""
    if not root.is_dir():
        return None
    best = _newest_handoff_file(root)
    if best is None:
        return None
    mtime, path = best
    text = read_text(path, HANDOFF_HEAD_BYTES)
    return Handoff(
        path=str(path),
        age_h=_finite(round(max(0.0, now - mtime) / 3600, 2)),
        goal=_handoff_goal(text),
        now=_handoff_field(text, "now"),
    )


def _newest_handoff_file(root: Path) -> tuple[float, Path] | None:
    """(mtime, path) of the newest handoff-suffixed file under root."""
    best: tuple[float, Path] | None = None
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = Path(dirpath) / name
            mtime = _mtime(path) if name.endswith(HANDOFF_SUFFIXES) else None
            if mtime is not None and (best is None or mtime > best[0]):
                best = (mtime, path)
    return best


def _handoff_goal(text: str) -> str | None:
    """goal, else topic, else the first ``# `` heading minus a ``Handoff:`` prefix."""
    goal = _handoff_field(text, "goal") or _handoff_field(text, "topic")
    if goal:
        return goal
    heading = re.search(r"^# (.+?)\s*$", text, re.MULTILINE)
    if not heading:
        return None
    return heading.group(1).replace("Handoff:", "").strip()[:200] or None


# --- harness, inbox, audit ---


def git_head(repo: str) -> str | None:
    """``git rev-parse HEAD`` of repo; None outside git or on any failure."""
    sha = model.run_git(repo, "rev-parse", "HEAD", timeout=3)
    return sha if sha is not None and re.fullmatch(r"[0-9a-f]{40,64}", sha) else None


def harness_info(warnings: list[str]) -> Harness:
    """Repo HEAD vs the head_sha recorded in the install manifest."""
    data = _load_json(model.manifest_path(), warnings, "manifest")
    data = data if isinstance(data, Mapping) else {}
    repo = _str(data.get("repo"))
    installed = _str(data.get("head_sha"))
    head = git_head(repo) if repo and Path(repo).is_dir() else None
    in_sync = head == installed if head and installed else None
    dirty = data.get("dirty")
    return Harness(
        repo=repo,
        head_sha=head,
        installed_sha=installed,
        extra={
            "in_sync": in_sync,
            "dirty": dirty if isinstance(dirty, bool) else None,
            "synced_at": _str(data.get("generated_at")),
        },
    )


def inbox_count() -> int:
    """Pending proposals in ~/.claude/harness-inbox."""
    return sum(1 for p in model.list_proposals() if p.status == "pending")


def audit_recent(path: Path) -> list[AuditEvent]:
    """Last ``AUDIT_RECENT`` events from the last ``AUDIT_TAIL_BYTES`` of the log."""
    return model.parse_audit_lines(read_tail(path, AUDIT_TAIL_BYTES))[-AUDIT_RECENT:]


def rotate_audit(path: Path, warnings: list[str]) -> None:
    """Move audit.jsonl to audit.jsonl.1 (replacing it) once past the size cap."""
    try:
        if path.stat().st_size <= AUDIT_ROTATE_BYTES:
            return
        os.replace(path, path.with_name(path.name + ".1"))
    except FileNotFoundError:
        return
    except OSError as exc:
        warnings.append(f"audit rotation failed: {type(exc).__name__}")


# --- sessions ---


@dataclass
class _Where:
    """Collect-wide inputs of build_session; ``handoffs`` caches newest_handoff."""

    projects: Path
    claude: Path
    now: float
    handoffs: dict[str, Handoff | None] = field(default_factory=dict)


def _session_from_file(path: Path, data: Mapping[str, Any]) -> Session:
    """Session fields, extras and the schema alert of one session file."""
    pid = _int(data.get("pid"))
    if pid is None:
        pid = int(path.stem)
    sid = _str(data.get("sessionId"))
    cwd = _str(data.get("cwd"))
    session = Session(
        pid=pid,
        session_id=sid,
        name=_str(data.get("name")),
        project=_basename(cwd) if cwd else None,
        cwd=cwd,
        status=_str(data.get("status")),
        kind=_str(data.get("kind")),
        version=_str(data.get("version")),
        alive=process_alive(pid, data.get("procStart")),
        started_at=_iso(data.get("startedAt")),
        updated_at=_iso(data.get("updatedAt")) or _iso(data.get("statusUpdatedAt")),
    )
    status_at = _iso(data.get("statusUpdatedAt"))
    if status_at:
        session.extra["status_updated_at"] = status_at
    waiting = _str(data.get("waitingFor"))
    if waiting:
        session.extra["waiting_for"] = waiting
    missing = [k for k in EXPECTED_SESSION_KEYS if k not in data]
    if missing:
        session.alerts.append(
            Alert(
                kind="schema_unknown",
                severity="warn",
                session=sid,
                detail="session file lacks: " + ", ".join(missing),
                evidence=path.name,
            )
        )
    return session


def _mtime(path: Path | None) -> float | None:
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _add_transcript(session: Session, transcript: Path, live_pct: Any) -> None:
    """Model, transcript context % (when no live reading), activity, agents, issues."""
    # Empty env: CLAUDE_CONTEXT_WINDOW here belongs to whichever session's Stop
    # hook spawned this collect, not to the session being parsed.
    info = parse_transcript(transcript, {})
    session.model = info["model"]
    if live_pct is None:
        session.context_pct = _finite(info["context_pct"])
        if session.context_pct is not None:
            session.extra["context_source"] = "transcript"
    session.last_activity = info["last_activity"]
    session.agents_running = info["agents_running"]
    session.extra["transcript"] = str(transcript)
    for issue in info["issues"]:
        session.alerts.append(
            Alert(
                kind="schema_unknown",
                severity="warn",
                session=session.session_id,
                detail=issue,
                evidence=transcript.name,
            )
        )


def _add_handoff(session: Session, where: _Where) -> None:
    if not session.cwd:
        return
    root = handoff_root(session.cwd, where.claude)
    key = str(root)
    if key not in where.handoffs:
        where.handoffs[key] = newest_handoff(root, where.now)
    session.handoff = where.handoffs[key]


def build_session(path: Path, data: Mapping[str, Any], where: _Where) -> Session:
    """One Session from a session file plus its transcript tail and handoff."""
    session = _session_from_file(path, data)
    sid = session.session_id
    transcript = transcript_path(where.projects, session.cwd, sid)
    reading = statusline_reading(sid, where.now, transcript_mtime=_mtime(transcript))
    live_pct = reading[0] if reading else None
    if reading is not None:
        session.context_pct, session.extra["context_source"] = reading
    if transcript is not None:
        _add_transcript(session, transcript, live_pct)
    _add_handoff(session, where)
    return session


def collect_sessions(
    claude: Path,
    now: float,
    warnings: list[str],
    tick: Callable[[], None] | None = None,
) -> list[Session]:
    """Every ``sessions/<digits>.json``, alive first, newest start first.

    ``tick`` runs before each session file (deadline check).
    """
    directory = claude / "sessions"
    try:
        names = sorted(e.name for e in os.scandir(directory) if e.is_file())
    except OSError:
        return []
    where = _Where(claude / "projects", claude, now)
    sessions = []
    for name in names:
        if not SESSION_FILE.fullmatch(name):
            continue
        if tick is not None:
            tick()
        path = directory / name
        data = _load_json(path, warnings, name)
        if not isinstance(data, Mapping):
            if data is not None:
                warnings.append(f"{name}: not a JSON object")
            continue
        sessions.append(build_session(path, data, where))
    sessions.sort(key=lambda s: s.started_at or "", reverse=True)
    sessions.sort(key=lambda s: not s.alive)
    return sessions


def _watchdog_fire() -> None:
    try:
        sys.stderr.write("fleet collect: deadline exceeded, aborted\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
    _hard_exit(WATCHDOG_EXIT)


def collect(deadline_s: float | None = COLLECT_DEADLINE_S) -> FleetState:
    """Build a FleetState from the current ``~/.claude``; never raises on bad data.

    Raises ``CollectTimeout`` once ``deadline_s`` has passed (None: no deadline); a
    step still hung ``WATCHDOG_GRACE_S`` later hard-exits the process.
    """
    started = time.perf_counter()
    expires = None if deadline_s is None else started + deadline_s

    def tick() -> None:
        if expires is not None and time.perf_counter() > expires:
            raise CollectTimeout(f"collect exceeded {deadline_s} s")

    watchdog = None
    if deadline_s is not None:
        watchdog = threading.Timer(deadline_s + WATCHDOG_GRACE_S, _watchdog_fire)
        watchdog.name = WATCHDOG_NAME
        watchdog.daemon = True
        watchdog.start()
    try:
        now = time.time()
        claude = model.claude_dir()
        warnings: list[str] = []
        sessions = collect_sessions(claude, now, warnings, tick)
        tick()
        audit = model.audit_path()
        recent = audit_recent(audit)
        rotate_audit(audit, warnings)
        memory = machine_memory()
        state = FleetState(
            generated_at=model.now_iso(),
            harness=harness_info(warnings),
            sessions=sessions,
            inbox_count=inbox_count(),
            audit_recent=recent,
            machine=Machine(
                mem_total_gb=_finite(memory.mem_total_gb),
                mem_free_gb=_finite(memory.mem_free_gb),
            ),
        )
        tick()
        checks.run_checks(state, now, tick)
        tick()
    finally:
        if watchdog is not None:
            watchdog.cancel()
    state.extra["warnings"] = warnings
    state.extra["collect_s"] = round(time.perf_counter() - started, 3)
    return state


def collect_and_save(
    path: Path | None = None, deadline_s: float | None = COLLECT_DEADLINE_S
) -> Path:
    """Collect and write state.json atomically; returns the path."""
    return model.save_state(collect(deadline_s), path)
