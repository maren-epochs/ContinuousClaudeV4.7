"""Fleet collector: builds a FleetState from ``~/.claude`` (shapes: schema.md).

Sources, all read-only except the audit rotation and state.json:

- ``sessions/<digits>.json`` only; ``<pid>.<hash>.key`` files are secrets and are
  never opened (the file-name regex admits digits + ``.json`` and nothing else).
- each session's transcript ``projects/<slug>/<sessionId>.jsonl``, last
  ``TAIL_BYTES`` only: model, context % (auto-handoff-stop.mjs rule), last
  activity, running subagents.
- the handoff root (project ``thoughts/shared/handoffs`` else
  ``~/.claude/handoffs/<basename>``), the install manifest, the inbox, the tail of
  ``fleet/audit.jsonl`` (rotated to ``audit.jsonl.1`` past ``AUDIT_ROTATE_BYTES``).

Claude Code's files are undocumented: every key is optional, wrong types become
null, and vanished keys raise a ``schema_unknown`` alert instead of an error.
``collect()`` ends with ``checks.run_checks`` (alerts, collisions, per-file
drift); the harness summary here is HEAD vs the sha recorded at the last sync.
"""

from __future__ import annotations

import ctypes
import datetime as _dt
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import checks, model
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
_NOTIFICATION = re.compile(r"<task-notification>.*?</task-notification>", re.DOTALL)
_TOOL_USE_ID = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")
_STATUS = re.compile(r"<status>([^<]+)</status>")


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


def process_alive(pid: Any, proc_start: Any) -> bool:
    """True when ``pid`` runs and, on win32, its creation time equals ``procStart``."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        running, started = _win_process(pid)
        if not running:
            return False
        try:
            expected = int(proc_start) if isinstance(proc_start, str | int) else None
        except ValueError:
            expected = None
        return expected is None or started is None or started == expected
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


def _blocks(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, Mapping)]


def parse_transcript(path: Path, env: Mapping[str, str]) -> dict[str, Any]:
    """Model, context %, last activity, running agents and schema issues of a tail."""
    model_name: str | None = None
    pct: float | None = None
    last: str | None = None
    spawned: set[str] = set()
    finished: set[str] = set()
    parsed = typed = assistants = with_usage = 0
    for line in read_tail(path, TAIL_BYTES).splitlines():
        if "<task-notification>" in line:
            for note in _NOTIFICATION.findall(line.replace("\\n", "\n")):
                status = _STATUS.search(note)
                if status and status.group(1).strip() == "running":
                    continue
                finished.update(i.strip() for i in _TOOL_USE_ID.findall(note))
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, Mapping):
            continue
        parsed += 1
        kind = record.get("type")
        if isinstance(kind, str):
            typed += 1
        stamp = record.get("timestamp")
        if isinstance(stamp, str) and (last is None or stamp > last):
            last = stamp
        if record.get("isSidechain") is True:
            continue
        if kind == "assistant":
            assistants += 1
            message = record.get("message")
            message = message if isinstance(message, Mapping) else {}
            name = message.get("model")
            if isinstance(name, str) and name and not name.startswith("<"):
                model_name = name
            usage = message.get("usage")
            if isinstance(usage, Mapping):
                with_usage += 1
                pct = context_pct(usage, env)
            for block in _blocks(record):
                tool_id = block.get("id")
                if (
                    block.get("type") == "tool_use"
                    and block.get("name") in AGENT_TOOLS
                    and isinstance(tool_id, str)
                ):
                    spawned.add(tool_id)
        elif kind == "user":
            result = record.get("toolUseResult")
            launched = isinstance(result, Mapping) and (
                result.get("status") == "async_launched"
                or result.get("isAsync") is True
            )
            if launched:
                continue
            for block in _blocks(record):
                tool_id = block.get("tool_use_id")
                if block.get("type") == "tool_result" and isinstance(tool_id, str):
                    finished.add(tool_id)
    issues = []
    if parsed and not typed:
        issues.append("transcript records lack 'type'")
    if assistants and not with_usage:
        issues.append("assistant records lack message.usage")
    return {
        "model": model_name,
        "context_pct": pct,
        "last_activity": last,
        "agents_running": len(spawned - finished),
        "issues": issues,
    }


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
    best: tuple[float, Path] | None = None
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(HANDOFF_SUFFIXES):
                continue
            path = Path(dirpath) / name
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, path)
    if best is None:
        return None
    mtime, path = best
    text = read_text(path, HANDOFF_HEAD_BYTES)
    goal = _handoff_field(text, "goal") or _handoff_field(text, "topic")
    if not goal:
        heading = re.search(r"^# (.+?)\s*$", text, re.MULTILINE)
        if heading:
            goal = heading.group(1).replace("Handoff:", "").strip()[:200] or None
    return Handoff(
        path=str(path),
        age_h=_finite(round(max(0.0, now - mtime) / 3600, 2)),
        goal=goal,
        now=_handoff_field(text, "now"),
    )


# --- harness, inbox, audit ---


def git_head(repo: str) -> str | None:
    """``git rev-parse HEAD`` of repo; None outside git or on any failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = proc.stdout.strip()
    return (
        sha if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", sha) else None
    )


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
    events = []
    for line in read_tail(path, AUDIT_TAIL_BYTES).splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, Mapping):
            events.append(AuditEvent.from_dict(data))
    return events[-AUDIT_RECENT:]


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


