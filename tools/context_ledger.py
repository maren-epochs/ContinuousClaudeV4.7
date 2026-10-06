#!/usr/bin/env python3
"""Main-context token ledger per skill, from a Claude Code transcript JSONL.

Streams the transcript line by line (never whole-file). Main thread only:
entries with isSidechain=true are skipped; subagent (worker) transcripts live
in <project>/<session-id>/subagents/ and are never in the main file, so worker
tokens are excluded by construction. Stdlib only.

Per assistant turn (type == "assistant" with message.usage): timestamp,
context = input_tokens + cache_read_input_tokens + cache_creation_input_tokens
(same formula as Claude Code used_percentage), output_tokens, attributionSkill
(absent/null -> "(none)"). attributionSkill is STICKY: it names the most
recently invoked skill, not the skill doing the work, so per-skill totals are
upper bounds. The ledger therefore reports per SPAN (a maximal run of
consecutive turns sharing one label) and leaves bounding to the reader.

Span delta = sum of per-turn context steps attributed to the span, including
the step INTO its first turn (the skill's own load; for the first span this is
the initial context). A drop larger than COMPACTION_DROP tokens between
consecutive turns is a compaction event: listed with its turn index and
excluded from deltas. Invariant: sum(span deltas) - sum(compaction drops) ==
final context. Turn indices are 1-based.

Usage:
    py -3.13 tools/context_ledger.py                      # newest transcript for cwd
    py -3.13 tools/context_ledger.py --session <id>       # ~/.claude/projects/<slug>/<id>.jsonl
    py -3.13 tools/context_ledger.py path/to/x.jsonl --json

slug = re.sub(r'[^A-Za-z0-9]', '-', cwd). The config root is $CLAUDE_CONFIG_DIR
if set, else $HOME/.claude (falls back to %USERPROFILE%, then Path.home()).

Exit: 0 ok, 1 no assistant usage turns found, 2 transcript/session not found
or bad arguments. Output is ASCII only.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

COMPACTION_DROP = 20000
NONE_LABEL = "(none)"
TOKEN_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def config_dir():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env)
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    return (Path(home) if home else Path.home()) / ".claude"


def slug_for(cwd):
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def project_folder():
    return config_dir() / "projects" / slug_for(os.getcwd())


def newest_transcript(folder):
    """Newest-mtime *.jsonl directly under folder, or None."""
    try:
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix == ".jsonl"]
    except OSError:
        return None
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


class Reader:
    """Streaming transcript reader. Iterating yields (timestamp, context,
    output_tokens, label) per main-thread assistant turn; .skipped and .version
    are valid once iteration finishes."""

    def __init__(self, path):
        self.path = path
        self.skipped = 0
        self.version = None

    def __iter__(self):
        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    self.skipped += 1
                    continue
                if not isinstance(e, dict):
                    self.skipped += 1
                    continue
                if self.version is None and isinstance(e.get("version"), str) and e["version"]:
                    self.version = e["version"]
                if e.get("type") != "assistant" or e.get("isSidechain"):
                    continue
                msg = e.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                ctx = sum(_int(usage.get(k)) for k in TOKEN_KEYS)
                label = e.get("attributionSkill")
                if not isinstance(label, str) or not label:
                    label = NONE_LABEL
                ts = e.get("timestamp")
                yield (ts if isinstance(ts, str) else "", ctx, _int(usage.get("output_tokens")), label)


def build_ledger(path):
    """Parse the transcript at path into the ledger dict (the --json shape)."""
    reader = Reader(path)
    turns = 0
    peak = 0
    total_out = 0
    prev_ctx = 0
    compactions = []
    spans = []
    span = None
    for ts, ctx, out, label in reader:
        turns += 1
        total_out += out
        peak = max(peak, ctx)
        step = ctx - prev_ctx
        if turns > 1 and step < -COMPACTION_DROP:
            compactions.append({"turn": turns, "from": prev_ctx, "to": ctx})
            step = 0
        if span is None or span["label"] != label:
            span = {"label": label, "first_turn": turns, "last_turn": turns, "turns": 0,
                    "start_context": ctx, "end_context": ctx, "delta": 0, "_ts": ts}
            spans.append(span)
        span["last_turn"] = turns
        span["turns"] += 1
        span["end_context"] = ctx
        span["delta"] += step
        prev_ctx = ctx

    skills = {}
    for s in spans:
        k = skills.setdefault(s["label"], {"delta": 0, "spans": 0, "turns": 0})
        k["delta"] += s["delta"]
        k["spans"] += 1
        k["turns"] += s["turns"]

    return {
        "transcript": str(path),
        "version": reader.version,
        "turns": turns,
        "skipped_lines": reader.skipped,
        "peak_context": peak,
        "total_output_tokens": total_out,
        "compactions": compactions,
        "spans": spans,
        "skills": skills,
    }


def _time(ts):
    """HH:MM:SS from an ISO timestamp, else the raw string."""
    m = re.search(r"T(\d\d:\d\d:\d\d)", ts or "")
    return m.group(1) if m else (ts or "")


def render_text(ledger):
    lines = [
        f"transcript: {ledger['transcript']}",
        f"version: {ledger['version'] or 'unknown'}",
        f"turns: {ledger['turns']}  (skipped lines: {ledger['skipped_lines']})",
        f"peak context: {ledger['peak_context']:,}",
        f"total output tokens: {ledger['total_output_tokens']:,}",
        f"compactions: {len(ledger['compactions'])}",
    ]
    for c in ledger["compactions"]:
        lines.append(f"  turn {c['turn']}: {c['from']:,} -> {c['to']:,}")

    spans = ledger["spans"]
    labw = max([len("label")] + [len(s["label"]) for s in spans])
    lines.append("")
    lines.append("SPANS")
    lines.append(f"  {'#':>4}  {'label':<{labw}}  {'first':>6}  {'last':>6}  {'turns':>5}  "
                 f"{'start':>9}  {'end':>9}  {'delta':>9}  started")
    for i, s in enumerate(spans, 1):
        lines.append(f"  {i:>4}  {s['label']:<{labw}}  {s['first_turn']:>6}  {s['last_turn']:>6}  "
                     f"{s['turns']:>5}  {s['start_context']:>9,}  {s['end_context']:>9,}  "
                     f"{s['delta']:>9,}  {_time(s.get('_ts', ''))}")

    skills = ledger["skills"]
    lines.append("")
    lines.append("SKILLS")
    lines.append(f"  {'label':<{labw}}  {'delta':>9}  {'spans':>5}  {'turns':>5}")
    for label, k in sorted(skills.items(), key=lambda kv: -kv[1]["delta"]):
        lines.append(f"  {label:<{labw}}  {k['delta']:>9,}  {k['spans']:>5}  {k['turns']:>5}")

    lines.append("")
    lines.append("note: attributionSkill is sticky (last invoked skill) - per-skill totals are upper bounds;")
    lines.append("      subagent/worker tokens live in <session>/subagents/ and are not in this ledger.")
    return "\n".join(lines) + "\n"


def resolve_transcript(args):
    """Return (Path, None) or (None, error message)."""
    if args.transcript and args.session:
        return None, "give a transcript path or --session, not both"
    if args.transcript:
        p = Path(args.transcript)
        if not p.is_file():
            return None, f"transcript not found: {p}"
        return p, None
    folder = project_folder()
    if args.session:
        p = folder / f"{args.session}.jsonl"
        if not p.is_file():
            return None, f"session not found: {args.session} (looked for {p})"
        return p, None
    p = newest_transcript(folder)
    if p is None:
        return None, f"no transcript found in {folder}"
    return p, None


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("transcript", nargs="?", help="transcript .jsonl path")
    ap.add_argument("--session", help="session id; resolves ~/.claude/projects/<slug>/<id>.jsonl")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0

    path, err = resolve_transcript(args)
    if err:
        print(err, file=sys.stderr)
        return 2
    try:
        ledger = build_ledger(path)
    except OSError as e:
        print(f"cannot read {path}: {e}", file=sys.stderr)
        return 2
    if ledger["turns"] == 0:
        print(f"no assistant usage turns found in {path} "
              f"(skipped lines: {ledger['skipped_lines']})", file=sys.stderr)
        return 1

    if args.json:
        out = dict(ledger)
        out["spans"] = [{k: v for k, v in s.items() if not k.startswith("_")} for s in ledger["spans"]]
        print(json.dumps(out, indent=2, ensure_ascii=True))
    else:
        sys.stdout.write(render_text(ledger))
    return 0


if __name__ == "__main__":
    sys.exit(main())
