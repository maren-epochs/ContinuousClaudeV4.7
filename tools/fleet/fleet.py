#!/usr/bin/env python3
"""Fleet CLI: ``py -3.13 tools/fleet/fleet.py <command>``.

collect          build state.json from ~/.claude and print a one-line summary
report           live sessions, alerts, collisions, drift, inbox and recent audit
json             state.json as JSON on stdout (report/json collect first when
                 state.json is missing or with --fresh)
inbox            harness-inbox proposals (pending; --all for every status)
show <id>        one proposal in full
apply <id>       apply a proposal to the harness REPO file (never the installed
                 copy, never commits); lessons append to --doc; --dry-run diffs
reject <id>      set status rejected in place (the file stays: lesson dedupe)
lessons          propose cross-project lessons (--source memory|bloks|all, --max N)
dashboard [out]  HTML dashboard via dashboard.py (default ~/.claude/fleet/dashboard.html)

Exit codes: 0 done, 1 refused (reason on stderr), 2 usage or dashboard missing.
"""

from __future__ import annotations

import argparse
import difflib
import importlib
import io
import json
import os
import secrets
import stat
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import ModuleType
from typing import Any

if __package__:
    from . import collect, lessons, model
    from .model import Change, FleetState, Proposal, Session
else:  # run as a script: tools/ first, so `fleet` is this package, not this file
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from fleet import collect, lessons, model
    from fleet.model import Change, FleetState, Proposal, Session

LESSONS_MAX = 10
LESSON_SOURCES = ("memory", "bloks", "all")
FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
AUDIT_SHOWN = 10
DRIFT_SHOWN = 20
SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}
SYNC_HINT = "py -3.13 install/sync_global.py --apply"


class Refused(Exception):
    """The command refuses to act; the message says why."""


def _state(fresh: bool) -> FleetState:
    state = None if fresh else model.load_state()
    if state is None:
        collect.collect_and_save()
        state = model.load_state() or FleetState()
    return state


def summary(state: FleetState) -> str:
    """One line: sessions, alive, elapsed seconds, warnings."""
    n = len(state.sessions)
    alive = sum(1 for s in state.sessions if s.alive)
    warnings = state.extra.get("warnings") or []
    elapsed = state.extra.get("collect_s")
    took = f" in {elapsed:.2f}s" if isinstance(elapsed, int | float) else ""
    plural = "" if n == 1 else "s"
    return (
        f"collected {n} session{plural} ({alive} alive){took}, {len(warnings)} warnings"
    )


# --- report ---


def _cell(value: object, width: int) -> str:
    text = "-" if value is None or value == "" else str(value)
    return text[:width].ljust(width)


def _clip(value: object, width: int) -> str:
    text = "-" if value is None or value == "" else " ".join(str(value).split())
    return text if len(text) <= width else text[: width - 3] + "..."


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


SESSION_COLUMNS = (
    ("project", 24),
    ("status", 8),
    ("model", 18),
    ("ctx", 5),
    ("agt", 3),
    ("handoff", 7),
    ("alr", 3),
    ("last activity", 24),
)


def _session_row(s: Session) -> str:
    ctx = f"{s.context_pct:.0f}%" if s.context_pct is not None else None
    age = (
        f"{s.handoff.age_h:.1f}h" if s.handoff and s.handoff.age_h is not None else None
    )
    values = (
        s.project,
        s.status,
        s.model,
        ctx,
        s.agents_running,
        age,
        len(s.alerts) or None,
        s.last_activity,
    )
    return " ".join(
        _cell(v, w) for v, (_, w) in zip(values, SESSION_COLUMNS, strict=True)
    )


def _target_of(p: Proposal) -> str | None:
    return p.target.repo_path or p.target.installed_path


def _session_lines(state: FleetState) -> list[str]:
    live = [s for s in state.sessions if s.alive]
    dead = len(state.sessions) - len(live)
    if live:
        header = " ".join(_cell(name, w) for name, w in SESSION_COLUMNS)
        lines = [f"live sessions ({len(live)}):", "  " + header.rstrip()]
        lines += ["  " + _session_row(s).rstrip() for s in live]
    else:
        lines = ["no live sessions"]
    if dead:
        lines.append(f"({dead} not alive, omitted)")
    return lines


