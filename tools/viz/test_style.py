#!/usr/bin/env python3
"""Tests for tools/viz/style.py - one house style applied to four backends.

Run from the repo root:  py -3.13 tools/viz/test_style.py

Every backend renders one bar, one line and one scatter headlessly into a
tempfile directory under warnings-as-errors; files must exist and be nonzero.
Matplotlib output is read back with PIL to prove dark and light differ.
"""
import contextlib
import os
import re
import subprocess
import sys
import tempfile
import unittest
import warnings

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import matplotlib

matplotlib.use("Agg")

from tools.viz import palette, style

STYLE_PY = os.path.join(REPO_ROOT, "tools", "viz", "style.py")
MODES = ("light", "dark")
X = [1, 2, 3, 4]
Y = [3, 5, 2, 6]
Y2 = [2, 3, 4, 5]

# Known third-party DeprecationWarnings that are not ours to fix. Each entry is
# (category, module-regex, message-regex). Empty = every warning is an error.
KNOWN_WARNINGS = ()


@contextlib.contextmanager
def strict():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for category, module, message in KNOWN_WARNINGS:
            warnings.filterwarnings("ignore", category=category, module=module,
                                    message=message)
        yield


def hex_to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def assert_file(test, path):
    test.assertTrue(os.path.exists(path), f"missing {path}")
    test.assertGreater(os.path.getsize(path), 0, f"empty {path}")


class StyleSource(unittest.TestCase):
    def test_no_hex_literals_in_style(self):
        with open(STYLE_PY, encoding="utf-8") as fh:
            src = fh.read()
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{6}", src), [])
        self.assertTrue(src.isascii())

    def test_import_is_lazy_and_costs_only_matplotlib(self):
        code = (
            f"import sys; sys.path.insert(0, {REPO_ROOT!r}); import tools.viz.style; "
            "print(sorted(m for m in ('plotly', 'altair', 'bokeh', 'seaborn') "
            "if m in sys.modules)); print('matplotlib' in sys.modules)")
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split(), ["[]", "True"])

    def test_px_to_pt(self):
        self.assertAlmostEqual(style.px_to_pt(2), 1.44)
        self.assertAlmostEqual(style.px_to_pt(8), 5.76)

    def test_names(self):
        self.assertEqual(style.template_name("light"), "house-light")
        self.assertEqual(style.template_name("dark"), "house-dark")
        with self.assertRaises(ValueError):
            style.template_name("sepia")


