#!/usr/bin/env python3
"""One house style, four backends - every value comes from tools/viz/palette.json.

The numbers encode references/marks-and-anatomy.md: 2px lines with round
join/cap, markers >= 8px, bars capped at 24px with a 2px surface gap, a 2px
surface ring on dots, hairline (1px) solid recessive gridlines/axes, text in
text tokens (never series colors), categorical slots in fixed order.

    from tools.viz import style
    style.apply_matplotlib("dark")        # rcParams; returns the dict it set
    style.set_seaborn("light")            # seaborn + the same rcParams
    fig.update_layout(template=style.plotly_template("light"))   # 'house-light'
    with alt.theme.enable(style.altair_theme("dark")): ...
    file_html(fig, INLINE, theme=style.bokeh_theme("light"))

Importing this module costs only matplotlib; plotly, altair, bokeh and
seaborn are imported inside the functions that need them. No hex literal
lives here - tools/viz/test_style.py greps for one.
"""
from __future__ import annotations

import functools
import sys
from pathlib import Path

import matplotlib
from matplotlib import font_manager
from matplotlib.colors import LinearSegmentedColormap

try:
    from tools.viz import palette
except ModuleNotFoundError:  # run as a script: py tools/viz/style.py [mode]
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tools.viz import palette

MODES = palette.MODES
TEMPLATE_NAMES = {"light": "house-light", "dark": "house-dark"}

# Mark specs in CSS px (marks-and-anatomy.md). Matplotlib takes points, so the
# px values convert at the 100dpi reference figure (figure.dpi below):
# pt = px * 72 / 100 -> 2px = 1.44pt (rounded up to 1.5pt), 8px = 5.76pt
# (6pt), 1px hairline = 0.72pt (0.75pt). savefig.dpi 144 then renders the
# same figure at 1.44x, so the pixel sizes scale together.
REFERENCE_DPI = 100
SAVE_DPI = 144
LINE_PX = 2
MARKER_PX = 8
GAP_PX = 2          # surface gap between touching marks and the dot ring
HAIRLINE_PX = 1
BAR_MAX_PX = 24
BAR_CORNER_PX = 4   # rounded data-end (altair only; see bar_kwargs)
AREA_OPACITY = 0.1
FALLBACK_FONT = "DejaVu Sans"   # bundled with matplotlib, always present
# CJK-capable families in fallback order (Windows, then Noto/Source Han,
# macOS, Linux). Every one font_manager sees whose cmap holds any CJK_PROBES
# code point joins font.family, so Han, kana and Hangul each find a face.
CJK_FONTS = ("Microsoft YaHei", "Yu Gothic", "Malgun Gothic", "MS Gothic",
             "Noto Sans CJK SC", "Noto Sans CJK JP", "Source Han Sans SC",
             "PingFang SC", "Hiragino Sans", "WenQuanYi Zen Hei")
CJK_PROBES = {
    "han": 0x4E2D,     # CJK UNIFIED IDEOGRAPH-4E2D
    "kana": 0x3042,    # HIRAGANA LETTER A
    "hangul": 0xD55C,  # HANGUL SYLLABLE HAN
}

LINE_PT = 1.5       # >= px_to_pt(LINE_PX) == 1.44
MARKER_PT = 6.0     # >= px_to_pt(MARKER_PX) == 5.76
GAP_PT = 1.5        # >= px_to_pt(GAP_PX)
HAIRLINE_PT = 0.75  # >= px_to_pt(HAIRLINE_PX) == 0.72

SEQ_CMAP = "house-seq"
DIV_CMAP = {"light": "house-div-light", "dark": "house-div-dark"}

# Non-hex, non-color literals (transparent) are fine; the hex ban is on tokens.
TRANSPARENT = "rgba(0,0,0,0)"


def px_to_pt(px: float, dpi: float = REFERENCE_DPI) -> float:
    """CSS px -> typographic points at the given dpi (72pt per inch)."""
    return px * 72.0 / dpi


def template_name(mode: str) -> str:
    """'house-light' / 'house-dark' - the one name shared by every backend."""
    if mode not in TEMPLATE_NAMES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    return TEMPLATE_NAMES[mode]


