"""Tests for tools/fleet/dashboard.py (VAL-810).

Run from the repo root:  py -3.13 -m pytest -q tools/fleet
Every page is rendered from a synthetic FleetState (project-A/B, fake ids).
"""

import html
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.fleet import dashboard
from tools.fleet.model import FleetState
from tools.viz import palette

EVIL = "<script>alert(1)</script> & \"q\" 'a'"


def fixture() -> FleetState:
    return FleetState.from_dict(
        {
            "schema_version": 1,
            "generated_at": "2026-10-07T12:00:00Z",
            "harness": {
                "repo": "C:/work/harness",
                "head_sha": "abc1234",
                "installed_sha": "def5678",
                "in_sync": False,
                "drift": [
                    {
                        "installed_path": "hooks/status.mjs",
                        "repo_path": ".claude/hooks/status.mjs",
                        "expected_sha": "1111aaaa",
                        "actual_sha": "2222bbbb",
                        "status": "modified",
                    },
                    {
                        "installed_path": "skills/review/SKILL.md",
                        "repo_path": "harness/skills/review/SKILL.md",
                        "expected_sha": "3333cccc",
                        "actual_sha": None,
                        "status": "missing",
                    },
                ],
            },
            "sessions": [
                {
                    "pid": 4242,
                    "session_id": "sess-a1",
                    "name": "build-api",
                    "project": "project-A",
                    "cwd": "C:/work/project-A",
                    "status": "waiting",
                    "kind": "interactive",
                    "version": "2.1.292",
                    "alive": True,
                    "model": "claude-opus-5-5",
                    "context_pct": 42.4,
                    "last_activity": "2026-10-07T11:55:00Z",
                    "agents_running": 2,
                    "handoff": {"path": "h.md", "age_h": 1.5, "goal": "ship the api"},
                    "alerts": [
                        {
                            "kind": "collision",
                            "severity": "error",
                            "session": "sess-a1",
                            "detail": "wrote C:/work/shared/x.py also written by sess-b2",
                            "evidence": "t-a.jsonl:12",
                        },
                        {
                            "kind": "stuck",
                            "severity": "warn",
                            "session": "sess-a1",
                            "detail": "waiting for 25 min",
                            "evidence": "t-a.jsonl:40",
                        },
                    ],
                },
                {
                    "pid": 5151,
                    "session_id": "sess-b2",
                    "name": EVIL,
                    "project": "project-B",
                    "cwd": "C:/work/project-B",
                    "status": "busy",
                    "kind": "bg",
                    "alive": False,
                    "alerts": [
                        {
                            "kind": "compliance",
                            "severity": "info",
                            "session": "sess-b2",
                            "detail": EVIL,
                            "evidence": "t-b.jsonl:7",
                        },
                        {"kind": "odd", "severity": 'bogus" onclick="x', "detail": "z"},
                    ],
                },
            ],
            "inbox_count": 3,
            "audit_recent": [
                {
                    "ts": "2026-10-07T11:50:00Z",
                    "project": "project-A",
                    "session_id": "sess-a1",
                    "category": "force-push",
                    "command": "git push --force origin main && echo " + EVIL,
                    "tool": "Bash",
                }
            ],
            "collisions": [
                {
                    "kind": "path",
                    "target": "C:/work/shared/x.py",
                    "sessions": ["sess-a1", "sess-b2"],
                    "detail": "same path within 30 min",
                }
            ],
            "machine": {"mem_total_gb": 32.0, "mem_free_gb": 7.5},
            "warnings": ["unreadable session file 9.json"],
        }
    )


def style_of(page: str) -> str:
    m = re.search(r"<style>(.*?)</style>", page, re.DOTALL)
    assert m is not None
    return m.group(1)


def block(css: str, selector: str) -> str:
    """Body of the first `selector { ... }` block (one nesting level allowed)."""
    start = css.index(selector)
    i = css.index("{", start) + 1
    depth = 1
    j = i
    while depth:
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
        j += 1
    return css[i : j - 1]


def text_of(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment))