class MatplotlibStyle(unittest.TestCase):
    def test_rcparams_encode_mark_specs(self):
        for mode in MODES:
            with matplotlib.rc_context(), strict():
                rc = style.apply_matplotlib(mode)
                sf = palette.surface(mode)
                tx = palette.text(mode)
                cyc = [c["color"] for c in rc["axes.prop_cycle"]]
                self.assertEqual(cyc, palette.categorical(mode))
                self.assertEqual(cyc, [c["color"]
                                       for c in matplotlib.rcParams["axes.prop_cycle"]])
                # 2px line at the 100dpi reference = 1.44pt, rounded to 1.5pt
                self.assertGreaterEqual(rc["lines.linewidth"], style.px_to_pt(2))
                self.assertEqual(rc["lines.linewidth"], 1.5)
                self.assertGreaterEqual(rc["lines.markersize"], style.px_to_pt(8))
                self.assertEqual(rc["lines.solid_joinstyle"], "round")
                self.assertEqual(rc["lines.solid_capstyle"], "round")
                for key in ("figure.facecolor", "axes.facecolor", "savefig.facecolor"):
                    self.assertEqual(rc[key], sf["surface"], key)
                self.assertEqual(rc["axes.edgecolor"], sf["axis"])
                self.assertEqual(rc["grid.color"], sf["grid"])
                self.assertEqual(rc["grid.linestyle"], "-")
                self.assertLessEqual(rc["grid.linewidth"], 1.0)
                self.assertFalse(rc["axes.spines.top"])
                self.assertFalse(rc["axes.spines.right"])
                self.assertTrue(rc["axes.axisbelow"])
                self.assertEqual(rc["text.color"], tx["primary"])
                self.assertEqual(rc["axes.titlecolor"], tx["primary"])
                self.assertEqual(rc["axes.labelcolor"], tx["secondary"])
                self.assertEqual(rc["xtick.labelcolor"], tx["secondary"])
                self.assertEqual(rc["ytick.labelcolor"], tx["secondary"])
                self.assertNotIn(rc["text.color"], cyc)
                self.assertEqual(rc["savefig.dpi"], 144)
                self.assertEqual(rc["savefig.bbox"], "tight")
                self.assertEqual(rc["svg.fonttype"], "none")
                self.assertFalse(rc["legend.frameon"])
                self.assertEqual(rc["patch.edgecolor"], sf["surface"])
                self.assertEqual(matplotlib.rcParams["savefig.dpi"], 144)

    def test_font_resolves_to_installed_family(self):
        from matplotlib import font_manager
        installed = {f.name for f in font_manager.fontManager.ttflist}
        family = style.resolve_font()
        self.assertIn(family, installed)
        with matplotlib.rc_context():
            rc = style.apply_matplotlib("light")
            self.assertEqual(rc["font.family"], ["sans-serif"])
            self.assertEqual(rc["font.sans-serif"][0], family)
            self.assertIn("DejaVu Sans", rc["font.sans-serif"])
        self.assertEqual(style.resolve_font(["No Such Face 123"]), "DejaVu Sans")

    def test_bar_kwargs(self):
        for mode in MODES:
            kw = style.bar_kwargs(mode)
            self.assertEqual(kw["edgecolor"], palette.surface(mode)["surface"])
            self.assertGreaterEqual(kw["linewidth"], style.px_to_pt(2))

    def test_colormaps_registered(self):
        with matplotlib.rc_context(), strict():
            rc = style.apply_matplotlib("light")
            self.assertEqual(rc["image.cmap"], "house-seq")
            seq = matplotlib.colormaps["house-seq"]
            div = matplotlib.colormaps["house-div-light"]
            self.assertEqual(matplotlib.colors.to_hex(seq(0.0)),
                             palette.sequential()[0])
            self.assertEqual(matplotlib.colors.to_hex(seq(1.0)),
                             palette.sequential()[-1])
            low, _mid, high = palette.diverging("light")
            self.assertEqual(matplotlib.colors.to_hex(div(0.0)), low)
            self.assertEqual(matplotlib.colors.to_hex(div(1.0)), high)
            # idempotent re-registration
            style.apply_matplotlib("dark")

    def test_renders_both_modes_and_surfaces_differ(self):
        import matplotlib.pyplot as plt
        from PIL import Image
        corners = {}
        with tempfile.TemporaryDirectory() as tmp:
            for mode in MODES:
                with matplotlib.rc_context(), strict():
                    style.apply_matplotlib(mode)
                    bar_kw = style.bar_kwargs(mode)
                    kinds = {
                        "bar": lambda ax, kw=bar_kw: ax.bar(X, Y, **kw),
                        "line": lambda ax: (ax.plot(X, Y, marker="o", label="a"),
                                            ax.plot(X, Y2, marker="o", label="b"),
                                            ax.legend()),
                        "scatter": lambda ax: ax.scatter(X, Y),
                    }
                    for kind, draw in kinds.items():
                        fig, ax = plt.subplots(figsize=(4, 3))
                        draw(ax)
                        ax.set_title(f"{kind} {mode}")
                        for ext in ("png", "svg"):
                            path = os.path.join(tmp, f"{kind}-{mode}.{ext}")
                            fig.savefig(path)
                            assert_file(self, path)
                        plt.close(fig)
                    with Image.open(os.path.join(tmp, f"bar-{mode}.png")) as im:
                        corners[mode] = im.convert("RGB").getpixel((0, 0))
                        self.assertGreater(im.width, 400)  # 4in * 144dpi minus tight
                self.assertEqual(corners[mode],
                                 hex_to_rgb(palette.surface(mode)["surface"]))
            with open(os.path.join(tmp, "line-light.svg"), encoding="utf-8") as fh:
                svg = fh.read()
            self.assertIn("<text", svg)  # svg.fonttype none keeps text editable
        self.assertNotEqual(corners["light"], corners["dark"])

    def test_seaborn_hook(self):
        with matplotlib.rc_context(), strict():
            rc = style.set_seaborn("dark")
            import seaborn as sns
            self.assertEqual(sns.color_palette().as_hex(), palette.categorical("dark"))
            self.assertEqual(matplotlib.rcParams["axes.facecolor"],
                             palette.surface("dark")["surface"])
            self.assertEqual(matplotlib.rcParams["lines.linewidth"], rc["lines.linewidth"])
            self.assertEqual(matplotlib.rcParams["savefig.dpi"], 144)


