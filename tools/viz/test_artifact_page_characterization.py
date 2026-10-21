"""Characterization tests for tools/viz/artifact_page.py (VAL-613 refactor guard).

Pins prepare_chart() output (patched spec, rows, warnings, height, fit) over
deterministic grids of Vega-Lite, ECharts and Plotly chart dicts.  The digests
were captured on the pre-refactor HEAD; any behavior change in the spec
patching changes a digest.  No browser needed.

Run: py -3.13 tools/viz/test_artifact_page_characterization.py
Regenerate (only for an intended behavior change):
     py -3.13 tools/viz/test_artifact_page_characterization.py --print-digests
"""

import copy
import hashlib
import itertools
import json
import os
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.viz import artifact_page as ap

MONTHS = ["2026-01", "2026-02", "2026-03"]
ROWS = [
    {
        "month": m,
        "region": r,
        "sales": 10 * i + j,
        "one": "solo",
        "North": i,
        "South": j,
        "a.b[0]": f"k{j}",
    }
    for i, m in enumerate(MONTHS)
    for j, r in enumerate(["North", "South"])
]


def _outcome(chart):
    """prepare_chart(chart), or the exception it raises, as plain data."""
    try:
        return ap.prepare_chart(copy.deepcopy(chart))
    except Exception as exc:  # noqa: BLE001 - the raised error is the behavior
        return {"raised": type(exc).__name__, "message": str(exc)}


def _digest(obj):
    """sha256 of the canonical JSON of obj."""
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- vega-lite
VL_DATA = (
    {"data": {"values": ROWS}},
    {"data": {"name": "d"}, "datasets": {"d": ROWS}},
    {},
)
VL_MARKS = (
    "line",
    "area",
    "bar",
    {"type": "line"},
    {"type": "bar", "tooltip": False},
    {"type": "line", "point": True},
)
VL_X = (
    {
        "field": "month",
        "type": "temporal",
        "timeUnit": "yearmonth",
        "title": "M",
        "format": "%b",
    },
    {"value": 0},
    None,
)
VL_Y = (
    {"field": "sales", "type": "quantitative"},
    {"field": "sales", "aggregate": "sum", "title": "S", "format": ".1f"},
    None,
)
VL_COLOR = (
    None,
    {"field": "region", "type": "nominal"},
    {"field": "one"},
    {"field": "series"},
    {"field": "zone"},
    {"field": "region", "legend": {"orient": "top"}},
    {"field": "series", "sort": ["South", "North"]},
    {"field": "series", "scale": {"domain": ["a"]}},
    {"field": "region", "title": "R", "type": "ordinal"},
    {"field": "a.b[0]"},
)
VL_TRANSFORM = (
    None,
    [{"fold": ["North", "South"], "as": ["series", "value"]}],
    [{"fold": "North"}, "skip"],
    [{"calculate": "'Z ' + datum.region", "as": "zone"}],
    [
        {"window": [{"op": "rank", "as": "series"}]},
        {"joinaggregate": {"op": "sum", "as": "tot"}},
        {"aggregate": [{"op": "sum", "field": "sales"}]},
    ],
)
VL_STRUCT = ("single", "layer", "layer_tf", "facet", "params", "hconcat")
VL_RESOLVE = (
    None,
    {"scale": {"y": "independent"}},
    {"scale": {"y": "independent", "x": "shared"}},
    {"scale": {"y": "independent"}, "axis": {"y": "independent"}},
    {"scale": {"y": "shared"}},
)


def _vl_spec(data, mark, x, y, color, transform, struct, resolve):
    """One Vega-Lite spec from the grid coordinates."""
    enc = {k: v for k, v in (("x", x), ("y", y), ("color", color)) if v is not None}
    view = {"mark": mark, "encoding": enc}
    if struct == "single":
        spec = dict(view)
    elif struct == "layer":
        spec = {"layer": [view, {"mark": "rule"}, "not-a-layer"]}
    elif struct == "layer_tf":
        spec = {"layer": [dict(view, transform=transform or [])]}
    elif struct == "facet":
        spec = {"facet": {"field": "region"}, "spec": view}
    elif struct == "params":
        spec = dict(view, params=[{"name": "p", "select": "interval"}])
    else:
        spec = {"hconcat": [view]}
    spec.update(copy.deepcopy(data))
    if transform is not None:
        spec["transform"] = transform
    if resolve is not None:
        spec["resolve"] = resolve
    return spec


