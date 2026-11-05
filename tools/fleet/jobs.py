"""Memory-heavy jobs queued for the away period; the memory guard starts them.

A session queues a job itself when its plan needs a lot of memory (``job add``). If memory
allows, it then offers to run it now; no answer leaves it queued, and a yes cancels the job and
runs the command through the session's own tools. The stored argv runs unchanged, without a
shell, in the stored folder. ``guard.tick`` starts
the shortest queued job while the user is away and enough memory is free, one at a time. It
spawns ``job supervise <id>``, a detached process that outlives the guard run, runs the
command, enforces the time limit and records the result. When the user returns a running
job finishes; no new one starts.

Files: ``~/.claude/fleet/jobs/<id>.json`` and ``<id>.log`` (the job's output).
Status: queued -> running -> done | failed | timed_out | lost; queued or running -> cancelled;
queued -> blocked (a job file matching :data:`BLOCKED`, checked again before the start).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import collect, launch, model

SUPERVISOR_GRACE_S = 120.0  # a running job with no supervisor pid after this is lost
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
)
_BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
DONE_STATUSES = ("done", "failed", "timed_out", "lost", "cancelled", "blocked")


# Commands never run unattended. Matched on the lower-case command line with "/" separators.
# It reads the command line only, not what a script does, and stays narrow on purpose: ordinary
# builds, tests, backtests, data pulls, normal pushes and cleanup of a project folder pass.
_ROOT = r"(/|~/?|\$home/?|[a-z]:/?|[a-z]:/users(/[^/\s\"']+)?/?)"
BLOCKED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern))
    for name, pattern in (
        ("force push", r"\bgit\b.*\bpush\b.*\s(--force\S*|-f\b|\+\S)"),
        ("history rewrite", r"\bgit\b.*\b(filter-branch|filter-repo)\b"),
        ("hard reset", r"\bgit\b.*\breset\b.*\s--hard\b"),
        (
            "delete of a drive or home folder",
            r"\b(rm|remove-item|rmdir|rd|del)\b.*[\s\"']" + _ROOT + r"([\"']|\s|$)",
        ),
        (
            "credential file",
            (
                r"(^|[\s/\"'=])\.env(\.(?!example|sample|template)[\w-]+)?([\"'\s]|$)"
                r"|/\.(ssh|aws)(/|[\"'\s]|$)|\bid_(rsa|ed25519|ecdsa)\b|\.(pem|pfx|key)\b"
                r"|\.credentials|privacy-key"
            ),
        ),
        ("claude settings", r"\.claude/settings|\.claude\.json"),
        (
            "live money",
            (
                r"\b(place|submit|send)_?order|\blive_?trad|\bemsx|\bib_?gateway\b"
                r"|(^|\s)--live(=|\s|$)"
            ),
        ),
        (
            "deploy or publish",
            (
                r"(^|\s)--prod(uction)?(=|\s|$)|\b(npm|pnpm|yarn|cargo)\s+publish\b"
                r"|\btwine\s+upload\b|\bterraform\s+(apply|destroy)\b"
            ),
        ),
        (
            "persistence or machine change",
            (
                r"\bschtasks\b.*/create|register-scheduledtask|\breg\s+(add|delete)\b"
                r"|\bshutdown\b|\brestart-computer\b|\bdiskpart\b"
            ),
        ),
        (
            "hidden or downloaded code",
            (
                r"\s-enc(odedcommand)?\b|\binvoke-expression\b|\biex\b"
                r"|\b(curl|wget|irm|iwr)\b.*\|\s*(sh|bash|pwsh|powershell|python)\b"
            ),
        ),
    )
)


class JobError(Exception):
    """A job request that cannot be carried out."""


@dataclass
class Job:
    """One queued command."""

    id: str
    project: str
    cwd: str
    argv: list[str]
    need_gb: float
    limit_min: float
    status: str = "queued"
    note: str = ""
    queued_at: str = ""
    started_at: str | None = None
    ended_at: str | None = None
    supervisor_pid: int | None = None
    supervisor_start: int | None = None
    child_pid: int | None = None
    exit_code: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def jobs_dir() -> Path:
    """``~/.claude/fleet/jobs``."""
    return model.fleet_dir() / "jobs"


def job_path(job_id: str) -> Path:
    """The job's JSON file; refuses an unsafe id."""
    if not model.is_safe_proposal_id(job_id):
        raise JobError(f"bad job id: {job_id!r}")
    return jobs_dir() / f"{job_id}.json"


