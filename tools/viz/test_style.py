#!/usr/bin/env python3
"""Tests for tools/viz/style.py - one house style applied to four backends.

Run from the repo root:  py -3.13 tools/viz/test_style.py

Every backend renders one bar, one line and one scatter headlessly into a
tempfile directory under warnings-as-errors; files must exist and be nonzero.
Matplotlib output is read back with PIL to prove dark and light differ.
"""

import contextlib
import dataclasses
import functools
import io
import itertools
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
            warnings.filterwarnings(
                "ignore", category=category, module=module, message=message
            )
        yield


GLYPH_MISSING = re.compile(r"Glyph .* missing")
GENERIC_FAMILIES = {
    "sans-serif",
    "serif",
    "monospace",
    "cursive",
    "fantasy",
    "system-ui",
    "-apple-system",
}
# Independent of style.py's detector so a broken detector cannot skip the test.
CJK_FAMILIES = (
    "Microsoft YaHei",
    "Yu Gothic",
    "Malgun Gothic",
    "MS Gothic",
    "Noto Sans CJK SC",
    "Noto Sans CJK JP",
    "Source Han Sans SC",
    "PingFang SC",
    "Hiragino Sans",
    "WenQuanYi Zen Hei",
)


def installed_families():
    from matplotlib import font_manager

    return {f.name for f in font_manager.fontManager.ttflist}


# One probe code point per CJK script: Han, Japanese hiragana, Korean Hangul.
CJK_SCRIPTS = {"han": 0x4E2D, "kana": 0x3042, "hangul": 0xD55C}


def cjk_coverage():
    """{installed CJK_FAMILIES name: set of CJK_SCRIPTS its regular face maps}."""
    from matplotlib import font_manager

    installed = installed_families()
    coverage = {}
    for name in CJK_FAMILIES:
        if name not in installed:
            continue
        path = font_manager.findfont(font_manager.FontProperties(family=name))
        charmap = font_manager.get_font(path).get_charmap()
        coverage[name] = {s for s, cp in CJK_SCRIPTS.items() if cp in charmap}
    return coverage


def installed_cjk_families():
    """Installed CJK_FAMILIES covering at least one CJK script, in order."""
    return [name for name, scripts in cjk_coverage().items() if scripts]