def font_stack() -> list[str]:
    """The palette's CSS font-family stack (generic names included)."""
    return list(palette.font()["family_stack"])


def css_font_family() -> str:
    """Comma-joined stack for CSS-speaking backends (plotly, altair, bokeh)."""
    return ", ".join(f'"{f}"' if " " in f else f for f in font_stack())


def resolve_font(stack: list[str] | None = None) -> str:
    """First family of the stack that matplotlib's font_manager can see.

    CSS generics (system-ui, sans-serif, -apple-system) are not real families on
    any OS, so they are skipped; DejaVu Sans is the floor (bundled).
    """
    installed = {f.name for f in font_manager.fontManager.ttflist}
    for name in (stack if stack is not None else font_stack()):
        if name in installed:
            return name
    return FALLBACK_FONT


@functools.lru_cache(maxsize=8)
def cjk_fonts(candidates: tuple[str, ...] = CJK_FONTS) -> tuple[str, ...]:
    """Installed candidates whose regular face maps Han, kana or Hangul, in order.

    Cached per process: fonts added via font_manager after the first call are
    not seen until cjk_fonts.cache_clear().
    """
    installed = {f.name for f in font_manager.fontManager.ttflist}
    found = []
    for name in candidates:
        if name not in installed:
            continue
        try:
            path = font_manager.findfont(font_manager.FontProperties(family=name),
                                         fallback_to_default=False)
            charmap = font_manager.get_font(path).get_charmap()
        except (OSError, RuntimeError, ValueError):
            continue
        if any(cp in charmap for cp in CJK_PROBES.values()):
            found.append(name)
    return tuple(found)


def font_families() -> list[str]:
    """Concrete families for rcParams font.family: palette font, CJK, DejaVu.

    matplotlib 3.11 falls back per glyph only across the concrete families
    listed in font.family; font.family=['sans-serif'] with a font.sans-serif
    list resolves ONE font and warns 'Glyph ... missing' for the rest.
    """
    return list(dict.fromkeys([resolve_font(), *cjk_fonts(), FALLBACK_FONT]))


def _register_colormaps() -> None:
    """Register house-seq(_r) and house-div-{mode} once (force= still warns)."""
    seq = LinearSegmentedColormap.from_list(SEQ_CMAP, palette.sequential())
    wanted = {SEQ_CMAP: seq, SEQ_CMAP + "_r": seq.reversed()}
    for m in MODES:
        wanted[DIV_CMAP[m]] = LinearSegmentedColormap.from_list(
            DIV_CMAP[m], list(palette.diverging(m)))
    for name, cmap in wanted.items():
        if name not in matplotlib.colormaps:
            matplotlib.colormaps.register(cmap, name=name)


