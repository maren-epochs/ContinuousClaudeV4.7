#!/usr/bin/env python3
"""Chart-form recommender: the dataviz skill's choosing-a-form heuristic as code.

    recommend(frame_or_profile, job=None)
        -> {job, job_inferred, form, reason, encoding, warnings}

``job`` is always the job used (given, or inferred when None); ``job_inferred``
says which.

The data's job picks the form, and sometimes the right form is not a chart
(stat tile, KPI row, meter, table).  Jobs:

    magnitude, identity, polarity, headline, change-over-time, distribution,
    relationship, part-to-whole, ranking, flow, spatial

When ``job`` is None it is inferred from the column profile (row count, column
kinds, cardinality, presence of a time or geo column).

Refusals (each names its anti-patterns.md entry in ``warnings``):

  * never a dual axis: several measures of different scale (over time, or as
    grouped bars over categories) -> small multiples by measure (or index to a
    common base) on ONE axis.  Scales compare
    pairwise (``scale`` = floor log10 of max |value|): the largest same-scale
    group is the base and the warning lists only the measures outside it.
  * part-to-whole past 5 slices -> bar, never a pie/donut
  * 2 slices -> meter; a one-bar chart -> stat tile
  * categorical series past 8 -> fold into Other / small multiples
  * more than ~7 color classes carrying meaning -> table

Color jobs (``encoding["color"]``; color-formula.md 'The four jobs' + Ordinal):

  * ``"single"``      one color, categorical slot 1, for every mark.  Used when
                      position/length already carries the value: a one-measure
                      bar over nominal categories, a single line, a histogram,
                      a dot plot.  Never a value-ramp on nominal categories
                      (anti-patterns.md "A value-ramp on nominal categories":
                      it double-encodes bar length as hue).
  * ``"ordinal"``     a one-hue ramp in monotone lightness steps across
                      ORDERED categories (profile column ``"ordered": true`` -
                      tiers, funnel stages, age bands); those bars keep their
                      natural order (no value sort, which would scramble the
                      ramp).  Light end >= 2:1 on the surface; validate the
                      picks with validate_palette ``--ordinal``.
  * a measure name    the color channel itself carries a continuous measure
                      (heatmap cells, choropleth regions) - a sequential ramp
                      of it.  ``"sequential"`` is never returned literally:
                      only a continuous measure earns the sequential job.
  * a dimension name / ``"measure"``  categorical identity, slots 1..N.
  * ``"diverging"``, ``"1 hue + gray"``  polarity / emphasis.

The core is pure stdlib and works on a plain-dict profile::

    {"n_rows": int,
     "columns": [{"name", "kind": numeric|categorical|temporal|boolean|text|geo,
                  "cardinality", "is_sorted_time"?, "scale"?, "ordered"?,
                  "is_series"?}],
     "measures": [names], "dimensions": [names]}

Series identity over time: a categorical column colors a line form only when it
is a series key - the frame is long-format, several rows (categories) per time
value, each category recurring across time (stocks: date x symbol).  A per-row
attribute (one value per time value, e.g. seattle-weather ``weather``) is never
the color; it is dropped from the encoding with a warning naming it.
``"is_series"`` (set by profile_frame from the data) decides when present;
otherwise the profile shape decides: n_rows > the time column's cardinality.

``profile_frame(df)`` builds that profile from a pandas DataFrame (pandas is
imported lazily there and in the CLI only).

CLI:
    py -3.13 tools/viz/recommend.py data.csv [--job JOB] [--json]
    py -3.13 tools/viz/recommend.py data.parquet --json
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from pathlib import Path

JOBS = (
    "magnitude",
    "identity",
    "polarity",
    "headline",
    "change-over-time",
    "distribution",
    "relationship",
    "part-to-whole",
    "ranking",
    "flow",
    "spatial",
)

# Every form this module can return, named as choosing-a-form.md names them.
FORMS = frozenset(
    {
        "stat tile",
        "KPI row",
        "hero figure",
        "meter",
        "table",
        "bar",
        "heatmap",
        "line",
        "area",
        "multi-line",
        "grouped bar",
        "stacked bar",
        "emphasis",
        "diverging bar",
        "line vs baseline",
        "diverging stacked bar",
        "dumbbell",
        "small multiples",
        "scatter",
        "bubble",
        "histogram",
        "dot plot",
        "slope",
        "sankey",
        "choropleth",
    }
)

SERIES_CEILING = 8  # token ceiling for categorical hues
ALL_PAIRS_CAP = 3  # scatter / bubble / choropleth / small multiples
COLOR_CLASS_CAP = 7  # past ~7 meaningful classes -> table
PIE_SLICE_CAP = 5  # part-to-whole past this is a bar, never a pie

# anti-patterns.md entry names, cited verbatim in warnings
AP_DUAL_AXIS = "Dual-axis charts (two y-scales on one plot)"
AP_PAST_8 = "Cycling / generating hues past 8"
AP_PIE = "A donut/pie for comparing close values"
AP_ONE_BAR = "A one-bar bar chart, or a 2-slice pie"
AP_7_CLASSES = "More than ~7 color classes carrying meaning"
AP_ONE_NUMBER = "Eight categorical hues when the story is one number"
LADDER_ALL_PAIRS = "series-count ladder: all-pairs forms cap at three"
FOLD = "fold into Other / small multiples"

FLOW_NAMES = {"source", "target", "from", "to", "origin", "destination", "src", "dst"}
GEO_NAMES = {
    "lat",
    "latitude",
    "lon",
    "lng",
    "long",
    "longitude",
    "geometry",
    "geom",
    "wkt",
    "fips",
    "iso",
    "iso2",
    "iso3",
    "iso_code",
    "iso_a2",
    "iso_a3",
    "country_code",
    "postcode",
    "postal_code",
    "zip",
    "zipcode",
    "zip_code",
    "geoid",
    "h3",
}
TIME_NAMES = {
    "year",
    "month",
    "quarter",
    "week",
    "period",
    "date",
    "fy",
    "fiscal_year",
    "yr",
    "day",
    "time",
    "timestamp",
    "datetime",
    "ds",
}
TEXT_NAMES = {
    "note",
    "notes",
    "comment",
    "comments",
    "description",
    "message",
    "text",
    "title",
    "body",
    "url",
    "summary",
    "remarks",
}


# --------------------------------------------------------------------------
# profile helpers
# --------------------------------------------------------------------------
class _Ctx:
    """Derived view of a profile: column lists by kind, in profile order."""

    def __init__(self, profile):
        """Index the profile's columns by name and split them into kind lists."""
        self.profile = profile
        self.n_rows = int(profile.get("n_rows") or 0)
        cols = list(profile.get("columns") or [])
        self.by_name = {c["name"]: c for c in cols}
        kinds = {c["name"]: c.get("kind", "categorical") for c in cols}
        measures = profile.get("measures")
        if measures is None:
            measures = [c["name"] for c in cols if kinds[c["name"]] == "numeric"]
        self.measures = [m for m in measures if m in kinds]
        self.temporal = [c["name"] for c in cols if kinds[c["name"]] == "temporal"]
        self.cats = [
            c["name"] for c in cols if kinds[c["name"]] in ("categorical", "boolean")
        ]
        self.geo = [c["name"] for c in cols if kinds[c["name"]] == "geo"]
        self.text = [c["name"] for c in cols if kinds[c["name"]] == "text"]

    def card(self, name, default=None):
        """Cardinality of column name; default (or n_rows) when the profile omits it."""
        c = self.by_name.get(name) or {}
        v = c.get("cardinality")
        if v is None:
            return default if default is not None else self.n_rows
        return int(v)

    def scale(self, name):
        """Profile scale (floor log10 of max |v|) of column name, or None."""
        c = self.by_name.get(name) or {}
        return c.get("scale")

    def ordered(self, name):
        """True when the column is an ordinal category (profile ``"ordered": true``)."""
        c = self.by_name.get(name) or {}
        return bool(c.get("ordered")) and c.get("kind") in ("categorical", "boolean")

    def names(self, measures):
        """Comma-joined measure names, each suffixed with its scale when known."""
        return ", ".join(
            f"{m} (1e{self.scale(m)})" if self.scale(m) is not None else m
            for m in measures
        )

    def is_series(self, cat):
        """True when ``cat`` is a series key over the first temporal column.

        The profile's ``"is_series"`` decides when present.  Otherwise a series
        needs several rows per time value (n_rows > time cardinality); one row
        per time value makes every categorical a per-row attribute.  An unknown
        time cardinality cannot disprove it, so the category stays a series.
        """
        c = self.by_name.get(cat) or {}
        if c.get("is_series") is not None:
            return bool(c["is_series"])
        if not self.temporal:
            return True
        t_card = (self.by_name.get(self.temporal[0]) or {}).get("cardinality")
        if t_card is None or not self.n_rows:
            return True
        return self.n_rows > int(t_card)

    def series_split(self):
        """(series keys, per-row attribute warnings) for the categoricals over time."""
        series = [c for c in self.cats if self.is_series(c)]
        t = self.temporal[0] if self.temporal else "row"
        warns = [_attr_warning(c, t) for c in self.cats if c not in series]
        return series, warns

    def scale_groups(self, measures):
        """Pairwise scale comparison -> (base group, off-scale, unknown-scale).

        Two measures share a scale when their ``scale`` (floor log10 of max |v|)
        is equal.  The base is the largest same-scale group (ties: the group of
        the earliest measure); everything else with a known scale is off-scale.
        """
        known = [m for m in measures if self.scale(m) is not None]
        unknown = [m for m in measures if self.scale(m) is None]
        if not known:
            return [], [], unknown
        counts = {}
        for m in known:
            counts[self.scale(m)] = counts.get(self.scale(m), 0) + 1
        first = {}
        for i, m in enumerate(known):
            first.setdefault(self.scale(m), i)
        dominant = max(counts, key=lambda s: (counts[s], -first[s]))
        base = [m for m in known if self.scale(m) == dominant]
        off = [m for m in known if self.scale(m) != dominant]
        return base, off, unknown


