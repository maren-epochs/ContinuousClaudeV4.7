#!/usr/bin/env python3
"""Fleet CLI: ``py -3.13 tools/fleet/fleet.py collect|report|json``.

collect  build state.json from ~/.claude and print a one-line summary
report   human-readable table from state.json (collects first when missing/--fresh)
json     state.json as JSON on stdout (collects first when missing/--fresh)
"""

from __future__ import annotations

import argparse
import io
import os
import sys

if __package__:
    from . import collect, model
    from .model import FleetState, Session
else:  # run as a script: tools/ first, so `fleet` is this package, not this file
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from fleet import collect, model
    from fleet.model import FleetState, Session


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


def _cell(value: object, width: int) -> str:
    text = "-" if value is None or value == "" else str(value)
    return text[:width].ljust(width)


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def _session_row(s: Session) -> str:
    ctx = f"{s.context_pct:.0f}%" if s.context_pct is not None else None
    age = (
        f"{s.handoff.age_h:.1f}h" if s.handoff and s.handoff.age_h is not None else None
    )
    return " ".join(
        (
            "*" if s.alive else " ",
            _cell(s.project, 24),
            _cell(s.status, 8),
            _cell(s.model, 18),
            _cell(ctx, 5),
            _cell(s.agents_running, 3),
            _cell(age, 7),
            _cell(len(s.alerts) or None, 3),
            _cell(s.last_activity, 24),
        )
    )


def report(state: FleetState) -> str:
    """Plain-text report of a FleetState."""
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
    if state.sessions:
        header = " ".join(
            (
                " ",
                _cell("project", 24),
                _cell("status", 8),
                _cell("model", 18),
                _cell("ctx", 5),
                _cell("agt", 3),
                _cell("handoff", 7),
                _cell("alr", 3),
                _cell("last activity", 24),
            )
        )
        lines += ["", header.rstrip()] + [
            _session_row(s).rstrip() for s in state.sessions
        ]
    warnings = state.extra.get("warnings") or []
    if warnings:
        lines += ["", f"{len(warnings)} parse warnings:"] + [f"  {w}" for w in warnings]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the exit code."""
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(
        prog="fleet.py", description=__doc__.split("\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="build ~/.claude/fleet/state.json")
    for name in ("report", "json"):
        p = sub.add_parser(name, help=f"{name} from state.json")
        p.add_argument("--fresh", action="store_true", help="collect first")
    args = parser.parse_args(argv)
    if args.command == "collect":
        state = collect.collect()
        model.save_state(state)
        print(summary(state))
    elif args.command == "report":
        print(report(_state(args.fresh)))
    else:
        print(_state(args.fresh).to_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
