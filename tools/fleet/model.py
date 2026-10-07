"""Fleet data contract: FleetState, Proposal and AuditEvent (see schema.md).

Stdlib dataclasses with a tolerant JSON round-trip. ``from_dict`` never raises:
a missing key or a value of the wrong type takes the field default, list items
of the wrong shape are dropped, and unknown keys are kept in ``extra`` and
written back by ``to_dict`` (newer writers, undocumented Claude Code fields).

All files live under ``~/.claude`` (outside any repo); home resolves from
USERPROFILE/HOME, so tests point both at a temp dir.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

SCHEMA_VERSION = 1
PROPOSAL_KINDS = ("edit", "lesson")
PROPOSAL_STATUSES = ("pending", "applied", "rejected")
SESSION_KINDS = ("interactive", "bg")
_PROPOSAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# Windows device names, with or without an extension (NUL.txt is still NUL).
_RESERVED_ID = re.compile(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?", re.IGNORECASE)


class _Bad(Exception):
    """A value does not fit its declared type; the caller uses the default."""


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _Bad
    return value


def _coerce_union(tp: Any, value: Any) -> Any:
    args = typing.get_args(tp)
    if value is None and type(None) in args:
        return None
    for arg in args:
        if arg is type(None):
            continue
        try:
            return _coerce(arg, value)
        except _Bad:
            pass
    raise _Bad


def _coerce_list(tp: Any, value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise _Bad
    (item_tp,) = typing.get_args(tp) or (Any,)
    out = []
    for item in value:
        try:
            out.append(_coerce(item_tp, item))
        except _Bad:
            pass
    return out


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise _Bad


def _coerce_int(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise _Bad


def _coerce_float(value: Any) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    raise _Bad


def _coerce_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    raise _Bad


_SCALARS: dict[Any, Any] = {
    bool: _coerce_bool,
    int: _coerce_int,
    float: _coerce_float,
    str: _coerce_str,
}


def _coerce(tp: Any, value: Any) -> Any:
    if tp is Any:
        return value
    origin = typing.get_origin(tp)
    if origin is types.UnionType or origin is typing.Union:
        return _coerce_union(tp, value)
    if origin is list:
        return _coerce_list(tp, value)
    if origin is dict:
        return dict(_mapping(value))
    if isinstance(tp, type) and issubclass(tp, Record):
        return tp.from_dict(_mapping(value))
    scalar = _SCALARS.get(tp)
    return value if scalar is None else scalar(value)


def _plain(value: Any) -> Any:
    if isinstance(value, Record):
        return value.to_dict()
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _default(f: dataclasses.Field[Any]) -> Any:
    if f.default_factory is not dataclasses.MISSING:
        return f.default_factory()
    return f.default


@dataclass(kw_only=True)
class Record:
    """Base for every contract type: tolerant from_dict, to_dict, JSON helpers."""

    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Build from parsed JSON; never raises (see module docstring)."""
        if not isinstance(data, Mapping):
            data = {}
        hints = typing.get_type_hints(cls)
        kwargs: dict[str, Any] = {}
        names = set()
        for f in dataclasses.fields(cls):
            if f.name == "extra":
                continue
            names.add(f.name)
            if f.name not in data:
                continue
            try:
                kwargs[f.name] = _coerce(hints[f.name], data[f.name])
            except _Bad:
                kwargs[f.name] = _default(f)
        extra = {k: v for k, v in data.items() if k not in names and k != "extra"}
        return cls(extra=extra, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-ready dict: declared fields, then the preserved extra keys."""
        out = {
            f.name: _plain(getattr(self, f.name))
            for f in dataclasses.fields(self)
            if f.name != "extra"
        }
        for k, v in self.extra.items():
            out.setdefault(k, v)
        return out

    @classmethod
    def from_json(cls, text: str) -> Self:
        """Parse a JSON document (raises json.JSONDecodeError on invalid JSON)."""
        return cls.from_dict(json.loads(text))

    def to_json(self, indent: int | None = 2) -> str:
        """Serialize to a JSON document (UTF-8 text, non-ASCII kept)."""
        return json.dumps(
            self.to_dict(), indent=indent, ensure_ascii=False, allow_nan=False
        )


# --- FleetState (~/.claude/fleet/state.json) ---


@dataclass(kw_only=True)
class DriftEntry(Record):
    """One installed harness file that differs from the repo/manifest."""

    installed_path: str | None = None
    repo_path: str | None = None
    expected_sha: str | None = None
    actual_sha: str | None = None
    status: str | None = None


@dataclass(kw_only=True)
class Harness(Record):
    """Repo HEAD vs installed harness version, plus per-file drift."""

    repo: str | None = None
    head_sha: str | None = None
    installed_sha: str | None = None
    drift: list[DriftEntry] = field(default_factory=list)


@dataclass(kw_only=True)
class Handoff(Record):
    """Newest handoff of a session's project."""

    path: str | None = None
    age_h: float | None = None
    goal: str | None = None
    now: str | None = None


@dataclass(kw_only=True)
class Alert(Record):
    """A check finding attached to a session."""

    kind: str | None = None
    severity: str | None = None
    session: str | None = None
    detail: str | None = None
    evidence: str | None = None


@dataclass(kw_only=True)
class Session(Record):
    """One Claude Code session (from ~/.claude/sessions/<pid>.json + transcript)."""

    pid: int | None = None
    session_id: str | None = None
    name: str | None = None
    project: str | None = None
    cwd: str | None = None
    status: str | None = None
    kind: str | None = None
    version: str | None = None
    alive: bool = False
    started_at: str | None = None
    updated_at: str | None = None
    model: str | None = None
    context_pct: float | None = None
    last_activity: str | None = None
    handoff: Handoff | None = None
    agents_running: int = 0
    alerts: list[Alert] = field(default_factory=list)


@dataclass(kw_only=True)
class AuditEvent(Record):
    """One risky action, one line of ~/.claude/fleet/audit.jsonl."""

    ts: str | None = None
    project: str | None = None
    session_id: str | None = None
    category: str | None = None
    command: str | None = None
    tool: str | None = None


@dataclass(kw_only=True)
class Collision(Record):
    """Two or more alive sessions touching the same path or repo."""

    kind: str | None = None
    target: str | None = None
    sessions: list[str] = field(default_factory=list)
    detail: str | None = None


@dataclass(kw_only=True)
class Machine(Record):
    """Host memory snapshot."""

    mem_total_gb: float | None = None
    mem_free_gb: float | None = None


@dataclass(kw_only=True)
class FleetState(Record):
    """Whole-fleet snapshot written by the collector."""

    schema_version: int = SCHEMA_VERSION
    generated_at: str | None = None
    harness: Harness = field(default_factory=Harness)
    sessions: list[Session] = field(default_factory=list)
    inbox_count: int = 0
    audit_recent: list[AuditEvent] = field(default_factory=list)
    collisions: list[Collision] = field(default_factory=list)
    machine: Machine = field(default_factory=Machine)


# --- Proposal (~/.claude/harness-inbox/<id>.json) ---


@dataclass(kw_only=True)
class ProposalSource(Record):
    """Session that produced the proposal."""

    project: str | None = None
    session_id: str | None = None
    cwd: str | None = None


@dataclass(kw_only=True)
class ProposalTarget(Record):
    """Installed file the session tried to change and its repo source."""

    installed_path: str | None = None
    repo_path: str | None = None


@dataclass(kw_only=True)
class Change(Record):
    """The intended change: content (Write/lesson), old/new (Edit) or command."""

    tool: str | None = None
    content: str | None = None
    old_string: str | None = None
    new_string: str | None = None
    command: str | None = None


@dataclass(kw_only=True)
class Proposal(Record):
    """A redirected harness edit or a shared lesson awaiting review."""

    schema_version: int = SCHEMA_VERSION
    id: str | None = None
    created_at: str | None = None
    kind: str = "edit"
    source: ProposalSource = field(default_factory=ProposalSource)
    target: ProposalTarget = field(default_factory=ProposalTarget)
    change: Change = field(default_factory=Change)
    reason: str | None = None
    status: str = "pending"


# --- Locations ---


def home_dir(env: Mapping[str, str] | None = None, platform: str | None = None) -> Path:
    """User home: USERPROFILE then HOME on win32, HOME then USERPROFILE elsewhere.

    The env value is normalized (``os.path.normpath``: ``a/../b`` -> ``b``) before
    use, so a ``..`` segment in HOME never survives into the derived ~/.claude paths.
    """
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    order = ("USERPROFILE", "HOME") if platform == "win32" else ("HOME", "USERPROFILE")
    for key in order:
        value = env.get(key)
        if value:
            return Path(os.path.normpath(value))
    return Path.home()


def claude_dir() -> Path:
    """``~/.claude``."""
    return home_dir() / ".claude"


def fleet_dir() -> Path:
    """``~/.claude/fleet``."""
    return claude_dir() / "fleet"


def state_path() -> Path:
    """``~/.claude/fleet/state.json``."""
    return fleet_dir() / "state.json"


def audit_path() -> Path:
    """``~/.claude/fleet/audit.jsonl``."""
    return fleet_dir() / "audit.jsonl"


def guard_errors_path() -> Path:
    """``~/.claude/fleet/guard-errors.log``."""
    return fleet_dir() / "guard-errors.log"


def inbox_dir() -> Path:
    """``~/.claude/harness-inbox``."""
    return claude_dir() / "harness-inbox"


def manifest_path() -> Path:
    """``~/.claude/.ccv47-manifest.json`` (written by install/sync_global.py)."""
    return claude_dir() / ".ccv47-manifest.json"


def is_safe_proposal_id(proposal_id: object) -> bool:
    """True for a path-safe id that is not a Windows device name."""
    return (
        isinstance(proposal_id, str)
        and _PROPOSAL_ID.fullmatch(proposal_id) is not None
        and _RESERVED_ID.fullmatch(proposal_id) is None
    )


def proposal_path(proposal_id: str) -> Path:
    """``~/.claude/harness-inbox/<id>.json``; ValueError on an unsafe id."""
    if not is_safe_proposal_id(proposal_id):
        raise ValueError(f"unsafe proposal id: {proposal_id!r}")
    return inbox_dir() / f"{proposal_id}.json"


def now_iso() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_proposal_id() -> str:
    """Sortable, path-safe id: ``<UTC stamp>-<8 hex>``."""
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


# --- File IO ---


def write_atomic(path: Path, text: str) -> Path:
    """Write text via a temp file in the same dir + os.replace; creates parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows: a reader holding the target open blocks the rename briefly.
                if attempt == 4:
                    raise
                time.sleep(0.05)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def run_git(repo: str, *args: str, timeout: float) -> str | None:
    """Stripped stdout of ``git -C repo <args>``; None on a nonzero exit or failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def parse_audit_lines(text: str) -> list[AuditEvent]:
    """AuditEvents of every JSON-object line in text; blank/invalid lines skipped."""
    events = []
    for line in text.splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, Mapping):
            events.append(AuditEvent.from_dict(data))
    return events


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_state(state: FleetState, path: Path | None = None) -> Path:
    """Write state.json atomically; returns the path."""
    return write_atomic(path or state_path(), state.to_json() + "\n")