class PlotlyStyle(unittest.TestCase):
    def test_template_registered_not_default(self):
        import plotly.io as pio
        before = pio.templates.default
        for mode in MODES:
            with strict():
                name = style.plotly_template(mode)
            self.assertEqual(name, style.template_name(mode))
            self.assertIn(name, pio.templates)
            tpl = pio.templates[name]
            sf = palette.surface(mode)
            self.assertEqual(list(tpl.layout.colorway), palette.categorical(mode))
            self.assertEqual(tpl.layout.paper_bgcolor, sf["surface"])
            self.assertEqual(tpl.layout.plot_bgcolor, sf["surface"])
            self.assertEqual(tpl.layout.font.color, palette.text(mode)["primary"])
            self.assertEqual(tpl.layout.xaxis.gridcolor, sf["grid"])
            self.assertEqual(tpl.layout.yaxis.gridcolor, sf["grid"])
            self.assertFalse(tpl.layout.xaxis.showline)
            self.assertEqual(tpl.layout.legend.orientation, "h")
            self.assertEqual(tpl.layout.hovermode, "x unified")
            self.assertGreater(tpl.layout.bargap, 0)
            self.assertEqual(tpl.data.bar[0].marker.line.color, sf["surface"])
            self.assertEqual(tpl.data.bar[0].marker.line.width, 2)
            self.assertEqual(tpl.data.scatter[0].line.width, 2)
            self.assertGreaterEqual(tpl.data.scatter[0].marker.size, 8)
        self.assertEqual(pio.templates.default, before)

    def test_renders(self):
        import plotly.graph_objects as go
        fallback = []
        with tempfile.TemporaryDirectory() as tmp:
            for mode in MODES:
                with strict():
                    name = style.plotly_template(mode)
                    figs = {
                        "bar": go.Figure(go.Bar(x=X, y=Y)),
                        "line": go.Figure([go.Scatter(x=X, y=Y, mode="lines+markers"),
                                           go.Scatter(x=X, y=Y2, mode="lines+markers")]),
                        "scatter": go.Figure(go.Scatter(x=X, y=Y, mode="markers")),
                    }
                    for kind, fig in figs.items():
                        fig.update_layout(template=name, title=f"{kind} {mode}")
                        path = os.path.join(tmp, f"{kind}-{mode}.png")
                        try:
                            fig.write_image(path, width=400, height=300)
                        except Exception as exc:  # noqa: BLE001 - kaleido/Chrome absent
                            path = os.path.join(tmp, f"{kind}-{mode}.html")
                            fig.write_html(path, include_plotlyjs="cdn")
                            fallback.append(f"{kind}-{mode}: {type(exc).__name__}: {exc}")
                        assert_file(self, path)
        if fallback:
            print("PLOTLY_HTML_FALLBACK", fallback, file=sys.stderr)


