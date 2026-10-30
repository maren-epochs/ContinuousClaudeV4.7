"""Fleet question queue: ~/.claude/fleet/questions/<session_id>.json, one file per session.

Written by the ask-queue.mjs hook while away mode is on (flag file ~/.claude/fleet/away):
every AskUserQuestion is queued instead of opening a box, so the asking session keeps
reading messages. Without the flag boxes open as usual. Read and answered here. A question
is answered once, in the asking session or from /fleet questions, whichever comes first.
Shape: tools/fleet/schema.md.

Writers (this module and ask-queue.mjs) serialize on one lock: exclusive create of
``<queue>.json.lock``, broken when older than LOCK_STALE_S (same value in the hook).
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import model

QUESTION_ID = re.compile(r"q-[0-9a-f]{8}")
RELAY_MARK = "[fleet {id}]"
VIA = ("session", "fleet")
LOCK_WAIT_S = 5.0
LOCK_STALE_S = 10.0  # must match LOCK_STALE_MS in .claude/hooks/ask-queue.mjs
_POLL_S = 0.025


class QuestionError(Exception):
    """The queue refuses the request; the message says why."""


class QueueBusy(QuestionError):
    """Transient: the lock stayed held or the file was unreadable; retry."""


@dataclass
class Question:
    """One queued question with the session it came from."""

    id: str
    session_id: str
    project: str | None
    cwd: str | None
    header: str
    question: str
    options: list[dict[str, str]] = field(default_factory=list)
    multi_select: bool = False
    created_at: str | None = None
    status: str = "pending"
    answer: str | None = None
    answered_at: str | None = None
    answered_via: str | None = None


def away_path() -> Path:
    """``~/.claude/fleet/away``: present = away mode on."""
    return model.fleet_dir() / "away"


def is_away() -> bool:
    """Whether away mode is on."""
    return away_path().is_file()


def set_away(on: bool) -> None:
    """Turn away mode on (create the flag file) or off (remove it)."""
    path = away_path()
    if on:
        stamp = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        model.write_atomic(path, stamp + "\n")
    else:
        path.unlink(missing_ok=True)


def questions_dir() -> Path:
    """``~/.claude/fleet/questions``."""
    return model.fleet_dir() / "questions"


def _read(path: Path) -> dict[str, Any] | None:
    data = model.read_json_dict(path)
    if data is None or not isinstance(data.get("questions"), list):
        return None
    return data


def lock_path(path: Path) -> Path:
    """``<queue>.json.lock``: the writer lock shared with ask-queue.mjs."""
    return path.with_name(path.name + ".lock")


@contextlib.contextmanager
def _locked(path: Path, wait: float | None = None) -> Iterator[None]:
    """Hold the queue's writer lock; QuestionError when another writer keeps it past wait."""
    lock = lock_path(path)
    deadline = time.monotonic() + (LOCK_WAIT_S if wait is None else wait)
    while True:
        try:
            # "x": create exclusively (O_CREAT | O_EXCL); fails when another writer holds it
            with open(lock, "x", encoding="utf-8") as fh:
                stamp = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                fh.write(f"{os.getpid()} {stamp}\n")
            break
        except (FileExistsError, PermissionError):
            pass  # PermissionError: Windows reports a lock file being deleted this way
        try:
            if time.time() - lock.stat().st_mtime > LOCK_STALE_S:
                lock.unlink(missing_ok=True)  # holder died; a hold lasts milliseconds
                continue
        except FileNotFoundError:
            continue
        except PermissionError:
            pass
        if time.monotonic() >= deadline:
            raise QueueBusy(f"queue busy (another writer holds {lock}); try again")
        time.sleep(_POLL_S)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _question(raw: dict[str, Any], queue: dict[str, Any]) -> Question | None:
    qid = raw.get("id")
    if not isinstance(qid, str) or not QUESTION_ID.fullmatch(qid):
        return None
    options = [
        {"label": _text(o.get("label")), "description": _text(o.get("description"))}
        for o in raw.get("options") or []
        if isinstance(o, dict)
    ]
    return Question(
        id=qid,
        session_id=_text(queue.get("session_id")),
        project=queue.get("project") if isinstance(queue.get("project"), str) else None,
        cwd=queue.get("cwd") if isinstance(queue.get("cwd"), str) else None,
        header=_text(raw.get("header")),
        question=_text(raw.get("question")),
        options=options,
        multi_select=raw.get("multi_select") is True,
        created_at=raw.get("created_at")
        if isinstance(raw.get("created_at"), str)
        else None,
        status=_text(raw.get("status")) or "pending",
        answer=raw.get("answer") if isinstance(raw.get("answer"), str) else None,
        answered_at=raw.get("answered_at")
        if isinstance(raw.get("answered_at"), str)
        else None,
        answered_via=raw.get("answered_via")
        if isinstance(raw.get("answered_via"), str)
        else None,
    )


def load(
    include_answered: bool = False, session_id: str | None = None
) -> list[Question]:
    """Queued questions, oldest first; pending only unless include_answered."""
    folder = questions_dir()
    found: list[Question] = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        queue = _read(path)
        if queue is None:
            continue
        if session_id and queue.get("session_id") != session_id:
            continue
        for raw in queue["questions"]:
            q = _question(raw, queue) if isinstance(raw, dict) else None
            if q and (include_answered or q.status == "pending"):
                found.append(q)
    return sorted(found, key=lambda q: q.created_at or "")


def answer(qid: str, text: str, via: str) -> Question:
    """Record the answer to one pending question; refuses unknown or answered ones."""
    if not QUESTION_ID.fullmatch(qid):
        raise QuestionError(f"not a question id: {qid!r}")
    if via not in VIA:
        raise QuestionError(f"via must be one of {', '.join(VIA)}")
    if not text.strip():
        raise QuestionError("empty answer")
    folder = questions_dir()
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        found = _read(path)
        if found is None or not any(
            isinstance(raw, dict) and raw.get("id") == qid for raw in found["questions"]
        ):
            continue
        with _locked(path):
            return _answer_locked(path, qid, text, via)
    raise QuestionError(f"no question {qid} in {folder}")


def _answer_locked(path: Path, qid: str, text: str, via: str) -> Question:
    """Re-read under the lock so a concurrent ask-queue append is kept, then write."""
    queue = _read(path)
    if queue is None:
        raise QueueBusy(f"could not read {path}; nothing written, try again")
    for raw in queue["questions"]:
        if not isinstance(raw, dict) or raw.get("id") != qid:
            continue
        if raw.get("status") == "answered":
            raise QuestionError(
                f"{qid} already answered via {raw.get('answered_via')}: {raw.get('answer')}"
            )
        raw["status"] = "answered"
        raw["answer"] = text.strip()
        raw["answered_at"] = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw["answered_via"] = via
        model.write_atomic(path, json.dumps(queue, indent=2))
        q = _question(raw, queue)
        assert q is not None
        return q
    raise QuestionError(f"no question {qid} in {path}")


def relay_text(q: Question) -> str:
    """Question text for the /fleet questions box; the marker lets ask-queue allow it."""
    return f"{RELAY_MARK.format(id=q.id)} {q.project or '?'}: {q.question}"