def _attr_warning(cat, t):
    """Warning that cat is a per-row attribute over t, not a line series."""
    return (
        f"'{cat}' is a per-row attribute (one value per '{t}'), not a series "
        f"identity - a line cannot be colored by it, so it is left out of the "
        f"encoding; facet or filter by it, or aggregate per '{t}'"
    )


def _dual_axis_warning(ctx, off, base, unknown):
    """Dual-axis refusal naming the off-scale and unknown-scale measures."""
    parts = []
    if off:
        parts.append(f"{ctx.names(off)} differ in scale from {ctx.names(base)}")
    if unknown:
        verb = "has an unknown scale" if len(unknown) == 1 else "have unknown scales"
        parts.append(f"{', '.join(unknown)} {verb}")
    return (
        f"{AP_DUAL_AXIS}: measures {'; '.join(parts)} - small multiples, or index "
        "them to a common base (=100 at t0) on ONE axis; never a second y-axis"
    )


def _out(form, reason, encoding, warnings=None):
    """Recommendation dict {form, reason, encoding, warnings}; x/y default to None."""
    assert form in FORMS, form
    enc = {"x": None, "y": None}
    enc.update(encoding)
    return {
        "form": form,
        "reason": reason,
        "encoding": enc,
        "warnings": list(warnings or []),
    }


