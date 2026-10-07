"""Lesson sharing: propose cross-project lessons into the harness inbox.

Sources: ``~/.claude/projects/*/memory/*.md`` with frontmatter ``metadata.type`` (or a
top-level ``type``) of ``feedback`` or ``reference``, and the rule/taste cards of
``bloks context`` (skipped when bloks is absent or fails). A candidate becomes a
``Proposal(kind="lesson")`` unless it is project-specific (``scope: project``, names
its project, or references a file that exists in the project's cwd; ``scope: global``
overrides the heuristics; bloks ``project_cards`` are always project-specific) or is
already present in ``~/.claude/CLAUDE.md`` / installed harness docs.

Dedupe: ``content_hash`` (sha256 of the normalized lesson text) is stored in the
proposal's ``extra``; any inbox file carrying that hash, in any status and any subdir,
blocks a new proposal, so a rejected lesson is never proposed again. Private terms are
masked with ``tools/privacy_guard.py`` (env, ``~/.claude/privacy-terms``, per-clone
terms file, home paths, usernames, session ids). The per-clone file is resolved from
the install manifest's ``repo`` (else the repo holding this file), never the cwd.
Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import model

# privacy_guard is a top-level script module in tools/ (siblings import it by name).
_TOOLS_DIR = str(Path(__file__).resolve().parent.parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
import privacy_guard

MASK = "<redacted>"
MEMORY_TYPES = ("feedback", "reference")
BLOKS_KINDS = ("rules", "tastes")
BLOKS_TIMEOUT = 30
SHINGLE = 4
PRESENT_RATIO = 0.6
TRANSCRIPT_PROBE_FILES = 3
TRANSCRIPT_PROBE_LINES = 50
TRANSCRIPT_PROBE_BYTES = 256 * 1024
GIT_TIMEOUT = 5
HARNESS_DIR: Path | None = Path(__file__).resolve().parent
_PATH_TOKEN = re.compile(r"(?<![\w/~.:-])[\w.-]+(?:/[\w.-]+)+")

BloksRunner = Callable[[Path], str | None]


@dataclass
class Candidate:
    """One lesson candidate before filtering."""

    origin: str  # memory | bloks
    label: str  # feedback | reference | rule | taste
    title: str
    content: str
    project: str
    specific: bool = False


def normalize(text: str) -> str:
    """Lowercase alphanumeric words joined by single spaces."""
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def content_hash(text: str) -> str:
    """Sha256 hex of the normalized text (whitespace, case, punctuation ignored)."""
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def _git_terms_path(repo: Path) -> Path | None:
    if not repo.is_dir():
        return None
    out = model.run_git(
        str(repo), "rev-parse", "--git-path", "info/privacy-terms", timeout=GIT_TIMEOUT
    )
    if not out:
        return None
    path = Path(out)
    return path if path.is_absolute() else repo / path


def clone_terms_file(claude: Path) -> Path | None:
    """Harness clone's ``.git/info/privacy-terms``: manifest ``repo``, else HARNESS_DIR."""
    try:
        data = json.loads((claude / ".ccv47-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    repo = data.get("repo") if isinstance(data, dict) else None
    candidates = [Path(repo)] if isinstance(repo, str) and repo else []
    if HARNESS_DIR is not None:
        candidates.append(HARNESS_DIR)
    for candidate in candidates:
        found = _git_terms_path(candidate)
        if found is not None:
            return found
    return None


def default_guard(home: Path) -> privacy_guard.Guard:
    """Privacy guard over every privacy_guard term source plus the home dir name."""
    terms = privacy_guard.load_terms(clone_terms_file(home / ".claude"))
    users = privacy_guard.detect_usernames()
    if home.name.lower() not in privacy_guard.SERVICE_ACCOUNTS:
        users.append(home.name)
    return privacy_guard.Guard(users, terms, [])


def mask(text: str, guard: privacy_guard.Guard) -> str:
    """Replace every privacy-guard hit in text with MASK, line by line."""
    out = []
    for line in text.split("\n"):
        hits = sorted({h.text for h in guard.scan(line)}, key=len, reverse=True)
        for hit in hits:
            line = line.replace(hit, MASK)
        out.append(line)
    return "\n".join(out)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """(metadata, body) of a ``---`` frontmatter block: flat keys + one nesting level."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text.strip()
    meta: dict[str, Any] = {}
    parent: dict[str, str] | None = None
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition(":")
        if not sep:
            continue
        key, value = key.strip(), _unquote(value.strip())
        if line[:1] not in (" ", "\t"):
            if value:
                meta[key], parent = value, None
            else:
                parent = {}
                meta[key] = parent
        elif parent is not None:
            parent[key] = value
    return meta, "\n".join(lines[end + 1 :]).strip()


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _meta(meta: dict[str, Any], key: str) -> str | None:
    nested = meta.get("metadata")
    if isinstance(nested, dict) and isinstance(nested.get(key), str):
        return nested[key]
    value = meta.get(key)
    return value if isinstance(value, str) else None


def project_cwd(project_dir: Path) -> str | None:
    """Session cwd recorded in the project's newest transcripts, if any.

    Reads at most TRANSCRIPT_PROBE_LINES lines and TRANSCRIPT_PROBE_BYTES per file.
    """
    try:
        transcripts = sorted(
            project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except OSError:
        return None
    for path in transcripts[:TRANSCRIPT_PROBE_FILES]:
        cwd = _probe_cwd(path)
        if cwd is not None:
            return cwd
    return None


def _probe_cwd(path: Path) -> str | None:
    """First ``cwd`` in the head of one transcript; None when absent or unreadable."""
    try:
        with path.open("rb") as fh:
            budget = TRANSCRIPT_PROBE_BYTES
            for _ in range(TRANSCRIPT_PROBE_LINES):
                line = fh.readline(budget) if budget > 0 else b""
                if not line:
                    break
                budget -= len(line)
                cwd = _line_cwd(line)
                if cwd is not None:
                    return cwd
    except OSError:
        return None
    return None


def _line_cwd(line: bytes) -> str | None:
    try:
        record = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if isinstance(record, dict) and isinstance(record.get("cwd"), str):
        return record["cwd"]
    return None


def _is_project_specific(text: str, name: str | None, cwd: str | None) -> bool:
    if name and len(name) >= 3:
        pattern = r"(?<![\w-])" + re.escape(name) + r"(?![\w-])"
        if re.search(pattern, text, re.IGNORECASE):
            return True
    if cwd and os.path.isdir(cwd):
        for token in _PATH_TOKEN.findall(text):
            if os.path.exists(os.path.join(cwd, token.rstrip("."))):
                return True
    return False


def memory_candidates(claude: Path) -> list[Candidate]:
    """Feedback/reference memories of every project under ``claude/projects``."""
    out: list[Candidate] = []
    projects = claude / "projects"
    if not projects.is_dir():
        return out
    for pdir in sorted(p for p in projects.iterdir() if p.is_dir()):
        out += _project_memories(pdir)
    return out


def _project_memories(pdir: Path) -> list[Candidate]:
    """Candidates of one project's ``memory/*.md`` (the MEMORY.md index skipped)."""
    files = sorted((pdir / "memory").glob("*.md"))
    files = [f for f in files if f.name.upper() != "MEMORY.MD"]
    if not files:
        return []
    cwd = project_cwd(pdir)
    name = re.split(r"[\\/]", cwd.rstrip("\\/"))[-1] if cwd else None
    found = (_memory_candidate(path, name, cwd, pdir.name) for path in files)
    return [c for c in found if c is not None]


def _memory_candidate(
    path: Path, name: str | None, cwd: str | None, fallback: str
) -> Candidate | None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    meta, body = parse_frontmatter(text)
    label = (_meta(meta, "type") or "").lower()
    if label not in MEMORY_TYPES:
        return None
    title = _meta(meta, "description") or _meta(meta, "name") or path.stem
    content = body or title
    specific = _scoped_specific(meta, content, name, cwd)
    return Candidate("memory", label, title, content, name or fallback, specific)


def _scoped_specific(
    meta: dict[str, Any], content: str, name: str | None, cwd: str | None
) -> bool:
    """Frontmatter scope decides (project / global|user); else the content heuristic."""
    scope = (_meta(meta, "scope") or "").lower()
    if scope == "project":
        return True
    if scope in ("global", "user"):
        return False
    return _is_project_specific(content, name, cwd)


def bloks_context(home: Path) -> str | None:
    """Stdout of ``bloks context --format json``; None when absent or failing."""
    exe = shutil.which("bloks")
    if not exe:
        return None
    argv = [exe, "context", "--format", "json", "--budget", "0", str(home)]
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=BLOKS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def bloks_candidates(raw: str | None) -> list[Candidate]:
    """Rule and taste cards from ``bloks context`` JSON; project cards are skipped."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for key in BLOKS_KINDS:
        cards = data.get(key)
        for card in cards if isinstance(cards, list) else []:
            candidate = _card_candidate(key, card)
            if candidate is not None:
                out.append(candidate)
    return out


def _card_candidate(key: str, card: Any) -> Candidate | None:
    """Candidate of one rule/taste card; None for a non-object or empty card."""
    if not isinstance(card, dict):
        return None
    title, body = card.get("title"), card.get("body")
    title = title.strip() if isinstance(title, str) else ""
    body = body.strip() if isinstance(body, str) else ""
    content = body or title
    if not content:
        return None
    return Candidate("bloks", key[:-1], title or content, content, "bloks")


def harness_docs(claude: Path) -> list[Path]:
    """Global CLAUDE.md, installed skill/agent docs and manifest-listed .md files."""
    docs = [claude / "CLAUDE.md"]
    docs += sorted(claude.glob("skills/*/SKILL.md"))
    docs += sorted(claude.glob("agents/*.md"))
    try:
        manifest = json.loads((claude / ".ccv47-manifest.json").read_text("utf-8"))
        files = manifest.get("files") if isinstance(manifest, dict) else None
    except (OSError, ValueError):
        files = None
    if isinstance(files, dict):
        docs += [claude / key for key in files if key.endswith(".md")]
    return list(dict.fromkeys(docs))


class Corpus:
    """Normalized harness docs; a lesson is present as a substring or by shingles."""

    def __init__(self, paths: Iterable[Path]):
        """Read and normalize every readable doc."""
        texts = []
        for path in paths:
            try:
                texts.append(normalize(path.read_text(encoding="utf-8-sig")))
            except (OSError, UnicodeDecodeError):
                continue
        self.text = " " + " | ".join(texts) + " "
        words = self.text.split()
        self.grams = {tuple(words[i : i + SHINGLE]) for i in range(len(words))}

    def contains(self, text: str) -> bool:
        """True when the normalized text appears, or most of its shingles do."""
        norm = normalize(text)
        if not norm or f" {norm} " in self.text:
            return True
        words = norm.split()
        if len(words) < SHINGLE * 2:
            return False
        grams = {tuple(words[i : i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}
        return sum(g in self.grams for g in grams) / len(grams) >= PRESENT_RATIO


def seen_hashes(inbox: Path) -> set[str]:
    """content_hash of every proposal file under inbox (any status, any subdir)."""
    seen: set[str] = set()
    if not inbox.is_dir():
        return seen
    for path in inbox.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        change = data.get("change")
        for holder in (data, change if isinstance(change, dict) else {}):
            value = holder.get("content_hash")
            if isinstance(value, str):
                seen.add(value)
    return seen


def _proposal(
    cand: Candidate, digest: str, guard: privacy_guard.Guard
) -> model.Proposal:
    origin = f"{cand.origin} {cand.label}"
    return model.Proposal(
        id=model.new_proposal_id(),
        created_at=model.now_iso(),
        kind="lesson",
        source=model.ProposalSource(project=mask(cand.project, guard)),
        target=model.ProposalTarget(installed_path="CLAUDE.md"),
        change=model.Change(tool="lesson", content=mask(cand.content, guard)),
        reason=mask(f"Shared lesson candidate ({origin}): {cand.title}", guard),
        extra={"content_hash": digest, "origin": cand.origin},
    )


def _save(proposal: model.Proposal, inbox: Path) -> None:
    same = os.path.normcase(os.path.abspath(inbox)) == os.path.normcase(
        os.path.abspath(model.inbox_dir())
    )
    if same:
        model.save_proposal(proposal)
    else:
        name = f"{proposal.id}.json"
        model.write_atomic(inbox / name, proposal.to_json() + "\n")


def propose_lessons(
    home: Path | None = None,
    *,
    run_bloks: BloksRunner | None = None,
    docs: Iterable[Path] = (),
    guard: privacy_guard.Guard | None = None,
    dry_run: bool = False,
) -> list[model.Proposal]:
    """Write new lesson proposals into ``<home>/.claude/harness-inbox``; return them.

    ``run_bloks(home)`` returns ``bloks context`` JSON or None (default: bloks_context).
    ``docs`` adds harness docs (e.g. a repo CLAUDE.md) to the already-present check.
    ``dry_run`` builds the proposals without writing them.
    """
    home = home or model.home_dir()
    claude = home / ".claude"
    if not claude.is_dir():
        return []
    runner = run_bloks or bloks_context
    guard = guard or default_guard(home)
    candidates = memory_candidates(claude) + bloks_candidates(runner(home))
    corpus = Corpus([*harness_docs(claude), *docs])
    inbox = claude / "harness-inbox"
    seen = seen_hashes(inbox)
    out = []
    for cand in candidates:
        digest = content_hash(cand.content)
        if cand.specific or digest in seen or corpus.contains(cand.content):
            continue
        seen.add(digest)
        proposal = _proposal(cand, digest, guard)
        if not dry_run:
            _save(proposal, inbox)
        out.append(proposal)
    return out
