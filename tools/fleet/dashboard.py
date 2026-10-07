"""Fleet dashboard (VAL-810): one self-contained HTML page from a FleetState.

``build_page(state)`` returns the page; ``write_page(state, path)`` writes it.
Sections: summary, alerts (collisions + per-session alerts, worst first),
sessions, inbox, recent audit, harness drift, collector warnings.

Colors come only from ``tools/viz/palette.css_tokens`` (light on ``:root``, dark
under ``prefers-color-scheme`` and ``[data-theme="dark"]``), the font stack from
``palette.font()``. Every data value is HTML-escaped. No external resources; the
one inline script only drives the theme toggle. The page carries project names
and paths: publish it privately, never commit it.
"""

from __future__ import annotations

import html
import os
from collections.abc import Iterable
from pathlib import Path

from . import model
from .model import Alert, AuditEvent, Collision, DriftEntry, FleetState, Session

try:  # tools.fleet: tools.viz is the sibling package
    from ..viz import palette
except ImportError:  # fleet.py as a script: tools/ is on sys.path, viz is top level
    from viz import palette  # type: ignore[no-redef]

TITLE = "Fleet Dashboard"
NARROW_PX = 800
_SEVERITY = {
    "error": "error",
    "critical": "error",
    "warn": "warn",
    "warning": "warn",
    "info": "info",
}
_RANK = {"error": 0, "warn": 1, "info": 2, "other": 3}


def _e(value: object) -> str:
    """Escaped text; None and empty strings render as '-'."""
    if value is None or value == "":
        return "-"
    return html.escape(str(value), quote=True)


def _num(value: float | None, suffix: str = "") -> str:
    return "-" if value is None else f"{value:g}{suffix}"


def _font_stack() -> str:
    names = palette.font()["family_stack"]
    return ", ".join(f'"{n}"' if " " in n else n for n in names)


def _indent(text: str, pad: str) -> str:
    return "\n".join(pad + line for line in text.splitlines())


# Static layout rules (no tokens): kept out of _css so the token prelude stays small.
_RULES = (
    """body {
  margin: 0;
  padding: 0 12px;
  background: var(--surface-2);
  color: var(--text-primary);
  font-family: var(--font-sans);
  font-size: 14px;
  line-height: 1.45;
  overflow-x: hidden;
}
[hidden] { display: none !important; }
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
.page { max-width: 1200px; margin: 0 auto; padding: 20px 0 32px; }
.page-head { display: flex; flex-wrap: wrap; align-items: baseline;
  justify-content: space-between; gap: 8px 16px; margin-bottom: 16px; }
h1 { font-size: 22px; font-weight: 600; margin: 0; }
h2 { font-size: 16px; font-weight: 600; margin: 0 0 10px; }
h3 { font-size: 13px; font-weight: 600; margin: 14px 0 6px;
  color: var(--text-secondary); }
.muted { color: var(--text-muted); }
.meta { color: var(--text-secondary); margin: 4px 0 0; }
button { font: inherit; font-size: 13px; color: var(--text-secondary);
  background: none; border: 1px solid var(--border); border-radius: 6px;
  padding: 4px 10px; min-height: 30px; cursor: pointer; }
button:hover { color: var(--text-primary); }
button:focus-visible { outline: 2px solid var(--series-1); outline-offset: 2px; }
.card { background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 16px 14px; margin: 0 0 16px; min-width: 0; }
.tiles { display: grid; gap: 12px; margin: 0 0 16px; padding: 0; list-style: none;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 150px), 1fr)); }
.tile { background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px 14px; min-width: 0; }
.tile-label { color: var(--text-secondary); font-size: 12px; }
.tile-value { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile-note { color: var(--text-muted); font-size: 12px; overflow-wrap: anywhere; }
table.grid { width: 100%; border-collapse: collapse; font-size: 13px; }
table.grid th { text-align: left; font-weight: 600; color: var(--text-secondary);
  border-bottom: 1px solid var(--axis); padding: 6px 8px; }
table.grid td { border-bottom: 1px solid var(--grid); padding: 6px 8px;
  vertical-align: top; overflow-wrap: anywhere; }
td.num { font-variant-numeric: tabular-nums; }
code { font-size: 12px; overflow-wrap: anywhere; white-space: pre-wrap; }
.sev::before, .dot::before { content: ""; display: inline-block; width: 8px;
  height: 8px; border-radius: 50%; margin-right: 6px; background: var(--gray); }
.sev-error::before { background: var(--status-critical); }
.sev-warn::before { background: var(--status-warning); }
.sev-info::before { background: var(--series-1); }
.dot-alive::before { background: var(--status-good); }
dl.facts { display: grid; grid-template-columns: max-content minmax(0, 1fr);
  gap: 4px 16px; margin: 0; }
dl.facts dt { color: var(--text-secondary); }
dl.facts dd { margin: 0; overflow-wrap: anywhere; }
.big { font-size: 28px; font-weight: 600; font-variant-numeric: tabular-nums; }
ul.warnings { margin: 0; padding-left: 18px; color: var(--text-secondary);
  overflow-wrap: anywhere; }
.empty { color: var(--text-muted); margin: 4px 0; }
@media (max-width: """
    + str(NARROW_PX)
    + """px) {
  table.grid, table.grid tbody, table.grid tr, table.grid td { display: block;
    width: 100%; }
  table.grid thead { position: absolute; width: 1px; height: 1px; overflow: hidden;
    clip-path: inset(50%); }
  table.grid tr { border-bottom: 1px solid var(--grid); padding: 6px 0; }
  table.grid td { border: 0; padding: 2px 0; display: grid;
    grid-template-columns: 7rem minmax(0, 1fr); gap: 8px; }
  table.grid td::before { content: attr(data-label); color: var(--text-secondary);
    font-weight: 600; }
}
"""
)