def _first_dims(ctx):
    """The first non-empty of the categorical, temporal and geo column lists."""
    for group in (ctx.cats, ctx.temporal, ctx.geo):
        if group:
            return group
    return []


def _item_dims(ctx):
    """Categorical columns when there are any, else the geo columns."""
    return ctx.cats if ctx.cats else ctx.geo


def _single_value(ctx):
    """True when the data is one number: one row, or one category with one measure."""
    if not ctx.measures:
        return False
    if ctx.n_rows == 1:
        return True
    dims = _first_dims(ctx)
    return bool(dims) and ctx.card(dims[0]) == 1 and len(ctx.measures) == 1


def _bar_color(ctx, x, encoding):
    """One-measure bar over ``x``: slot-1 single color, or the ordinal ramp.

    Nominal categories get ONE color - bar length carries magnitude, and a
    value-ramp would re-encode it (anti-patterns.md "A value-ramp on nominal
    categories").  Ordinal categories take the "ordinal" job (one-hue ramp) in
    their natural order, so any value sort is dropped.
    """
    enc = dict(encoding)
    if ctx.ordered(x):
        enc["color"] = "ordinal"
        enc.pop("sort", None)
    else:
        enc["color"] = "single"
    return enc


def _stat_tile(ctx, warnings=None):
    """Stat-tile recommendation for the first measure (x = first time column)."""
    m = ctx.measures[0]
    enc = {"x": ctx.temporal[0] if ctx.temporal else None, "y": m}
    return _out(
        "stat tile",
        "choosing-a-form 'Is it even a chart?': a single current value "
        "(+ maybe a trend) is a stat tile, not a one-bar bar chart.",
        enc,
        warnings,
    )


def _series_fold(ctx, form, encoding, reason, warnings):
    """Apply the categorical series-count ladder; past 8 -> small multiples.

    The series dimension is ``encoding["color"]``.
    """
    series_dim = encoding["color"]
    n = ctx.card(series_dim)
    warnings = list(warnings)
    if n > SERIES_CEILING:
        warnings.append(
            f"{AP_PAST_8}: '{series_dim}' has {n} series - "
            f"{FOLD} (never generate a 9th hue)"
        )
        enc = dict(encoding)
        enc.pop("color", None)
        enc["facet"] = series_dim
        return _out(
            "small multiples",
            f"series-count ladder: {n} series exceed the token ceiling of "
            f"{SERIES_CEILING}, so facet into small multiples (one per "
            f"'{series_dim}') or fold the tail into Other.",
            enc,
            warnings,
        )
    enc = dict(encoding)
    if n >= 4:
        enc["label"] = "direct"
    return _out(form, reason, enc, warnings)


# --------------------------------------------------------------------------
# job rules (one function per row of the table)
# --------------------------------------------------------------------------
def _magnitude_counts(ctx, warnings):
    """Magnitude without a measure: row counts per first dimension, else the table."""
    xs = _first_dims(ctx)
    if not xs:
        return _out(
            "table",
            "no measures or dimensions to chart; show the table.",
            {},
            warnings,
        )
    return _out(
        "bar",
        "choosing-a-form 'Compare magnitude, low -> high' -> bar / column "
        "of row counts per category; bar length carries the count, so one "
        "color (ordinal categories: one-hue ramp in category order).",
        _bar_color(ctx, xs[0], {"x": xs[0], "y": "count", "sort": "-y"}),
        warnings,
    )


def _magnitude(ctx, warnings=()):
    """Magnitude job: bar of the first measure (or counts), heatmap for 2 dims."""
    warnings = list(warnings)
    if _single_value(ctx):
        warnings.append(f"{AP_ONE_BAR}: the number is the chart - use a stat tile")
        return _stat_tile(ctx, warnings)
    if not ctx.measures:
        return _magnitude_counts(ctx, warnings)
    m = ctx.measures[0]
    if len(ctx.cats) >= 2:
        return _out(
            "heatmap",
            "choosing-a-form 'Compare magnitude, low -> high': a grid of two "
            "dimensions takes a heatmap with a sequential ramp.",
            {"x": ctx.cats[1], "y": ctx.cats[0], "color": m},
            warnings,
        )
    xs = _first_dims(ctx)
    if not xs:
        return _out(
            "table",
            "magnitude needs a dimension to compare across; with measures only, "
            "show the table (or ask for 'distribution').",
            {"y": m},
            warnings,
        )
    return _out(
        "bar",
        "choosing-a-form 'Compare magnitude, low -> high' -> bar / column; bar "
        "length carries the value, so every bar takes one color (slot 1) - "
        "no value-ramp on nominal categories (ordinal categories: one-hue "
        "ramp in category order).",
        _bar_color(ctx, xs[0], {"x": xs[0], "y": m, "sort": "-y"}),
        warnings,
    )