def vega_lite_charts():
    """Every Vega-Lite grid chart, in a fixed order (resolve crossed separately)."""
    for coords in itertools.product(
        VL_DATA, VL_MARKS, VL_X, VL_Y, VL_COLOR, VL_TRANSFORM, VL_STRUCT
    ):
        yield {"kind": "vega-lite", "title": "VL", "spec": _vl_spec(*coords, None)}
    for resolve, struct, mark, color in itertools.product(
        VL_RESOLVE, VL_STRUCT, VL_MARKS[:3], VL_COLOR[:2]
    ):
        spec = _vl_spec(
            VL_DATA[0], mark, VL_X[0], VL_Y[0], color, None, struct, resolve
        )
        yield {"kind": "vega-lite", "title": "VL", "spec": spec}


# ----------------------------------------------------------------------- echarts
CATS = ["a", "b", "c"]
EC_DATA = (
    [1, 2, 3],
    [{"value": 1}, {"value": 2}, 3, 4],
    [["a", 1], ["b", 2]],
)
EC_SERIES = (
    (("bar", "Tickets", {}),),
    (("bar", "One", {}), ("bar", "Two", {})),
    (("line", "a", {}),),
    (("line", "a", {"showSymbol": False}), ("line", "b", {"symbol": "none"})),
    (("line", "a", {"showSymbol": False}), ("line", "b", {})),
    (("line", None, {}),),
    (("bar", "a", {}), ("line", "b", {"yAxisIndex": 1})),
    (),
    "single-dict",
    (("line", "a", {"endLabel": {"show": False}}), ("line", "b", {})),
    (("scatter", "s", {}),),
    "mixed",
)
EC_X = ({"type": "category", "data": CATS}, {"type": "value"}, [{"type": "time"}], None)
EC_Y = (
    {"type": "value"},
    {"type": "category", "data": CATS},
    [{"type": "value"}, {"type": "value"}],
    None,
)
EC_DATASET = (
    None,
    {"source": [{"k": "a", "v": 1}, {"k": "b", "v": 2}]},
    {"source": [["k", "v"], ["a", 1], "bad", ["b", 2]]},
    {"source": []},
    {"source": [{"k": "a"}, ["x"]]},
)
EC_LEGEND = (None, {"top": 10}, [{"x": 1}], {"show": False, "icon": "circle"})
EC_END = (
    "absent",
    True,
    False,
    None,
    ["a"],
    ["zz"],
    "a",
    ("a", "b"),
    [1],
    [],
    ["a", "zz", "Tickets"],
)


def _ec_series(variant, data):
    """Series list (or a lone dict) for one EC_SERIES variant."""
    if variant == "single-dict":
        return {"type": "line", "name": "a", "data": list(data)}
    if variant == "mixed":
        return ["not-a-series", {"type": "line", "name": "a", "data": list(data)}]
    return [
        dict({"type": t, "data": list(data)}, **({"name": n} if n else {}), **extra)
        for t, n, extra in variant
    ]


def echarts_charts():
    """Two ECharts grids: axes/data/dataset, and legend/tooltip/grid/end_labels."""
    for series, data, x, y, dataset in itertools.product(
        EC_SERIES, EC_DATA, EC_X, EC_Y, EC_DATASET
    ):
        spec = {"series": _ec_series(series, data)}
        for key, val in (("xAxis", x), ("yAxis", y), ("dataset", dataset)):
            if val is not None:
                spec[key] = copy.deepcopy(val)
        yield {"kind": "echarts", "title": "EC", "spec": spec}
    for series, legend, tooltip, grid, end in itertools.product(
        EC_SERIES, EC_LEGEND, (False, True), (False, True), EC_END
    ):
        spec = {
            "xAxis": {"type": "category", "data": CATS},
            "yAxis": {"type": "value"},
            "series": _ec_series(series, EC_DATA[0]),
        }
        if legend is not None:
            spec["legend"] = copy.deepcopy(legend)
        if tooltip:
            spec["tooltip"] = {"trigger": "none"}
        if grid:
            spec["grid"] = {"left": 1}
        chart = {"kind": "echarts", "title": "EC", "spec": spec}
        if end != "absent":
            chart["end_labels"] = end
        yield chart