class StructureTests(unittest.TestCase):
    def setUp(self):
        self.page = dashboard.build_page(fixture())

    def test_document_shell(self):
        page = self.page
        self.assertTrue(page.lower().startswith("<!doctype html>"))
        self.assertIn('<html lang="en"', page)
        self.assertIn('<meta charset="utf-8">', page)
        self.assertRegex(page, r'<meta name="viewport" content="width=device-width,')
        titles = re.findall(r"<title>(.*?)</title>", page, re.DOTALL)
        self.assertEqual(len(titles), 1)
        self.assertTrue(2 <= len(titles[0].split()) <= 4, titles[0])
        self.assertEqual(page.count("<style>"), 1)

    def test_sections_present(self):
        for section in ("summary", "alerts", "sessions", "inbox", "audit", "harness"):
            with self.subTest(section=section):
                self.assertRegex(self.page, rf'<section[^>]* id="{section}"')

    def section(self, name: str) -> str:
        m = re.search(rf'<section[^>]* id="{name}".*?</section>', self.page, re.DOTALL)
        assert m is not None, name
        return m.group(0)

    def test_sessions_table(self):
        sec = self.section("sessions")
        self.assertIn("<table", sec)
        rows = re.findall(r"<tr[ >].*?</tr>", sec.split("<tbody>", 1)[1], re.DOTALL)
        self.assertEqual(len(rows), 2)
        first = text_of(rows[0])
        for value in ("project-A", "build-api", "waiting", "interactive", "42%"):
            self.assertIn(value, first)
        self.assertIn("ship the api", first)
        self.assertIn("claude-opus-5-5", first)
        self.assertIn("project-B", text_of(rows[1]))
        # every data cell carries a label for the stacked mobile layout
        cells = re.findall(r"<td[^>]*>", rows[0])
        self.assertTrue(cells)
        self.assertTrue(all("data-label=" in c for c in cells))

    def test_alerts_include_collisions_sorted_by_severity(self):
        sec = self.section("alerts")
        plain = text_of(sec)
        self.assertIn("C:/work/shared/x.py", plain)
        self.assertIn("same path within 30 min", plain)
        self.assertIn("project-A", plain)
        self.assertIn("project-B", plain)
        for kind in ("collision", "stuck", "compliance"):
            self.assertIn(kind, plain)
        self.assertIn("t-a.jsonl:12", plain)
        err, warn, info = (plain.index(s) for s in ("error", "warn", "info"))
        self.assertLess(err, warn)
        self.assertLess(warn, info)

    def test_unknown_severity_gets_a_safe_class(self):
        self.assertNotIn('onclick="x"', self.page)
        for cls in re.findall(r'class="sev sev-([^"]*)"', self.page):
            self.assertIn(cls, ("error", "warn", "info", "other"))

    def test_inbox_summary(self):
        plain = text_of(self.section("inbox"))
        self.assertIn("3", plain)
        self.assertIn("pending", plain)

    def test_recent_audit(self):
        plain = text_of(self.section("audit"))
        for value in (
            "force-push",
            "Bash",
            "project-A",
            "git push --force origin main",
        ):
            self.assertIn(value, plain)

    def test_harness_drift(self):
        plain = text_of(self.section("harness"))
        for value in (
            "abc1234",
            "def5678",
            "hooks/status.mjs",
            "harness/skills/review/SKILL.md",
            "modified",
            "missing",
        ):
            self.assertIn(value, plain)

    def test_summary_counts(self):
        plain = text_of(self.section("summary"))
        self.assertRegex(plain, r"1\s*/\s*2")  # alive / total sessions
        self.assertIn("7.5", plain)
        self.assertIn("32", plain)

    def test_warnings_shown(self):
        self.assertIn("unreadable session file 9.json", text_of(self.page))

    def test_empty_state_renders(self):
        page = dashboard.build_page(FleetState())
        self.assertIn("No sessions", page)
        self.assertIn("No alerts", page)
        self.assertIn("No drift", page)
        self.assertIn("No audit events", page)

    def test_tolerant_of_junk_state(self):
        state = FleetState.from_dict(
            {"sessions": [{"pid": "x", "alerts": "nope"}, 7], "machine": []}
        )
        page = dashboard.build_page(state)
        self.assertIn('id="sessions"', page)