def _measures_on_one_axis(ctx, form, x, reason, warnings):
    """Several measures as categorical series on ONE axis - only when they share a
    scale.  Any off-scale (or unknown-scale) measure -> small multiples by measure
    with the dual-axis warning; never a shared or second y-axis."""
    base, off, unknown = ctx.scale_groups(ctx.measures)
    if not off and not unknown:
        return _out(
            form,
            reason,
            {"x": x, "y": list(ctx.measures), "color": "measure"},
            warnings,
        )
    return _out(
        "small multiples",
        f"choosing-a-form: a {form} of measures of different scale would need a "
        "second axis - one panel per measure, never a dual axis.",
        {"x": x, "y": "value", "facet": "measure"},
        [_dual_axis_warning(ctx, off, base, unknown)] + list(warnings),
    )


def _identity_over_time(ctx, reason):
    """Identity over the first time column; None when there is no measure to draw."""
    x = ctx.temporal[0]
    series, attr_warns = ctx.series_split()
    if series and ctx.measures:
        enc = {"x": x, "y": ctx.measures[0], "color": series[0]}
        return _series_fold(ctx, "multi-line", enc, reason, attr_warns)
    if len(ctx.measures) >= 2:
        return _measures_on_one_axis(ctx, "multi-line", x, reason, attr_warns)
    if ctx.measures:
        return _out(
            "emphasis",
            "choosing-a-form 'One series is the point, rest are context' -> "
            "emphasis: one series in the accent hue, nothing else to tell apart.",
            {"x": x, "y": ctx.measures[0], "color": "1 hue + gray"},
            attr_warns,
        )
    return None


def _identity_by_category(ctx, reason):
    """Identity across categorical (else geo) dimensions: grouped bar or emphasis."""
    xs = _item_dims(ctx)
    if not xs:
        return _out("table", "no dimension carries identity; show the table.", {})
    if len(xs) >= 2 and ctx.measures:
        # the lower-cardinality dimension is the series (fewest hues); ties -> second
        x, series = xs[0], xs[1]
        if ctx.card(series) > ctx.card(x):
            x, series = series, x
        enc = {"x": x, "y": ctx.measures[0], "color": series}
        return _series_fold(ctx, "grouped bar", enc, reason, [])
    if len(ctx.measures) >= 2:
        return _measures_on_one_axis(ctx, "grouped bar", xs[0], reason, [])
    if ctx.measures:
        return _out(
            "emphasis",
            "choosing-a-form 'One series is the point, rest are context' -> "
            "emphasis (highlight one, gray the rest); categorical color would "
            "bury the one bar that matters.",
            {"x": xs[0], "y": ctx.measures[0], "color": "1 hue + gray"},
        )
    return _out(
        "bar",
        "no measure: row counts per category, one color.",
        _bar_color(ctx, xs[0], {"x": xs[0], "y": "count", "sort": "-y"}),
    )


def _identity(ctx):
    """Identity job: multi-line / grouped bar by series, emphasis for one series."""
    if _single_value(ctx):
        return _stat_tile(
            ctx, [f"{AP_ONE_NUMBER}: one value is a stat tile, not a series"]
        )
    reason = (
        "choosing-a-form 'Tell distinct series apart' -> grouped/stacked bar or "
        "multi-line with a categorical color job."
    )
    if ctx.temporal:
        out = _identity_over_time(ctx, reason)
        if out is not None:
            return out
    return _identity_by_category(ctx, reason)


def _polarity(ctx):
    """Polarity job: diverging bar, line vs baseline, or diverging stacked bar."""
    if _single_value(ctx):
        return _stat_tile(ctx)
    reason = (
        "choosing-a-form 'Above/below a baseline; delta to target' -> diverging "
        "bar, or line vs baseline over time, with a diverging color job."
    )
    if not ctx.measures:
        return _magnitude(ctx, ["polarity needs a signed measure; none found"])
    m = ctx.measures[0]
    if ctx.temporal:
        enc = {"x": ctx.temporal[0], "y": m, "color": "diverging"}
        series, attr_warns = ctx.series_split()
        if series:
            enc["color"] = series[0]
            return _series_fold(ctx, "line vs baseline", enc, reason, attr_warns)
        return _out("line vs baseline", reason, enc, attr_warns)
    xs = ctx.cats or ctx.geo
    if len(xs) >= 2:
        warnings = []
        if ctx.card(xs[1]) > COLOR_CLASS_CAP:
            warnings.append(
                f"{AP_7_CLASSES}: '{xs[1]}' has {ctx.card(xs[1])} levels - "
                "collapse the scale or show a table"
            )
        return _out(
            "diverging stacked bar",
            "choosing-a-form 'Ordered-scale share (Likert, sentiment, "
            "agree<->disagree)' -> diverging stacked bar centered on neutral.",
            {"x": m, "y": xs[0], "color": xs[1], "orientation": "horizontal"},
            warnings,
        )
    if xs:
        return _out(
            "diverging bar",
            reason,
            {"x": xs[0], "y": m, "color": "diverging", "sort": "-y"},
        )
    return _out(
        "table", "polarity needs a dimension to compare against a baseline.", {"y": m}
    )


def _headline(ctx):
    """Headline job: stat tile for one measure, KPI row for a handful, else table."""
    n = len(ctx.measures)
    if n == 0:
        return _out("table", "no measure to headline; show the table.", {})
    if n == 1:
        return _stat_tile(ctx)
    if n <= COLOR_CLASS_CAP:
        return _out(
            "KPI row",
            "choosing-a-form 'Is it even a chart?': a handful of headline "
            "numbers is a KPI row of stat tiles, not a grouped bar chart.",
            {"x": ctx.temporal[0] if ctx.temporal else None, "y": list(ctx.measures)},
        )
    return _out(
        "table",
        f"{n} headline numbers is past a handful; show a table "
        "(choosing-a-form 'More than ~7 classes').",
        {"y": list(ctx.measures)},
    )