def build_session(
    path: Path,
    data: Mapping[str, Any],
    projects: Path,
    claude: Path,
    now: float,
    handoffs: dict[str, Handoff | None],
) -> Session:
    """One Session from a session file plus its transcript tail and handoff."""
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
    transcript = transcript_path(projects, cwd, sid)
    if transcript is not None:
        info = parse_transcript(transcript, os.environ)
        session.model = info["model"]
        session.context_pct = _finite(info["context_pct"])
        session.last_activity = info["last_activity"]
        session.agents_running = info["agents_running"]
        session.extra["transcript"] = str(transcript)
        for issue in info["issues"]:
            session.alerts.append(
                Alert(
                    kind="schema_unknown",
                    severity="warn",
                    session=sid,
                    detail=issue,
                    evidence=transcript.name,
                )
            )
    if cwd:
        root = handoff_root(cwd, claude)
        key = str(root)
        if key not in handoffs:
            handoffs[key] = newest_handoff(root, now)
        session.handoff = handoffs[key]
    return session


def collect_sessions(claude: Path, now: float, warnings: list[str]) -> list[Session]:
    """Every ``sessions/<digits>.json``, alive first, newest start first."""
    directory = claude / "sessions"
    try:
        names = sorted(e.name for e in os.scandir(directory) if e.is_file())
    except OSError:
        return []
    projects = claude / "projects"
    handoffs: dict[str, Handoff | None] = {}
    sessions = []
    for name in names:
        if not SESSION_FILE.fullmatch(name):
            continue
        path = directory / name
        data = _load_json(path, warnings, name)
        if not isinstance(data, Mapping):
            if data is not None:
                warnings.append(f"{name}: not a JSON object")
            continue
        sessions.append(build_session(path, data, projects, claude, now, handoffs))
    sessions.sort(key=lambda s: s.started_at or "", reverse=True)
    sessions.sort(key=lambda s: not s.alive)
    return sessions


def collect() -> FleetState:
    """Build a FleetState from the current ``~/.claude``; never raises on bad data."""
    started = time.perf_counter()
    now = time.time()
    claude = model.claude_dir()
    warnings: list[str] = []
    sessions = collect_sessions(claude, now, warnings)
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
    checks.run_checks(state, now)
    state.extra["warnings"] = warnings
    state.extra["collect_s"] = round(time.perf_counter() - started, 3)
    return state


def collect_and_save(path: Path | None = None) -> Path:
    """Collect and write state.json atomically; returns the path."""
    return model.save_state(collect(), path)
