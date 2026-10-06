#!/usr/bin/env python3
"""Tests for tools/viz/export.py - save() dispatch + render_html rasterizer.

Run from the repo root:  py -3.13 tools/viz/test_export.py

One tiny figure per chart library (a branch is skipped with its reason when
the library does not import). Browser-backed tests share the module's single
lazily-started Playwright chromium and skip with a reason when it is absent.
All output goes to tempfile directories.
"""

import ast
import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import matplotlib

matplotlib.use("Agg")

from tools.viz import export

EXPORT_PY = os.path.join(REPO_ROOT, "tools", "viz", "export.py")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
LAZY_LIBS = (
    "matplotlib",
    "plotly",
    "altair",
    "bokeh",
    "great_tables",
    "holoviews",
    "playwright",
    "kaleido",
    "vl_convert",
)

BROWSER_SKIP = None
try:
    export.get_browser()
except export.ExportError as exc:  # chromium or playwright missing
    BROWSER_SKIP = f"Playwright chromium unavailable: {exc}"


def needs_browser(fn):
    return unittest.skipIf(BROWSER_SKIP is not None, BROWSER_SKIP or "")(fn)


def try_import(name):
    try:
        return __import__(name, fromlist=["_"]), None
    except Exception as exc:  # noqa: BLE001 - any import failure means skip
        return None, f"{name} not importable: {type(exc).__name__}: {exc}"


def rgb(hexstr):
    hexstr = hexstr.lstrip("#")
    return tuple(int(hexstr[i : i + 2], 16) for i in (0, 2, 4))


def png_colors(path):
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        return {color: count for count, color in im.getcolors(maxcolors=1 << 24)}


def corner(path):
    from PIL import Image

    with Image.open(path) as im:
        return im.convert("RGB").getpixel((2, 2))


class ExportCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.out = os.path.join(self.root, "out")  # save() must stay inside this
        os.makedirs(self.out)

    def tearDown(self):
        self._tmp.cleanup()

    def assert_png(self, path):
        self.assertIsNotNone(path)
        self.assertTrue(os.path.isabs(path), path)
        self.assertTrue(os.path.isfile(path), path)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(8), PNG_MAGIC, path)

    def assert_file(self, path, needle=None):
        self.assertIsNotNone(path)
        self.assertTrue(os.path.isabs(path), path)
        self.assertTrue(os.path.isfile(path), path)
        self.assertGreater(os.path.getsize(path), 0, path)
        if needle is not None:
            with open(path, encoding="utf-8", errors="replace") as fh:
                self.assertIn(needle, fh.read(), path)

    def assert_confined(self):
        """Nothing written outside the directory of `path`."""
        self.assertEqual(os.listdir(self.root), ["out"])

    def save_quiet(self, obj, name, **kw):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            result = export.save(obj, os.path.join(self.out, name), **kw)
        self.assertEqual(buf_out.getvalue(), "", "save() must never print")
        self.assertEqual(set(result), {"png", "svg", "html", "notes"})
        self.assertIsInstance(result["notes"], list)
        self.assert_confined()
        return result


class Source(unittest.TestCase):
    def test_ascii_and_no_print_outside_cli(self):
        with open(EXPORT_PY, encoding="utf-8") as fh:
            src = fh.read()
        self.assertTrue(src.isascii())
        tree = ast.parse(src)
        offenders = []
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.name != "main":
                for node in ast.walk(fn):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "print"
                    ):
                        offenders.append(fn.name)
        self.assertEqual(offenders, [])

    def test_import_is_lazy(self):
        code = (
            f"import sys; sys.path.insert(0, {REPO_ROOT!r}); import tools.viz.export; "
            f"print(sorted(m for m in {LAZY_LIBS!r} if m in sys.modules))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=False
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "[]")

    def test_public_api(self):
        self.assertIs(export.html_to_png, export.render_html)
        self.assertEqual(export.READY_HOOK, "window.__chartsReady")
        self.assertTrue(issubclass(export.ExportError, RuntimeError))


class Arguments(ExportCase):
    def test_unknown_object_is_type_error(self):
        with self.assertRaises(TypeError):
            export.save(object(), os.path.join(self.out, "x"))

    def test_unknown_format_is_value_error(self):
        import matplotlib.pyplot as plt

        fig = plt.figure()
        try:
            with self.assertRaises(ValueError):
                export.save(fig, os.path.join(self.out, "x"), formats=("png", "gif"))
        finally:
            plt.close(fig)

    def test_bad_mode_is_value_error(self):
        with self.assertRaises(ValueError):
            export.render_html(
                "<p>x</p>", os.path.join(self.out, "x.png"), mode="sepia"
            )