def _trend_of_measures(ctx, t, series, attr_warns):
    """Trend of several measures: one shared axis when same-scale, else panels."""
    base, off, unknown = ctx.scale_groups(ctx.measures)
    same = not off and not unknown
    if same and not series:
        return _out(
            "multi-line",
            "choosing-a-form 'Trend over time': several measures on one "
            "shared axis, same scale, categorical color per measure.",
            {"x": t, "y": list(ctx.measures), "color": "measure"},
            attr_warns,
        )
    warnings = [] if same else [_dual_axis_warning(ctx, off, base, unknown)]
    warnings += attr_warns
    enc = {"x": t, "y": "value", "facet": "measure"}
    if series:
        s = series[0]
        enc["color"] = s
        if ctx.card(s) > SERIES_CEILING:
            warnings.append(f"{AP_PAST_8}: '{s}' has {ctx.card(s)} series - {FOLD}")
            enc.pop("color")
    why = (
        "several same-scale measures and a series dimension: one panel per "
        "measure, one line per series"
        if same
        else "measures of different scale: one line per panel, never a dual axis"
    )
    return _out(
        "small multiples",
        f"choosing-a-form 'Trend over time' with {why}.",
        enc,
        warnings,
    )


def _is_before_after(ctx, t):
    """True for two time values, a category and one measure (before -> after)."""
    return bool(ctx.card(t) == 2 and ctx.cats and len(ctx.measures) == 1)


def _change_over_time(ctx):
    """Change-over-time job: line, multi-line, dumbbell or small multiples."""
    if not ctx.temporal:
        return _magnitude(
            ctx,
            [
                (
                    "no temporal column in profile; falling back to "
                    "magnitude (choosing-a-form 'Trend over time' needs time)"
                )
            ],
        )
    if _single_value(ctx):
        return _stat_tile(ctx)
    t = ctx.temporal[0]
    reason = "choosing-a-form 'Trend over time' -> line (area for a single series)."
    if not ctx.measures:
        return _out(
            "line",
            reason + " No measure: row counts per period.",
            {"x": t, "y": "count", "color": "single"},
        )
    if _is_before_after(ctx, t):
        return _out(
            "dumbbell",
            "choosing-a-form 'Before -> after per item' -> dumbbell (1 hue, 2 shades).",
            {
                "x": ctx.measures[0],
                "y": ctx.cats[0],
                "color": t,
                "orientation": "horizontal",
                "sort": "-x",
            },
        )
    series, attr_warns = ctx.series_split()
    if len(ctx.measures) >= 2:
        return _trend_of_measures(ctx, t, series, attr_warns)
    m = ctx.measures[0]
    if series:
        enc = {"x": t, "y": m, "color": series[0]}
        return _series_fold(ctx, "multi-line", enc, reason, attr_warns)
    return _out("line", reason, {"x": t, "y": m, "color": "single"}, attr_warns)


def _distribution(ctx):
    """Distribution job: dot plot per category, else a histogram."""
    if not ctx.measures:
        return _magnitude(
            ctx,
            [
                (
                    "distribution needs a numeric measure; none found - "
                    "showing counts per category"
                )
            ],
        )
    if _single_value(ctx):
        return _stat_tile(ctx)
    m = ctx.measures[0]
    if ctx.cats:
        y = ctx.cats[0]
        warnings = []
        if ctx.card(y) > SERIES_CEILING:
            warnings.append(
                f"'{y}' has {ctx.card(y)} groups - sort by median and "
                "direct-label the extremes; no categorical hues"
            )
        return _out(
            "dot plot",
            "distribution per category: a dot plot (one row per category, "
            "one hue) shows spread without a hue per group.",
            {"x": m, "y": y, "color": "single", "sort": "-x"},
            warnings,
        )
    return _out(
        "histogram",
        "distribution of a single measure: histogram (one color; bar height "
        "carries the count).",
        {"x": m, "y": "count", "color": "single"},
    )


def _relationship(ctx):
    """Relationship job: scatter/bubble of the first measures, heatmap fallback."""
    if len(ctx.measures) < 2:
        if len(ctx.cats) >= 2 and ctx.measures:
            return _out(
                "heatmap",
                "relationship between two dimensions: a heatmap grid with a "
                "sequential ramp.",
                {"x": ctx.cats[1], "y": ctx.cats[0], "color": ctx.measures[0]},
            )
        return _out(
            "table",
            "relationship needs two numeric measures; fewer found - show the "
            "table or ask for 'magnitude'.",
            {"y": ctx.measures[0] if ctx.measures else None},
        )
    x, y = ctx.measures[0], ctx.measures[1]
    form = "bubble" if len(ctx.measures) >= 3 else "scatter"
    enc = {"x": x, "y": y}
    if form == "bubble":
        enc["size"] = ctx.measures[2]
    reason = (
        f"relationship between two measures -> {form}; one hue unless a "
        "series dimension is the subject."
    )
    warnings = []
    if ctx.cats:
        c = ctx.cats[0]
        n = ctx.card(c)
        if n > ALL_PAIRS_CAP:
            warnings.append(
                f"{LADDER_ALL_PAIRS}: '{c}' has {n} series on an all-pairs "
                f"form ({form}) - {FOLD}"
            )
            enc["facet"] = c
            return _out(
                "small multiples",
                f"{form} faceted by '{c}': all-pairs forms seat at most "
                f"{ALL_PAIRS_CAP} hues.",
                enc,
                warnings,
            )
        enc["color"] = c
    return _out(form, reason, enc, warnings)


