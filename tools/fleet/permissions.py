"""Permission denials across every project, and the standing-grant queue.

``denials`` scans ``~/.claude/projects/**/*.jsonl`` for tool calls the permission system
or the auto-mode classifier refused (the user's own rejected question boxes are choices,
not denials, and are left out), grouped per project and (tool, reason).

The grant queue ``~/.claude/fleet/grants.json`` collects narrow standing authorizations
Claude proposes after a denial: ``allow`` rules for ``permissions.allow`` and ``note``
lines for ``autoMode.environment`` in ``~/.claude/settings.json``. Claude adds to the
queue; the user runs ``grant apply`` (one line per batch) because Claude cannot write
settings.json. Blanket rules, self-escalation (settings.json, ``fleet.py grant``) and
destructive git are refused at ``add`` and again at ``apply``. Shape: schema.md.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import os
import re
import secrets
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import lessons, model, questions

GRANT_ID = re.compile(r"g-[0-9a-f]{8}")
KINDS = ("allow", "note", "revoke")
APPLY_HINT = "py -3.13 ~/.claude/tools/fleet/fleet.py grant apply"
NOTE_PREFIX = "**Standing authorization (granted {date})**: "
CMD_SHOWN = 110
GROUPS_SHOWN = 8

_DENIED = re.compile(
    r"denied by the Claude Code auto mode classifier"
    r"|Permission to use \S+ (?:has been|was) denied"
    r"|blocked by deny rule"
)
_REASON = re.compile(r"Reason: (\[[^\]]{1,60}\])")
_RULE = re.compile(r"(?P<tool>[A-Za-z][\w-]*)\((?P<spec>.+)\)|mcp__[\w-]+")
_INTERPRETERS = frozenset(
    {
        *("py", "python", "python3", "pythonw", "node", "npx", "uv", "uvx"),
        *("bash", "sh", "zsh", "pwsh", "powershell", "cmd", "env", "sudo", "xargs"),
        *("eval", "iex", "invoke-expression", "start-process"),
    }
)
_INLINE_CODE = frozenset({"-c", "-e", "-m", "-command", "-encodedcommand", "/c", "/k"})
_SCRIPT = re.compile(r"[\\/]|\.(?:py|mjs|cjs|js|ps1|sh)$", re.IGNORECASE)
_ESCALATION = re.compile(
    r"(?<![\w.-])settings\.json|fleet\.py[\"']?\s+grant|grant_standing_auth"
    r"|--force|\bpush\s+-f\b|reset\s+--hard|filter-(?:repo|branch)|\brm\s+-\w*r",
    re.IGNORECASE,
)
# A wildcard rule is refused when its pattern matches any of these (what a narrow grant
# must never reach: Claude's own settings/instructions/hooks, the grant queue, destructive git).
_SENSITIVE_PATHS = tuple(
    prefix + rel
    for rel in (
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/CLAUDE.md",
        ".claude/hooks/x.mjs",
        ".claude/tools/fleet/fleet.py",
        ".claude/fleet/grants.json",
        ".claude/projects/p/memory/m.md",
        ".claude/skills/s/SKILL.md",
        ".claude/agents/a.md",
    )
    for prefix in ("~/", "")
)
_SENSITIVE_COMMANDS = (
    "py -3.13 ~/.claude/tools/fleet/fleet.py grant apply",
    "py -3.13 tools/fleet/fleet.py grant apply",
    "git push --force",
    "git push -f origin main",
    "git push origin --force main",
    "git push origin +main",
    "git reset --hard",
    "git filter-repo",
    "rm -rf ~",
    "cp x ~/.claude/settings.json",
    "Set-Content ~/.claude/settings.json x",
)
_FILE_TOOLS = frozenset({"edit", "write", "multiedit", "notebookedit"})
_SECRET = re.compile(
    r"(?i)((?:key|token|secret|password|passwd|client_id|code_challenge|auth)\w*[=:]\s*"
    r"(?:bearer\s+|basic\s+)?)\S+"
    r"|\b[A-Za-z0-9_\-]{32,}\b"
)


class GrantError(Exception):
    """The grant queue refuses the request; the message says why."""


# --- denials ---


@dataclasses.dataclass
class Denial:
    """One refused tool call; command is raw (mask before showing)."""

    project: str
    session: str
    date: str
    tool: str
    reason: str
    command: str


def project_name(slug: str) -> str:
    """``C--Users-x-Documents-acorn`` -> ``acorn``; other slugs unchanged."""
    _, sep, tail = slug.partition("-Documents-")
    return tail if sep and tail else slug


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _brief(inp: Any) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in ("command", "file_path", "url", "cron", "description"):
        if key in inp:
            return str(inp[key])
    return json.dumps(inp)[:200]


def _reason(text: str) -> str:
    m = _REASON.search(text)
    if m:
        return m.group(1)
    if "blocked by deny rule" in text:
        return "[deny rule]"
    if "Permission to use" in text:
        return "[permission prompt]"
    return "[classifier]"


def _scan_file(path: Path, project: str, cutoff: str) -> list[Denial]:
    uses: dict[str, tuple[str, str]] = {}
    out: list[Denial] = []
    seen: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        if '"tool_use"' not in line and '"tool_result"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        stamp = str(entry.get("timestamp", ""))
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use":
                uses[str(c.get("id"))] = (str(c.get("name")), _brief(c.get("input")))
                continue
            if c.get("type") != "tool_result" or not c.get("is_error"):
                continue
            text = _text(c.get("content"))
            use_id = str(c.get("tool_use_id"))
            if not _DENIED.search(text) or use_id in seen or stamp[:10] < cutoff:
                continue
            seen.add(use_id)
            tool, command = uses.get(use_id, ("?", ""))
            out.append(
                Denial(project, path.stem[:8], stamp[:10], tool, _reason(text), command)
            )
    return out


def scan(
    project: str | None = None,
    since_days: float | None = None,
    projects_dir: Path | None = None,
    now: _dt.datetime | None = None,
) -> list[Denial]:
    """Denials in every transcript (subagents included), oldest first."""
    root = projects_dir or model.claude_dir() / "projects"
    now = now or _dt.datetime.now(_dt.UTC)
    cutoff, min_mtime = "", 0.0
    if since_days is not None:
        start = now - _dt.timedelta(days=since_days)
        cutoff, min_mtime = start.strftime("%Y-%m-%d"), start.timestamp() - 86400
    want = project.lower() if project else None
    rows: list[Denial] = []
    if not root.is_dir():
        return rows
    for slug_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        name = project_name(slug_dir.name)
        if want and want not in (name.lower(), slug_dir.name.lower()):
            continue
        for path in slug_dir.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime < min_mtime:
                    continue
            except OSError:
                continue
            rows.extend(_scan_file(path, name, cutoff))
    rows.sort(key=lambda d: (d.date, d.project))
    return rows


def _shown(command: str, guard: Any) -> str:
    text = " ".join(command.split())
    text = _SECRET.sub(lambda m: (m.group(1) or "") + lessons.MASK, text)
    text = lessons.mask(text, guard)
    return text if len(text) <= CMD_SHOWN else text[: CMD_SHOWN - 3] + "..."


def denials_report(rows: list[Denial], as_json: bool = False) -> str:
    """Grouped text (or masked JSON) of rows."""
    guard = lessons.default_guard(model.home_dir())
    if as_json:
        items = [
            {**dataclasses.asdict(d), "command": _shown(d.command, guard)} for d in rows
        ]
        return json.dumps(items, indent=2)
    if not rows:
        return "no denials found"
    by_project: dict[str, list[Denial]] = defaultdict(list)
    for d in rows:
        by_project[d.project].append(d)
    out = [f"{len(rows)} denials across {len(by_project)} project(s)"]
    for name in sorted(by_project, key=lambda n: -len(by_project[n])):
        groups: dict[tuple[str, str], list[Denial]] = defaultdict(list)
        for d in by_project[name]:
            groups[(d.tool, d.reason)].append(d)
        out.append(f"\n{name} ({len(by_project[name])})")
        ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))
        for (tool, reason), ds in ranked[:GROUPS_SHOWN]:
            last = ds[-1]
            out.append(
                f"  {len(ds)}x {tool} {reason} last {last.date or '?'}: "
                f"{_shown(last.command, guard)}"
            )
        if len(ranked) > GROUPS_SHOWN:
            out.append(f"  ... {len(ranked) - GROUPS_SHOWN} more group(s) (--json)")
    out.append(
        "\nFor each recurring, low-risk one: fleet.py grant add '<exact rule>' "
        "--reason '...' (credentials, live money, production data stay with the user)."
    )
    return "\n".join(out)


# --- grant queue ---


def grants_path() -> Path:
    """``~/.claude/fleet/grants.json``."""
    return model.fleet_dir() / "grants.json"


def settings_path() -> Path:
    """``~/.claude/settings.json``."""
    return model.claude_dir() / "settings.json"


def _open_interpreter(words: list[str]) -> bool:
    """An interpreter/shell rule whose wildcard (or inline code) is not behind a script."""
    exe = re.split(r"[\\/]", words[0].lower())[-1] if words else ""
    if exe.removesuffix(".exe") not in _INTERPRETERS:
        return False
    for word in words[1:]:
        if word.lower() in _INLINE_CODE:
            return True
        if "*" in word:
            return True
        if _SCRIPT.search(word):
            return False
    return False


def _glob_regex(spec: str) -> re.Pattern[str]:
    """gitignore-style path glob (``**``, ``*``, ``?``) as an anchored regex."""
    out, i = [], 0
    while i < len(spec):
        if spec.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif spec.startswith("**", i):
            out.append(".*")
            i += 2
        elif spec[i] == "*":
            out.append("[^/]*")
            i += 1
        elif spec[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(spec[i]))
            i += 1
    return re.compile("".join(out), re.IGNORECASE)


def _command_regex(spec: str) -> re.Pattern[str]:
    """Shell rule pattern (``*`` anywhere, ``:*`` prefix form) as an anchored regex."""
    if spec.endswith(":*"):
        spec = spec[:-2] + "*"
    return re.compile(".*".join(re.escape(p) for p in spec.split("*")), re.IGNORECASE)


def _reaches_sensitive(tool: str, spec: str) -> str | None:
    """The first sensitive path or command the rule's pattern matches, else None."""
    if "*" not in spec and "?" not in spec:
        return None
    tool = tool.lower()
    if tool in _FILE_TOOLS:
        rx = _glob_regex(spec.replace("\\", "/"))
        return next((p for p in _SENSITIVE_PATHS if rx.fullmatch(p)), None)
    if tool in ("bash", "powershell"):
        rx = _command_regex(spec)
        return next((c for c in _SENSITIVE_COMMANDS if rx.fullmatch(c)), None)
    return None