def _alert_lines(state: FleetState) -> list[str]:
    found = [(s, a) for s in state.sessions for a in s.alerts]
    if not found:
        return ["alerts: none"]
    found.sort(key=lambda sa: SEVERITY_ORDER.get(sa[1].severity or "", 3))
    lines = [f"alerts ({len(found)}):"]
    for s, a in found:
        who = s.project or s.session_id or "-"
        lines.append(
            f"  [{a.severity or '-'}] {a.kind or '-'} {who}: {a.detail or '-'}"
        )
        if a.evidence:
            lines.append(f"      at {a.evidence}")
    return lines


def _collision_lines(state: FleetState) -> list[str]:
    if not state.collisions:
        return ["collisions: none"]
    lines = [f"collisions ({len(state.collisions)}):"]
    for c in state.collisions:
        who = ", ".join(c.sessions) or "-"
        lines.append(f"  {c.kind or '-'} {c.target or '-'}: sessions {who}")
        if c.detail:
            lines.append(f"      {c.detail}")
    return lines


def _drift_lines(state: FleetState) -> list[str]:
    drift = state.harness.drift
    if not drift:
        return ["drift: none"]
    lines = [f"drift ({len(drift)}):"]
    for d in drift[:DRIFT_SHOWN]:
        repo = f" (repo {d.repo_path})" if d.repo_path else ""
        lines.append(f"  {d.status or '-'} {d.installed_path or '-'}{repo}")
        detail = d.extra.get("detail")
        if isinstance(detail, str) and detail:
            lines.append(f"      {detail}")
    if len(drift) > DRIFT_SHOWN:
        lines.append(f"  ... {len(drift) - DRIFT_SHOWN} more")
    return lines


def _inbox_lines(proposals: list[Proposal]) -> list[str]:
    pending = [p for p in proposals if p.status == "pending"]
    if not pending:
        return ["inbox: empty"]
    lines = [f"inbox ({len(pending)} pending):"]
    for p in pending:
        lines.append(
            f"  {p.id}  {p.kind:<6} {_cell(p.source.project, 16)} "
            f"{_clip(_target_of(p), 40)}  {_clip(p.reason, 60)}"
        )
    return lines


def _audit_lines(state: FleetState) -> list[str]:
    events = state.audit_recent[-AUDIT_SHOWN:]
    if not events:
        return ["audit: none"]
    lines = [f"audit ({len(events)} recent):"]
    for e in reversed(events):
        lines.append(
            f"  {e.ts or '-'} {_cell(e.project, 16)} {e.category or '-'}"
            f" ({e.tool or '-'}): {_clip(e.command, 80)}"
        )
    return lines


def report(state: FleetState, proposals: list[Proposal] | None = None) -> str:
    """Plain-text report: live sessions, alerts, collisions, drift, inbox, audit.

    ``proposals`` defaults to the inbox on disk; only pending ones are listed.
    """
    if proposals is None:
        proposals = model.list_proposals()
    alive = sum(1 for s in state.sessions if s.alive)
    h = state.harness
    sync = h.extra.get("in_sync")
    sync_text = {True: "in sync", False: "behind HEAD", None: "unknown"}[
        sync if isinstance(sync, bool) else None
    ]
    m = state.machine
    lines = [
        f"fleet state {state.generated_at or '-'}",
        (
            f"harness: {sync_text} (installed {(h.installed_sha or '-')[:7]},"
            f" head {(h.head_sha or '-')[:7]})"
        ),
        f"machine: {_num(m.mem_free_gb)} GB free of {_num(m.mem_total_gb)} GB",
        (
            f"sessions: {len(state.sessions)} ({alive} alive)"
            f" | inbox: {state.inbox_count} | audit: {len(state.audit_recent)} recent"
        ),
    ]
    for section in (
        _session_lines(state),
        _alert_lines(state),
        _collision_lines(state),
        _drift_lines(state),
        _inbox_lines(proposals),
        _audit_lines(state),
    ):
        lines += ["", *section]
    warnings = state.extra.get("warnings") or []
    if warnings:
        lines += ["", f"{len(warnings)} parse warnings:"] + [f"  {w}" for w in warnings]
    return "\n".join(lines)


# --- inbox / show ---