def hex_to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


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
            "print(sorted(m for m in ('plotly', 'altair', 'bokeh', 'seaborn', "
            "'great_tables') "
            "if m in sys.modules)); print('matplotlib' in sys.modules)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=False
        )
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
                self.assertEqual(
                    cyc, [c["color"] for c in matplotlib.rcParams["axes.prop_cycle"]]
                )
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
        # matplotlib 3.11 falls back per glyph only across concrete families
        # listed in font.family; a ['sans-serif'] + font.sans-serif list
        # resolves one font and warns 'Glyph ... missing' for the rest.
        installed = installed_families()
        family = style.resolve_font()
        self.assertIn(family, installed)
        with matplotlib.rc_context():
            rc = style.apply_matplotlib("light")
            families = rc["font.family"]
            self.assertEqual(families[0], family)
            # DejaVu Sans is the floor: last, unless it is also the resolved
            # primary font (no palette font installed, e.g. a bare Linux CI).
            self.assertIn("DejaVu Sans", families)
            if family != "DejaVu Sans":
                self.assertEqual(families[-1], "DejaVu Sans")
            self.assertEqual(len(families), len(set(families)))
            for name in families:
                self.assertIn(name, installed, name)
                self.assertNotIn(name, GENERIC_FAMILIES, name)
            self.assertEqual(matplotlib.rcParams["font.family"], families)
            # every installed CJK-capable candidate, in order, between the
            # palette font and DejaVu Sans
            cjk = installed_cjk_families()
            self.assertEqual(
                families, list(dict.fromkeys([family, *cjk, "DejaVu Sans"]))
            )
            self.assertEqual(list(style.cjk_fonts()), cjk)
            self.assertTrue(hasattr(style.cjk_fonts, "cache_info"))  # lru_cache
        self.assertEqual(style.resolve_font(["No Such Face 123"]), "DejaVu Sans")

    def test_mixed_script_labels_have_no_missing_glyphs(self):
        import matplotlib.pyplot as plt

        labels = {
            "latin": "Revenue by region",
            "greek": "Δ growth αβγ σ μ",
            "em-dash": "Q1 — Q4",
            "han": "销售额 年度",
            "kana": "ひらがな データ",
            "hangul": "한국 매출",
        }
        covered = set().union(*cjk_coverage().values())
        for script, text in labels.items():
            with self.subTest(script=script):
                if script in CJK_SCRIPTS and script not in covered:
                    self.skipTest(
                        f"no installed font covers {script} "
                        f"(checked {', '.join(CJK_FAMILIES)})"
                    )
                for mode in MODES:
                    with matplotlib.rc_context(), strict():
                        style.apply_matplotlib(mode)
                        fig, ax = plt.subplots(figsize=(4, 3))
                        try:
                            ax.bar([text, "b"], [1, 2])
                            ax.set_title(text)
                            ax.set_xlabel(text)
                            ax.set_ylabel(text)
                            for fmt in ("png", "svg"):
                                fig.savefig(io.BytesIO(), format=fmt)
                        except UserWarning as w:
                            if GLYPH_MISSING.search(str(w)):
                                self.fail(f"{script} ({mode}): {w}")
                            raise
                        finally:
                            plt.close(fig)

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
            self.assertEqual(
                matplotlib.colors.to_hex(seq(0.0)), palette.sequential()[0]
            )
            self.assertEqual(
                matplotlib.colors.to_hex(seq(1.0)), palette.sequential()[-1]
            )
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
                        "line": lambda ax: (
                            ax.plot(X, Y, marker="o", label="a"),
                            ax.plot(X, Y2, marker="o", label="b"),
                            ax.legend(),
                        ),
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
                self.assertEqual(
                    corners[mode], hex_to_rgb(palette.surface(mode)["surface"])
                )
            with open(os.path.join(tmp, "line-light.svg"), encoding="utf-8") as fh:
                svg = fh.read()
            self.assertIn("<text", svg)  # svg.fonttype none keeps text editable
        self.assertNotEqual(corners["light"], corners["dark"])

    def test_seaborn_hook(self):
        with matplotlib.rc_context(), strict():
            rc = style.set_seaborn("dark")
            import seaborn as sns

            self.assertEqual(sns.color_palette().as_hex(), palette.categorical("dark"))
            self.assertEqual(
                matplotlib.rcParams["axes.facecolor"],
                palette.surface("dark")["surface"],
            )
            self.assertEqual(
                matplotlib.rcParams["lines.linewidth"], rc["lines.linewidth"]
            )
            self.assertEqual(matplotlib.rcParams["savefig.dpi"], 144)