def matplotlib_rc(mode: str = "light") -> dict:
    """The rcParams dict for one mode (pure; apply_matplotlib installs it)."""
    template_name(mode)
    sf = palette.surface(mode)
    tx = palette.text(mode)
    families = font_families()
    return {
        # series identity: fixed slot order, never cycled past 8
        "axes.prop_cycle": matplotlib.cycler(color=palette.categorical(mode)),
        # lines: 2px, round join/cap; markers >= 8px with a 2px surface ring
        "lines.linewidth": LINE_PT,
        "lines.solid_joinstyle": "round",
        "lines.solid_capstyle": "round",
        "lines.dash_joinstyle": "round",
        "lines.dash_capstyle": "round",
        "lines.markersize": MARKER_PT,
        "lines.markeredgewidth": GAP_PT,
        "lines.markeredgecolor": sf["surface"],
        "scatter.edgecolors": sf["surface"],
        # bars and other patches: surface-colored edge = the 2px surface gap
        "patch.edgecolor": sf["surface"],
        "patch.linewidth": GAP_PT,
        "patch.force_edgecolor": True,
        "hatch.linewidth": HAIRLINE_PT,
        # surfaces
        "figure.facecolor": sf["surface"],
        "figure.edgecolor": sf["surface"],
        "axes.facecolor": sf["surface"],
        "savefig.facecolor": sf["surface"],
        "savefig.edgecolor": sf["surface"],
        "legend.facecolor": "inherit",
        "legend.edgecolor": "inherit",
        # recessive axes and grid: hairline, solid, one step off the surface
        "axes.edgecolor": sf["axis"],
        "axes.linewidth": HAIRLINE_PT,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "axes.grid.axis": "y",
        "axes.axisbelow": True,
        "grid.color": sf["grid"],
        "grid.linewidth": HAIRLINE_PT,
        "grid.linestyle": "-",
        "grid.alpha": 1.0,   # the grid token is already the recessive step
        "xtick.color": sf["axis"],
        "ytick.color": sf["axis"],
        "xtick.major.width": HAIRLINE_PT,
        "ytick.major.width": HAIRLINE_PT,
        "xtick.minor.width": HAIRLINE_PT,
        "ytick.minor.width": HAIRLINE_PT,
        "xtick.direction": "out",
        "ytick.direction": "out",
        # text wears text tokens, never series colors
        "text.color": tx["primary"],
        "axes.titlecolor": tx["primary"],
        "axes.labelcolor": tx["secondary"],
        "xtick.labelcolor": tx["secondary"],
        "ytick.labelcolor": tx["secondary"],
        "legend.labelcolor": tx["primary"],
        "legend.frameon": False,
        # fonts: palette font, installed CJK fonts, DejaVu Sans floor - listed
        # as concrete families so matplotlib 3.11 falls back per glyph
        "font.family": families,
        "font.sans-serif": families,
        "axes.formatter.use_mathtext": False,
        # ramps
        "image.cmap": SEQ_CMAP,
        # output: 100dpi reference figure, 144dpi raster, editable svg text
        "figure.dpi": REFERENCE_DPI,
        "savefig.dpi": SAVE_DPI,
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.1,
        "svg.fonttype": "none",
    }


def apply_matplotlib(mode: str = "light") -> dict:
    """Install the house rcParams for ``mode`` and return the dict that was set."""
    rc = matplotlib_rc(mode)
    _register_colormaps()
    matplotlib.rcParams.update(rc)
    return rc


def bar_kwargs(mode: str = "light") -> dict:
    """kwargs for ax.bar/barh: a surface-colored edge draws the 2px surface gap.

    The 4px rounded data-end from marks-and-anatomy.md is not expressible as a
    plain Rectangle kwarg in matplotlib (FancyBboxPatch rounds every corner,
    the spec wants the baseline square); altair gets it via cornerRadiusEnd.
    """
    return {"edgecolor": palette.surface(mode)["surface"], "linewidth": GAP_PT}


def set_seaborn(mode: str = "light") -> dict:
    """Seaborn hook: house palette + the matplotlib rcParams; returns the rc dict.

    seaborn.set_theme(rc=...) only honours keys in its own style/context sets,
    so the full rc is applied after it to win on every key.
    """
    import seaborn as sns

    sns.set_theme(style="white", palette=palette.categorical(mode),
                  font="sans-serif")
    return apply_matplotlib(mode)