def _load(pid: str) -> tuple[Path, dict[str, Any], Proposal]:
    """Path, raw JSON and parsed proposal; Refused unless id/kind/status are valid."""
    if not model.is_safe_proposal_id(pid):
        raise Refused(f"unsafe proposal id {pid!r}")
    path = model.proposal_path(pid)
    if not path.is_file():
        raise Refused(f"no proposal {pid} in {model.inbox_dir()}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"{path} is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise Refused(f"{path} is not a JSON object")
    if raw.get("id") != pid:
        raise Refused(f"stored id {raw.get('id')!r} does not match the file id {pid}")
    kind, status = raw.get("kind", "edit"), raw.get("status", "pending")
    if not isinstance(kind, str) or kind not in model.PROPOSAL_KINDS:
        raise Refused(f"unknown kind {kind!r} (expected {model.PROPOSAL_KINDS})")
    if not isinstance(status, str) or status not in model.PROPOSAL_STATUSES:
        raise Refused(f"unknown status {status!r} (expected {model.PROPOSAL_STATUSES})")
    return path, raw, Proposal.from_dict(raw)


def _save_raw(path: Path, raw: dict[str, Any]) -> None:
    try:
        text = json.dumps(raw, indent=2, ensure_ascii=False, allow_nan=False)
    except ValueError as exc:
        raise Refused(f"{path}: {exc}") from exc
    model.write_atomic(path, text + "\n")


def inbox(show_all: bool = False) -> str:
    """Table of inbox proposals (pending only unless show_all)."""
    props = model.list_proposals()
    if not show_all:
        props = [p for p in props if p.status == "pending"]
    if not props:
        return "inbox is empty" if show_all else "no pending proposals"
    columns = (("id", 25), ("kind", 6), ("status", 8), ("project", 16), ("target", 40))
    lines = [" ".join(_cell(name, w) for name, w in columns) + " reason"]
    for p in props:
        values = (p.id, p.kind, p.status, p.source.project, _clip(_target_of(p), 40))
        row = " ".join(_cell(v, w) for v, (_, w) in zip(values, columns, strict=True))
        lines.append(f"{row} {_clip(p.reason, 60)}")
    return "\n".join(lines)


def show(pid: str) -> str:
    """One proposal in full: metadata, target and the exact change."""
    _, _, p = _load(pid)
    src, tgt, ch = p.source, p.target, p.change
    repo = f", repo {tgt.extra['repo']}" if tgt.extra.get("repo") else ""
    lines = [
        f"id: {p.id}",
        f"kind: {p.kind}  status: {p.status}  created: {p.created_at or '-'}",
        (
            f"source: project {src.project or '-'}, session {src.session_id or '-'},"
            f" cwd {src.cwd or '-'}"
        ),
        f"target: installed {tgt.installed_path or '-'}",
        f"        repo_path {tgt.repo_path or '-'}{repo}",
        f"reason: {p.reason or '-'}",
        f"change: {ch.tool or '-'}",
    ]
    for key, value in ch.extra.items():
        if key != "edits":
            lines.append(f"  {key}: {json.dumps(value, ensure_ascii=False)}")
    for key in ("command", "content", "old_string", "new_string"):
        value = getattr(ch, key)
        if value is not None:
            lines += [f"--- {key}", value]
    edits = ch.extra.get("edits")
    for i, e in enumerate(edits if isinstance(edits, list) else [], 1):
        if isinstance(e, dict):
            lines += [f"--- edit {i} old_string", str(e.get("old_string"))]
            lines += [f"--- edit {i} new_string", str(e.get("new_string"))]
            if e.get("replace_all"):
                lines.append(f"  edit {i} replace_all: true")
    for key, value in p.extra.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines)


# --- apply ---