# ------------------------------------------------------------------------ plotly
PL_TRACES = (
    {"type": "scatter", "mode": "lines", "x": [1, 2], "y": [3, 4], "name": "a"},
    {"type": "scatter", "mode": "markers", "x": [1, 2], "y": [3, 4]},
    {"x": [1, 2], "y": [5, 6], "name": "b"},
    {"type": "scattergl", "mode": "lines+markers", "x": ["p", "q"], "y": [1, 2]},
    {"type": "bar", "showlegend": False, "x": ["p"], "y": [1]},
    {"type": "bar", "x": ["p", "q"], "y": [2, 3], "name": "bars"},
    {
        "type": "scatter",
        "x": {"dtype": "f8", "bdata": "AAAAAAAA8D8AAAAAAAAAQA=="},
        "y": {"dtype": "i4", "bdata": "AQAAAAIAAAA="},
    },
    {"type": "scatter", "x": [1], "y": [2], "yaxis": "y2", "name": "right"},
    {"type": "scatter", "x": [1, 2], "y": None},
    {"type": "scatter", "x": {"dtype": "c16", "bdata": "AA=="}, "y": [1]},
    "not-a-trace",
)
PL_LAYOUT = (
    "absent",
    {},
    {"width": 500, "height": 200},
    {"yaxis2": {"overlaying": "y"}},
    {"yaxis2": {"overlaying": "y"}, "yaxis3": {"anchor": "x"}},
    {"legend": {"x": 0.5}},
    {"hovermode": "closest"},
    {"yaxis2": "not-a-dict", "yaxis": {"overlaying": "y2"}},
)


def plotly_charts():
    """Plotly grid: every 0/1/2-trace combination x every layout variant."""
    combos = [()] + [(t,) for t in PL_TRACES]
    combos += list(itertools.combinations(PL_TRACES, 2))
    combos.append("single-dict")
    for traces, layout in itertools.product(combos, PL_LAYOUT):
        if traces == "single-dict":
            spec = {"data": copy.deepcopy(PL_TRACES[0])}
        else:
            spec = {"data": copy.deepcopy(list(traces))}
        if layout != "absent":
            spec["layout"] = copy.deepcopy(layout)
        yield {"kind": "plotly", "title": "PL", "spec": spec}


# ------------------------------------------------------------------ prepare_chart
def guard_charts():
    """prepare_chart() argument guards, rows override, height and title handling."""
    vl = {"mark": "bar", "encoding": {"x": {"field": "region"}}}
    yield "not-a-dict"
    yield {"kind": "svg", "spec": {}}
    yield {"kind": "vega-lite", "spec": "nope", "title": "T"}
    yield {"kind": "vega-lite", "spec": vl}
    yield {"kind": "vega-lite", "spec": vl, "rows": ({"a": 1},), "height": 50}
    yield {"kind": "vega-lite", "spec": vl, "rows": [], "height": "400"}
    yield {"kind": "vega-lite", "spec": vl, "height": 0, "caption": "c"}
    yield {"kind": "echarts", "spec": {"color": [ap.token("text-muted")]}}
    yield {"kind": "echarts", "spec": {"color": ["token:nope"]}, "title": "Bad"}
    yield {"kind": "plotly", "spec": {"data": []}, "height": None}


GRIDS = {
    "vega-lite": vega_lite_charts,
    "echarts": echarts_charts,
    "plotly": plotly_charts,
    "guards": guard_charts,
}


def digests():
    """{grid: [count, digest of prepare_chart outcomes]}."""
    out = {}
    for name, gen in GRIDS.items():
        results = [_outcome(c) for c in gen()]
        out[name] = [len(results), _digest(results)]
    return out


EXPECTED = {
    "vega-lite": [
        48780,
        "472ca939836e978b2ace6f0fd45bcc1714d1fcab4752439fa59a1d6353675416",
    ],
    "echarts": [
        4992,
        "6de742c8d5bbfdaec0acd5c13b24a09f580252780880fda8030b0c5abde38be4",
    ],
    "plotly": [
        544,
        "92becb7a51aaa465d0bbd48030454755a2c52fe0ae97e47e5501702efc172c18",
    ],
    "guards": [
        10,
        "c5b558151c95759afd2553abdf353ad3d33be386c0a60273d53b7c368348e3dc",
    ],
}


class Characterization(unittest.TestCase):
    """Digests captured on HEAD before any VAL-613 refactor."""

    maxDiff = None

    def test_prepare_chart_grids(self):
        """prepare_chart() output over every grid is unchanged."""
        self.assertEqual(digests(), EXPECTED)


if __name__ == "__main__":
    if "--print-digests" in sys.argv:
        print(json.dumps(digests(), indent=1))
        sys.exit(0)
    unittest.main()
