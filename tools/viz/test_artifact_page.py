#!/usr/bin/env python3
"""Tests for tools/viz/artifact_page.py - Artifact-ready HTML chart pages.

Run from the repo root:  py -3.13 tools/viz/test_artifact_page.py

Structure tests are pure string/regex checks. Browser tests share the
single lazily started Playwright chromium of tools.viz.export and skip with a
reason when it is absent; chart-content pixel checks additionally skip when
the pinned CDNs are unreachable (the surface light/dark check still runs).
All output goes to tempfile directories.
"""

import ast
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.request

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.viz import artifact_page as ap
from tools.viz import export, palette

PAGE_PY = os.path.join(REPO_ROOT, "tools", "viz", "artifact_page.py")
ALLOWED_SRC = re.compile(
    r"^https://(cdnjs\.cloudflare\.com/|cdn\.jsdelivr\.net/npm/|unpkg\.com/)"
)
PINNED = {
    "vega": "https://cdn.jsdelivr.net/npm/vega@6.4.0",
    "vega-lite": "https://cdn.jsdelivr.net/npm/vega-lite@6.4.3",
    "vega-embed": "https://cdn.jsdelivr.net/npm/vega-embed@7.3.0",
    "echarts": "https://cdnjs.cloudflare.com/ajax/libs/echarts/6.1.0/echarts.min.js",
    "plotly": "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@4.1.2",
}

BROWSER_SKIP = None
try:
    export.get_browser()
except export.ExportError as exc:
    BROWSER_SKIP = f"Playwright chromium unavailable: {exc}"


def _cdn_reachable():
    for url in (PINNED["vega-embed"], PINNED["echarts"]):
        try:
            with urllib.request.urlopen(url, timeout=8) as resp:
                if resp.status != 200:
                    return False
        except Exception:  # noqa: BLE001 - any network failure means offline
            return False
    return True


CDN_OK = _cdn_reachable() if BROWSER_SKIP is None else False
CDN_SKIP = "pinned CDNs unreachable - chart content cannot load"


def line_rows():
    rows = []
    for i, month in enumerate(
        ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]
    ):
        rows.append({"month": month, "region": "North", "sales": 120 + 15 * i})
        rows.append({"month": month, "region": "South", "sales": 160 - 8 * i})
    return rows


def line_chart():
    return {
        "kind": "vega-lite",
        "title": "Monthly sales",
        "caption": "Two regions, first half of 2026.",
        "height": 300,
        "spec": {
            "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
            "data": {"values": line_rows()},
            "mark": "line",
            "encoding": {
                "x": {
                    "field": "month",
                    "type": "temporal",
                    "timeUnit": "utcyearmonth",
                    "title": "Month",
                },
                "y": {"field": "sales", "type": "quantitative", "title": "Sales"},
                "color": {"field": "region", "type": "nominal", "title": "Region"},
            },
        },
    }


def bar_chart():
    cats = ["Alpha", "Beta", "Gamma", "Delta"]
    return {
        "kind": "echarts",
        "title": "Tickets by team",
        "caption": None,
        "height": 300,
        "spec": {
            "xAxis": {"type": "category", "data": cats},
            "yAxis": {"type": "value"},
            "series": [{"type": "bar", "name": "Tickets", "data": [42, 30, 55, 18]}],
        },
    }


def two_chart_page(**kw):
    return ap.build_page(
        [line_chart(), bar_chart()],
        "Quarterly Ops Review",
        description="Sales and ticket volume.",
        **kw,
    )


def head_of(html):
    return html[: html.index("</head>")]


def css_block(html, selector):
    """Body of the first `selector { ... }` rule (no nested braces)."""
    i = html.index(selector)
    start = html.index("{", i) + 1
    return html[start : html.index("}", start)]


def rgb(hexstr):
    h = hexstr.lstrip("#")
    return tuple(int(h[k : k + 2], 16) for k in (0, 2, 4))


def near(px, target, tol=6):
    return all(abs(a - b) <= tol for a, b in zip(px[:3], target))


# --------------------------------------------------------------------------- source


class Source(unittest.TestCase):
    def test_ascii_and_no_hex_literals(self):
        with open(PAGE_PY, "rb") as fh:
            raw = fh.read()
        raw.decode("ascii")  # raises on non-ASCII
        self.assertIsNone(
            re.search(rb"#[0-9a-fA-F]{6}\b", raw),
            "colors must come from palette tokens, not hex literals",
        )

    def test_no_print_outside_main(self):
        with open(PAGE_PY, encoding="ascii") as fh:
            tree = ast.parse(fh.read())
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            if fn.name == "main":
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "print"
                ):
                    self.fail(f"print() inside {fn.name}")

    def test_import_is_lazy(self):
        code = (
            f"import sys; sys.path.insert(0, {REPO_ROOT!r}); import tools.viz.artifact_page; "
            "bad = [m for m in ('plotly', 'altair', 'playwright', 'pandas') "
            "if m in sys.modules]; print(','.join(bad))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(out, "", f"importing artifact_page pulled in: {out}")


# --------------------------------------------------------------------------- structure


class PageStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = two_chart_page()

    def test_title_is_two_to_four_words(self):
        m = re.search(r"<title>(.*?)</title>", self.html, re.DOTALL)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).strip(), "Quarterly Ops Review")
        self.assertTrue(2 <= len(m.group(1).split()) <= 4)

    def test_doctype_charset_viewport(self):
        self.assertTrue(self.html.lstrip().lower().startswith("<!doctype html>"))
        self.assertIn('<meta charset="utf-8">', self.html)
        self.assertRegex(
            self.html,
            r'<meta name="viewport" content="width=device-width, '
            r'initial-scale=1">',
        )

    def test_one_style_block_with_light_tokens_on_root(self):
        self.assertEqual(self.html.count("<style>"), 1)
        root = css_block(self.html, ":root {")
        for line in palette.css_tokens("light").splitlines():
            self.assertIn(line, root)

    def test_dark_tokens_under_both_selectors(self):
        media = (
            '@media (prefers-color-scheme: dark) {\n  :root:not([data-theme="light"]) {'
        )
        self.assertIn(media, self.html)
        self.assertIn(':root[data-theme="dark"] {', self.html)
        media_body = css_block(self.html, ':root:not([data-theme="light"]) {')
        attr_body = css_block(self.html, ':root[data-theme="dark"] {')
        for line in palette.css_tokens("dark").splitlines():
            self.assertIn(line, media_body)
            self.assertIn(line, attr_body)

    def test_body_background_and_gutters(self):
        body = css_block(self.html, "\nbody {")
        self.assertIn("background: var(--surface-2);", body)
        self.assertIn("color: var(--text-primary);", body)
        self.assertIn("margin: 0;", body)
        self.assertIn("padding: 0 16px;", body)
        self.assertIn("max-width: 1100px", self.html)
        self.assertRegex(self.html, r"\.card \{[^}]*overflow-x: hidden;")
        self.assertRegex(self.html, r"\.card \{[^}]*min-width: 0;")

    def test_scripts_only_pinned_from_allowed_hosts(self):
        srcs = re.findall(r"<script[^>]*\ssrc=\"([^\"]+)\"", self.html)
        self.assertTrue(srcs)
        for src in srcs:
            self.assertRegex(src, ALLOWED_SRC)
            self.assertIn(src, PINNED.values())
        self.assertEqual(
            srcs,
            [
                PINNED["vega"],
                PINNED["vega-lite"],
                PINNED["vega-embed"],
                PINNED["echarts"],
            ],
        )
        self.assertNotIn("plotly", " ".join(srcs))
        self.assertNotRegex(self.html, r"<link[^>]+stylesheet")

    def test_ready_hook_assigned_in_head(self):
        head = head_of(self.html)
        self.assertIn("window.__chartsReady =", head)
        # assigned before any external script so it exists even if a CDN hangs
        self.assertLess(
            head.index("window.__chartsReady ="), head.index("<script src=")
        )

    def test_no_dual_axis_and_size(self):
        self.assertNotIn("yaxis2", self.html)
        self.assertLess(len(self.html.encode("utf-8")), 16 * 1024 * 1024)

    def test_table_view_markup(self):
        self.assertEqual(self.html.count('class="table-toggle"'), 2)
        self.assertEqual(self.html.count("<table"), 2)
        self.assertIn("<thead>", self.html)
        self.assertRegex(self.html, r"thead th \{[^}]*position: sticky;")
        # line rows come from the inline Vega-Lite values; bar rows from the echarts option
        self.assertIn("<td>North</td>", self.html)
        self.assertIn("<td>Gamma</td>", self.html)
        self.assertEqual(self.html.count('href="data:text/csv;charset=utf-8,'), 2)
        self.assertIn('download="monthly-sales.csv"', self.html)

    def test_theme_toggle_button(self):
        self.assertRegex(self.html, r'<button[^>]*id="theme-toggle"')

    def test_captions_and_card_titles(self):
        self.assertIn("Two regions, first half of 2026.", self.html)
        self.assertIn(">Monthly sales</h2>", self.html)
        self.assertIn(">Tickets by team</h2>", self.html)
        self.assertIn("Sales and ticket volume.", self.html)

    def test_mode_default(self):
        self.assertNotRegex(self.html, r"<html[^>]*data-theme")
        dark = two_chart_page(mode_default="dark")
        self.assertRegex(dark, r'<html lang="en" data-theme="dark">')
        with self.assertRaises(ValueError):
            two_chart_page(mode_default="sepia")