class MatplotlibBranch(ExportCase):
    def test_png_svg_and_html_note(self):
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(3, 2))
        ax.bar([1, 2, 3], [3, 1, 2])
        try:
            res = self.save_quiet(fig, "mpl.png")  # extension is stripped to a stem
        finally:
            plt.close(fig)
        self.assert_png(res["png"])
        self.assertEqual(res["png"], os.path.join(self.out, "mpl.png"))
        self.assert_file(res["svg"], "<svg")
        self.assertIsNone(res["html"])
        self.assertTrue(any("html" in n for n in res["notes"]), res["notes"])

    def test_axes_resolves_to_figure_and_formats_subset(self):
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(3, 2))
        ax.plot([1, 2, 3])
        try:
            res = self.save_quiet(ax, "axes", formats=("png",))
        finally:
            plt.close(fig)
        self.assert_png(res["png"])
        self.assertIsNone(res["svg"])
        self.assertEqual(os.listdir(self.out), ["axes.png"])


PLOTLY, PLOTLY_SKIP = try_import("plotly.graph_objects")


@unittest.skipIf(PLOTLY is None, PLOTLY_SKIP or "")
class PlotlyBranch(ExportCase):
    def fig(self):
        return PLOTLY.Figure(
            PLOTLY.Bar(x=["a", "b"], y=[2, 3], marker_color="#1060e0"),
            layout={"width": 400, "height": 300},
        )

    def test_png_svg_html(self):
        res = self.save_quiet(self.fig(), "plotly")
        self.assert_png(res["png"])
        self.assert_file(res["html"], "plotly")
        joined = " ".join(res["notes"])
        if "kaleido" in joined and "fallback" not in joined:
            self.assert_file(res["svg"], "<svg")
        else:  # no usable Chrome for kaleido: browser fallback, no svg
            self.assertIsNone(res["svg"])

    @needs_browser
    def test_kaleido_failure_falls_back_to_render_html(self):
        fig = self.fig()
        with mock.patch.object(
            type(fig), "write_image", side_effect=RuntimeError("no chrome")
        ):
            res = self.save_quiet(fig, "fallback", formats=("png", "svg"))
        self.assert_png(res["png"])
        self.assertIsNone(res["svg"])
        self.assertIsNone(res["html"])
        self.assertTrue(any("fallback" in n for n in res["notes"]), res["notes"])
        self.assertIn(rgb("#1060e0"), png_colors(res["png"]))
        self.assertEqual(sorted(os.listdir(self.out)), ["fallback.png"])


ALTAIR, ALTAIR_SKIP = try_import("altair")


@unittest.skipIf(ALTAIR is None, ALTAIR_SKIP or "")
class AltairBranch(ExportCase):
    def test_png_svg_html_without_browser(self):
        import pandas as pd

        chart = (
            ALTAIR.Chart(pd.DataFrame({"k": ["a", "b"], "v": [2, 3]}))
            .mark_bar()
            .encode(x="k:N", y="v:Q")
        )
        with mock.patch.object(
            export,
            "get_browser",
            side_effect=AssertionError("altair must not use the browser"),
        ):
            res = self.save_quiet(chart, "alt")
        self.assert_png(res["png"])
        self.assert_file(res["svg"], "<svg")
        self.assert_file(res["html"], "vega")
        with open(res["html"], encoding="utf-8") as fh:
            self.assertNotRegex(
                fh.read(), r"<script[^>]+src=\"https?://"
            )  # self-contained


BOKEH, BOKEH_SKIP = try_import("bokeh.plotting")


@unittest.skipIf(BOKEH is None, BOKEH_SKIP or "")
class BokehBranch(ExportCase):
    def fig(self):
        f = BOKEH.figure(width=320, height=240, toolbar_location=None)
        f.vbar(x=[1, 2], top=[3, 4], width=0.5, color="#e01060")
        return f

    def test_html_only_without_browser(self):
        res = self.save_quiet(self.fig(), "bk", formats=("html",))
        self.assert_file(res["html"], "Bokeh")
        self.assertIsNone(res["png"])

    @needs_browser
    def test_png_via_render_html_and_svg_note(self):
        res = self.save_quiet(self.fig(), "bk")
        self.assert_png(res["png"])
        self.assert_file(res["html"], "Bokeh")
        self.assertIsNone(res["svg"])
        self.assertTrue(any("svg" in n for n in res["notes"]), res["notes"])
        self.assertIn(rgb("#e01060"), png_colors(res["png"]))


GT, GT_SKIP = try_import("great_tables")


