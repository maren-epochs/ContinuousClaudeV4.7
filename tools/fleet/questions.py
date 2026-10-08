"""Fleet question queue: ~/.claude/fleet/questions/<session_id>.json, one file per session.

Written by the ask-queue.mjs hook while away mode is on (flag file ~/.claude/fleet/away):
every AskUserQuestion is queued instead of opening a box, so the asking session keeps
reading messages. Without the flag boxes open as usual. Read and answered here. A question
is answered once, in the asking session or from /fleet questions, whichever comes first.
Shape: tools/fleet/schema.md.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import model

QUESTION_ID = re.compile(r"q-[0-9a-f]{8}")
RELAY_MARK = "[fleet {id}]"
VIA = ("session", "fleet")


class QuestionError(Exception):
    """The queue refuses the request; the message says why."""


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
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        return None
    return data


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
        queue = _read(path)
        if queue is None:
            continue
        for raw in queue["questions"]:
            if not isinstance(raw, dict) or raw.get("id") != qid:
                continue
            if raw.get("status") == "answered":
                raise QuestionError(
                    f"{qid} already answered via {raw.get('answered_via')}: {raw.get('answer')}"
                )
            raw["status"] = "answered"
            raw["answer"] = text.strip()
            raw["answered_at"] = _dt.datetime.now(_dt.UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            raw["answered_via"] = via
            model.write_atomic(path, json.dumps(queue, indent=2))
            q = _question(raw, queue)
            assert q is not None
            return q
    raise QuestionError(f"no question {qid} in {folder}")


def relay_text(q: Question) -> str:
    """Question text for the /fleet questions box; the marker lets ask-queue allow it."""
    return f"{RELAY_MARK.format(id=q.id)} {q.project or '?'}: {q.question}"