def log_path(job_id: str) -> Path:
    """The job's output file."""
    return job_path(job_id).with_suffix(".log")


def save(job: Job) -> None:
    """Write the job's file atomically."""
    model.write_atomic(job_path(job.id), json.dumps(asdict(job), indent=2) + "\n")


def load(job_id: str) -> Job:
    """The job with this id; JobError when missing or malformed."""
    raw = model.read_json_dict(job_path(job_id))
    if raw is None:
        raise JobError(f"no job {job_id}")
    return _from_raw(raw, job_id)


def _from_raw(raw: dict[str, Any], label: str) -> Job:
    known = {k: v for k, v in raw.items() if k in Job.__dataclass_fields__}
    try:
        return Job(**known)
    except TypeError as exc:
        raise JobError(f"malformed job {label}: {exc}") from None


def all_jobs() -> list[Job]:
    """Every readable job, oldest first (ids sort by creation time)."""
    out = []
    for path in sorted(jobs_dir().glob("*.json")):
        raw = model.read_json_dict(path)
        if raw is None:
            continue
        try:
            out.append(_from_raw(raw, path.stem))
        except JobError:
            continue
    return out


def blocked_reason(argv: list[str]) -> str | None:
    """The blocklist rule this command matches, else None."""
    text = " ".join(argv).lower().replace(chr(92), "/")
    return next((name for name, rule in BLOCKED if rule.search(text)), None)


def add(
    cwd: str, argv: list[str], need_gb: float, limit_min: float, note: str = ""
) -> Job:
    """Queue a command; refuses a missing folder, no command or an impossible need."""
    folder = Path(cwd)
    if not folder.is_dir():
        raise JobError(f"not a directory: {cwd}")
    if not argv:
        raise JobError("no command given")
    reason = blocked_reason(argv)
    if reason:
        raise JobError(f"blocked ({reason}): not run unattended")
    total = collect.machine_memory().mem_total_gb
    if need_gb <= 0 or (total is not None and need_gb >= total):
        raise JobError(f"--need-gb must be above 0 and under this PC's {total} GB")
    if limit_min <= 0:
        raise JobError("--limit-min must be above 0")
    resolved = os.path.normpath(os.path.abspath(cwd))
    job = Job(
        id=f"job-{time.time_ns() // 1_000_000}",
        project=Path(resolved).name,
        cwd=resolved,
        argv=list(argv),
        need_gb=need_gb,
        limit_min=limit_min,
        note=note,
        queued_at=model.now_iso(),
    )
    save(job)
    return job


def running() -> list[Job]:
    """Jobs currently marked running."""
    return [j for j in all_jobs() if j.status == "running"]


def next_queued(mark_blocked: bool = True) -> Job | None:
    """The queued job with the shortest limit the blocklist allows, oldest first on a tie.

    Marks the ones the blocklist refuses. A long job cannot hold up a short one behind it.
    """
    queued = [j for j in all_jobs() if j.status == "queued"]
    for job in sorted(queued, key=lambda j: j.limit_min):
        reason = blocked_reason(job.argv)
        if reason is None:
            return job
        if mark_blocked:
            job.status, job.ended_at = "blocked", model.now_iso()
            job.extra["blocked"] = reason
            save(job)
    return None


def _pid_alive(pid: int | None, start: int | None = None) -> bool:
    return pid is not None and collect.process_alive(pid, start)


def reconcile(now: float | None = None) -> None:
    """Mark running jobs whose supervisor is gone (reboot, sleep kill) as lost."""
    now = time.time() if now is None else now
    for job in running():
        if job.supervisor_pid is None:
            started = model.parse_epoch(job.started_at) or 0.0
            if now - started < SUPERVISOR_GRACE_S:
                continue
        elif _pid_alive(job.supervisor_pid, job.supervisor_start):
            continue
        job.status = "lost"
        job.ended_at = model.now_iso()
        save(job)


def spawn_supervisor(job_id: str) -> None:
    """Start ``job supervise <id>`` detached, so it outlives this process."""
    command = [sys.executable, str(Path(__file__).with_name("fleet.py"))]
    command += ["job", "supervise", job_id]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform != "win32":
        subprocess.Popen(command, start_new_session=True, **kwargs)
        return
    try:
        subprocess.Popen(command, creationflags=_DETACHED | _BREAKAWAY, **kwargs)
    except OSError:  # the parent's job object forbids breaking away
        subprocess.Popen(command, creationflags=_DETACHED, **kwargs)