def check_rule(rule: str) -> str:
    """The stripped rule; GrantError for a malformed, blanket or self-escalating one."""
    rule = rule.strip()
    m = _RULE.fullmatch(rule)
    if not m:
        raise GrantError(f"not a permission rule (Tool(spec) or mcp__name): {rule!r}")
    spec = (m.group("spec") or "").strip()
    shell = (m.group("tool") or "").lower() in ("bash", "powershell")
    if m.group("spec") is not None and (
        not spec.strip("*:/\\. ") or (shell and spec.startswith("*"))
    ):
        raise GrantError(
            f"blanket rule refused: {rule!r}; name the exact command or path"
        )
    if _open_interpreter(spec.split()):
        raise GrantError(
            f"interpreter rule refused: {rule!r}; a wildcard must follow a script path"
        )
    hit = _reaches_sensitive(m.group("tool") or "", spec)
    if _ESCALATION.search(rule) or hit:
        raise GrantError(
            f"refused: {rule!r} reaches settings.json, the grant queue or destructive git"
            + (f" (matches {hit!r})" if hit else "")
        )
    return rule


def check_note(note: str) -> str:
    """The one-line note; GrantError when empty or self-escalating."""
    note = " ".join(note.split())
    if not note:
        raise GrantError("empty note")
    if _ESCALATION.search(note):
        raise GrantError(
            "refused: the note reaches settings.json, the grant queue or destructive git"
        )
    return note