def plotly_template(mode: str = "light") -> str:
    """Build + register the plotly template; returns its name ('house-light').

    Registered in plotly.io.templates only - never set as the global default.
    hovermode 'x unified' suits line charts; override per figure for scatter.
    """
    import plotly.graph_objects as go
    import plotly.io as pio

    name = template_name(mode)
    sf = palette.surface(mode)
    tx = palette.text(mode)
    low, mid, high = palette.diverging(mode)
    axis = {
        "showline": False,
        "showgrid": True,
        "gridcolor": sf["grid"],
        "gridwidth": HAIRLINE_PX,
        "zeroline": True,
        "zerolinecolor": sf["axis"],
        "zerolinewidth": HAIRLINE_PX,
        "linecolor": sf["axis"],
        "tickcolor": sf["axis"],
        "ticks": "outside",
        "tickfont": {"color": tx["secondary"]},
        "title": {"font": {"color": tx["secondary"]}},
        "automargin": True,
    }
    layout = go.Layout(
        paper_bgcolor=sf["surface"],
        plot_bgcolor=sf["surface"],
        font={"family": css_font_family(), "color": tx["primary"]},
        title={"font": {"color": tx["primary"]}, "x": 0, "xref": "paper",
               "xanchor": "left", "y": 1, "yref": "container", "yanchor": "top",
               "pad": {"t": 12}},
        colorway=palette.categorical(mode),
        colorscale={"sequential": _scale(palette.sequential()),
                    "sequentialminus": _scale(list(reversed(palette.sequential()))),
                    "diverging": _scale([low, mid, high])},
        xaxis=dict(axis, showgrid=False),
        yaxis=axis,
        hovermode="x unified",
        hoverlabel={"font": {"family": css_font_family()}},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02,
                "xanchor": "left", "x": 0, "bgcolor": TRANSPARENT,
                "font": {"color": tx["secondary"]}},
        margin={"l": 48, "r": 24, "t": 80, "b": 40},   # room for title + legend
        bargap=0.25,          # bars never fill the slot (<= 24px cap is per chart)
        bargroupgap=0.05,
    )
    ring = {"color": sf["surface"], "width": GAP_PX}
    template = go.layout.Template(
        layout=layout,
        data={
            "bar": [go.Bar(marker={"line": ring})],
            "histogram": [go.Histogram(marker={"line": ring})],
            "scatter": [go.Scatter(line={"width": LINE_PX},
                                   marker={"size": MARKER_PX, "line": ring})],
            "scattergl": [go.Scattergl(line={"width": LINE_PX},
                                       marker={"size": MARKER_PX, "line": ring})],
            "heatmap": [go.Heatmap(colorscale=_scale(palette.sequential()))],
        },
    )
    pio.templates[name] = template
    return name


def _scale(colors: list[str]) -> list[list]:
    n = len(colors) - 1
    return [[i / n, c] for i, c in enumerate(colors)]


def altair_config(mode: str = "light") -> dict:
    """The Vega-Lite theme config dict (pure; altair_theme registers it)."""
    template_name(mode)
    sf = palette.surface(mode)
    tx = palette.text(mode)
    family = css_font_family()
    return {
        "config": {
            "background": sf["surface"],
            "font": family,
            "view": {"stroke": None},
            "axis": {
                "domainColor": sf["axis"], "domainWidth": HAIRLINE_PX,
                "gridColor": sf["grid"], "gridWidth": HAIRLINE_PX,
                "gridDash": [],
                "tickColor": sf["axis"], "tickWidth": HAIRLINE_PX,
                "labelColor": tx["secondary"], "titleColor": tx["secondary"],
                "labelFont": family, "titleFont": family,
                "titleFontWeight": "normal",
            },
            "axisX": {"grid": False},
            "legend": {
                "orient": "top", "direction": "horizontal",
                "labelColor": tx["secondary"], "titleColor": tx["secondary"],
                "labelFont": family, "titleFont": family,
                "titleFontWeight": "normal",
                "symbolType": "circle", "symbolSize": MARKER_PX ** 2,
            },
            "title": {"color": tx["primary"], "subtitleColor": tx["secondary"],
                      "font": family, "subtitleFont": family,
                      "anchor": "start", "fontWeight": "normal"},
            "header": {"labelColor": tx["secondary"], "titleColor": tx["primary"],
                       "labelFont": family, "titleFont": family},
            "text": {"color": tx["primary"], "font": family},
            "range": {
                "category": palette.categorical(mode),
                "ordinal": palette.sequential(),
                "heatmap": palette.sequential(),
                "ramp": palette.sequential(),
                "diverging": list(palette.diverging(mode)),
            },
            "mark": {"color": palette.categorical(mode)[0]},
            "bar": {"stroke": sf["surface"], "strokeWidth": GAP_PX,
                    "cornerRadiusEnd": BAR_CORNER_PX},
            "rect": {"stroke": sf["surface"], "strokeWidth": GAP_PX},
            "line": {"strokeWidth": LINE_PX, "strokeJoin": "round",
                     "strokeCap": "round"},
            "point": {"filled": True, "size": MARKER_PX ** 2,
                      "stroke": sf["surface"], "strokeWidth": GAP_PX},
            "circle": {"size": MARKER_PX ** 2,
                       "stroke": sf["surface"], "strokeWidth": GAP_PX},
            "area": {"opacity": AREA_OPACITY, "line": {"strokeWidth": LINE_PX}},
        }
    }