_PART_REASON = (
    "choosing-a-form 'Part-to-whole' -> stacked bar (horizontal for many / "
    "long-named categories); never a pie."
)


def _parts_without_category(ctx):
    """Part-to-whole with no category: measures as parts (one row), else the table."""
    if len(ctx.measures) >= 2 and ctx.n_rows == 1:
        return _out(
            "stacked bar",
            _PART_REASON + " Parts are the measures.",
            {
                "x": "share",
                "y": None,
                "color": "measure",
                "orientation": "horizontal",
            },
        )
    return _out(
        "table",
        "part-to-whole needs a category of parts; show the table.",
        {"y": ctx.measures[0] if ctx.measures else None},
    )


def _per_row_parts_note(ctx, parts, use_time):
    """[note] when parts is a per-row attribute over the time column, else []."""
    if not ctx.temporal or use_time:
        return []
    t = ctx.temporal[0]
    return [
        (
            f"'{parts}' is a per-row attribute (one value per '{t}'), not a "
            f"series over '{t}' - a stacked bar per '{t}' would hold one "
            "segment; showing its share of the whole instead (aggregate per "
            "period for composition over time)"
        )
    ]


def _stacked_parts(ctx, x, parts, value, pre):
    """Stacked bar of parts per x; a table past ~7 classes."""
    n = ctx.card(parts)
    if n > COLOR_CLASS_CAP:
        return _out(
            "table",
            f"part-to-whole with {n} parts per '{x}': past ~7 classes the "
            "segments blur - a table (or table + chart).",
            {"x": x, "y": value, "color": parts},
            pre
            + [
                (
                    f"{AP_7_CLASSES}: '{parts}' has {n} classes - a table, "
                    "or table + chart"
                )
            ],
        )
    return _out(
        "stacked bar",
        _PART_REASON,
        {"x": x, "y": value, "color": parts, "sort": "-y"},
        pre,
    )


def _whole_of_parts(ctx, parts, value, pre):
    """One whole split by parts: meter for 2, sorted bar past the pie cap, else bar."""
    n = ctx.card(parts)
    if n == 2:
        return _out(
            "meter",
            "choosing-a-form 'A single ratio against a limit' -> meter "
            "(same-ramp track), not a pie of 2 slices.",
            {"x": parts, "y": value},
            pre + [f"{AP_ONE_BAR}: 2 slices is one ratio - a meter or the number"],
        )
    if n > PIE_SLICE_CAP:
        return _out(
            "bar",
            f"part-to-whole with {n} parts: a sorted bar with share labels; "
            f"past {PIE_SLICE_CAP} slices a pie cannot be read.",
            _bar_color(
                ctx,
                parts,
                {"x": parts, "y": value, "orientation": "horizontal", "sort": "-y"},
            ),
            pre
            + [
                (
                    f"{AP_PIE}: {n} slices - a bar, or the numbers "
                    f"(part-to-whole at a glance only, <= {PIE_SLICE_CAP} "
                    "segments)"
                )
            ],
        )
    return _out(
        "stacked bar",
        _PART_REASON,
        {
            "x": "share",
            "y": None,
            "color": parts,
            "orientation": "horizontal",
            "sort": "-y",
        },
        pre,
    )


def _part_to_whole(ctx):
    """Part-to-whole job: stacked bar, meter or sorted bar; never a pie."""
    if _single_value(ctx):
        return _stat_tile(ctx, [f"{AP_ONE_BAR}: a single part is a stat tile"])
    if not ctx.cats:
        return _parts_without_category(ctx)
    parts = ctx.cats[0]
    value = ctx.measures[0] if ctx.measures else "count"
    # composition over time needs the parts to be a series per time value; a
    # per-row attribute would give one segment per bar, so drop the time axis
    use_time = bool(ctx.temporal) and ctx.is_series(parts)
    pre = _per_row_parts_note(ctx, parts, use_time)
    if use_time:
        return _stacked_parts(ctx, ctx.temporal[0], parts, value, pre)
    if len(ctx.cats) >= 2:
        return _stacked_parts(ctx, ctx.cats[0], ctx.cats[1], value, pre)
    return _whole_of_parts(ctx, parts, value, pre)


def _ranking(ctx):
    """Ranking job: horizontal bar sorted by value, dumbbell for two periods."""
    if _single_value(ctx):
        return _stat_tile(ctx, [f"{AP_ONE_BAR}: nothing to rank"])
    reason = (
        "choosing-a-form 'Compare magnitude, low -> high' sorted: a horizontal "
        "bar ordered by value, one hue."
    )
    if not ctx.measures:
        return _magnitude(ctx, ["ranking needs a measure; ranking by row count"])
    m = ctx.measures[0]
    if ctx.temporal and ctx.card(ctx.temporal[0]) == 2 and ctx.cats:
        return _out(
            "dumbbell",
            "choosing-a-form 'Before -> after per item' -> dumbbell, rows "
            "sorted by the after value.",
            {
                "x": m,
                "y": ctx.cats[0],
                "color": ctx.temporal[0],
                "orientation": "horizontal",
                "sort": "-x",
            },
        )
    xs = _item_dims(ctx)
    if not xs:
        return _out(
            "table",
            "ranking needs an item dimension; show the sorted table.",
            {"y": m, "sort": "-y"},
        )
    return _out(
        "bar",
        reason,
        {
            "x": xs[0],
            "y": m,
            "color": "single",
            "orientation": "horizontal",
            "sort": "-y",
        },
    )


