"""Memory guard: keep enough free memory by holding new sessions and closing the browser.

``fleet.py guard run`` is meant for a scheduled task (install/register_guard_task.ps1,
every 5 minutes, only while the user is logged on: the input clock is per logon session).
Each run takes at most one action and only when the user is away: no keyboard or mouse
input for ``idle_min`` minutes, inside the ``window_start``-``window_end`` local hours or
while away mode is on. Unknown idle time or memory = not away, so nothing happens.

1. Hold (any hour, used by ``fleet.py open``): below ``hold_gb`` no new tabs without --force.
2. Close the browser: below ``browser_gb``, unless a live session used Claude in Chrome in
   the last ``browser_quiet_min`` minutes. Its windows are asked to close; a window that
   stays open is left and logged, never killed. Windowless leftovers are ended afterwards.
   A queued job (jobs.py) short of memory also closes it, above that level.
3. Jobs: while away with none running, starts the oldest queued one (see jobs.py) if
   ``need_gb`` + ``job_reserve_gb`` is free; none in the window's last ``job_last_start_min``.
4. Sessions, log-only: below ``hold_gb`` on two runs in a row with no browser to close,
   logs which idle sessions it would close (``would_close_sessions``). Nothing closes them.

Settings: optional ``~/.claude/fleet/guard.json`` over the defaults in :class:`Config`
(``protected`` = project names never proposed for closing). Log: ``guard.jsonl``, last run:
``guard-state.json``, both in ``~/.claude/fleet``.
"""

from __future__ import annotations

import ctypes
import datetime as _dt
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from . import collect, jobs, limits, model, questions