class EscapingTests(unittest.TestCase):
    def setUp(self):
        self.page = dashboard.build_page(fixture())

    def test_no_raw_markup_from_data(self):
        self.assertNotIn("<script>alert(1)", self.page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", self.page)
        self.assertIn("&amp;", self.page)
        self.assertIn("&quot;q&quot;", self.page)
        self.assertIn("&#x27;a&#x27;", self.page)

    def test_only_inline_script_no_external_resources(self):
        scripts = re.findall(r"<script[^>]*>", self.page)
        self.assertEqual(scripts, ["<script>"])
        for needle in ("src=", "http://", "https://", "@import", "url(", "<link"):
            self.assertNotIn(needle, self.page)


class ThemeTests(unittest.TestCase):
    def setUp(self):
        self.page = dashboard.build_page(fixture())
        self.css = style_of(self.page)

    def assertTokens(self, body: str, mode: str):
        for line in palette.css_tokens(mode).splitlines():
            self.assertIn(line.strip(), body, f"{mode}: {line}")

    def test_light_tokens_on_root(self):
        self.assertTokens(block(self.css, ":root {"), "light")

    def test_dark_tokens_for_media_query_and_attribute(self):
        media = block(self.css, "@media (prefers-color-scheme: dark)")
        self.assertIn(':root:not([data-theme="light"])', media)
        self.assertTokens(media, "dark")
        self.assertTokens(block(self.css, ':root[data-theme="dark"]'), "dark")

    def test_explicit_body_background_and_font(self):
        body = block(self.css, "body {")
        self.assertIn("background: var(--surface-2)", body)
        self.assertIn("color: var(--text-primary)", body)
        self.assertIn("font-family: var(--font-sans)", body)
        root = block(self.css, ":root {")
        for name in palette.font()["family_stack"]:
            self.assertIn(name, root)

    def test_no_color_literals_outside_tokens(self):
        rest = "\n".join(
            line for line in self.css.splitlines() if not line.strip().startswith("--")
        )
        self.assertNotRegex(rest, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotRegex(rest, r"\b(rgba?|hsla?|oklch|color-mix)\(")
        self.assertNotRegex(rest, r"font-family:(?!\s*(var\(--font-sans\)|inherit))")
        markup = self.page.replace(self.css, "")
        self.assertNotIn("style=", markup)

    def test_theme_toggle(self):
        self.assertIn("data-theme", self.page.split("</style>", 1)[1])
        self.assertRegex(self.page, r"<button[^>]*id=\"theme-toggle\"[^>]*hidden")


class MobileTests(unittest.TestCase):
    def test_no_horizontal_scroll_rules(self):
        css = style_of(dashboard.build_page(fixture()))
        self.assertIn("overflow-x: hidden", block(css, "body {"))
        self.assertIn("overflow-wrap: anywhere", css)
        narrow = block(css, "@media (max-width:")
        self.assertIn("display: block", narrow)
        self.assertIn("attr(data-label)", narrow)

    def test_cell_content_is_one_grid_item(self):
        # stacked cells are 2-column grids: label + one wrapper, so <br> cannot
        # spill multi-line content into the label column
        page = dashboard.build_page(fixture())
        cells = re.findall(r"<td [^>]*>(.*?)</td>", page, re.DOTALL)
        self.assertTrue(cells)
        for cell in cells:
            self.assertRegex(cell, r"^<div>.*</div>$")


class WritePageTests(unittest.TestCase):
    def test_write_page_creates_parents_and_returns_path(self):
        state = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            out = dashboard.write_page(state, Path(tmp) / "sub" / "fleet.html")
            self.assertIsInstance(out, Path)
            self.assertTrue(out.is_absolute())
            self.assertEqual(
                out.read_text(encoding="utf-8"), dashboard.build_page(state)
            )

    def test_write_page_accepts_str_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = dashboard.write_page(FleetState(), os.path.join(tmp, "d.html"))
            self.assertTrue(out.exists())


class ScriptModeImportTests(unittest.TestCase):
    def test_importable_as_top_level_fleet_package(self):
        # fleet.py run as a script puts tools/ on sys.path and imports `fleet.*`
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "from fleet import dashboard; from fleet.model import FleetState;"
            "print(len(dashboard.build_page(FleetState())) > 0)"
        )
        tools_dir = os.path.join(REPO_ROOT, "tools")
        proc = subprocess.run(
            [sys.executable, "-I", "-c", code, tools_dir],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