class AltairStyle(unittest.TestCase):
    def test_theme_registered_not_enabled(self):
        import altair as alt
        before = alt.theme.active
        for mode in MODES:
            with strict():
                name = style.altair_theme(mode)
            self.assertEqual(name, style.template_name(mode))
            self.assertIn(name, alt.theme.names())
            cfg = style.altair_config(mode)
            sf = palette.surface(mode)
            c = cfg["config"]
            self.assertEqual(c["background"], sf["surface"])
            self.assertIsNone(c["view"]["stroke"])
            self.assertEqual(c["axis"]["domainColor"], sf["axis"])
            self.assertEqual(c["axis"]["gridColor"], sf["grid"])
            self.assertEqual(c["range"]["category"], palette.categorical(mode))
            self.assertEqual(c["range"]["diverging"], list(palette.diverging(mode)))
            self.assertEqual(c["range"]["heatmap"], palette.sequential())
            self.assertEqual(c["legend"]["orient"], "top")
            self.assertEqual(c["title"]["color"], palette.text(mode)["primary"])
            self.assertEqual(c["bar"]["stroke"], sf["surface"])
            self.assertEqual(c["line"]["strokeWidth"], 2)
            self.assertIn("font", c)
        self.assertEqual(alt.theme.active, before)

    def test_renders(self):
        import altair as alt
        import pandas as pd
        df = pd.DataFrame({"x": X, "y": Y, "s": ["a", "b", "a", "b"]})
        with tempfile.TemporaryDirectory() as tmp:
            for mode in MODES:
                with strict():
                    name = style.altair_theme(mode)
                    base = alt.Chart(df)
                    charts = {
                        "bar": base.mark_bar().encode(x="x:O", y="y:Q"),
                        "line": base.mark_line(point=True).encode(x="x:Q", y="y:Q",
                                                                  color="s:N"),
                        "scatter": base.mark_point().encode(x="x:Q", y="y:Q"),
                    }
                    with alt.theme.enable(name):
                        for kind, chart in charts.items():
                            path = os.path.join(tmp, f"{kind}-{mode}.png")
                            chart.properties(title=f"{kind} {mode}").save(path)
                            assert_file(self, path)


class BokehStyle(unittest.TestCase):
    def test_theme_and_palette(self):
        from bokeh.themes import Theme
        for mode in MODES:
            with strict():
                theme = style.bokeh_theme(mode)
            self.assertIsInstance(theme, Theme)
            attrs = style.bokeh_attrs(mode)["attrs"]
            sf = palette.surface(mode)
            self.assertEqual(attrs["Plot"]["background_fill_color"], sf["surface"])
            self.assertEqual(attrs["Plot"]["border_fill_color"], sf["surface"])
            self.assertEqual(attrs["Axis"]["axis_line_color"], sf["axis"])
            self.assertEqual(attrs["Grid"]["grid_line_color"], sf["grid"])
            self.assertEqual(attrs["Title"]["text_color"], palette.text(mode)["primary"])
            self.assertEqual(attrs["Legend"]["label_text_color"],
                             palette.text(mode)["secondary"])
            self.assertEqual(list(style.bokeh_palette(mode)), palette.categorical(mode))

    def test_renders_html(self):
        from bokeh.embed import file_html
        from bokeh.plotting import figure
        from bokeh.resources import INLINE
        with tempfile.TemporaryDirectory() as tmp:
            for mode in MODES:
                with strict():
                    theme = style.bokeh_theme(mode)
                    pal = style.bokeh_palette(mode)
                    bar = figure(title=f"bar {mode}", width=400, height=300)
                    bar.vbar(x=X, top=Y, width=0.6, color=pal[0])
                    line = figure(title=f"line {mode}", width=400, height=300)
                    line.line(X, Y, color=pal[0], legend_label="a")
                    line.line(X, Y2, color=pal[1], legend_label="b")
                    scatter = figure(title=f"scatter {mode}", width=400, height=300)
                    scatter.scatter(X, Y, color=pal[0])
                    for kind, fig in (("bar", bar), ("line", line), ("scatter", scatter)):
                        html = file_html(fig, INLINE, title=kind, theme=theme)
                        path = os.path.join(tmp, f"{kind}-{mode}.html")
                        with open(path, "w", encoding="utf-8") as fh:
                            fh.write(html)
                        assert_file(self, path)
                        self.assertIn(palette.surface(mode)["surface"], html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
