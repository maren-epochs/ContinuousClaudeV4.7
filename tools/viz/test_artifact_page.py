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
    r"^https://(cdnjs\.cloudflare\.com/|cdn\.jsdelivr\.net/npm/|unpkg\.com/)")
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
    for i, month in enumerate(["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]):
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
                "x": {"field": "month", "type": "temporal", "timeUnit": "utcyearmonth",
                      "title": "Month"},
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
    return ap.build_page([line_chart(), bar_chart()], "Quarterly Ops Review",
                         description="Sales and ticket volume.", **kw)


def head_of(html):
    return html[: html.index("</head>")]


def css_block(html, selector):
    """Body of the first `selector { ... }` rule (no nested braces)."""
    i = html.index(selector)
    start = html.index("{", i) + 1
    return html[start: html.index("}", start)]


def rgb(hexstr):
    h = hexstr.lstrip("#")
    return tuple(int(h[k:k + 2], 16) for k in (0, 2, 4))


def near(px, target, tol=6):
    return all(abs(a - b) <= tol for a, b in zip(px[:3], target))


# --------------------------------------------------------------------------- source


class Source(unittest.TestCase):
    def test_ascii_and_no_hex_literals(self):
        with open(PAGE_PY, "rb") as fh:
            raw = fh.read()
        raw.decode("ascii")  # raises on non-ASCII
        self.assertIsNone(re.search(rb"#[0-9a-fA-F]{6}\b", raw),
                          "colors must come from palette tokens, not hex literals")

    def test_no_print_outside_main(self):
        with open(PAGE_PY, encoding="ascii") as fh:
            tree = ast.parse(fh.read())
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            if fn.name == "main":
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print":
                    self.fail(f"print() inside {fn.name}")

    def test_import_is_lazy(self):
        code = (f"import sys; sys.path.insert(0, {REPO_ROOT!r}); import tools.viz.artifact_page; "
                "bad = [m for m in ('plotly', 'altair', 'playwright', 'pandas') "
                "if m in sys.modules]; print(','.join(bad))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             check=True).stdout.strip()
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
        self.assertRegex(self.html, r'<meta name="viewport" content="width=device-width, '
                                    r'initial-scale=1">')

    def test_one_style_block_with_light_tokens_on_root(self):
        self.assertEqual(self.html.count("<style>"), 1)
        root = css_block(self.html, ":root {")
        for line in palette.css_tokens("light").splitlines():
            self.assertIn(line, root)

    def test_dark_tokens_under_both_selectors(self):
        media = '@media (prefers-color-scheme: dark) {\n  :root:not([data-theme="light"]) {'
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
        self.assertEqual(srcs, [PINNED["vega"], PINNED["vega-lite"], PINNED["vega-embed"],
                                PINNED["echarts"]])
        self.assertNotIn("plotly", " ".join(srcs))
        self.assertNotRegex(self.html, r"<link[^>]+stylesheet")

    def test_ready_hook_assigned_in_head(self):
        head = head_of(self.html)
        self.assertIn("window.__chartsReady =", head)
        # assigned before any external script so it exists even if a CDN hangs
        self.assertLess(head.index("window.__chartsReady ="), head.index("<script src="))

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
        rules = [lyr for lyr in spec["layer"] if ap._mark_type(lyr.get("mark")) == "rule"]
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        sel = rule["params"][0]["select"]
        self.assertEqual(sel["type"], "point")
        self.assertTrue(sel["nearest"])
        self.assertEqual(sel["on"], "pointerover")
        self.assertEqual(rule["transform"][0]["pivot"], "region")
        titles = [t.get("title") for t in rule["encoding"]["tooltip"]]
        self.assertEqual(titles, ["Month", "North", "South"])  # one tooltip, every series

    def test_two_series_shows_legend_one_series_hides(self):
        spec = ap.prepare_chart(line_chart())["spec"]
        base = spec["layer"][0]
        self.assertIsNot(base["encoding"]["color"].get("legend", {}), None)
        one = line_chart()
        one["spec"]["data"]["values"] = [r for r in line_rows() if r["region"] == "North"]
        base1 = ap.prepare_chart(one)["spec"]["layer"][0]
        self.assertIsNone(base1["encoding"]["color"]["legend"])

    def test_bar_gets_tooltip_and_existing_layer_untouched(self):
        bar = {"kind": "vega-lite", "title": "t", "spec": {
            "data": {"values": [{"a": "x", "b": 1}]}, "mark": "bar",
            "encoding": {"x": {"field": "a", "type": "nominal"},
                         "y": {"field": "b", "type": "quantitative"}}}}
        spec = ap.prepare_chart(bar)["spec"]
        self.assertEqual(spec["mark"], {"type": "bar", "tooltip": True})
        layered = {"kind": "vega-lite", "title": "t", "spec": {
            "data": {"values": [{"a": 1, "b": 1}]},
            "layer": [{"mark": "line", "encoding": {"x": {"field": "a"}, "y": {"field": "b"}}}]}}
        self.assertEqual(ap.prepare_chart(layered)["spec"]["layer"], layered["spec"]["layer"])

    def test_independent_y_is_removed_with_warning(self):
        dual = line_chart()
        dual["spec"]["resolve"] = {"scale": {"y": "independent"}}
        out = ap.prepare_chart(dual)
        self.assertNotIn("resolve", out["spec"])
        self.assertTrue(any("dual" in w.lower() for w in out["warnings"]))

    def test_altair_datasets_are_read_for_rows(self):
        spec = {"data": {"name": "d1"}, "datasets": {"d1": [{"k": "a", "v": 2}]},
                "mark": "point", "encoding": {"x": {"field": "k", "type": "nominal"},
                                              "y": {"field": "v", "type": "quantitative"}}}
        out = ap.prepare_chart({"kind": "vega-lite", "title": "t", "spec": spec})
        self.assertEqual(out["rows"], [{"k": "a", "v": 2}])


class EchartsPatching(unittest.TestCase):
    def test_bar_tooltip_item_and_single_series_no_legend(self):
        opt = ap.prepare_chart(bar_chart())["spec"]
        self.assertEqual(opt["tooltip"]["trigger"], "item")
        self.assertFalse(opt["legend"]["show"])
        self.assertEqual(opt["series"][0]["barMaxWidth"], 24)

    def test_line_tooltip_axis_and_legend_for_two_series(self):
        opt = {"xAxis": {"type": "category", "data": ["a", "b"]}, "yAxis": {"type": "value"},
               "series": [{"type": "line", "name": "s1", "data": [1, 2]},
                          {"type": "line", "name": "s2", "data": [2, 1]}]}
        out = ap.prepare_chart({"kind": "echarts", "title": "t", "spec": opt})
        self.assertEqual(out["spec"]["tooltip"]["trigger"], "axis")
        self.assertTrue(out["spec"]["legend"]["show"])
        self.assertEqual(out["rows"], [{"category": "a", "s1": 1, "s2": 2},
                                       {"category": "b", "s1": 2, "s2": 1}])

    def test_second_y_axis_dropped(self):
        opt = {"xAxis": {"type": "category", "data": ["a"]},
               "yAxis": [{"type": "value"}, {"type": "value"}],
               "series": [{"type": "bar", "data": [1]},
                          {"type": "line", "data": [5], "yAxisIndex": 1}]}
        out = ap.prepare_chart({"kind": "echarts", "title": "t", "spec": opt})
        self.assertIsInstance(out["spec"]["yAxis"], dict)
        self.assertNotIn("yAxisIndex", out["spec"]["series"][1])
        self.assertTrue(out["warnings"])


class PlotlyPatching(unittest.TestCase):
    def fig(self, dual):
        layout = {"title": {"text": "x"}, "width": 900}
        data = [{"type": "scatter", "mode": "lines", "name": "a", "x": [1, 2], "y": [3, 4]},
                {"type": "scatter", "mode": "lines", "name": "b", "x": [1, 2], "y": [30, 10]}]
        if dual:
            data[1]["yaxis"] = "y2"
            layout["yaxis2"] = {"overlaying": "y", "side": "right"}
        return {"kind": "plotly", "title": "t", "spec": {"data": data, "layout": layout}}

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
        a = ap.from_altair(alt.Chart(alt.Data(values=[{"a": 1, "b": 2}])).mark_point()
                           .encode(x="a:Q", y="b:Q"), title="Points")
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
        chart = {"kind": "vega-lite", "title": "Big", "spec": {
            "data": {"values": big}, "mark": "point",
            "encoding": {"x": {"field": "i", "type": "quantitative"}}}}
        with self.assertRaisesRegex(ValueError, "aggregate first"):
            ap.build_page([chart], "Too Much Data")

    def test_table_truncates_at_max_rows_with_note(self):
        rows = [{"n": i} for i in range(600)]
        c = bar_chart()
        c["rows"] = rows
        html = ap.build_page([c], "Truncation Check Page")
        tbody = html[html.index("<tbody>"): html.index("</tbody>")]
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
            path = ap.write_page([bar_chart()], os.path.join(d, "p.html"), title="Write Page Test")
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
                cls.html, os.path.join(cls.tmp.name, f"page-{mode}.png"),
                width=1200, height=800, mode=mode, timeout_ms=45000)

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
        ctx = export.get_browser().new_context(viewport={"width": width, "height": height},
                                               color_scheme=mode)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.set_content(self.html, wait_until="networkidle", timeout=45000)
        page.wait_for_function("() => window.__chartsReady.then(() => true)", timeout=45000)
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
        self.assertLessEqual(m["scroll"], m["inner"], "horizontal scroll at phone width")
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
        page.wait_for_function("() => window.__chartsReady.then(() => true)", timeout=45000)
        after = page.evaluate("() => [document.documentElement.dataset.theme, "
                              "getComputedStyle(document.body).backgroundColor]")
        self.assertEqual(after[0], "dark")
        self.assertNotEqual(before, after[1])
        self.assertTrue(page.is_hidden(".card >> nth=0 >> .table-wrap"))
        page.click(".card >> nth=0 >> .table-toggle")
        self.assertTrue(page.is_visible(".card >> nth=0 >> .table-wrap"))
        self.assertTrue(page.is_hidden(".card >> nth=0 >> .chart"))
        self.assertEqual(page.get_attribute(".card >> nth=0 >> .table-toggle", "aria-pressed"),
                         "true")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        unittest.main(verbosity=2)
    finally:
        export.close_browser()