@unittest.skipIf(GT is None, GT_SKIP or "")
class GreatTablesBranch(ExportCase):
    def table(self):
        import pandas as pd

        return GT.GT(pd.DataFrame({"name": ["a", "b"], "value": [1, 2]}))

    def test_html_without_browser(self):
        with mock.patch.object(
            type(self.table()),
            "gtsave",
            side_effect=AssertionError("gtsave must not be called"),
            create=True,
        ):
            res = self.save_quiet(self.table(), "gt", formats=("html",))
        self.assert_file(res["html"], "<table")

    @needs_browser
    def test_png_via_render_html(self):
        res = self.save_quiet(self.table(), "gt", formats=("png", "html"))
        self.assert_png(res["png"])
        self.assert_file(res["html"], "<table")

    @needs_browser
    def test_house_styled_png_has_surface_margin(self):
        """gt_style + save: a surface band of TABLE_MARGIN_PX frames the rules."""
        from PIL import Image

        from tools.viz import palette, style

        band = style.TABLE_MARGIN_PX * export.FIGURE_SCALE
        for mode in ("light", "dark"):
            sf = palette.surface(mode)
            table = (
                self.table()
                .tab_header(title="Styled", subtitle="sub")
                .tab_source_note("Source: test")
            )
            res = self.save_quiet(
                style.gt_style(table, mode), f"gt-{mode}", formats=("png",), mode=mode
            )
            self.assert_png(res["png"])
            self.assertGreater(os.path.getsize(res["png"]), 0)
            with Image.open(res["png"]) as im:
                im = im.convert("RGB")
                w, h = im.size
                self.assertGreater(w, 2 * band)
                self.assertGreater(h, 2 * band)
                px = im.load()
                edge = [
                    px[x, y]
                    for y in range(h)
                    for x in range(w)
                    if x < band - 1
                    or y < band - 1
                    or x >= w - band + 1
                    or y >= h - band + 1
                ]
                colors = {c for _, c in im.getcolors(maxcolors=1 << 24)}
            self.assertEqual(
                set(edge),
                {rgb(sf["surface"])},
                f"{mode}: margin band is not pure surface",
            )
            self.assertIn(rgb(sf["axis"]), colors, f"{mode}: no hairline rule drawn")
            # the column-label rule is axis ink: two axis rules (label + body bottom),
            # not one - the first body row's grid hline must not win the collapse tie
            with Image.open(res["png"]) as im:
                im = im.convert("RGB")
                column = [im.getpixel((w // 2, y)) for y in range(h)]
            runs = [
                y
                for y in range(1, h)
                if column[y] == rgb(sf["axis"]) and column[y - 1] != rgb(sf["axis"])
            ]
            self.assertEqual(len(runs), 2, f"{mode}: axis rules at {runs}")


HV, HV_SKIP = try_import("holoviews")


@unittest.skipIf(HV is None, HV_SKIP or "")
class HoloviewsBranch(ExportCase):
    def test_render_then_recurse(self):
        import matplotlib.pyplot as plt

        mpl_fig = plt.figure(figsize=(2, 2))
        try:
            with mock.patch.object(HV, "render", return_value=mpl_fig) as render:
                res = self.save_quiet(HV.Curve([1, 2, 3]), "hv", formats=("png",))
        finally:
            plt.close(mpl_fig)
        self.assertTrue(render.called)
        self.assert_png(res["png"])
        self.assertTrue(any("holoviews" in n for n in res["notes"]), res["notes"])

    def test_real_backend_or_clear_error(self):
        backend_ok = True
        try:
            HV.render(HV.Curve([1, 2]), backend="matplotlib")
        except Exception:  # noqa: BLE001 - backend import failures vary by version
            backend_ok = False
        if backend_ok:
            res = self.save_quiet(HV.Curve([1, 2, 3]), "hv", formats=("png",))
            self.assert_png(res["png"])
        else:
            with self.assertRaises(export.ExportError) as ctx:
                export.save(HV.Curve([1, 2, 3]), os.path.join(self.out, "hv"))
            self.assertIn("holoviews", str(ctx.exception))
            self.assert_confined()


THEME_PAGE = """<!DOCTYPE html><html><head><style>
:root { --bg: #ffffff; }
@media (prefers-color-scheme: dark) { :root { --bg: #000000; } }
html, body { margin: 0; background: var(--bg); }
rect { fill: #20a040; }
:root[data-theme="dark"] rect { fill: #c03020; }
</style></head><body>
<svg width="200" height="100" xmlns="http://www.w3.org/2000/svg">
<rect x="60" y="20" width="80" height="60"/></svg>
</body></html>"""

LATE_PROMISE_PAGE = """<!DOCTYPE html><html><head><style>body{margin:0}</style>
<script>
window.__chartsReady = new Promise(function (resolve) {
  setTimeout(function () {
    document.body.innerHTML = '<div style="width:100px;height:100px;background:#3050d0"></div>';
    resolve();
  }, 1500);
});
</script></head><body></body></html>"""

LATE_FLAG_PAGE = """<!DOCTYPE html><html><head><style>body{margin:0}</style>
<script>
window.__chartsReady = false;
setTimeout(function () {
  document.body.innerHTML = '<div style="width:100px;height:100px;background:#d0a030"></div>';
  window.__chartsReady = true;
}, 1500);
</script></head><body></body></html>"""

NEVER_READY_PAGE = """<!DOCTYPE html><html><head>
<script>window.__chartsReady = new Promise(function () {});</script>
</head><body>x</body></html>"""


@unittest.skipIf(BROWSER_SKIP is not None, BROWSER_SKIP or "")
class RenderHtml(ExportCase):
    def render(self, html, name, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            path = export.render_html(
                html,
                os.path.join(self.out, name),
                width=kw.pop("width", 320),
                height=kw.pop("height", 200),
                **kw,
            )
        self.assertEqual(buf.getvalue(), "", "render_html must never print")
        self.assert_png(path)
        return path

    def test_browser_is_shared(self):
        self.assertIs(export.get_browser(), export.get_browser())

    def test_light_and_dark_differ_inline_string(self):
        light = self.render(THEME_PAGE, "light.png", mode="light")
        dark = self.render(THEME_PAGE, "dark.png", mode="dark")
        self.assertEqual(corner(light), (255, 255, 255))
        self.assertEqual(corner(dark), (0, 0, 0))
        self.assertIn(rgb("#20a040"), png_colors(light))
        self.assertIn(rgb("#c03020"), png_colors(dark))  # data-theme set to mode
        self.assertNotIn(rgb("#c03020"), png_colors(light))

    def test_from_file_path(self):
        page = os.path.join(self.out, "page.html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(THEME_PAGE)
        path = self.render(page, "file.png", mode="dark")
        self.assertEqual(corner(path), (0, 0, 0))

    def test_viewport_size(self):
        from PIL import Image

        path = self.render(THEME_PAGE, "size.png", width=500, height=260)
        with Image.open(path) as im:
            self.assertEqual(im.size, (500, 260))

    def test_ready_hook_promise(self):
        path = self.render(LATE_PROMISE_PAGE, "promise.png")
        self.assertIn(rgb("#3050d0"), png_colors(path))

    def test_ready_hook_flag(self):
        path = self.render(LATE_FLAG_PAGE, "flag.png")
        self.assertIn(rgb("#d0a030"), png_colors(path))

    def test_ready_hook_timeout_is_export_error(self):
        with self.assertRaises(export.ExportError) as ctx:
            export.render_html(
                NEVER_READY_PAGE, os.path.join(self.out, "never.png"), timeout_ms=1000
            )
        self.assertIn("__chartsReady", str(ctx.exception))

    def test_tiny_plotly_cdn_page(self):
        if PLOTLY is None:
            self.skipTest(PLOTLY_SKIP)
        fig = PLOTLY.Figure(
            PLOTLY.Bar(x=["a", "b"], y=[2, 3], marker_color="#1060e0"),
            layout={"width": 400, "height": 300},
        )
        html = fig.to_html(include_plotlyjs="cdn", full_html=True)
        src = re.search(r'src="(https://[^"]+)"', html).group(1)
        try:
            urllib.request.urlopen(src, timeout=5).close()
        except Exception as exc:  # noqa: BLE001 - offline machine
            self.skipTest(f"plotly CDN unreachable ({src}): {exc}")
        path = self.render(html, "cdn.png", width=420, height=320)
        self.assertIn(rgb("#1060e0"), png_colors(path))

    def test_cli_render(self):
        page = os.path.join(self.out, "page.html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(THEME_PAGE)
        out = os.path.join(self.out, "cli.png")
        proc = subprocess.run(
            [
                sys.executable,
                EXPORT_PY,
                "render",
                page,
                out,
                "--mode",
                "dark",
                "--width",
                "300",
                "--height",
                "150",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), os.path.abspath(out))
        self.assert_png(out)
        self.assertEqual(corner(out), (0, 0, 0))

    def test_cli_missing_input_exits_nonzero(self):
        proc = subprocess.run(
            [
                sys.executable,
                EXPORT_PY,
                "render",
                os.path.join(self.out, "nope.html"),
                os.path.join(self.out, "x.png"),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("nope.html", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