def _load() -> dict[str, Any]:
    path = grants_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"grants": []}
    except (OSError, ValueError) as exc:
        raise GrantError(f"unreadable grant queue {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("grants"), list):
        raise GrantError(f"malformed grant queue {path}")
    return data


def _save(data: dict[str, Any]) -> None:
    model.write_atomic(grants_path(), json.dumps(data, indent=2) + "\n")


def _pending(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [g for g in data["grants"] if g.get("status") == "pending"]


def check_revoke(rule: str) -> str:
    """A rule to remove: any well-formed rule (removing only narrows)."""
    rule = rule.strip()
    if not _RULE.fullmatch(rule):
        raise GrantError(f"not a permission rule (Tool(spec) or mcp__name): {rule!r}")
    return rule


def add(
    rule: str | None,
    note: str | None,
    reason: str,
    project: str | None = None,
    revoke: str | None = None,
) -> str:
    """Queue one grant; the confirmation line. An identical pending grant is reused."""
    options = (("allow", rule), ("note", note), ("revoke", revoke))
    given = [(k, v) for k, v in options if v is not None]
    if len(given) != 1:
        raise GrantError("give exactly one of a rule, --note or --revoke")
    kind, raw = given[0]
    check = {"allow": check_rule, "note": check_note, "revoke": check_revoke}[kind]
    value = check(raw)
    reason = " ".join(reason.split())
    if not reason:
        raise GrantError(
            "--reason is required (what was denied and why it is low-risk)"
        )
    path = grants_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with questions._locked(path):
            data = _load()
            for g in _pending(data):
                if g.get("kind") == kind and g.get("value") == value:
                    return f"already queued {g['id']}: {value}\napply: {APPLY_HINT}"
            gid = f"g-{secrets.token_hex(4)}"
            data["grants"].append(
                {
                    "id": gid,
                    "kind": kind,
                    "value": value,
                    "reason": reason,
                    "project": project or project_name(Path.cwd().name),
                    "created_at": model.now_iso(),
                    "status": "pending",
                    "applied_at": None,
                }
            )
            _save(data)
    except questions.QuestionError as exc:
        raise GrantError(str(exc)) from exc
    n = len(_pending(data))
    return f"queued {gid} ({n} pending): {value}\napply: {APPLY_HINT}"


def pending_count() -> int:
    """Pending grants on disk; 0 when the queue is absent or unreadable."""
    try:
        return len(_pending(_load()))
    except GrantError:
        return 0


def list_grants(show_all: bool = False) -> str:
    """Pending (or every) grant, one block each, plus the apply line."""
    data = _load()
    rows = data["grants"] if show_all else _pending(data)
    if not rows:
        return "no pending grants" if not show_all else "no grants"
    out = []
    for g in rows:
        out.append(
            f"{g.get('id')}  {g.get('status'):<8} {g.get('kind'):<5} "
            f"{g.get('project', '')}: {g.get('value')}\n    why: {g.get('reason', '')}"
        )
    if _pending(data):
        out.append(f"apply: {APPLY_HINT}")
    return "\n".join(out)


def drop(gid: str) -> str:
    """Mark a pending grant dropped."""
    if not GRANT_ID.fullmatch(gid):
        raise GrantError(f"bad grant id {gid!r}")
    try:
        with questions._locked(grants_path()):
            data = _load()
            for g in data["grants"]:
                if g.get("id") == gid and g.get("status") == "pending":
                    g["status"] = "dropped"
                    _save(data)
                    return f"dropped {gid}"
    except questions.QuestionError as exc:
        raise GrantError(str(exc)) from exc
    raise GrantError(f"no pending grant {gid}")


def merge(settings: dict[str, Any], grants: list[dict[str, Any]], date: str) -> int:
    """Apply grants to a settings dict in place; the number of entries changed."""
    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    env = settings.setdefault("autoMode", {}).setdefault("environment", [])
    added = 0
    for g in grants:
        if g["kind"] == "revoke":
            rule = check_revoke(g["value"])
            if rule in allow:
                allow.remove(rule)
                added += 1
        elif g["kind"] == "allow":
            rule = check_rule(g["value"])
            if rule not in allow:
                allow.append(rule)
                added += 1
        else:
            line = NOTE_PREFIX.format(date=date) + check_note(g["value"])
            if not any(e.endswith(check_note(g["value"])) for e in env):
                env.append(line)
                added += 1
    return added


def apply(dry_run: bool = False) -> str:
    """Write every pending grant into settings.json (backup first); run by the user."""
    path = settings_path()
    try:
        raw = path.read_bytes().decode("utf-8")
        settings = json.loads(raw)
    except FileNotFoundError as exc:
        raise GrantError(f"{path} not found") from exc
    except (OSError, ValueError) as exc:
        raise GrantError(f"unreadable {path}: {exc}") from exc
    try:
        with questions._locked(grants_path()):
            data = _load()
            pending = [g for g in _pending(data) if g.get("kind") in KINDS]
            if not pending:
                return "no pending grants"
            today = _dt.datetime.now(model.harness_tz()).strftime("%Y-%m-%d")
            added = merge(settings, pending, today)
            lines = [f"{g['kind']}: {g['value']}" for g in pending]
            if dry_run:
                return "would add:\n  " + "\n  ".join(lines)
            if added:
                stamp = _dt.datetime.now(model.harness_tz()).strftime("%Y%m%d-%H%M%S")
                backup = path.with_name(f"settings.json.bak-{stamp}-grants")
                shutil.copy2(path, backup)
                eol = "\r\n" if "\r\n" in raw else "\n"
                text = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
                tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
                tmp.write_bytes(text.replace("\n", eol).encode("utf-8"))
                os.replace(tmp, path)
            for g in pending:
                g["status"], g["applied_at"] = "applied", model.now_iso()
            _save(data)
    except questions.QuestionError as exc:
        raise GrantError(str(exc)) from exc
    head = f"applied {len(pending)} grant(s), {added} changed in {path.name}"
    tail = f"; backup {backup.name}" if added else ""
    return head + tail + ":\n  " + "\n  ".join(lines)