def start(job: Job) -> None:
    """Mark the job running and hand it to a supervisor; failed when that cannot start."""
    job.status = "running"
    job.started_at = model.now_iso()
    save(job)
    try:
        spawn_supervisor(job.id)
    except OSError as exc:
        job.status = "failed"
        job.ended_at = model.now_iso()
        job.extra["error"] = str(exc)
        save(job)


def _kill_tree(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
            creationflags=model.NO_WINDOW,
        )
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def supervise(job_id: str) -> str:
    """Run the job's command to its end or time limit and record the result."""
    job = load(job_id)
    if job.status != "running":
        raise JobError(f"{job_id} is {job.status}, not running")
    job.supervisor_pid = os.getpid()
    job.supervisor_start = collect.process_start(job.supervisor_pid)
    save(job)
    status, code, error = "failed", None, None
    with log_path(job_id).open("ab") as output:
        try:
            exe = shutil.which(job.argv[0], path=os.environ.get("PATH")) or job.argv[0]
            child = subprocess.Popen(
                [exe, *job.argv[1:]],
                cwd=job.cwd,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                env=launch.clean_env(dict(os.environ)),
                creationflags=model.NO_WINDOW,
            )
        except OSError as exc:
            error = str(exc)
        else:
            if load(job_id).status == "cancelled":  # cancelled before the pid was saved
                _kill_tree(child.pid)
                child.wait()
                return f"{job_id}: cancelled"
            job.child_pid = child.pid
            save(job)
            try:
                code = child.wait(timeout=job.limit_min * 60)
                status = "done" if code == 0 else "failed"
            except subprocess.TimeoutExpired:
                _kill_tree(child.pid)
                child.wait()
                status = "timed_out"
    current = load(job_id)  # cancel may have written while the command ran
    if current.status == "cancelled":
        return f"{job_id}: cancelled"
    job.status, job.exit_code, job.ended_at = status, code, model.now_iso()
    if error:
        job.extra["error"] = error
    save(job)
    return f"{job_id}: {status}"


def cancel(job_id: str) -> str:
    """Cancel a queued job, or stop a running one and its child processes."""
    job = load(job_id)
    if job.status not in ("queued", "running"):
        raise JobError(f"{job_id} already {job.status}")
    was_running = job.status == "running"
    job.status = "cancelled"
    job.ended_at = model.now_iso()
    save(job)
    if (
        was_running
        and job.child_pid
        and _pid_alive(job.supervisor_pid, job.supervisor_start)
    ):
        _kill_tree(job.child_pid)
    return f"{job_id}: cancelled"


def describe(job: Job) -> str:
    """One line for lists and reports."""
    command = " ".join(job.argv)
    clipped = command if len(command) <= 60 else command[:57] + "..."
    code = "" if job.exit_code is None else f" exit {job.exit_code}"
    return (
        f"{job.id}  {job.status}{code}  {job.project}  needs {job.need_gb:g} GB,"
        f" limit {job.limit_min:g} min  {clipped}"
    )


def queued_lines() -> list[str]:
    """Plain lines for what is waiting: command and reason, so the user can see them."""
    return [
        f"queued {j.id} ({j.project}): {' '.join(j.argv)[:120]} - {j.note}"
        for j in all_jobs()
        if j.status == "queued"
    ]


def report(include_done: bool = False) -> str:
    """The queue: queued and running jobs, plus finished ones when asked."""
    rows = [j for j in all_jobs() if include_done or j.status in ("queued", "running")]
    return "\n".join(describe(j) for j in rows) if rows else "no jobs"


def finished_lines(now: float, window_s: float = 24 * 3600) -> list[str]:
    """Plain lines for jobs that ended in the window."""
    lines = []
    for job in all_jobs():
        ended = model.parse_epoch(job.ended_at)
        if (
            job.status in DONE_STATUSES
            and ended is not None
            and now - ended <= window_s
        ):
            code = "" if job.exit_code is None else f", exit {job.exit_code}"
            lines.append(
                f"{model.local_time(ended)} job {job.status} ({job.project}{code}):"
                f" {job.id}"
            )
    return lines