# --------------------------------------------------------------------------- spec patching


class VegaLitePatching(unittest.TestCase):
    def test_line_gets_crosshair_tooltip_layer(self):
        spec = ap.prepare_chart(line_chart())["spec"]
        self.assertIn("layer", spec)
        self.assertNotIn("mark", spec)
        self.assertEqual(spec["encoding"]["x"]["field"], "month")
        rules = [
            lyr for lyr in spec["layer"] if ap._mark_type(lyr.get("mark")) == "rule"
        ]
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        sel = rule["params"][0]["select"]
        self.assertEqual(sel["type"], "point")
        self.assertTrue(sel["nearest"])
        self.assertEqual(sel["on"], "pointerover")
        self.assertEqual(rule["transform"][0]["pivot"], "region")
        titles = [t.get("title") for t in rule["encoding"]["tooltip"]]
        self.assertEqual(
            titles, ["Month", "North", "South"]
        )  # one tooltip, every series

    def test_two_series_shows_legend_one_series_hides(self):
        spec = ap.prepare_chart(line_chart())["spec"]
        base = spec["layer"][0]
        self.assertIsNot(base["encoding"]["color"].get("legend", {}), None)
        one = line_chart()
        one["spec"]["data"]["values"] = [
            r for r in line_rows() if r["region"] == "North"
        ]
        base1 = ap.prepare_chart(one)["spec"]["layer"][0]
        self.assertIsNone(base1["encoding"]["color"]["legend"])

    def test_bar_gets_tooltip_and_existing_layer_untouched(self):
        bar = {
            "kind": "vega-lite",
            "title": "t",
            "spec": {
                "data": {"values": [{"a": "x", "b": 1}]},
                "mark": "bar",
                "encoding": {
                    "x": {"field": "a", "type": "nominal"},
                    "y": {"field": "b", "type": "quantitative"},
                },
            },
        }
        spec = ap.prepare_chart(bar)["spec"]
        self.assertEqual(spec["mark"], {"type": "bar", "tooltip": True})
        layered = {
            "kind": "vega-lite",
            "title": "t",
            "spec": {
                "data": {"values": [{"a": 1, "b": 1}]},
                "layer": [
                    {
                        "mark": "line",
                        "encoding": {"x": {"field": "a"}, "y": {"field": "b"}},
                    }
                ],
            },
        }
        self.assertEqual(
            ap.prepare_chart(layered)["spec"]["layer"], layered["spec"]["layer"]
        )

    def test_independent_y_is_removed_with_warning(self):
        dual = line_chart()
        dual["spec"]["resolve"] = {"scale": {"y": "independent"}}
        out = ap.prepare_chart(dual)
        self.assertNotIn("resolve", out["spec"])
        self.assertTrue(any("dual" in w.lower() for w in out["warnings"]))

    def test_altair_datasets_are_read_for_rows(self):
        spec = {
            "data": {"name": "d1"},
            "datasets": {"d1": [{"k": "a", "v": 2}]},
            "mark": "point",
            "encoding": {
                "x": {"field": "k", "type": "nominal"},
                "y": {"field": "v", "type": "quantitative"},
            },
        }
        out = ap.prepare_chart({"kind": "vega-lite", "title": "t", "spec": spec})
        self.assertEqual(out["rows"], [{"k": "a", "v": 2}])


class EchartsPatching(unittest.TestCase):
    def test_bar_tooltip_item_and_single_series_no_legend(self):
        opt = ap.prepare_chart(bar_chart())["spec"]
        self.assertEqual(opt["tooltip"]["trigger"], "item")
        self.assertFalse(opt["legend"]["show"])
        self.assertEqual(opt["series"][0]["barMaxWidth"], 24)

    def test_line_tooltip_axis_and_legend_for_two_series(self):
        opt = {
            "xAxis": {"type": "category", "data": ["a", "b"]},
            "yAxis": {"type": "value"},
            "series": [
                {"type": "line", "name": "s1", "data": [1, 2]},
                {"type": "line", "name": "s2", "data": [2, 1]},
            ],
        }
        out = ap.prepare_chart({"kind": "echarts", "title": "t", "spec": opt})
        self.assertEqual(out["spec"]["tooltip"]["trigger"], "axis")
        self.assertTrue(out["spec"]["legend"]["show"])
        self.assertEqual(
            out["rows"],
            [{"category": "a", "s1": 1, "s2": 2}, {"category": "b", "s1": 2, "s2": 1}],
        )

    def test_second_y_axis_dropped(self):
        opt = {
            "xAxis": {"type": "category", "data": ["a"]},
            "yAxis": [{"type": "value"}, {"type": "value"}],
            "series": [
                {"type": "bar", "data": [1]},
                {"type": "line", "data": [5], "yAxisIndex": 1},
            ],
        }
        out = ap.prepare_chart({"kind": "echarts", "title": "t", "spec": opt})
        self.assertIsInstance(out["spec"]["yAxis"], dict)
        self.assertNotIn("yAxisIndex", out["spec"]["series"][1])
        self.assertTrue(out["warnings"])


class PlotlyPatching(unittest.TestCase):
    def fig(self, dual):
        layout = {"title": {"text": "x"}, "width": 900}
        data = [
            {"type": "scatter", "mode": "lines", "name": "a", "x": [1, 2], "y": [3, 4]},
            {
                "type": "scatter",
                "mode": "lines",
                "name": "b",
                "x": [1, 2],
                "y": [30, 10],
            },
        ]
        if dual:
            data[1]["yaxis"] = "y2"
            layout["yaxis2"] = {"overlaying": "y", "side": "right"}
        return {
            "kind": "plotly",
            "title": "t",
            "spec": {"data": data, "layout": layout},
        }

    def test_dual_axis_dropped_with_visible_warning(self):
        html = ap.build_page([self.fig(True)], "Plotly Dual Test")
        self.assertNotIn("yaxis2", html)
        self.assertNotIn('"y2"', html)
        self.assertIn('class="warning"', html)
        self.assertIn(PINNED["plotly"], html)

    def test_hovermode_legend_width(self):
        out = ap.prepare_chart(self.fig(False))
        lay = out["spec"]["layout"]
        self.assertEqual(lay["hovermode"], "x unified")
        self.assertTrue(lay["showlegend"])
        self.assertNotIn("width", lay)
        self.assertEqual(out["rows"][0], {"series": "a", "x": 1, "y": 3})

    def test_subplot_axis_without_overlaying_is_kept(self):
        fig = self.fig(False)
        fig["spec"]["data"][1]["yaxis"] = "y2"
        fig["spec"]["layout"]["yaxis2"] = {"domain": [0.55, 1]}
        out = ap.prepare_chart(fig)
        self.assertIn("yaxis2", out["spec"]["layout"])
        self.assertEqual(out["warnings"], [])

    def test_from_plotly_and_from_altair(self):
        import altair as alt
        import plotly.graph_objects as go

        p = ap.from_plotly(go.Figure(go.Bar(x=["a", "b"], y=[1, 2])), title="Bars")
        self.assertEqual(p["kind"], "plotly")
        self.assertIn("data", p["spec"])
        a = ap.from_altair(
            alt.Chart(alt.Data(values=[{"a": 1, "b": 2}]))
            .mark_point()
            .encode(x="a:Q", y="b:Q"),
            title="Points",
        )
        self.assertEqual(a["kind"], "vega-lite")
        html = ap.build_page([p, a], "Helper Smoke Test")
        self.assertIn(PINNED["plotly"], html)
        self.assertIn(PINNED["vega-embed"], html)