def altair_theme(mode: str = "light") -> str:
    """Register the theme as 'house-light'/'house-dark' (not enabled); returns the name."""
    import altair as alt

    name = template_name(mode)
    config = altair_config(mode)

    def house_theme():
        return config

    if hasattr(alt, "theme") and hasattr(alt.theme, "register"):
        alt.theme.register(name, enable=False)(house_theme)   # altair >= 5.5
    else:  # altair < 5.5
        alt.themes.register(name, house_theme)
    return name


def bokeh_attrs(mode: str = "light") -> dict:
    """The theme json dict (pure; bokeh_theme wraps it in bokeh.themes.Theme)."""
    template_name(mode)
    sf = palette.surface(mode)
    tx = palette.text(mode)
    family = css_font_family()
    return {
        "attrs": {
            "Plot": {
                "background_fill_color": sf["surface"],
                "border_fill_color": sf["surface"],
                "outline_line_color": None,
            },
            "Axis": {
                "axis_line_color": sf["axis"],
                "axis_line_width": HAIRLINE_PX,
                "major_tick_line_color": sf["axis"],
                "major_tick_line_width": HAIRLINE_PX,
                "minor_tick_line_color": None,
                "major_label_text_color": tx["secondary"],
                "major_label_text_font": family,
                "axis_label_text_color": tx["secondary"],
                "axis_label_text_font": family,
                "axis_label_text_font_style": "normal",
            },
            "Grid": {
                "grid_line_color": sf["grid"],
                "grid_line_width": HAIRLINE_PX,
                "grid_line_dash": [],
            },
            "Title": {
                "text_color": tx["primary"],
                "text_font": family,
                "text_font_style": "normal",
                "align": "left",
            },
            "Legend": {
                "background_fill_color": sf["surface"],
                "background_fill_alpha": 0,
                "border_line_color": None,
                "label_text_color": tx["secondary"],
                "label_text_font": family,
                "title_text_color": tx["secondary"],
                "title_text_font": family,
                "orientation": "horizontal",
                "location": "top_left",
            },
            "BaseColorBar": {
                "background_fill_color": sf["surface"],
                "bar_line_color": None,
                "major_tick_line_color": sf["axis"],
                "major_label_text_color": tx["secondary"],
                "major_label_text_font": family,
                "title_text_color": tx["secondary"],
                "title_text_font": family,
            },
            "Line": {"line_width": LINE_PX, "line_join": "round",
                     "line_cap": "round"},
            "Scatter": {"size": MARKER_PX, "line_color": sf["surface"],
                        "line_width": GAP_PX},
            "VBar": {"line_color": sf["surface"], "line_width": GAP_PX},
            "HBar": {"line_color": sf["surface"], "line_width": GAP_PX},
            "VArea": {"fill_alpha": AREA_OPACITY},
            "HArea": {"fill_alpha": AREA_OPACITY},
        }
    }


def bokeh_theme(mode: str = "light"):
    """bokeh.themes.Theme for ``mode``; pass as ``theme=`` to file_html/curdoc."""
    from bokeh.themes import Theme

    return Theme(json=bokeh_attrs(mode))


def bokeh_palette(mode: str = "light") -> tuple[str, ...]:
    """Categorical slots as a tuple, bokeh's palette shape."""
    return tuple(palette.categorical(mode))


if __name__ == "__main__":
    import argparse
    import json

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        prog="style.py",
        description="Dump the house style (matplotlib rcParams, altair config, "
                    "bokeh theme attrs) for one mode as JSON.")
    parser.add_argument("mode", nargs="?", default="light", choices=MODES,
                        help="color mode (default: light)")
    mode = parser.parse_args().mode
    rc = {k: (str(v) if k == "axes.prop_cycle" else v)
          for k, v in matplotlib_rc(mode).items()}
    print(json.dumps({"matplotlib": rc, "altair": altair_config(mode),
                      "bokeh": bokeh_attrs(mode)}, indent=1))