class StyleCli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, STYLE_PY, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            cwd=REPO_ROOT,
        )

    def test_help_prints_usage_and_exits_0(self):
        for flag in ("--help", "-h"):
            proc = self.run_cli(flag)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("usage:", proc.stdout)
            self.assertNotIn("Traceback", proc.stderr)

    def test_unknown_args_exit_2(self):
        for args in (("--bogus",), ("blue",), ("light", "extra")):
            proc = self.run_cli(*args)
            self.assertEqual(proc.returncode, 2, (args, proc.stderr))
            self.assertIn("usage:", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)

    def test_mode_dumps_json(self):
        import json

        for args in ((), ("dark",)):
            proc = self.run_cli(*args)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            dump = json.loads(proc.stdout)
            self.assertEqual(set(dump), {"matplotlib", "altair", "bokeh"})
            self.assertEqual(dump["matplotlib"]["font.family"], style.font_families())


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
                        "line": go.Figure(
                            [
                                go.Scatter(x=X, y=Y, mode="lines+markers"),
                                go.Scatter(x=X, y=Y2, mode="lines+markers"),
                            ]
                        ),
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
                            fallback.append(
                                f"{kind}-{mode}: {type(exc).__name__}: {exc}"
                            )
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
            self.assertEqual(c["range"]["ramp"], palette.sequential())
            self.assertEqual(c["legend"]["orient"], "top")
            self.assertEqual(c["title"]["color"], palette.text(mode)["primary"])
            self.assertEqual(c["bar"]["stroke"], sf["surface"])
            self.assertEqual(c["line"]["strokeWidth"], 2)
            self.assertIn("font", c)
        self.assertEqual(alt.theme.active, before)

    def test_ordinal_range_is_bounded_and_validates(self):
        # VAL-501: range.ordinal stays inside palette.json ordinal_bounds for the
        # mode, adjacent steps >= 100 apart, passes validate_palette --ordinal.
        from tools.viz import validate_palette

        data = palette.load()
        full = palette.ramp()
        names = list(full)
        by_hex = {v: k for k, v in full.items()}
        validator = os.path.join(REPO_ROOT, "tools", "viz", "validate_palette.py")
        for mode in MODES:
            b = data["ordinal_bounds"][mode]
            lo = int(b.get("min_step", names[0]))
            hi = int(b.get("max_step", names[-1]))
            ordinal = style.altair_config(mode)["config"]["range"]["ordinal"]
            self.assertGreaterEqual(len(ordinal), 5, (mode, ordinal))
            steps = [int(by_hex[c]) for c in ordinal]
            self.assertTrue(all(lo <= s <= hi for s in steps), (mode, steps, lo, hi))
            gaps = [b2 - a for a, b2 in itertools.pairwise(steps)]
            self.assertTrue(all(g >= 100 for g in gaps), (mode, steps))
            rep = validate_palette.validate(ordinal, mode=mode, ordinal=True)
            self.assertTrue(rep["ok"], (mode, rep["checks"]))
            proc = subprocess.run(
                [
                    sys.executable,
                    validator,
                    ",".join(ordinal),
                    "--mode",
                    mode,
                    "--ordinal",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(proc.returncode, 0, (mode, proc.stdout, proc.stderr))
        light = style.altair_config("light")["config"]["range"]["ordinal"]
        dark = style.altair_config("dark")["config"]["range"]["ordinal"]
        self.assertNotEqual(light, dark)

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
                        "line": base.mark_line(point=True).encode(
                            x="x:Q", y="y:Q", color="s:N"
                        ),
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
            self.assertEqual(
                attrs["Title"]["text_color"], palette.text(mode)["primary"]
            )
            self.assertEqual(
                attrs["Legend"]["label_text_color"], palette.text(mode)["secondary"]
            )
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
                    for kind, fig in (
                        ("bar", bar),
                        ("line", line),
                        ("scatter", scatter),
                    ):
                        html = file_html(fig, INLINE, title=kind, theme=theme)
                        path = os.path.join(tmp, f"{kind}-{mode}.html")
                        with open(path, "w", encoding="utf-8") as fh:
                            fh.write(html)
                        assert_file(self, path)
                        self.assertIn(palette.surface(mode)["surface"], html)


def gt_table():
    import great_tables as gt
    import pandas as pd

    frame = pd.DataFrame(
        {"month": ["Jan", "Feb"], "temp": [8.25, 10.5], "precip": [142.0, 99.75]}
    )
    return (
        gt.GT(frame, rowname_col="month")
        .tab_header(title="Weather", subtitle="by month")
        .fmt_number(columns=["temp", "precip"], decimals=1)
        .tab_source_note("Source: test")
    )


def gt_option(table, name):
    return getattr(table._options, name).value


class GreatTablesStyle(unittest.TestCase):
    def test_options_encode_house_style(self):
        hairline = f"{style.HAIRLINE_PX}px"
        margin = f"{style.TABLE_MARGIN_PX}px"
        for mode in MODES:
            sf, tx = palette.surface(mode), palette.text(mode)
            with strict():
                table = style.gt_style(gt_table(), mode)
            opt = functools.partial(gt_option, table)
            self.assertEqual(opt("table_font_names"), palette.font()["family_stack"])
            self.assertEqual(opt("table_background_color"), sf["surface"])
            self.assertEqual(opt("table_font_color"), tx["primary"])
            self.assertEqual(opt("heading_align"), "left")
            # rules: hairline structure, grid between rows, body top zeroed
            self.assertEqual(opt("column_labels_border_bottom_width"), hairline)
            self.assertEqual(opt("column_labels_border_bottom_color"), sf["axis"])
            self.assertEqual(opt("table_body_border_bottom_width"), hairline)
            self.assertEqual(opt("table_body_border_bottom_color"), sf["axis"])
            self.assertEqual(opt("table_body_border_top_width"), "0px")
            self.assertEqual(opt("table_body_hlines_width"), hairline)
            self.assertEqual(opt("table_body_hlines_color"), sf["grid"])
            for name in (
                "table_border_top",
                "table_border_bottom",
                "heading_border_bottom",
                "column_labels_border_top",
            ):
                self.assertEqual(opt(f"{name}_style"), "none", name)
            # margin around the table (container padding)
            self.assertEqual(opt("container_padding_x"), margin)
            self.assertEqual(opt("container_padding_y"), margin)

    def test_no_gt_default_rule_or_color_survives(self):
        """Every visible border is a hairline (or zeroed); every color is a token."""
        allowed_widths = {f"{style.HAIRLINE_PX}px", "0px"}
        for mode in MODES:
            table = style.gt_style(gt_table(), mode)
            tokens = set(palette.surface(mode).values()) | set(
                palette.text(mode).values()
            )
            names = [f.name for f in dataclasses.fields(table._options)]
            for name in names:
                value = gt_option(table, name)
                if name.endswith("_width") and ("border" in name or "lines" in name):
                    style_name = name[: -len("_width")] + "_style"
                    hidden = (
                        style_name in names and gt_option(table, style_name) == "none"
                    )
                    if not hidden:
                        self.assertIn(value, allowed_widths, f"{mode} {name}={value}")
                if (
                    name.endswith("_color")
                    and name != "table_font_color_light"
                    and isinstance(value, str)
                    and value.startswith("#")
                ):
                    self.assertIn(value, tokens, f"{mode} {name}={value}")

    def test_rendered_html_carries_tokens_font_and_tabular_nums(self):
        for mode in MODES:
            sf, tx = palette.surface(mode), palette.text(mode)
            table = style.gt_style(gt_table(), mode)
            html = table.as_raw_html()
            table_id = gt_option(table, "table_id")
            self.assertTrue(table_id, "gt_style must pin a table id for its scoped css")
            css = "\n".join(gt_option(table, "table_additional_css"))
            self.assertIn(f"#{table_id}", css)
            self.assertRegex(
                css, rf"#{table_id}\s*\{{[^}}]*background-color:\s*{sf['surface']}"
            )
            self.assertRegex(
                css,
                r"\.gt_table_body[^{]*\{[^}]*font-variant-numeric:\s*"
                r"tabular-nums",
            )
            for cls in ("gt_col_heading", "gt_subtitle", "gt_sourcenote"):
                self.assertRegex(
                    css, rf"\.{cls}[^{{]*\{{[^}}]*color:\s*{tx['secondary']}", cls
                )
            self.assertIn(css.splitlines()[0], html)
            self.assertIn("Segoe UI", html)
            tokens = set(sf.values()) | set(tx.values())
            stray = re.sub(rf"#{re.escape(table_id)}\b", "", css)
            for token in tokens:
                stray = stray.replace(token, "")
            self.assertNotIn(
                "#", stray, "only token hexes and the id selector in the css"
            )

    def test_existing_id_kept_input_untouched_bad_mode(self):
        base = gt_table().with_id("weather")
        styled = style.gt_style(base, "dark")
        self.assertEqual(gt_option(styled, "table_id"), "weather")
        self.assertIn("#weather", "\n".join(gt_option(styled, "table_additional_css")))
        self.assertIsNone(gt_option(base, "table_additional_css") or None)
        self.assertNotEqual(
            gt_option(base, "table_background_color"),
            palette.surface("dark")["surface"],
        )
        with self.assertRaises(ValueError):
            style.gt_style(base, "sepia")


if __name__ == "__main__":
    unittest.main(verbosity=2)