def load_state(path: Path | None = None) -> FleetState | None:
    """Read state.json; None when missing or not valid JSON."""
    data = _load_json(path or state_path())
    return FleetState.from_dict(data) if isinstance(data, Mapping) else None


def save_proposal(proposal: Proposal) -> Path:
    """Write ``harness-inbox/<id>.json`` atomically (assigns an id if unset)."""
    if not proposal.id:
        proposal.id = new_proposal_id()
    return write_atomic(proposal_path(proposal.id), proposal.to_json() + "\n")


def load_proposal(proposal_id: str) -> Proposal | None:
    """Read one proposal; None when the id is unsafe, the file missing or not JSON."""
    if not is_safe_proposal_id(proposal_id):
        return None
    data = _load_json(proposal_path(proposal_id))
    return Proposal.from_dict(data) if isinstance(data, Mapping) else None


def list_proposals(directory: Path | None = None) -> list[Proposal]:
    """Every readable ``*.json`` proposal, oldest first (created_at, then id).

    Skips files whose stored id is unsafe or differs from the filename stem.
    """
    directory = directory or inbox_dir()
    if not directory.is_dir():
        return []
    out = [pr for pr in map(_proposal_file, directory.glob("*.json")) if pr]
    return sorted(out, key=lambda pr: (pr.created_at or "", pr.id or ""))


def _proposal_file(path: Path) -> Proposal | None:
    """The proposal in path when its stored id is safe and matches the file stem."""
    data = _load_json(path)
    if not isinstance(data, Mapping):
        return None
    proposal = Proposal.from_dict(data)
    if proposal.id == path.stem and is_safe_proposal_id(proposal.id):
        return proposal
    return None


def append_audit(event: AuditEvent, path: Path | None = None) -> Path:
    """Append one compact JSON line to audit.jsonl; creates parents."""
    path = path or audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        event.to_dict(), ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
    return path


def read_audit(path: Path | None = None, limit: int | None = None) -> list[AuditEvent]:
    """Parse audit.jsonl, skipping blank/invalid lines; ``limit`` keeps the last N."""
    path = path or audit_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    events = parse_audit_lines(text)
    return events[-limit:] if limit else events