class Guards(unittest.TestCase):
    def test_title_word_count_enforced(self):
        for bad in ("Sales", "One two three four five"):
            with self.assertRaises(ValueError):
                ap.build_page([bar_chart()], bad)

    def test_unknown_kind_and_empty(self):
        with self.assertRaises(ValueError):
            ap.build_page([{"kind": "d3", "spec": {}}], "Two Words")
        with self.assertRaises(ValueError):
            ap.build_page([], "Two Words")

    def test_large_inline_data_says_aggregate_first(self):
        big = [{"i": i, "label": "x" * 40} for i in range(60000)]
        chart = {
            "kind": "vega-lite",
            "title": "Big",
            "spec": {
                "data": {"values": big},
                "mark": "point",
                "encoding": {"x": {"field": "i", "type": "quantitative"}},
            },
        }
        with self.assertRaisesRegex(ValueError, "aggregate first"):
            ap.build_page([chart], "Too Much Data")

    def test_table_truncates_at_max_rows_with_note(self):
        rows = [{"n": i} for i in range(600)]
        c = bar_chart()
        c["rows"] = rows
        html = ap.build_page([c], "Truncation Check Page")
        tbody = html[html.index("<tbody>") : html.index("</tbody>")]
        self.assertEqual(tbody.count("<tr>"), 500)
        self.assertIn("Showing 500 of 600 rows", html)
        html2 = ap.build_page([c], "Truncation Check Page", table_rows=10)
        self.assertIn("Showing 10 of 600 rows", html2)

    def test_untrusted_labels_escaped(self):
        c = bar_chart()
        evil = "</script><script>alert(1)</script>"
        c["spec"]["xAxis"]["data"][0] = evil
        c["title"] = "<b>t</b>"
        html = ap.build_page([c], "Escaping Check Page")
        self.assertEqual(html.count("<script>alert(1)"), 0)
        self.assertIn("&lt;/script&gt;", html)
        self.assertIn("&lt;b&gt;t&lt;/b&gt;", html)

    def test_csv_link_neutralizes_formulas(self):
        c = bar_chart()
        c["rows"] = [{"name": "=HYPERLINK(1)", "v": -3}]
        html = ap.build_page([c], "Formula Check Page")
        href = re.search(r'href="data:text/csv;charset=utf-8,([^"]+)"', html).group(1)
        from urllib.parse import unquote

        csv_text = unquote(href)
        self.assertIn("'=HYPERLINK(1)", csv_text)
        self.assertIn(",-3", csv_text)

    def test_write_page(self):
        with tempfile.TemporaryDirectory() as d:
            path = ap.write_page(
                [bar_chart()], os.path.join(d, "p.html"), title="Write Page Test"
            )
            self.assertTrue(os.path.isabs(path))
            with open(path, encoding="utf-8") as fh:
                self.assertIn("<title>Write Page Test</title>", fh.read())


# --------------------------------------------------------------------------- browser