def _css() -> str:
    light = _indent(palette.css_tokens("light"), "  ")
    dark_media = _indent(palette.css_tokens("dark"), "    ")
    dark_attr = _indent(palette.css_tokens("dark"), "  ")
    return f"""
:root {{
  color-scheme: light;
  --font-sans: {_font_stack()};
{light}
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
{dark_media}
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
{dark_attr}
}}
{_RULES}"""


_SCRIPT = """
(function () {
  var root = document.documentElement;
  var btn = document.getElementById("theme-toggle");
  if (!btn) return;
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  function dark() {
    var t = root.getAttribute("data-theme");
    return t ? t === "dark" : !!(media && media.matches);
  }
  function label() { btn.textContent = dark() ? "Light theme" : "Dark theme"; }
  btn.hidden = false;
  label();
  btn.addEventListener("click", function () {
    root.setAttribute("data-theme", dark() ? "light" : "dark");
    label();
  });
  if (media && media.addEventListener) media.addEventListener("change", label);
})();
"""


def _severity(value: str | None) -> str:
    return _SEVERITY.get((value or "").lower(), "other")


def _sev_cell(value: str | None) -> str:
    return f'<span class="sev sev-{_severity(value)}">{_e(value)}</span>'


def _table(headers: Iterable[str], rows: list[list[str]], empty: str) -> str:
    """Grid table; cells are pre-escaped HTML, labels feed the stacked layout."""
    if not rows:
        return f'<p class="empty">{_e(empty)}</p>'
    heads = list(headers)
    head = "".join(f'<th scope="col">{_e(h)}</th>' for h in heads)
    body = "".join(
        "<tr>"
        + "".join(
            f'<td data-label="{_e(h)}"><div>{c}</div></td>' for h, c in zip(heads, row)
        )
        + "</tr>"
        for row in rows
    )
    return (
        f'<table class="grid"><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


def _label(s: Session) -> str:
    sid = s.session_id or (str(s.pid) if s.pid is not None else None)
    project = s.project or "?"
    return f"{project} ({sid[:8]})" if sid else project


def _labels(state: FleetState) -> dict[str, str]:
    return {s.session_id: _label(s) for s in state.sessions if s.session_id}


def _alerts(state: FleetState) -> list[tuple[Session, Alert]]:
    pairs = [(s, a) for s in state.sessions for a in s.alerts]
    return sorted(pairs, key=lambda p: _RANK[_severity(p[1].severity)])


def _summary(state: FleetState) -> str:
    alive = sum(1 for s in state.sessions if s.alive)
    counts = {k: 0 for k in _RANK}
    for _, a in _alerts(state):
        counts[_severity(a.severity)] += 1
    alerts = sum(counts.values())
    alert_note = ", ".join(f"{n} {k}" for k, n in counts.items() if n) or "none"
    m = state.machine
    tiles = [
        ("Sessions", f"{alive} / {len(state.sessions)}", "alive / total"),
        ("Alerts", str(alerts), alert_note),
        ("Collisions", str(len(state.collisions)), "cross-session overlaps"),
        ("Inbox", str(state.inbox_count), "pending proposals"),
        ("Drift", str(len(state.harness.drift)), "installed files off manifest"),
        (
            "Memory free",
            _num(m.mem_free_gb, " GB"),
            f"of {_num(m.mem_total_gb, ' GB')}",
        ),
    ]
    items = "".join(
        f'<li class="tile"><div class="tile-label">{_e(label)}</div>'
        f'<div class="tile-value">{_e(value)}</div>'
        f'<div class="tile-note">{_e(note)}</div></li>'
        for label, value, note in tiles
    )
    return (
        f'<section id="summary" aria-label="Summary"><ul class="tiles">{items}</ul>'
        "</section>"
    )


def _collision_rows(state: FleetState, labels: dict[str, str]) -> list[list[str]]:
    def row(c: Collision) -> list[str]:
        names = ", ".join(labels.get(s, s) for s in c.sessions)
        return [
            '<span class="sev sev-error">collision</span>',
            _e(c.kind),
            f"<code>{_e(c.target)}</code>",
            _e(names),
            _e(c.detail),
        ]

    return [row(c) for c in state.collisions]


def _alerts_section(state: FleetState) -> str:
    labels = _labels(state)
    collisions = _table(
        ("Severity", "Kind", "Target", "Sessions", "Detail"),
        _collision_rows(state, labels),
        "No collisions.",
    )
    rows = [
        [
            _sev_cell(a.severity),
            _e(a.kind),
            _e(labels.get(a.session or "", _label(s))),
            _e(a.detail),
            f"<code>{_e(a.evidence)}</code>" if a.evidence else "-",
        ]
        for s, a in _alerts(state)
    ]
    alerts = _table(
        ("Severity", "Kind", "Session", "Detail", "Evidence"), rows, "No alerts."
    )
    return (
        '<section id="alerts" class="card"><h2>Alerts</h2>'
        f"<h3>Collisions</h3>{collisions}<h3>Session alerts</h3>{alerts}</section>"
    )


def _session_row(s: Session) -> list[str]:
    who = f"<strong>{_e(s.project)}</strong>"
    if s.name:
        who += f"<br>{_e(s.name)}"
    ids = " · ".join(_e(v) for v in (s.session_id, s.pid) if v is not None)
    if ids:
        who += f'<br><span class="muted">{ids}</span>'
    dot = "dot dot-alive" if s.alive else "dot"
    state = f'<span class="{dot}">{"alive" if s.alive else "ended"}</span>'
    state += f"<br>{_e(s.status)} · {_e(s.kind)}"
    ctx = f"{s.context_pct:.0f}%" if s.context_pct is not None else None
    handoff = "-"
    if s.handoff:
        age = _num(s.handoff.age_h, "h")
        handoff = f'{_e(s.handoff.goal)}<br><span class="muted">{_e(age)} old</span>'
    return [
        who,
        state,
        _e(s.model),
        _e(ctx),
        _e(s.agents_running),
        _e(s.last_activity),
        handoff,
        _e(len(s.alerts)),
        _e(s.version),
    ]


def _sessions_section(state: FleetState) -> str:
    sessions = sorted(state.sessions, key=lambda s: (not s.alive, s.project or ""))
    table = _table(
        (
            "Session",
            "State",
            "Model",
            "Context",
            "Agents",
            "Last activity",
            "Handoff",
            "Alerts",
            "CLI",
        ),
        [_session_row(s) for s in sessions],
        "No sessions.",
    )
    return f'<section id="sessions" class="card"><h2>Sessions</h2>{table}</section>'


def _inbox_section(state: FleetState) -> str:
    n = state.inbox_count
    noun = "proposal" if n == 1 else "proposals"
    return (
        '<section id="inbox" class="card"><h2>Harness inbox</h2>'
        f'<div class="big">{_e(n)}</div>'
        f'<p class="meta">pending {noun} in ~/.claude/harness-inbox</p></section>'
    )


def _audit_section(state: FleetState) -> str:
    def row(ev: AuditEvent) -> list[str]:
        return [
            _e(ev.ts),
            _e(ev.project),
            _e(ev.category),
            _e(ev.tool),
            f"<code>{_e(ev.command)}</code>" if ev.command else "-",
        ]

    events = sorted(state.audit_recent, key=lambda ev: ev.ts or "", reverse=True)
    table = _table(
        ("Time", "Project", "Category", "Tool", "Command"),
        [row(ev) for ev in events],
        "No audit events.",
    )
    return f'<section id="audit" class="card"><h2>Recent audit</h2>{table}</section>'


def _harness_section(state: FleetState) -> str:
    h = state.harness
    in_sync = h.extra.get("in_sync")
    dirty = h.extra.get("dirty")
    facts = [
        ("Repo", h.repo),
        ("HEAD", h.head_sha),
        ("Installed", h.installed_sha),
        ("In sync", None if in_sync is None else ("yes" if in_sync else "no")),
        ("Dirty at sync", None if dirty is None else ("yes" if dirty else "no")),
        ("Synced at", h.extra.get("synced_at")),
    ]
    dl = "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in facts)

    def row(d: DriftEntry) -> list[str]:
        return [
            _e(d.status),
            f"<code>{_e(d.installed_path)}</code>",
            f"<code>{_e(d.repo_path)}</code>",
            _e(d.expected_sha),
            _e(d.actual_sha),
            _e(d.extra.get("detail")),
        ]

    table = _table(
        ("Status", "Installed path", "Repo path", "Expected", "Actual", "Detail"),
        [row(d) for d in h.drift],
        "No drift.",
    )
    return (
        '<section id="harness" class="card"><h2>Harness drift</h2>'
        f'<dl class="facts">{dl}</dl><h3>Drift entries</h3>{table}</section>'
    )


def _warnings_section(state: FleetState) -> str:
    warnings = state.extra.get("warnings")
    if not isinstance(warnings, list) or not warnings:
        return ""
    items = "".join(f"<li>{_e(w)}</li>" for w in warnings)
    return (
        '<section id="warnings" class="card"><h2>Collector warnings</h2>'
        f'<ul class="warnings">{items}</ul></section>'
    )


def build_page(state: FleetState) -> str:
    """Self-contained HTML dashboard for one FleetState."""
    took = state.extra.get("collect_s")
    meta = f"Generated {_e(state.generated_at)}"
    if isinstance(took, int | float) and not isinstance(took, bool):
        meta += f" in {took:.2f} s"
    body = "".join(
        (
            _summary(state),
            _alerts_section(state),
            _sessions_section(state),
            _inbox_section(state),
            _audit_section(state),
            _harness_section(state),
            _warnings_section(state),
        )
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(TITLE)}</title>\n<style>{_css()}</style>\n</head>\n<body>\n"
        '<main class="page">\n<header class="page-head"><div>'
        f'<h1>{_e(TITLE)}</h1><p class="meta">{meta}</p></div>'
        '<button type="button" id="theme-toggle" hidden>Theme</button></header>\n'
        f"{body}\n</main>\n<script>{_SCRIPT}</script>\n</body>\n</html>\n"
    )


def write_page(state: FleetState, path: str | os.PathLike[str]) -> Path:
    """Write build_page(state) to path (atomic, parents created); absolute path."""
    out = Path(path).resolve()
    return model.write_atomic(out, build_page(state))