def _manifest() -> dict[str, Any]:
    try:
        data = json.loads(model.manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _repo_root(repo_arg: str | None, manifest: dict[str, Any], p: Proposal) -> Path:
    """--repo, else the manifest repo, else the proposal's target.repo."""
    for cand in (repo_arg, manifest.get("repo"), p.target.extra.get("repo")):
        if isinstance(cand, str) and cand:
            root = Path(cand).resolve()
            break
    else:
        raise Refused(
            "harness repo unknown: pass --repo (no manifest repo, no target.repo)"
        )
    if not root.is_dir():
        raise Refused(f"harness repo {root} is not a directory")
    if not (root / ".git").exists():
        raise Refused(f"harness repo {root} is not a git checkout (no .git)")
    return root


def _same(a: str, b: str) -> bool:
    a, b = a.replace("\\", "/"), b.replace("\\", "/")
    return a.lower() == b.lower() if sys.platform == "win32" else a == b


def _manifest_rel(installed: str | None, manifest: dict[str, Any]) -> str | None:
    """Manifest repo_path of an installed path under ~/.claude, if listed."""
    files = manifest.get("files")
    if not installed or not isinstance(files, dict):
        return None
    try:
        key = (
            Path(os.path.realpath(installed))
            .relative_to(Path(os.path.realpath(model.claude_dir())))
            .as_posix()
        )
    except ValueError:
        return None
    for name, entry in files.items():
        if isinstance(name, str) and _same(name, key) and isinstance(entry, dict):
            rel = entry.get("repo_path")
            return rel if isinstance(rel, str) else None
    return None


def _repo_rel(p: Proposal, manifest: dict[str, Any]) -> str:
    """repo_path of the proposal, cross-checked against the manifest entry."""
    from_manifest = _manifest_rel(p.target.installed_path, manifest)
    rel = p.target.repo_path
    if rel and from_manifest and not _same(rel, from_manifest):
        raise Refused(
            f"proposal repo_path {rel} differs from the manifest repo_path"
            f" {from_manifest} of {p.target.installed_path}"
        )
    rel = rel or from_manifest
    if not rel:
        raise Refused("no repo_path in the proposal or the manifest")
    return rel


def _checked(root: Path, path: Path, label: str) -> Path:
    """Resolved path; Refused when outside the repo, in ~/.claude or a .key file."""
    path = path.resolve()
    if not path.is_relative_to(root):
        raise Refused(f"{label} resolves outside the harness repo {root}: {path}")
    claude = Path(os.path.realpath(model.claude_dir()))
    if path == claude or path.is_relative_to(claude):
        raise Refused(
            f"{path} is inside {claude}, the installed copy; apply changes the repo only"
        )
    if path.suffix.lower() == ".key":
        raise Refused(f"refusing to open a .key file: {path}")
    return path


def _repo_file(root: Path, rel: str) -> Path:
    posix = PurePosixPath(rel.replace("\\", "/"))
    if posix.is_absolute() or PureWindowsPath(rel).anchor or ".." in posix.parts:
        raise Refused(f"repo_path {rel!r} must be repo-relative without '..'")
    return _checked(root, root / rel, f"repo_path {rel!r}")


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise Refused(f"cannot read {path}: {exc}") from exc


def _write(path: Path, text: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    model.write_atomic(path, text)
    os.chmod(path, mode)


def _eol(text: str, s: str) -> str:
    """s with the line endings of text (CRLF when text has any)."""
    return s.replace("\r\n", "\n").replace("\n", "\r\n") if "\r\n" in text else s


def _replace(text: str, old: object, new: object, replace_all: bool, label: str) -> str:
    if not isinstance(old, str) or not old:
        raise Refused(f"{label}: old_string is missing or empty")
    if not isinstance(new, str):
        raise Refused(f"{label}: new_string is missing")
    old, new = _eol(text, old), _eol(text, new)
    n = text.count(old)
    if n == 0:
        raise Refused(
            f"{label}: old_string not found in the repo file (it differs from what the"
            " session saw; installed .md copies also carry sync path rewrites)"
        )
    if n > 1 and not replace_all:
        raise Refused(f"{label}: old_string found {n} times and replace_all is not set")
    return text.replace(old, new) if replace_all else text.replace(old, new, 1)


def _notebook(text: str, change: Change) -> str:
    """Apply a NotebookEdit (replace/insert/delete by cell id) to .ipynb JSON."""
    try:
        nb = json.loads(text)
    except ValueError as exc:
        raise Refused(f"notebook is not JSON: {exc}") from exc
    cells = nb.get("cells") if isinstance(nb, dict) else None
    if not isinstance(cells, list):
        raise Refused("notebook has no cells list")
    meta = change.extra.get("notebook")
    meta = meta if isinstance(meta, dict) else {}
    cell_id, cell_type = meta.get("cell_id"), meta.get("cell_type")
    mode = meta.get("edit_mode") or "replace"
    idx = None
    if cell_id:
        ids = [c.get("id") if isinstance(c, dict) else None for c in cells]
        if cell_id not in ids:
            raise Refused(f"notebook cell {cell_id!r} not found")
        idx = ids.index(cell_id)
    content = change.content
    if mode in ("replace", "insert") and not isinstance(content, str):
        raise Refused("NotebookEdit change has no new source")
    source = (content or "").splitlines(keepends=True)
    if mode == "replace":
        if idx is None:
            raise Refused("NotebookEdit replace needs a cell_id")
        cell = cells[idx]
        cell["source"] = source
        if cell_type in ("code", "markdown"):
            cell["cell_type"] = cell_type
        if cell.get("cell_type") == "code":
            cell["outputs"], cell["execution_count"] = [], None
    elif mode == "insert":
        if cell_type not in ("code", "markdown"):
            raise Refused("NotebookEdit insert needs cell_type code or markdown")
        new: dict[str, Any] = {"cell_type": cell_type, "metadata": {}}
        if any(isinstance(c, dict) and "id" in c for c in cells):
            new["id"] = secrets.token_hex(4)
        new["source"] = source
        if cell_type == "code":
            new["outputs"], new["execution_count"] = [], None
        cells.insert(0 if idx is None else idx + 1, new)
    elif mode == "delete":
        if idx is None:
            raise Refused("NotebookEdit delete needs a cell_id")
        del cells[idx]
    else:
        raise Refused(f"unknown NotebookEdit edit_mode {mode!r}")
    return _eol(text, json.dumps(nb, indent=1, ensure_ascii=False) + "\n")


def _edited(text: str | None, p: Proposal) -> str:
    """New file text for an edit proposal (text None: the repo file is absent)."""
    ch = p.change
    if ch.tool == "Write":
        if not isinstance(ch.content, str):
            raise Refused("Write change has no content")
        return ch.content if text is None else _eol(text, ch.content)
    if text is None:
        raise Refused(f"{ch.tool} needs an existing repo file")
    if ch.tool == "Edit":
        replace_all = ch.extra.get("replace_all") is True
        return _replace(text, ch.old_string, ch.new_string, replace_all, "Edit")
    if ch.tool == "MultiEdit":
        edits = ch.extra.get("edits")
        if not isinstance(edits, list) or not edits:
            raise Refused("MultiEdit change has no edits")
        for i, e in enumerate(edits, 1):
            if not isinstance(e, dict):
                raise Refused(f"edit {i}: not an object")
            text = _replace(
                text,
                e.get("old_string"),
                e.get("new_string"),
                e.get("replace_all") is True,
                f"edit {i}",
            )
        return text
    return _notebook(text, ch)


def _with_lesson(text: str, p: Proposal) -> str:
    """text plus a blank line and the lesson, in the doc's line endings."""
    content = p.change.content
    if not isinstance(content, str) or not content.strip():
        raise Refused("lesson proposal has no content")
    eol = "\r\n" if "\r\n" in text else "\n"
    body = content.strip().replace("\r\n", "\n").replace("\n", eol)
    if text and not text.endswith("\n"):
        text += eol
    return text + (eol if text.strip() else "") + body + eol


def apply(
    pid: str, repo: str | None = None, doc: str | None = None, dry_run: bool = False
) -> str:
    """Apply a pending proposal to the harness repo file; returns the report text.

    Writes only the repo file and the proposal's own status; never commits.
    """
    path, raw, p = _load(pid)
    if p.status != "pending":
        raise Refused(f"{pid} is {p.status}, not pending")
    manifest = _manifest()
    root = _repo_root(repo, manifest, p)
    old: str | None
    if p.kind == "lesson":
        if not doc:
            raise Refused(
                "lesson proposals need --doc <repo doc> (the skill asks which)"
            )
        doc_path = Path(doc)
        target = _checked(
            root, doc_path if doc_path.is_absolute() else root / doc_path, "--doc"
        )
        if not target.is_file():
            raise Refused(f"--doc {target} does not exist")
        old = _read(target)
        new = _with_lesson(old, p)
    else:
        if p.change.tool not in FILE_TOOLS:
            raise Refused(
                f"{p.change.tool or 'unknown'} proposals carry a shell command; apply it"
                " by hand in the repo (show prints it), then reject the proposal"
            )
        target = _repo_file(root, _repo_rel(p, manifest))
        old = _read(target) if target.exists() else None
        new = _edited(old, p)
    rel = target.relative_to(root).as_posix()
    if dry_run:
        diff = difflib.unified_diff(
            (old or "").splitlines(),
            new.splitlines(),
            f"a/{rel}",
            f"b/{rel}",
            lineterm="",
        )
        return "\n".join([f"dry run: {pid} -> {target}", *diff])
    _write(target, new)
    raw.update(status="applied", applied_at=model.now_iso(), applied_to=rel)
    _save_raw(path, raw)
    return "\n".join(
        (
            f"applied {pid} ({p.kind} {p.change.tool or '-'}) to {target}",
            f"repo: {root}",
            f"file: {rel}",
            (
                "not committed. Run the tests for this file, commit it on approval"
                f" (git -C <repo> add -- {rel}), then run {SYNC_HINT} from the repo."
            ),
        )
    )


def reject(pid: str) -> str:
    """Set status rejected in place; the file stays (lesson dedupe reads it)."""
    path, raw, p = _load(pid)
    if p.status == "rejected":
        raise Refused(f"{pid} is already rejected")
    raw.update(status="rejected", rejected_at=model.now_iso())
    _save_raw(path, raw)
    return f"rejected {pid} (status set in place: {path})"


# --- lessons / dashboard ---


def _no_bloks(home: Path) -> str | None:
    return None


def propose(
    source: str = "memory", max_n: int = LESSONS_MAX, dry_run: bool = False
) -> str:
    """Wrap lessons.propose_lessons: filter by source, cap per run, then save."""
    docs = []
    repo = _manifest().get("repo")
    if isinstance(repo, str) and repo and (Path(repo) / "CLAUDE.md").is_file():
        docs.append(Path(repo) / "CLAUDE.md")
    found = lessons.propose_lessons(
        run_bloks=_no_bloks if source == "memory" else None, docs=docs, dry_run=True
    )
    if source != "all":
        found = [p for p in found if p.extra.get("origin") == source]
    picked = found[:max_n]
    if not dry_run:
        for p in picked:
            model.save_proposal(p)
    more = len(found) - len(picked)
    head = f"{'would propose' if dry_run else 'proposed'} {len(picked)} lesson(s)"
    lines = [
        f"{head} from {source}" + (f" ({more} more on later runs)" if more else "")
    ]
    lines += [
        f"  {p.id}  {p.extra.get('origin')}  {_clip(p.reason, 80)}" for p in picked
    ]
    return "\n".join(lines)


def _load_dashboard() -> ModuleType | None:
    """Sibling dashboard module, None when it is not installed."""
    package = __package__ or "fleet"
    try:
        return importlib.import_module(".dashboard", package)
    except ModuleNotFoundError as exc:
        if exc.name != f"{package}.dashboard":
            raise
        return None


def dashboard(out: str | None, fresh: bool) -> int:
    """Write the HTML dashboard (default ~/.claude/fleet/dashboard.html)."""
    module = _load_dashboard()
    if module is None or not hasattr(module, "write_page"):
        print(
            "dashboard unavailable: tools/fleet/dashboard.py is missing from this"
            " install (sync the harness repo)",
            file=sys.stderr,
        )
        return 2
    target = out or str(model.fleet_dir() / "dashboard.html")
    written = module.write_page(_state(fresh), target)
    print(f"wrote {written or target}")
    return 0


def _positive(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return n


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fleet.py", description=(__doc__ or "").split("\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="build ~/.claude/fleet/state.json")
    for name in ("report", "json"):
        p = sub.add_parser(name, help=f"{name} from state.json")
        p.add_argument("--fresh", action="store_true", help="collect first")
    p = sub.add_parser("inbox", help="list harness-inbox proposals")
    p.add_argument("--all", action="store_true", help="every status, not just pending")
    for name, text in (("show", "one proposal in full"), ("reject", "mark rejected")):
        sub.add_parser(name, help=text).add_argument("id")
    p = sub.add_parser("apply", help="apply a proposal to the harness repo file")
    p.add_argument("id")
    p.add_argument("--repo", help="harness repo root (default: manifest repo)")
    p.add_argument("--doc", help="lesson proposals: repo doc to append to")
    p.add_argument("--dry-run", action="store_true", help="print the diff only")
    p = sub.add_parser("lessons", help="propose cross-project lessons")
    p.add_argument("--source", choices=LESSON_SOURCES, default="memory")
    p.add_argument("--max", type=_positive, default=LESSONS_MAX, dest="max_n")
    p.add_argument("--dry-run", action="store_true", help="write nothing")
    p = sub.add_parser("dashboard", help="write the HTML dashboard")
    p.add_argument("out", nargs="?", help="default ~/.claude/fleet/dashboard.html")
    p.add_argument("--fresh", action="store_true", help="collect first")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the exit code."""
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")
    args = _parser().parse_args(argv)
    try:
        if args.command == "collect":
            state = collect.collect()
            model.save_state(state)
            print(summary(state))
        elif args.command == "report":
            print(report(_state(args.fresh)))
        elif args.command == "json":
            print(_state(args.fresh).to_json())
        elif args.command == "inbox":
            print(inbox(args.all))
        elif args.command == "show":
            print(show(args.id))
        elif args.command == "apply":
            print(apply(args.id, args.repo, args.doc, args.dry_run))
        elif args.command == "reject":
            print(reject(args.id))
        elif args.command == "lessons":
            print(propose(args.source, args.max_n, args.dry_run))
        else:
            return dashboard(args.out, args.fresh)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