BROWSER_TOOL = "mcp__claude-in-chrome__"
CLOSE_WAIT_S = 60.0
CLOSE_ROUND_S = 2.0
LEFTOVER_GRACE_S = 15.0
LOG_MAX_BYTES = 1024 * 1024
LOW_READINGS = 2
PS_TIMEOUT_S = 30.0
# Children of an idle claude.exe that do not count as running work (MCP servers).
MCP_CHILDREN = frozenset({"node.exe", "cmd.exe", "conhost.exe", "npx.exe", "uvx.exe"})
_PROCESS_NAME = re.compile(r"[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class Config:
    """Thresholds (GB, minutes, hours); ``guard.json`` overrides any of them."""

    hold_gb: float = 1.0
    browser_gb: float = 2.4
    idle_min: float = 60.0
    window_start: int = 4
    window_end: int = 10
    session_idle_h: float = 2.0
    browser_quiet_min: float = 30.0
    job_reserve_gb: float = 0.5
    job_last_start_min: float = 30.0
    browser: str = "brave"
    protected: tuple[str, ...] = ()


def config_path() -> Path:
    """``~/.claude/fleet/guard.json``."""
    return model.fleet_dir() / "guard.json"


def log_path() -> Path:
    """``~/.claude/fleet/guard.jsonl``."""
    return model.fleet_dir() / "guard.jsonl"


def state_path() -> Path:
    """``~/.claude/fleet/guard-state.json``."""
    return model.fleet_dir() / "guard-state.json"


def load_config() -> Config:
    """Defaults, overridden by valid values from guard.json; bad values are ignored."""
    raw = model.read_json_dict(config_path()) or {}
    values: dict[str, Any] = {}
    for f in fields(Config):
        value = raw.get(f.name)
        if f.name == "protected":
            if isinstance(value, list):
                values[f.name] = tuple(str(v).lower() for v in value)
        elif f.name == "browser":
            if isinstance(value, str) and _PROCESS_NAME.fullmatch(value):
                values[f.name] = value
        elif isinstance(value, int | float) and not isinstance(value, bool):
            values[f.name] = int(value) if f.name.startswith("window") else float(value)
    return Config(**values)


def free_gb() -> float | None:
    """Available physical memory in GB, None when unknown."""
    return collect.machine_memory().mem_free_gb


class _LastInput(ctypes.Structure):
    _fields_ = (("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint))


def idle_minutes() -> float | None:
    """Minutes since the last keyboard or mouse input in this logon session, else None."""
    if sys.platform != "win32":
        return None
    windll = ctypes.windll  # type: ignore[attr-defined,unused-ignore]
    info = _LastInput()
    info.cbSize = ctypes.sizeof(_LastInput)
    if not windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    tick = ctypes.c_uint(windll.kernel32.GetTickCount()).value
    return ((tick - info.dwTime) & 0xFFFFFFFF) / 60000.0


def local_hour(now: float) -> int:
    """Hour of the day in the harness time zone."""
    return _dt.datetime.fromtimestamp(now, model.harness_tz()).hour


def user_away(idle: float | None, hour: int, away_flag: bool, cfg: Config) -> bool:
    """Idle long enough, and inside the window or marked away. Unknown idle = present."""
    if idle is None or idle < cfg.idle_min:
        return False
    return away_flag or cfg.window_start <= hour < cfg.window_end


def window_closing(now: float, cfg: Config) -> bool:
    """Inside the last ``job_last_start_min`` minutes before the window ends."""
    local = _dt.datetime.fromtimestamp(now, model.harness_tz())
    end = local.replace(hour=cfg.window_end % 24, minute=0, second=0, microsecond=0)
    left = (end - local).total_seconds() / 60
    return 0 <= left < cfg.job_last_start_min


def headroom(need_gb: float, cfg: Config | None = None) -> str:
    """Whether a job needing ``need_gb`` could run now: ``can run now`` or ``short``."""
    cfg = cfg or load_config()
    free = free_gb()
    if free is None:
        return "memory now: unknown"
    required = need_gb + cfg.job_reserve_gb
    verdict = "can run now" if free >= required else "short"
    return f"memory now: {free:.2f} GB free, {required:g} GB required: {verdict}"


def hold_reason(cfg: Config | None = None) -> str | None:
    """Why a new session should wait: free memory under ``hold_gb``; None = go ahead."""
    cfg = cfg or load_config()
    free = free_gb()
    if free is None or free >= cfg.hold_gb:
        return None
    return f"{free:.2f} GB free, under the {cfg.hold_gb:g} GB needed for a new session"


# --- browser ---


def _powershell(script: str) -> str | None:
    """Run a PowerShell script without a window; its stdout, None on failure."""
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PS_TIMEOUT_S,
            check=False,
            creationflags=model.NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def browser_processes(name: str) -> tuple[int, int] | None:
    """(processes, those with a window) of the browser; None when the check failed."""
    out = _powershell(
        f"$p = @(Get-Process {name} -ErrorAction SilentlyContinue);"
        " $w = @($p | Where-Object { $_.MainWindowHandle -ne 0 });"
        ' Write-Output "$($p.Count) $($w.Count)"'
    )
    parts = (out or "").split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1])


def close_browser(
    name: str,
    processes: Callable[[str], tuple[int, int] | None] = browser_processes,
    run: Callable[[str], str | None] = _powershell,
    sleep: Callable[[float], object] = time.sleep,
) -> dict[str, Any]:
    """Ask every browser window to close; end windowless leftovers; never force a window.

    Returns ``{"outcome": closed|windows_left|check_failed, ...}``.
    """
    waited = 0.0
    while True:
        seen = processes(name)
        if seen is None:
            return {"outcome": "check_failed"}
        if seen[1] == 0:
            break
        if waited >= CLOSE_WAIT_S:
            return {"outcome": "windows_left", "windows": seen[1]}
        run(
            f"Get-Process {name} -ErrorAction SilentlyContinue |"
            " Where-Object { $_.MainWindowHandle -ne 0 } |"
            " ForEach-Object { [void]$_.CloseMainWindow() }"
        )
        sleep(CLOSE_ROUND_S)
        waited += CLOSE_ROUND_S
    if seen[0] == 0:
        return {"outcome": "closed", "leftovers": 0}
    sleep(LEFTOVER_GRACE_S)  # let it finish saving the session
    seen = processes(name)
    if seen is None:
        return {"outcome": "check_failed"}
    if seen[1]:  # a window came back: the user or a task opened it
        return {"outcome": "windows_left", "windows": seen[1]}
    if seen[0]:
        run(f"Get-Process {name} -ErrorAction SilentlyContinue | Stop-Process -Force")
    return {"outcome": "closed", "leftovers": seen[0]}


def _used_browser(entries: Iterable[dict[str, Any]], since: float) -> bool:
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        stamp = model.parse_epoch(entry.get("timestamp"))
        if stamp is None or stamp < since:
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for item in content if isinstance(content, list) else []:
            name = item.get("name") if isinstance(item, dict) else None
            if isinstance(name, str) and name.startswith(BROWSER_TOOL):
                return True
    return False


def browser_in_use(sessions: list[model.Session], now: float, cfg: Config) -> list[str]:
    """Projects of alive sessions (or their subagents) that drove the browser recently."""
    since = now - cfg.browser_quiet_min * 60
    users = []
    for s in sessions:
        path = s.extra.get("transcript") if s.alive else None
        if not isinstance(path, str):
            continue
        main = Path(path)
        paths = [main, *main.with_suffix("").glob("subagents/*.jsonl")]
        recent = [p for p in paths if (model.mtime(p) or 0.0) >= since]
        if any(_used_browser(limits.tail_entries(p), since) for p in recent):
            users.append(s.project or s.name or "?")
    return users


# --- sessions (log-only) ---


def child_names(pid: int) -> list[str] | None:
    """Lower-case names of a process's children; None when the check failed."""
    out = _powershell(
        f"Get-CimInstance Win32_Process -Filter 'ParentProcessId={int(pid)}' |"
        " ForEach-Object { $_.Name }"
    )
    if out is None:
        return None
    return [name.strip().lower() for name in out.splitlines() if name.strip()]


def session_candidates(
    sessions: list[model.Session],
    now: float,
    cfg: Config,
    children: Callable[[int], list[str] | None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(sessions it would close, longest idle first; alive sessions skipped, with why)."""
    children = children or child_names
    would: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for s in sessions:
        if not s.alive:
            continue
        project = s.project or s.name or "?"
        last = model.parse_epoch(s.last_activity)
        idle_h = None if last is None else round((now - last) / 3600, 2)
        why = None
        if project.lower() in cfg.protected:
            why = "protected"
        elif s.kind != "interactive":
            why = f"kind {s.kind}"
        elif s.status not in ("idle", "waiting"):
            why = f"status {s.status}"
        elif s.agents_running:
            why = "agents running"
        elif idle_h is None or idle_h < cfg.session_idle_h:
            why = "recently active"
        elif s.pid is None:
            why = "no pid"
        else:
            names = children(s.pid)
            if names is None:
                why = "child check failed"
            elif busy := sorted(set(names) - MCP_CHILDREN):
                why = "running " + ", ".join(busy)
        if why:
            skipped.append({"project": project, "why": why})
        else:
            would.append(
                {"project": project, "session_id": s.session_id, "idle_h": idle_h}
            )
    would.sort(key=lambda row: -row["idle_h"])
    return would, skipped


# --- one run ---


def _log(entry: dict[str, Any]) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if path.stat().st_size > LOG_MAX_BYTES:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        model.write_atomic(path, "".join(lines[len(lines) // 2 :]))


def _sessions() -> list[model.Session]:
    collect.collect_and_save()
    state = model.load_state()
    return state.sessions if state else []


def tick(
    now: float | None = None,
    dry_run: bool = False,
    sessions: list[model.Session] | None = None,
) -> dict[str, Any]:
    """One guard run; returns the record it logged and saved as the last run."""
    now = time.time() if now is None else now
    cfg = load_config()
    free, idle = free_gb(), idle_minutes()
    flag = questions.is_away()
    away = user_away(idle, local_hour(now), flag, cfg)
    if not dry_run:
        jobs.reconcile(now)
    previous = model.read_json_dict(state_path()) or {}
    low = 0
    record: dict[str, Any] = {
        "ts": model.now_iso(),
        "free_gb": free,
        "idle_min": None if idle is None else round(idle, 1),
        "away": away,
        "action": "none",
    }
    if free is None:
        record["why"] = "memory unknown"
    elif not away:
        record["why"] = "user present"
    else:
        record, acted = _job_step(record, cfg, free, now, dry_run, flag, sessions)
        if not acted:
            if free >= cfg.browser_gb:
                record.setdefault("why", "memory ok")
            else:
                record, low = _act(record, cfg, free, now, dry_run, previous, sessions)
    record["low_readings"] = low
    if dry_run:
        record["dry_run"] = True
        return record
    model.write_atomic(state_path(), json.dumps(record, indent=2) + "\n")
    if away or record["action"] != "none":
        _log(record)
    return record


def _job_step(
    record: dict[str, Any],
    cfg: Config,
    free: float,
    now: float,
    dry_run: bool,
    flag: bool,
    sessions: list[model.Session] | None,
) -> tuple[dict[str, Any], bool]:
    """Start the next queued job, or close the browser for it; True when it acted."""
    active = jobs.running()
    if active:
        return {**record, "job_running": active[0].id}, False
    job = jobs.next_queued()
    if job is None:
        return record, False
    if not flag and window_closing(now, cfg):
        return {**record, "waiting_job": job.id, "why": "window ending"}, False
    if free >= job.need_gb + cfg.job_reserve_gb:
        if dry_run:
            return {**record, "action": "would_start_job", "job": job.id}, True
        jobs.start(job)
        started = {"action": "start_job", "job": job.id, "project": job.project}
        return {**record, **started}, True
    live = _sessions() if sessions is None else sessions
    seen = browser_processes(cfg.browser)
    if seen and seen[0] and not browser_in_use(live, now, cfg):
        if dry_run:
            return {**record, "action": "would_close_browser", "for_job": job.id}, True
        result = close_browser(cfg.browser)
        return {
            **record,
            "action": "close_browser",
            "for_job": job.id,
            **result,
            "free_after_gb": free_gb(),
        }, True
    return {**record, "waiting_job": job.id, "why": "waiting for memory"}, False


def _act(
    record: dict[str, Any],
    cfg: Config,
    free: float,
    now: float,
    dry_run: bool,
    previous: dict[str, Any],
    sessions: list[model.Session] | None,
) -> tuple[dict[str, Any], int]:
    """Away and under ``browser_gb``: close the browser, or log step 3."""
    live = _sessions() if sessions is None else sessions
    seen = browser_processes(cfg.browser)
    if seen is None:
        return {**record, "why": "browser check failed"}, 0
    if seen[0]:
        users = browser_in_use(live, now, cfg)
        if users:
            record = {**record, "browser_in_use_by": users}
        elif dry_run:
            return {**record, "action": "would_close_browser"}, 0
        else:
            result = close_browser(cfg.browser)
            return {
                **record,
                "action": "close_browser",
                **result,
                "free_after_gb": free_gb(),
            }, 0
    if free >= cfg.hold_gb:
        return {**record, "why": "under the browser level, no browser to close"}, 0
    count = previous.get("low_readings")
    low = (count if isinstance(count, int) else 0) + 1
    if low < LOW_READINGS:
        return {**record, "why": "first low reading"}, low
    would, skipped = session_candidates(live, now, cfg)
    return {
        **record,
        "action": "would_close_sessions",
        "would_close": would,
        "skipped": skipped,
    }, low


# --- reporting ---


def _entries() -> list[dict[str, Any]]:
    try:
        text = log_path().read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _describe(entry: dict[str, Any]) -> str:
    when = model.parse_epoch(entry.get("ts"))
    stamp = model.local_time(when) if when is not None else "?"
    if entry.get("action") == "close_browser":
        text = f"closed the browser ({entry.get('outcome')})"
        if entry.get("free_after_gb") is not None:
            text += f", {entry.get('free_gb')} -> {entry.get('free_after_gb')} GB free"
    elif entry.get("action") == "start_job":
        text = f"started job {entry.get('job')} ({entry.get('project')})"
    else:
        names = ", ".join(r.get("project", "?") for r in entry.get("would_close") or [])
        text = f"would close sessions: {names or 'none eligible'}"
    return f"{stamp} {text}"


def recent_actions(now: float, window_s: float = 24 * 3600) -> list[str]:
    """Plain lines for the last day's actions; repeated would-close runs count once."""
    lines: list[str] = []
    last_key = None
    for entry in _entries():
        stamp = model.parse_epoch(entry.get("ts"))
        action = entry.get("action")
        if action in (None, "none") or stamp is None or now - stamp > window_s:
            continue
        key = (action, str(entry.get("would_close")))
        if action == "close_browser" or key != last_key:
            lines.append(_describe(entry))
        last_key = key
    return [*lines, *jobs.finished_lines(now, window_s), *jobs.queued_lines()]


def status(now: float | None = None) -> str:
    """Thresholds, the current readings, the last run and the last day's actions."""
    now = time.time() if now is None else now
    cfg = load_config()
    free, idle = free_gb(), idle_minutes()
    away = user_away(idle, local_hour(now), questions.is_away(), cfg)
    idle_text = "unknown" if idle is None else f"{idle:.0f} min"
    lines = [
        (
            f"memory: {free if free is not None else 'unknown'} GB free"
            f" | hold new sessions under {cfg.hold_gb:g} GB"
            f" | close {cfg.browser} under {cfg.browser_gb:g} GB when away"
        ),
        (
            f"away: {'yes' if away else 'no'} (idle {idle_text}; counts after"
            f" {cfg.idle_min:g} min,"
            f" {cfg.window_start}:00-{cfg.window_end}:00 or away mode)"
        ),
        "sessions: log-only; protected: " + (", ".join(cfg.protected) or "none"),
        "jobs: " + jobs.report(),
    ]
    last = model.read_json_dict(state_path())
    if last:
        why = f" ({last['why']})" if last.get("why") else ""
        lines.append(f"last run: {last.get('ts')}: {last.get('action')}{why}")
    else:
        lines.append("last run: never")
    actions = recent_actions(now)
    if actions:
        lines += ["last 24 h:", *[f"  {a}" for a in actions]]
    else:
        lines.append("last 24 h: no actions")
    return "\n".join(lines)