def _flow(ctx):
    """Flow job: sankey between the first two (flow-named first) dimensions."""
    dims = ctx.cats + ctx.geo
    if len(dims) < 2:
        return _out(
            "table",
            "flow needs a source and a target dimension; fewer found - show the table.",
            {"y": ctx.measures[0] if ctx.measures else None},
        )
    hinted = [d for d in dims if d.lower() in FLOW_NAMES]
    src, dst = (hinted + [d for d in dims if d not in hinted])[:2]
    value = ctx.measures[0] if ctx.measures else "count"
    warnings = []
    for d in (src, dst):
        if ctx.card(d) > SERIES_CEILING:
            warnings.append(
                f"'{d}' has {ctx.card(d)} nodes - color by stage, not by "
                f"node ({AP_PAST_8})"
            )
    return _out(
        "sankey",
        "flow between stages -> sankey; link width carries the value.",
        {"x": src, "y": dst, "size": value, "color": src},
        warnings,
    )


def _spatial(ctx):
    """Spatial job: choropleth by measure, class or count; small multiples past 3."""
    if not ctx.geo:
        return _magnitude(ctx, ["no geo column in profile; falling back to magnitude"])
    g = ctx.geo[0]
    enc = {"x": g, "y": None, "geo": g}
    warnings = []
    if ctx.measures:
        enc["color"] = ctx.measures[0]
        return _out(
            "choropleth",
            "spatial magnitude -> choropleth with a sequential ramp (all-pairs "
            "form: one hue, more-is-darker).",
            enc,
            warnings,
        )
    if ctx.cats:
        c = ctx.cats[0]
        n = ctx.card(c)
        if n > ALL_PAIRS_CAP:
            warnings.append(
                f"{LADDER_ALL_PAIRS}: '{c}' has {n} classes on a choropleth - {FOLD}"
            )
            enc["facet"] = c
            return _out(
                "small multiples",
                "categorical map with too many classes: one map per class.",
                enc,
                warnings,
            )
        enc["color"] = c
        return _out(
            "choropleth",
            "spatial identity -> categorical choropleth, <= 3 hues.",
            enc,
            warnings,
        )
    enc["color"] = "count"
    return _out(
        "choropleth",
        "spatial counts -> choropleth of row counts per region.",
        enc,
        warnings,
    )


_RULES = {
    "magnitude": _magnitude,
    "identity": _identity,
    "polarity": _polarity,
    "headline": _headline,
    "change-over-time": _change_over_time,
    "distribution": _distribution,
    "relationship": _relationship,
    "part-to-whole": _part_to_whole,
    "ranking": _ranking,
    "flow": _flow,
    "spatial": _spatial,
}


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def _is_flow(ctx):
    """True when two or more dimensions carry flow names (source, target, ...)."""
    dims = ctx.cats + ctx.geo
    return len(dims) >= 2 and sum(d.lower() in FLOW_NAMES for d in dims) >= 2


def infer_job(profile):
    """Pick the job from the profile shape (deterministic, first match wins)."""
    ctx = _Ctx(_as_profile(profile))
    if ctx.n_rows <= 1 and ctx.measures:
        return "headline"
    if ctx.geo:
        return "spatial"
    if _is_flow(ctx):
        return "flow"
    if ctx.temporal:
        return "change-over-time"
    if not ctx.cats:
        if len(ctx.measures) >= 2:
            return "relationship"
        if len(ctx.measures) == 1:
            return "distribution"
    return "magnitude"


def _as_profile(frame_or_profile):
    """Profile dict as given, or profile_frame() of a DataFrame; else TypeError."""
    if isinstance(frame_or_profile, dict):
        return frame_or_profile
    if hasattr(frame_or_profile, "dtypes") and hasattr(frame_or_profile, "columns"):
        return profile_frame(frame_or_profile)
    raise TypeError("recommend() takes a profile dict or a pandas DataFrame")


def recommend(frame_or_profile, job=None):
    """Return {job, job_inferred, form, reason, encoding, warnings}.

    ``job`` is the job used: the one given, or the inferred one when None.
    """
    profile = copy.deepcopy(_as_profile(frame_or_profile))
    inferred = job is None
    if inferred:
        job = infer_job(profile)
    if job not in _RULES:
        raise ValueError(f"unknown job {job!r}; expected one of {', '.join(JOBS)}")
    return {"job": job, "job_inferred": inferred, **_RULES[job](_Ctx(profile))}


# --------------------------------------------------------------------------
# pandas bridge (lazy import)
# --------------------------------------------------------------------------
def _scale_of(series):
    """Floor log10 of the series' max |value|; 0 when empty, NaN or zero."""
    import pandas as pd

    vals = pd.to_numeric(series, errors="coerce").abs()
    mx = vals.max(skipna=True)
    if mx is None or (isinstance(mx, float) and math.isnan(mx)) or mx <= 0:
        return 0
    return math.floor(math.log10(float(mx)))


def _dtype_kind(lname, series):
    """(kind, parsed) decided by name or dtype alone, or None to inspect the values."""
    import pandas as pd
    from pandas.api import types as pt

    if lname in GEO_NAMES or getattr(series.dtype, "name", "") == "geometry":
        return "geo", None
    if pt.is_bool_dtype(series):
        return "boolean", None
    if pt.is_datetime64_any_dtype(series) or isinstance(series.dtype, pd.PeriodDtype):
        return "temporal", series
    if pt.is_numeric_dtype(series):
        if lname in TIME_NAMES:
            return "temporal", series
        return "numeric", None
    return None