@unittest.skipIf(BROWSER_SKIP is not None, BROWSER_SKIP or "")
class Render(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PIL import Image

        cls.Image = Image
        cls.tmp = tempfile.TemporaryDirectory()
        cls.html = two_chart_page()
        cls.png = {}
        for mode in ("light", "dark"):
            cls.png[mode] = export.render_html(
                cls.html,
                os.path.join(cls.tmp.name, f"page-{mode}.png"),
                width=1200,
                height=800,
                mode=mode,
                timeout_ms=45000,
            )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def pixels(self, mode):
        return self.Image.open(self.png[mode]).convert("RGB")

    def test_light_and_dark_surfaces_differ(self):
        light, dark = self.pixels("light"), self.pixels("dark")
        lp, dp = light.getpixel((4, 4)), dark.getpixel((4, 4))
        self.assertTrue(near(lp, rgb(palette.tokens("light")["surface-2"])), lp)
        self.assertTrue(near(dp, rgb(palette.tokens("dark")["surface-2"])), dp)
        self.assertNotEqual(light.tobytes(), dark.tobytes())

    @unittest.skipUnless(CDN_OK, CDN_SKIP)
    def test_charts_drawn_in_series_colors(self):
        for mode in ("light", "dark"):
            img = self.pixels(mode)
            colors = img.getcolors(img.width * img.height)
            for slot in ("series-1", "series-2"):
                target = rgb(palette.tokens(mode)[slot])
                hits = sum(n for n, c in colors if near(c, target, 10))
                self.assertGreater(hits, 150, f"{mode}: too few {slot} pixels ({hits})")

    def page(self, width, height=900, mode="light"):
        ctx = export.get_browser().new_context(
            viewport={"width": width, "height": height}, color_scheme=mode
        )
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.set_content(self.html, wait_until="networkidle", timeout=45000)
        page.wait_for_function(
            "() => window.__chartsReady.then(() => true)", timeout=45000
        )
        return page

    def test_phone_width_layout(self):
        page = self.page(390)
        m = page.evaluate("""() => {
          const cards = [...document.querySelectorAll('.card')].map(
            (c) => c.getBoundingClientRect());
          const cs = getComputedStyle(document.body);
          return {scroll: document.documentElement.scrollWidth, inner: window.innerWidth,
                  pl: cs.paddingLeft, pr: cs.paddingRight, cards: cards.map(
                  (r) => [r.left, r.top, r.right, r.bottom]),
                  errors: window.__chartsErrors || []};
        }""")
        self.assertLessEqual(
            m["scroll"], m["inner"], "horizontal scroll at phone width"
        )
        self.assertEqual((m["pl"], m["pr"]), ("16px", "16px"))
        (l1, _t1, r1, b1), (l2, t2, _r2, _b2) = m["cards"]
        self.assertEqual(l1, 16)
        self.assertEqual(l1, l2)
        self.assertGreaterEqual(t2, b1, "cards must stack at phone width")
        self.assertLessEqual(r1, 390 - 16)
        if CDN_OK:
            self.assertEqual(m["errors"], [])

    def test_toggles(self):
        page = self.page(1200)
        before = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
        page.click("#theme-toggle")
        page.wait_for_function(
            "() => window.__chartsReady.then(() => true)", timeout=45000
        )
        after = page.evaluate(
            "() => [document.documentElement.dataset.theme, "
            "getComputedStyle(document.body).backgroundColor]"
        )
        self.assertEqual(after[0], "dark")
        self.assertNotEqual(before, after[1])
        self.assertTrue(page.is_hidden(".card >> nth=0 >> .table-wrap"))
        page.click(".card >> nth=0 >> .table-toggle")
        self.assertTrue(page.is_visible(".card >> nth=0 >> .table-wrap"))
        self.assertTrue(page.is_hidden(".card >> nth=0 >> .chart"))
        self.assertEqual(
            page.get_attribute(".card >> nth=0 >> .table-toggle", "aria-pressed"),
            "true",
        )


# --------------------------------------------------------------------------- VAL-401


def fold_chart(transform=None, legend=None):
    """Wide rows; series come from a fold (or the given transform), not the inline rows."""
    rows = [
        {"month": r["month"], "North": r["sales"]}
        for r in line_rows()
        if r["region"] == "North"
    ]
    for r, s in zip(rows, [r for r in line_rows() if r["region"] == "South"]):
        r["South"] = s["sales"]
    color = {"field": "series", "type": "nominal"}
    if legend is not None:
        color["legend"] = legend
    return {
        "kind": "vega-lite",
        "title": "Folded sales",
        "height": 300,
        "spec": {
            "data": {"values": rows},
            "transform": transform
            or [{"fold": ["North", "South"], "as": ["series", "value"]}],
            "mark": "line",
            "encoding": {
                "x": {
                    "field": "month",
                    "type": "temporal",
                    "timeUnit": "utcyearmonth",
                    "title": "Month",
                },
                "y": {"field": "value", "type": "quantitative", "title": "Sales"},
                "color": color,
            },
        },
    }


def calc_chart():
    """Long rows; the color field is a calculate output (series names unknown up front)."""
    c = line_chart()
    c["spec"]["transform"] = [{"calculate": "'Region ' + datum.region", "as": "zone"}]
    c["spec"]["encoding"]["color"] = {"field": "zone", "type": "nominal"}
    return c


def stock_lines(names, show_symbol=False, **extra):
    days = [f"2026-01-{d:02d}" for d in range(1, 11)]
    series = [
        {
            "name": n,
            "type": "line",
            "showSymbol": show_symbol,
            "data": [[d, 10 * (i + 1) + j] for j, d in enumerate(days)],
        }
        for i, n in enumerate(names)
    ]
    chart = {
        "kind": "echarts",
        "title": "Price lines",
        "height": 300,
        "spec": {
            "xAxis": {"type": "time"},
            "yAxis": {"type": "value"},
            "series": series,
        },
    }
    chart.update(extra)
    return chart


class Capabilities(unittest.TestCase):
    def test_required_capabilities_constant_and_docstring(self):
        self.assertEqual(ap.REQUIRED_CAPABILITIES, {"downloads": True})
        self.assertIn("REQUIRED_CAPABILITIES", ap.__doc__)
        self.assertIn("capabilities_for", ap.__doc__)

    def test_capabilities_for_pages_with_and_without_csv(self):
        self.assertEqual(
            ap.capabilities_for([line_chart(), bar_chart()]), {"downloads": True}
        )
        no_rows = stock_lines(
            ["A", "B"]
        )  # time axis, no category data -> no table rows
        self.assertEqual(ap.capabilities_for([no_rows]), {})
        no_rows["rows"] = [{"a": 1}]
        self.assertEqual(ap.capabilities_for([no_rows]), {"downloads": True})

    def test_page_keeps_data_href_and_wires_downloads(self):
        html = two_chart_page()
        self.assertEqual(html.count('href="data:text/csv;charset=utf-8,'), 2)
        self.assertIn('use("downloads")', html)


class VegaLiteTransformSeries(unittest.TestCase):
    def rule(self, spec):
        return next(
            lyr for lyr in spec["layer"] if ap._mark_type(lyr.get("mark")) == "rule"
        )

    def test_fold_series_keep_legend_and_pivot_tooltip(self):
        spec = ap.prepare_chart(fold_chart())["spec"]
        legend = spec["layer"][0]["encoding"]["color"]["legend"]
        self.assertIsInstance(legend, dict)
        self.assertEqual(legend.get("symbolType"), "stroke")
        rule = self.rule(spec)
        self.assertEqual(
            rule["transform"][0],
            {"pivot": "series", "value": "value", "groupby": ["month"]},
        )
        titles = [t.get("title") for t in rule["encoding"]["tooltip"]]
        self.assertEqual(titles, ["Month", "North", "South"])
        self.assertEqual(
            spec["transform"][0]["fold"], ["North", "South"]
        )  # stays top-level

    def test_fold_default_key_name_and_author_legend_kept(self):
        tf = [
            {
                "window": [{"op": "mean", "field": "North", "as": "North_7d"}],
                "frame": [-2, 0],
            },
            {"fold": ["North", "North_7d"]},
        ]
        c = fold_chart(transform=tf, legend={"title": None, "orient": "bottom"})
        c["spec"]["encoding"]["color"]["field"] = "key"
        spec = ap.prepare_chart(c)["spec"]
        legend = spec["layer"][0]["encoding"]["color"]["legend"]
        self.assertEqual(legend["orient"], "bottom")
        self.assertIsNone(legend["title"])
        titles = [t.get("title") for t in self.rule(spec)["encoding"]["tooltip"]]
        self.assertEqual(titles[1:], ["North", "North_7d"])

    def test_fold_order_sets_color_sort_unless_author_set(self):
        c = fold_chart(
            transform=[{"fold": ["South", "North"], "as": ["series", "value"]}]
        )
        color = ap.prepare_chart(c)["spec"]["layer"][0]["encoding"]["color"]
        self.assertEqual(color["sort"], ["South", "North"])
        own = fold_chart()
        own["spec"]["encoding"]["color"]["sort"] = "descending"
        color = ap.prepare_chart(own)["spec"]["layer"][0]["encoding"]["color"]
        self.assertEqual(color["sort"], "descending")
        dom = fold_chart()
        dom["spec"]["encoding"]["color"]["scale"] = {"domain": ["South", "North"]}
        color = ap.prepare_chart(dom)["spec"]["layer"][0]["encoding"]["color"]
        self.assertNotIn("sort", color)
        self.assertEqual(color["scale"]["domain"], ["South", "North"])

    def test_single_fold_field_hides_legend(self):
        c = fold_chart(transform=[{"fold": ["North"], "as": ["series", "value"]}])
        base = ap.prepare_chart(c)["spec"]["layer"][0]
        self.assertIsNone(base["encoding"]["color"]["legend"])

    def test_calculate_series_keep_legend_and_list_every_series(self):
        spec = ap.prepare_chart(calc_chart())["spec"]
        self.assertIsInstance(spec["layer"][0]["encoding"]["color"]["legend"], dict)
        rule = self.rule(spec)
        self.assertEqual(rule["transform"][0]["pivot"], "zone")
        self.assertEqual(rule["mark"].get("tooltip"), {"content": "data"})
        self.assertNotIn("tooltip", rule["encoding"])
        off = calc_chart()
        off["spec"]["encoding"]["color"]["legend"] = (
            None  # author's explicit choice stays
        )
        base = ap.prepare_chart(off)["spec"]["layer"][0]
        self.assertIsNone(base["encoding"]["color"]["legend"])


class EchartsLines(unittest.TestCase):
    def test_markerless_lines_get_stroke_legend_icon(self):
        opt = ap.prepare_chart(stock_lines(["AAPL", "MSFT"]))["spec"]
        self.assertEqual(
            (
                opt["legend"]["icon"],
                opt["legend"]["itemWidth"],
                opt["legend"]["itemHeight"],
            ),
            ("rect", 16, 2),
        )
        marked = ap.prepare_chart(stock_lines(["AAPL", "MSFT"], show_symbol=True))[
            "spec"
        ]
        self.assertNotIn("icon", marked["legend"])
        own = stock_lines(["AAPL", "MSFT"])
        own["spec"]["legend"] = {"icon": "circle"}
        self.assertEqual(ap.prepare_chart(own)["spec"]["legend"]["icon"], "circle")

    def test_auto_grid_is_flagged_for_runtime_fit(self):
        self.assertTrue(ap.prepare_chart(stock_lines(["A", "B"]))["fit"])
        own = stock_lines(["A", "B"])
        own["spec"]["grid"] = {"top": 80}
        self.assertFalse(ap.prepare_chart(own)["fit"])
        html = ap.build_page([stock_lines(["A", "B"])], "Fit Flag Page")
        self.assertIn('"fit":true', html)

    def test_end_labels_opt_in(self):
        plain = ap.prepare_chart(stock_lines(["A", "B"]))["spec"]
        self.assertNotIn("endLabel", plain["series"][0])
        opt = ap.prepare_chart(stock_lines(["A", "B"], end_labels=True))["spec"]
        for s in opt["series"]:
            self.assertTrue(s["endLabel"]["show"])
            self.assertEqual(s["endLabel"]["formatter"], "{a}")
            self.assertNotIn(
                "color", s["endLabel"]
            )  # theme token applied at render time
        self.assertEqual(
            ap.prepare_chart(stock_lines(["A", "B"], end_labels=True))["warnings"], []
        )

    def test_end_labels_list_selects_named_series(self):
        out = ap.prepare_chart(stock_lines(["A", "B", "C"], end_labels=["B", "C"]))
        by_name = {s["name"]: s for s in out["spec"]["series"]}
        self.assertNotIn("endLabel", by_name["A"])
        self.assertNotIn("labelLayout", by_name["A"])
        for n in ("B", "C"):
            self.assertTrue(by_name[n]["endLabel"]["show"])
            self.assertEqual(by_name[n]["endLabel"]["formatter"], "{a}")
        self.assertEqual(out["warnings"], [])

    def test_end_labels_series_opt_out_kept_under_true(self):
        chart = stock_lines(["A", "B"], end_labels=True)
        chart["spec"]["series"][1]["endLabel"] = {"show": False}
        out = ap.prepare_chart(chart)
        a, b = out["spec"]["series"]
        self.assertTrue(a["endLabel"]["show"])
        self.assertIs(b["endLabel"]["show"], False)
        self.assertEqual(out["warnings"], [])

    def test_end_labels_off_values_add_nothing(self):
        for value in (False, None, []):
            out = ap.prepare_chart(stock_lines(["A", "B"], end_labels=value))
            self.assertFalse(any("endLabel" in s for s in out["spec"]["series"]), value)
            self.assertEqual(out["warnings"], [], value)

    def test_end_labels_unknown_name_warns(self):
        out = ap.prepare_chart(stock_lines(["A", "B"], end_labels=["A", "Zed"]))
        a, b = out["spec"]["series"]
        self.assertTrue(a["endLabel"]["show"])
        self.assertNotIn("endLabel", b)
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("'Zed'", out["warnings"][0])
        html = ap.build_page(
            [stock_lines(["A", "B"], end_labels=["Zed"])], "End Label Page"
        )
        self.assertIn('<p class="warning" role="note">Warning: end_labels', html)

    def test_end_labels_non_line_series_name_warns(self):
        chart = stock_lines(["A"], end_labels=["Bars"])
        chart["spec"]["series"].append({"name": "Bars", "type": "bar", "data": [1]})
        out = ap.prepare_chart(chart)
        self.assertFalse(any("endLabel" in s for s in out["spec"]["series"]))
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("'Bars'", out["warnings"][0])

    def test_end_labels_invalid_type_warns(self):
        for value in ("A", 1, {"A": True}, ["A", 2]):
            out = ap.prepare_chart(stock_lines(["A", "B"], end_labels=value))
            self.assertFalse(any("endLabel" in s for s in out["spec"]["series"]), value)
            self.assertEqual(len(out["warnings"]), 1, value)
            self.assertIn("end_labels", out["warnings"][0])

    def test_end_labels_ignored_outside_echarts(self):
        chart = {
            "kind": "plotly",
            "title": "t",
            "end_labels": True,
            "spec": {
                "data": [{"type": "scatter", "mode": "lines", "x": [1, 2], "y": [1, 2]}]
            },
        }
        self.assertEqual(ap.prepare_chart(chart)["warnings"], [])


FAKE_CLAUDE = """
(() => {
  const cfg = %s;
  window.__saves = [];
  window.__useCalls = [];
  const ns = Object.freeze({save(req) {
    window.__saves.push({filename: req.filename, data: req.data});
    return cfg.reject ? Promise.reject({code: cfg.reject, message: "x"})
                      : Promise.resolve({status: "saved"});
  }});
  window.claude = {use(name) {
    window.__useCalls.push(name);
    if (cfg.hang) { return new Promise(() => {}); }
    return Promise.resolve(cfg.nullns || name !== "downloads" ? null : ns);
  }};
  window.addEventListener("click", (ev) => {
    if (ev.target.closest && ev.target.closest("a.csv")) {
      window.__lastPrevented = ev.defaultPrevented;
      ev.preventDefault();              // keep the test page from downloading
    }
  });
})();
"""


@unittest.skipIf(BROWSER_SKIP is not None, BROWSER_SKIP or "")
class Downloads(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from urllib.parse import unquote

        cls.html = ap.build_page([bar_chart()], "Downloads Check Page")
        href = re.search(r'href="data:text/csv;charset=utf-8,([^"]+)"', cls.html).group(
            1
        )
        cls.csv = unquote(href)
        # init scripts run on navigation only (not set_content): load from a file URL
        cls.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(cls.tmp.name, "downloads.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(cls.html)
        cls.url = "file:///" + path.replace(os.sep, "/").lstrip("/")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def load_page(self, absent=False, **cfg):
        import json

        ctx = export.get_browser().new_context(viewport={"width": 900, "height": 700})
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        if not absent:
            page.add_init_script(FAKE_CLAUDE % json.dumps(cfg))
        page.goto(self.url, wait_until="domcontentloaded", timeout=45000)
        return page

    def settle(self, page, ms=100):
        page.evaluate(f"() => new Promise((r) => setTimeout(r, {ms}))")

    def test_viewer_click_saves_csv_through_capability(self):
        page = self.load_page()
        page.wait_for_selector(
            'a.csv[data-save="ready"]', state="attached", timeout=15000
        )
        self.assertEqual(
            page.evaluate("() => window.__saves.length"), 0, "save on load"
        )
        page.click("a.csv")
        page.wait_for_function("() => window.__saves.length === 1", timeout=5000)
        saved = page.evaluate("() => window.__saves[0]")
        self.assertEqual(saved["filename"], "tickets-by-team.csv")
        self.assertEqual(saved["data"], self.csv)
        self.assertTrue(page.evaluate("() => window.__lastPrevented"))
        self.assertEqual(page.evaluate("() => window.__useCalls"), ["downloads"])
        self.assertTrue(page.get_attribute("a.csv", "href").startswith("data:text/csv"))
        page.wait_for_selector(
            'a.csv[data-save="saved"]', state="attached", timeout=5000
        )

    def test_null_namespace_keeps_data_link(self):
        page = self.load_page(nullns=True)
        page.wait_for_function("() => window.__useCalls.length === 1", timeout=15000)
        self.settle(page)
        self.assertIsNone(page.get_attribute("a.csv", "data-save"))
        page.click("a.csv")
        self.assertFalse(page.evaluate("() => window.__lastPrevented"))
        self.assertEqual(page.evaluate("() => window.__saves.length"), 0)
        self.assertTrue(page.is_visible("a.csv"))
        self.assertTrue(page.get_attribute("a.csv", "href").startswith("data:text/csv"))

    def test_no_window_claude_keeps_data_link(self):
        page = self.load_page(absent=True)
        self.settle(page)
        self.assertIsNone(page.get_attribute("a.csv", "data-save"))
        self.assertTrue(page.is_visible("a.csv"))

    def test_unavailable_hides_button_and_declined_does_not_retry(self):
        page = self.load_page(reject="unavailable")
        page.wait_for_selector(
            'a.csv[data-save="ready"]', state="attached", timeout=15000
        )
        page.click("a.csv")
        page.wait_for_selector("a.csv", state="hidden", timeout=5000)
        page = self.load_page(reject="declined")
        page.wait_for_selector(
            'a.csv[data-save="ready"]', state="attached", timeout=15000
        )
        page.click("a.csv")
        page.wait_for_selector(
            'a.csv[data-save="declined"]', state="attached", timeout=5000
        )
        self.settle(page)
        self.assertEqual(page.evaluate("() => window.__saves.length"), 1)
        self.assertTrue(page.is_visible("a.csv"))

    def test_pending_use_does_not_block_first_paint(self):
        page = self.load_page(hang=True)
        page.wait_for_function(
            "() => window.__chartsReady.then(() => true)", timeout=45000
        )
        self.assertEqual(page.evaluate("() => window.__useCalls"), ["downloads"])
        self.assertTrue(page.is_visible("a.csv"))


@unittest.skipIf(BROWSER_SKIP is not None, BROWSER_SKIP or "")
@unittest.skipUnless(CDN_OK, CDN_SKIP)
class LiveCharts(unittest.TestCase):
    def load_page(self, charts, width, title="Live Check Page"):
        ctx = export.get_browser().new_context(
            viewport={"width": width, "height": 900}, color_scheme="light"
        )
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.set_content(
            ap.build_page(charts, title), wait_until="networkidle", timeout=45000
        )
        page.wait_for_function(
            "() => window.__chartsReady.then(() => true)", timeout=45000
        )
        self.assertEqual(page.evaluate("() => window.__chartsErrors"), [])
        return page

    def tooltip_keys(self, page):
        box = page.locator("#chart-1-plot").bounding_box()
        page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.6)
        page.wait_for_selector("#vg-tooltip-element.visible", timeout=5000)
        return page.evaluate(
            "() => [...document.querySelectorAll("
            "'#vg-tooltip-element td.key')].map((t) => t.textContent)"
        )

    def test_fold_and_calculate_tooltip_lists_every_series(self):
        page = self.load_page([fold_chart()], 900)
        keys = self.tooltip_keys(page)
        self.assertTrue({"North", "South"} <= set(keys), keys)
        labels = page.evaluate(
            "() => document.querySelectorAll("
            "'#chart-1-plot .role-legend-label text').length"
        )
        self.assertEqual(labels, 2)
        page = self.load_page([calc_chart()], 900)
        keys = self.tooltip_keys(page)
        self.assertTrue({"Region North", "Region South"} <= set(keys), keys)

    def test_fold_series_take_slots_in_fold_order(self):
        # fold order South, North (not alphabetical): South must draw in series-1.
        # South starts at 160, North at 120, so South's first point sits higher.
        c = fold_chart(
            transform=[{"fold": ["South", "North"], "as": ["series", "value"]}]
        )
        for mode in ("light", "dark"):
            ctx = export.get_browser().new_context(
                viewport={"width": 900, "height": 900}, color_scheme=mode
            )
            self.addCleanup(ctx.close)
            page = ctx.new_page()
            page.set_content(
                ap.build_page([c], "Fold Order Page"),
                wait_until="networkidle",
                timeout=45000,
            )
            page.wait_for_function(
                "() => window.__chartsReady.then(() => true)", timeout=45000
            )
            lines = page.evaluate(r"""() => [...document.querySelectorAll(
                '#chart-1-plot g.mark-line path')].map((p) => {
                  const m = /^M\s*([-\d.]+)[ ,]([-\d.]+)/.exec(p.getAttribute('d'));
                  return {stroke: (p.getAttribute('stroke') || '').toLowerCase(),
                          y0: m ? parseFloat(m[2]) : null};
                })""")
            self.assertEqual(len(lines), 2, lines)
            top, low = sorted(lines, key=lambda d: d["y0"])  # smaller y = higher value
            tok = palette.tokens(mode)
            self.assertEqual(top["stroke"], tok["series-1"].lower(), f"{mode}: South")
            self.assertEqual(low["stroke"], tok["series-2"].lower(), f"{mode}: North")

    def option(self, page):
        return page.evaluate("""() => {
          const o = echarts.getInstanceByDom(document.getElementById('chart-1-plot'))
            .getOption();
          return {top: o.grid[0].top, right: o.grid[0].right, type: o.legend[0].type,
                  end: o.series.map((s) => s.endLabel && s.endLabel.color)};
        }""")

    def test_echarts_grid_top_fits_one_or_two_legend_rows(self):
        names = ["Alpha Holdings", "Beta Partners", "Gamma Group", "Delta Works"]
        wide = self.option(self.load_page([stock_lines(names)], 1200))
        narrow = self.option(self.load_page([stock_lines(names)], 390))
        self.assertEqual(wide["top"], 40)
        self.assertGreater(narrow["top"], wide["top"])
        self.assertNotEqual(narrow["type"], "scroll")
        many = [f"Company number {i}" for i in range(8)]
        crowded = self.option(self.load_page([stock_lines(many)], 390))
        self.assertEqual(crowded["type"], "scroll")

    def test_echarts_end_labels_use_text_token(self):
        o = self.option(
            self.load_page([stock_lines(["AAPL", "MSFT"], end_labels=True)], 900)
        )
        t2 = palette.tokens("light")["text-secondary"].lower()
        self.assertEqual([c.lower() for c in o["end"]], [t2, t2])
        self.assertGreater(o["right"], 16)


# --------------------------------------------------------------------------- VAL-404 tokens


def token_lines_vl():
    """Two-layer Vega-Lite line: raw series in token:text-muted (mark color),
    smoothed series in token:series-1 (value-encoded color)."""
    rows = [
        {"day": i, "raw": 10 + (i * 7) % 5, "smooth": 11 + i * 0.2} for i in range(12)
    ]
    x = {"field": "day", "type": "quantitative"}
    return {
        "kind": "vega-lite",
        "title": "Raw and smoothed",
        "height": 300,
        "spec": {
            "data": {"values": rows},
            "layer": [
                {
                    "mark": {
                        "type": "line",
                        "color": ap.token("text-muted"),
                        "strokeWidth": 1,
                    },
                    "encoding": {"x": x, "y": {"field": "raw", "type": "quantitative"}},
                },
                {
                    "mark": "line",
                    "encoding": {
                        "x": x,
                        "y": {"field": "smooth", "type": "quantitative"},
                        "color": {"value": "token:series-1"},
                    },
                },
            ],
        },
    }


def token_lines_echarts():
    chart = stock_lines(["Raw", "Smoothed"])
    chart["spec"]["series"][0]["lineStyle"] = {
        "color": ap.token("text-muted"),
        "width": 1,
    }
    chart["spec"]["series"][1]["lineStyle"] = {"color": "token:series-1"}
    return chart


class TokenReferences(unittest.TestCase):
    def test_token_helper_returns_reference(self):
        self.assertEqual(ap.token("text-muted"), "token:text-muted")
        self.assertEqual(ap.token("series-1"), "token:series-1")
        self.assertIn("token(", ap.__doc__)
        self.assertIn("token:", ap.__doc__)

    def test_unknown_token_raises_naming_it(self):
        with self.assertRaisesRegex(ValueError, "no-such-token"):
            ap.token("no-such-token")
        bad = token_lines_vl()
        bad["spec"]["layer"][1]["encoding"]["color"]["value"] = "token:no-such-token"
        with self.assertRaisesRegex(ValueError, "no-such-token"):
            ap.build_page([bad], "Token Check Page")
        eb = token_lines_echarts()
        eb["spec"]["series"][0]["lineStyle"]["color"] = "token:serie-1"
        with self.assertRaisesRegex(ValueError, "serie-1"):
            ap.build_page([eb], "Token Check Page")

    def test_known_tokens_pass_through_to_payload(self):
        html = ap.build_page(
            [token_lines_vl(), token_lines_echarts()], "Token Check Page"
        )
        self.assertIn('"token:text-muted"', html)
        self.assertIn('"token:series-1"', html)

    def test_gray_token_is_declared_both_modes(self):
        # VAL-502: token('gray') is valid and the page declares --gray per theme scope.
        self.assertIn("gray", ap.TOKEN_NAMES)
        self.assertEqual(ap.token("gray"), "token:gray")
        html = ap.build_page([gray_emphasis_vl()], "Token Check Page")
        self.assertIn('"token:gray"', html)
        for mode in ("light", "dark"):
            self.assertIn(f"--gray: {palette.gray(mode)};", html)


def gray_emphasis_vl():
    """'1 hue + gray' emphasis: context series in token:gray, the highlight in token:series-1."""
    rows = [{"day": i, "ctx": 10 + (i * 7) % 5, "hi": 11 + i * 0.2} for i in range(12)]
    x = {"field": "day", "type": "quantitative"}
    return {
        "kind": "vega-lite",
        "title": "Highlight one series",
        "height": 300,
        "spec": {
            "data": {"values": rows},
            "layer": [
                {
                    "mark": {"type": "line", "color": ap.token("gray")},
                    "encoding": {"x": x, "y": {"field": "ctx", "type": "quantitative"}},
                },
                {
                    "mark": {"type": "line", "color": ap.token("series-1")},
                    "encoding": {"x": x, "y": {"field": "hi", "type": "quantitative"}},
                },
            ],
        },
    }


@unittest.skipIf(BROWSER_SKIP is not None, BROWSER_SKIP or "")
@unittest.skipUnless(CDN_OK, CDN_SKIP)
class LiveTokens(unittest.TestCase):
    STROKES = r"""(sel) => {
      const ctx = document.createElement('canvas').getContext('2d');
      const norm = (c) => { ctx.fillStyle = '#000'; ctx.fillStyle = c; return ctx.fillStyle; };
      const tok = (n) => norm(getComputedStyle(document.documentElement)
                                .getPropertyValue('--' + n).trim());
      return {strokes: [...document.querySelectorAll(sel)].map(
                (p) => norm(p.getAttribute('stroke') || 'none')),
              muted: tok('text-muted'), s1: tok('series-1'),
              raw: [...document.querySelectorAll(sel)].map((p) => p.getAttribute('stroke'))};
    }"""

    def load_page(self, charts, mode):
        ctx = export.get_browser().new_context(
            viewport={"width": 900, "height": 900}, color_scheme=mode
        )
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.set_content(
            ap.build_page(charts, "Token Render Page"),
            wait_until="networkidle",
            timeout=45000,
        )
        page.wait_for_function(
            "() => window.__chartsReady.then(() => true)", timeout=45000
        )
        self.assertEqual(page.evaluate("() => window.__chartsErrors"), [])
        return page

    def toggle(self, page):
        page.click("#theme-toggle")
        page.wait_for_function(
            "() => window.__chartsReady.then(() => true)", timeout=45000
        )

    def test_vega_lite_layers_stroke_in_tokens_light_and_dark(self):
        sel = "#chart-1-plot g.mark-line path"
        for mode in ("light", "dark"):
            page = self.load_page([token_lines_vl()], mode)
            seen = {}
            for step in (mode, "toggled"):
                m = page.evaluate(self.STROKES, sel)
                self.assertEqual(len(m["strokes"]), 2, m)
                self.assertNotIn(None, m["raw"], m)
                self.assertFalse(any(s.startswith("token:") for s in m["raw"]), m)
                self.assertEqual(
                    m["strokes"], [m["muted"], m["s1"]], f"{mode}/{step}: {m}"
                )
                seen[step] = m["s1"]
                if step == mode:
                    self.toggle(page)  # theme change re-resolves the references
            self.assertNotEqual(
                seen[mode], seen["toggled"], "series-1 must differ by mode"
            )
            want = palette.tokens(mode)["series-1"].lower()
            self.assertEqual(seen[mode], want)

    def test_echarts_line_style_tokens_light_and_dark(self):
        sel = "#chart-1-plot svg path"
        for mode in ("light", "dark"):
            page = self.load_page([token_lines_echarts()], mode)
            for _step in (mode, "toggled"):
                m = page.evaluate(self.STROKES, sel)
                self.assertIn(m["muted"], m["strokes"], m)
                self.assertIn(m["s1"], m["strokes"], m)
                self.assertFalse(
                    any((s or "").startswith("token:") for s in m["raw"]), m
                )
                colors = page.evaluate("""() => echarts.getInstanceByDom(
                    document.getElementById('chart-1-plot')).getOption().series.map(
                    (s) => s.lineStyle.color)""")
                tok = page.evaluate("""() => ['text-muted', 'series-1'].map((n) =>
                    getComputedStyle(document.documentElement).getPropertyValue('--' + n)
                    .trim())""")
                self.assertEqual(colors, tok)
                if _step == mode:
                    self.toggle(page)

    def test_gray_token_resolves_per_theme(self):
        sel = "#chart-1-plot g.mark-line path"
        js = r"""(sel) => {
          const ctx = document.createElement('canvas').getContext('2d');
          const norm = (c) => { ctx.fillStyle = '#000'; ctx.fillStyle = c; return ctx.fillStyle; };
          const root = document.documentElement;
          return {strokes: [...document.querySelectorAll(sel)].map(
                    (p) => norm(p.getAttribute('stroke') || 'none')),
                  gray: norm(getComputedStyle(root).getPropertyValue('--gray').trim()),
                  theme: root.getAttribute('data-theme')};
        }"""
        for mode in ("light", "dark"):
            page = self.load_page([gray_emphasis_vl()], mode)
            for step in (mode, "toggled"):
                m = page.evaluate(js, sel)
                shown = (
                    mode if step == mode else ("dark" if mode == "light" else "light")
                )
                self.assertEqual(
                    m["gray"], palette.gray(shown).lower(), f"{mode}/{step}: {m}"
                )
                self.assertEqual(len(m["strokes"]), 2, m)
                self.assertEqual(m["strokes"][0], m["gray"], f"{mode}/{step}: {m}")
                self.assertNotEqual(m["strokes"][1], m["gray"], m)
                if step == mode:
                    self.toggle(page)

    def test_unresolvable_token_stays_and_reports(self):
        html = ap.build_page([token_lines_vl()], "Token Render Page")
        html = html.replace(
            '"token:series-1"', '"token:gone-away"'
        )  # bypass the Python check
        ctx = export.get_browser().new_context(viewport={"width": 900, "height": 900})
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.set_content(html, wait_until="networkidle", timeout=45000)
        page.wait_for_function(
            "() => window.__chartsReady.then(() => true)", timeout=45000
        )
        errors = page.evaluate("() => window.__chartsErrors")
        self.assertTrue(any("gone-away" in e for e in errors), errors)


# VAL-612 characterization: full prepare_chart output for ECharts options,
# captured on HEAD 2aff883 before _prepare_echarts was split into helpers.
_CAT = {"type": "category", "data": ["a", "b", "c"]}
_VAL = {"type": "value"}
ECHARTS_CASES = {
    "two-lines-end-labels-all": {
        "kind": "echarts",
        "title": "t",
        "end_labels": True,
        "spec": {
            "xAxis": _CAT,
            "yAxis": _VAL,
            "series": [
                {"type": "line", "name": "s1", "data": [1, 2, 3]},
                {"type": "line", "name": "s2", "data": [3, 2, 1], "showSymbol": False},
            ],
        },
    },
    "end-labels-list-with-unknown": {
        "kind": "echarts",
        "end_labels": ["s1", "ghost"],
        "spec": {
            "xAxis": _CAT,
            "yAxis": _VAL,
            "series": [
                {"type": "line", "name": "s1", "data": [1, 2, 3], "symbol": "circle"},
                {"type": "line", "name": "s2", "data": [3, 2, 1]},
                {
                    "type": "line",
                    "name": "s3",
                    "data": [0, 0, 0],
                    "endLabel": {"show": False},
                },
            ],
        },
    },
    "no-end-labels-default": {
        "kind": "echarts",
        "spec": {
            "xAxis": _CAT,
            "yAxis": _VAL,
            "series": [
                {"type": "line", "name": "s1", "data": [1, 2, 3]},
                {"type": "line", "data": [{"value": 4}, {"value": 5}, 6, 7]},
            ],
        },
    },
    "horizontal-bar-own-grid-tooltip": {
        "kind": "echarts",
        "spec": {
            "xAxis": {"type": "value"},
            "yAxis": {"type": "category", "data": ["x", "y"]},
            "grid": {"left": 1},
            "tooltip": {"show": False},
            "series": [{"type": "bar", "data": [5, 6], "itemStyle": {"color": "red"}}],
        },
    },
    "vertical-bars-two-series": {
        "kind": "echarts",
        "spec": {
            "xAxis": [_CAT],
            "yAxis": [_VAL],
            "series": [
                {"type": "bar", "name": "b1", "data": [1, 2, 3], "barMaxWidth": 10},
                {"type": "bar", "name": "b2", "data": [1, 2]},
                "not-a-series",
            ],
        },
    },
    "dual-axis-dropped": {
        "kind": "echarts",
        "spec": {
            "xAxis": _CAT,
            "yAxis": [_VAL, {"type": "value", "name": "right"}],
            "series": [
                {"type": "bar", "name": "b", "data": [1, 2, 3]},
                {"type": "line", "name": "l", "data": [5, 6, 7], "yAxisIndex": 1},
            ],
        },
    },
    "legend-list-untouched": {
        "kind": "echarts",
        "spec": {
            "legend": [{"top": 5}],
            "xAxis": _CAT,
            "yAxis": _VAL,
            "series": [{"type": "scatter", "data": [1]}, {"type": "pie", "data": [2]}],
        },
    },
    "legend-dict-kept": {
        "kind": "echarts",
        "spec": {
            "legend": {"top": 30, "icon": "circle"},
            "xAxis": _CAT,
            "yAxis": _VAL,
            "series": [
                {"type": "line", "name": "a", "data": [1]},
                {"type": "line", "name": "b", "data": [2]},
            ],
        },
    },
    "dataset-dict-rows": {
        "kind": "echarts",
        "spec": {
            "dataset": {"source": [{"k": "a", "v": 1}, {"k": "b", "v": 2}]},
            "xAxis": {"type": "category"},
            "yAxis": _VAL,
            "series": [{"type": "bar"}],
        },
    },
    "dataset-list-rows": {
        "kind": "echarts",
        "spec": {
            "dataset": {"source": [["k", 2020], ["a", 1], "junk", ["b", 2]]},
            "xAxis": {"type": "category"},
            "yAxis": _VAL,
            "series": [{"type": "line"}],
        },
    },
    "dataset-empty-falls-back-to-axis": {
        "kind": "echarts",
        "spec": {
            "dataset": {"source": []},
            "xAxis": {"type": "value"},
            "yAxis": {"type": "category", "data": ["p", "q"]},
            "series": [{"type": "bar", "data": [3, 4]}],
        },
    },
    "no-series": {"kind": "echarts", "spec": {"xAxis": _CAT, "yAxis": _VAL}},
    "no-axes": {
        "kind": "echarts",
        "spec": {"series": [{"type": "pie", "data": [1, 2]}]},
    },
}

ECHARTS_GOLDEN = {
    "two-lines-end-labels-all": {
        "kind": "echarts",
        "spec": {
            "xAxis": {"type": "category", "data": ["a", "b", "c"]},
            "yAxis": {"type": "value"},
            "series": [
                {
                    "type": "line",
                    "name": "s1",
                    "data": [1, 2, 3],
                    "lineStyle": {"width": 2},
                    "symbolSize": 8,
                    "endLabel": {"show": True, "formatter": "{a}"},
                    "labelLayout": {"moveOverlap": "shiftY"},
                },
                {
                    "type": "line",
                    "name": "s2",
                    "data": [3, 2, 1],
                    "showSymbol": False,
                    "lineStyle": {"width": 2},
                    "symbolSize": 8,
                    "endLabel": {"show": True, "formatter": "{a}"},
                    "labelLayout": {"moveOverlap": "shiftY"},
                },
            ],
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "line"}},
            "legend": {"show": True, "top": 0},
            "grid": {
                "left": 8,
                "right": 16,
                "top": 40,
                "bottom": 8,
                "containLabel": True,
            },
        },
        "rows": [
            {"category": "a", "s1": 1, "s2": 3},
            {"category": "b", "s1": 2, "s2": 2},
            {"category": "c", "s1": 3, "s2": 1},
        ],
        "warnings": [],
        "title": "t",
        "caption": None,
        "height": 320,
        "fit": True,
    },
    "end-labels-list-with-unknown": {
        "kind": "echarts",
        "spec": {
            "xAxis": {"type": "category", "data": ["a", "b", "c"]},
            "yAxis": {"type": "value"},
            "series": [
                {
                    "type": "line",
                    "name": "s1",
                    "data": [1, 2, 3],
                    "symbol": "circle",
                    "lineStyle": {"width": 2},
                    "symbolSize": 8,
                    "endLabel": {"show": True, "formatter": "{a}"},
                    "labelLayout": {"moveOverlap": "shiftY"},
                },
                {
                    "type": "line",
                    "name": "s2",
                    "data": [3, 2, 1],
                    "lineStyle": {"width": 2},
                    "symbolSize": 8,
                },
                {
                    "type": "line",
                    "name": "s3",
                    "data": [0, 0, 0],
                    "endLabel": {"show": False},
                    "lineStyle": {"width": 2},
                    "symbolSize": 8,
                },
            ],
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "line"}},
            "legend": {"show": True, "top": 0},
            "grid": {
                "left": 8,
                "right": 16,
                "top": 40,
                "bottom": 8,
                "containLabel": True,
            },
        },
        "rows": [
            {"category": "a", "s1": 1, "s2": 3, "s3": 0},
            {"category": "b", "s1": 2, "s2": 2, "s3": 0},
            {"category": "c", "s1": 3, "s2": 1, "s3": 0},
        ],
        "warnings": [
            "end_labels: no line series named 'ghost'; line series are 's1', 's2', 's3'."
        ],
        "title": "",
        "caption": None,
        "height": 320,
        "fit": True,
    },
    "no-end-labels-default": {
        "kind": "echarts",
        "spec": {
            "xAxis": {"type": "category", "data": ["a", "b", "c"]},
            "yAxis": {"type": "value"},
            "series": [
                {
                    "type": "line",
                    "name": "s1",
                    "data": [1, 2, 3],
                    "lineStyle": {"width": 2},
                    "symbolSize": 8,
                },
                {
                    "type": "line",
                    "data": [{"value": 4}, {"value": 5}, 6, 7],
                    "lineStyle": {"width": 2},
                    "symbolSize": 8,
                },
            ],
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "line"}},
            "legend": {"show": True, "top": 0},
            "grid": {
                "left": 8,
                "right": 16,
                "top": 40,
                "bottom": 8,
                "containLabel": True,
            },
        },
        "rows": [
            {"category": "a", "s1": 1, "series 2": 4},
            {"category": "b", "s1": 2, "series 2": 5},
            {"category": "c", "s1": 3, "series 2": 6},
        ],
        "warnings": [],
        "title": "",
        "caption": None,
        "height": 320,
        "fit": True,
    },
    "horizontal-bar-own-grid-tooltip": {
        "kind": "echarts",
        "spec": {
            "xAxis": {"type": "value"},
            "yAxis": {"type": "category", "data": ["x", "y"]},
            "grid": {"left": 1},
            "tooltip": {"show": False},
            "series": [
                {
                    "type": "bar",
                    "data": [5, 6],
                    "itemStyle": {"color": "red", "borderRadius": [0, 4, 4, 0]},
                    "barMaxWidth": 24,
                }
            ],
            "legend": {"show": False},
        },
        "rows": [{"category": "x", "series 1": 5}, {"category": "y", "series 1": 6}],
        "warnings": [],
        "title": "",
        "caption": None,
        "height": 320,
        "fit": False,
    },
    "vertical-bars-two-series": {
        "kind": "echarts",
        "spec": {
            "xAxis": [{"type": "category", "data": ["a", "b", "c"]}],
            "yAxis": [{"type": "value"}],
            "series": [
                {
                    "type": "bar",
                    "name": "b1",
                    "data": [1, 2, 3],
                    "barMaxWidth": 10,
                    "itemStyle": {"borderRadius": [4, 4, 0, 0]},
                },
                {
                    "type": "bar",
                    "name": "b2",
                    "data": [1, 2],
                    "barMaxWidth": 24,
                    "itemStyle": {"borderRadius": [4, 4, 0, 0]},
                },
                "not-a-series",
            ],
            "tooltip": {"trigger": "item"},
            "legend": {"show": True, "top": 0},
            "grid": {
                "left": 8,
                "right": 16,
                "top": 40,
                "bottom": 8,
                "containLabel": True,
            },
        },
        "rows": [
            {"category": "a", "b1": 1, "b2": 1},
            {"category": "b", "b1": 2, "b2": 2},
            {"category": "c", "b1": 3},
        ],
        "warnings": [],
        "title": "",
        "caption": None,
        "height": 320,
        "fit": True,
    },
    "dual-axis-dropped": {
        "kind": "echarts",
        "spec": {
            "xAxis": {"type": "category", "data": ["a", "b", "c"]},
            "yAxis": {"type": "value"},
            "series": [
                {
                    "type": "bar",
                    "name": "b",
                    "data": [1, 2, 3],
                    "barMaxWidth": 24,
                    "itemStyle": {"borderRadius": [4, 4, 0, 0]},
                },
                {
                    "type": "line",
                    "name": "l",
                    "data": [5, 6, 7],
                    "lineStyle": {"width": 2},
                    "symbolSize": 8,
                },
            ],
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "line"}},
            "legend": {"show": True, "top": 0},
            "grid": {
                "left": 8,
                "right": 16,
                "top": 40,
                "bottom": 8,
                "containLabel": True,
            },
        },
        "rows": [
            {"category": "a", "b": 1, "l": 5},
            {"category": "b", "b": 2, "l": 6},
            {"category": "c", "b": 3, "l": 7},
        ],
        "warnings": [
            "Dual y-axis removed: two scales on one plot mislead. Plot the second measure as its own chart or index both to a common base."
        ],
        "title": "",
        "caption": None,
        "height": 320,
        "fit": True,
    },
    "legend-list-untouched": {
        "kind": "echarts",
        "spec": {
            "legend": [{"top": 5}],
            "xAxis": {"type": "category", "data": ["a", "b", "c"]},
            "yAxis": {"type": "value"},
            "series": [{"type": "scatter", "data": [1]}, {"type": "pie", "data": [2]}],
            "tooltip": {"trigger": "item"},
            "grid": {
                "left": 8,
                "right": 16,
                "top": 40,
                "bottom": 8,
                "containLabel": True,
            },
        },
        "rows": [
            {"category": "a", "series 1": 1, "series 2": 2},
            {"category": "b"},
            {"category": "c"},
        ],
        "warnings": [],
        "title": "",
        "caption": None,
        "height": 320,
        "fit": True,
    },
    "legend-dict-kept": {
        "kind": "echarts",
        "spec": {
            "legend": {"top": 30, "icon": "circle", "show": True},
            "xAxis": {"type": "category", "data": ["a", "b", "c"]},
            "yAxis": {"type": "value"},
            "series": [
                {
                    "type": "line",
                    "name": "a",
                    "data": [1],
                    "lineStyle": {"width": 2},
                    "symbolSize": 8,
                },
                {
                    "type": "line",
                    "name": "b",
                    "data": [2],
                    "lineStyle": {"width": 2},
                    "symbolSize": 8,
                },
            ],
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "line"}},
            "grid": {
                "left": 8,
                "right": 16,
                "top": 40,
                "bottom": 8,
                "containLabel": True,
            },
        },
        "rows": [
            {"category": "a", "a": 1, "b": 2},
            {"category": "b"},
            {"category": "c"},
        ],
        "warnings": [],
        "title": "",
        "caption": None,
        "height": 320,
        "fit": True,
    },
    "dataset-dict-rows": {
        "kind": "echarts",
        "spec": {
            "dataset": {"source": [{"k": "a", "v": 1}, {"k": "b", "v": 2}]},
            "xAxis": {"type": "category"},
            "yAxis": {"type": "value"},
            "series": [
                {
                    "type": "bar",
                    "barMaxWidth": 24,
                    "itemStyle": {"borderRadius": [4, 4, 0, 0]},
                }
            ],
            "tooltip": {"trigger": "item"},
            "legend": {"show": False},
            "grid": {
                "left": 8,
                "right": 16,
                "top": 16,
                "bottom": 8,
                "containLabel": True,
            },
        },
        "rows": [{"k": "a", "v": 1}, {"k": "b", "v": 2}],
        "warnings": [],
        "title": "",
        "caption": None,
        "height": 320,
        "fit": True,
    },
    "dataset-list-rows": {
        "kind": "echarts",
        "spec": {
            "dataset": {"source": [["k", 2020], ["a", 1], "junk", ["b", 2]]},
            "xAxis": {"type": "category"},
            "yAxis": {"type": "value"},
            "series": [{"type": "line", "lineStyle": {"width": 2}, "symbolSize": 8}],
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "line"}},
            "legend": {"show": False},
            "grid": {
                "left": 8,
                "right": 16,
                "top": 16,
                "bottom": 8,
                "containLabel": True,
            },
        },
        "rows": [{"k": "a", "2020": 1}, {"k": "b", "2020": 2}],
        "warnings": [],
        "title": "",
        "caption": None,
        "height": 320,
        "fit": True,
    },
    "dataset-empty-falls-back-to-axis": {
        "kind": "echarts",
        "spec": {
            "dataset": {"source": []},
            "xAxis": {"type": "value"},
            "yAxis": {"type": "category", "data": ["p", "q"]},
            "series": [
                {
                    "type": "bar",
                    "data": [3, 4],
                    "barMaxWidth": 24,
                    "itemStyle": {"borderRadius": [0, 4, 4, 0]},
                }
            ],
            "tooltip": {"trigger": "item"},
            "legend": {"show": False},
            "grid": {
                "left": 8,
                "right": 16,
                "top": 16,
                "bottom": 8,
                "containLabel": True,
            },
        },
        "rows": [{"category": "p", "series 1": 3}, {"category": "q", "series 1": 4}],
        "warnings": [],
        "title": "",
        "caption": None,
        "height": 320,
        "fit": True,
    },
    "no-series": {
        "kind": "echarts",
        "spec": {
            "xAxis": {"type": "category", "data": ["a", "b", "c"]},
            "yAxis": {"type": "value"},
            "tooltip": {"trigger": "item"},
            "legend": {"show": False},
            "grid": {
                "left": 8,
                "right": 16,
                "top": 16,
                "bottom": 8,
                "containLabel": True,
            },
        },
        "rows": None,
        "warnings": [],
        "title": "",
        "caption": None,
        "height": 320,
        "fit": True,
    },
    "no-axes": {
        "kind": "echarts",
        "spec": {
            "series": [{"type": "pie", "data": [1, 2]}],
            "tooltip": {"trigger": "item"},
            "legend": {"show": False},
            "grid": {
                "left": 8,
                "right": 16,
                "top": 16,
                "bottom": 8,
                "containLabel": True,
            },
        },
        "rows": None,
        "warnings": [],
        "title": "",
        "caption": None,
        "height": 320,
        "fit": True,
    },
}


class EChartsPreparePinned(unittest.TestCase):
    def test_prepare_echarts_output_pinned(self):
        self.assertEqual(sorted(ECHARTS_GOLDEN), sorted(ECHARTS_CASES))
        for case, chart in ECHARTS_CASES.items():
            with self.subTest(case=case):
                self.assertEqual(ap.prepare_chart(chart), ECHARTS_GOLDEN[case])

    def test_input_not_mutated(self):
        import copy

        chart = ECHARTS_CASES["dual-axis-dropped"]
        before = copy.deepcopy(chart)
        ap.prepare_chart(chart)
        self.assertEqual(chart, before)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        unittest.main(verbosity=2)
    finally:
        export.close_browser()