def _string_kind(non_null):
    """(kind, parsed) of string values: ISO dates -> temporal, long unique -> text."""
    import pandas as pd

    sample = non_null.astype(str).head(200)
    parsed = pd.to_datetime(sample, errors="coerce", format="ISO8601")
    if parsed.notna().mean() >= 0.9:
        return "temporal", pd.to_datetime(
            non_null.astype(str), errors="coerce", format="ISO8601"
        )
    ratio = non_null.nunique() / max(len(non_null), 1)
    avg_len = sample.str.len().mean()
    if ratio > 0.5 and avg_len > 12:
        return "text", None
    return "categorical", None


def _value_kind(lname, series):
    """(kind, parsed) of a column whose dtype did not decide it, from its values."""
    import pandas as pd
    from pandas.api import types as pt

    non_null = series.dropna()
    if len(non_null) == 0:
        return "categorical", None
    if lname in TIME_NAMES:
        parsed = pd.to_datetime(non_null.astype(str), errors="coerce", format="ISO8601")
        if parsed.notna().mean() >= 0.9:
            return "temporal", parsed
    if lname in TEXT_NAMES:
        return "text", None
    if pt.is_object_dtype(series) or pt.is_string_dtype(series):
        return _string_kind(non_null)
    return "categorical", None


def _kind_of(name, series):
    """(kind, parsed time series or None) for one DataFrame column."""
    lname = str(name).lower()
    kind = _dtype_kind(lname, series)
    if kind is not None:
        return kind
    return _value_kind(lname, series)


def _is_series(cat, time_key):
    """True when ``cat`` holds several categories per time value (long format).

    stocks (date x symbol): ~5 symbols per date -> series.  seattle-weather: one
    ``weather`` value per date -> a per-row attribute, not a series.
    """
    per_time = cat.groupby(time_key, dropna=True).nunique(dropna=True)
    return bool(len(per_time) and float(per_time.mean()) > 1)


def _column_profile(name, series, kind, parsed):
    """Profile entry of one column: name, kind, cardinality (+ ordered/scale/sorted)."""
    col = {
        "name": str(name),
        "kind": kind,
        "cardinality": int(series.dropna().nunique()),
    }
    if kind == "categorical" and getattr(series.dtype, "ordered", False):
        col["ordered"] = True
    if kind == "numeric":
        col["scale"] = _scale_of(series)
    elif kind == "temporal":
        t = parsed if parsed is not None else series
        t = t.dropna()
        col["is_sorted_time"] = bool(t.is_monotonic_increasing)
    return col


def _mark_series(df, columns, time_key):
    """Set ``is_series`` on every categorical/boolean column profile over time_key."""
    for col in columns:
        if col["kind"] in ("categorical", "boolean"):
            col["is_series"] = _is_series(df[col["name"]], time_key)


def profile_frame(df):
    """Build the plain-dict profile recommend() consumes from a pandas DataFrame."""
    columns = []
    measures, dimensions = [], []
    time_key = None
    for name in df.columns:
        series = df[name]
        kind, parsed = _kind_of(name, series)
        if kind == "temporal" and time_key is None:
            time_key = (parsed if parsed is not None else series).reindex(df.index)
        columns.append(_column_profile(name, series, kind, parsed))
        if kind == "numeric":
            measures.append(str(name))
        elif kind != "text":
            dimensions.append(str(name))
    if time_key is not None:
        _mark_series(df, columns, time_key)
    return {
        "n_rows": len(df),
        "columns": columns,
        "measures": measures,
        "dimensions": dimensions,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _load(path):
    """Read a parquet (.parquet/.pq) or csv file into a DataFrame."""
    import pandas as pd

    suffix = path.suffix.lower()
    if suffix in (".parquet", ".pq"):
        return pd.read_parquet(path)
    return pd.read_csv(path)


def _format_text(job, inferred, out, profile):
    """Plain-text CLI report: job, form, reason, encoding, warnings, profile."""
    enc = " ".join(f"{k}={v}" for k, v in out["encoding"].items() if v is not None)
    lines = [
        f"job: {job}{' (inferred)' if inferred else ''}",
        f"form: {out['form']}",
        f"reason: {out['reason']}",
        f"encoding: {enc or '-'}",
        "warnings: " + ("; ".join(out["warnings"]) if out["warnings"] else "none"),
        (
            f"profile: {profile['n_rows']} rows, measures={profile['measures']}, "
            f"dimensions={profile['dimensions']}"
        ),
    ]
    return "\n".join(lines)


def main(argv=None):
    """CLI: profile a csv/parquet file and print the recommendation; exit 0 or 2."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description="Recommend a chart form for a csv/parquet file."
    )
    ap.add_argument("path", help="csv or parquet file")
    ap.add_argument(
        "--job",
        choices=JOBS,
        default=None,
        help="reader's job; inferred from the profile when omitted",
    )
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = ap.parse_args(argv)
    path = Path(args.path)
    if not path.is_file():
        print(f"error: no such file: {path}", file=sys.stderr)
        return 2
    try:
        df = _load(path)
    except Exception as exc:  # noqa: BLE001 - surface loader errors as exit 2
        print(f"error: could not load {path}: {exc}", file=sys.stderr)
        return 2
    profile = profile_frame(df)
    out = recommend(profile, args.job)
    if args.json:
        payload = {**out, "profile": profile}
        print(json.dumps(payload, indent=2))
    else:
        print(_format_text(out["job"], out["job_inferred"], out, profile))
    return 0


if __name__ == "__main__":
    sys.exit(main())
