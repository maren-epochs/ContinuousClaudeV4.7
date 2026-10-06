#!/usr/bin/env python3
"""Artifact-ready HTML chart pages from Vega-Lite / ECharts / Plotly specs.

build_page(charts, title, description='', mode_default='auto', table_rows=None)
returns one self-contained HTML string:

- one <style>: palette.css_tokens('light') on :root, css_tokens('dark') under
  both `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) }`
  and `:root[data-theme="dark"]`; body background var(--surface-2) (page plane),
  cards on var(--surface-1); 16px side gutters, max-width 1100px, cards stack
  at phone width, no horizontal scroll.
- external scripts: only the pinned CDN URLs a page's chart kinds need.
- a theme bridge (inline JS) reads the CSS tokens and themes each library,
  re-rendering on prefers-color-scheme change and on the header toggle.
- window.__chartsReady is assigned synchronously in <head> (a promise that
  settles when every chart has drawn) for tools.viz.export.render_html.
- every card has a Table toggle (plain <table>, sticky header, <= table_rows
  rows, default 500) and a Download CSV link.

Downloads. The CSV link's href is a data: URL - the fallback for a saved file
or any host without the capability. Inside the claude.ai viewer the data: link
cannot download, so after first paint the bridge calls
`await window.claude.use("downloads")` (never awaited by rendering); when it
resolves a namespace, a click is intercepted (preventDefault) and the CSV text
goes through `save({filename, data})`. Null keeps the data: link. Rejections:
declined / rate_limited do nothing (no retry); unavailable, not_granted,
capability_* and unknown codes hide every CSV link; extension_not_enabled
hides them with a note. save() is only ever called from a click.
Publishers: REQUIRED_CAPABILITIES = {"downloads": True} is every capability
the page code can use; capabilities_for(charts) returns the subset a given
page needs ({} when no card has rows) - pass it as the Artifact's
`capabilities` when publishing, or the viewer resolves use() to null.

A chart is a dict: {'kind': 'vega-lite'|'plotly'|'echarts', 'spec': dict,
'title': str, 'caption': str|None, 'rows': list[dict]|None, 'height': int,
'end_labels': bool (ECharts lines only)}.
Spec patching (prepare_chart): Vega-Lite line/area gets a crosshair+tooltip
layer, other marks tooltip:true; ECharts tooltip axis/item; Plotly hovermode
'x unified' for lines; legend shown for >= 2 series, hidden for one; a second
(overlaid / independent) y axis is removed and a visible warning added.
Vega-Lite series whose color field is a transform output (fold `as`, default
'key'; calculate/window/... `as`) are counted from the transform: fold lists
its series, so legend + every-series pivot tooltip work as for inline rows,
and its series take slots in fold order (color.sort, unless the author set
sort or scale.domain);
other outputs keep the author's legend and the pivot rule shows every pivoted
field (mark tooltip content 'data'). ECharts lines: markerless lines (showSymbol
false / symbol 'none') get a 16x2 'rect' legend icon; with no author grid the
bridge measures the legend at render/resize time and sets grid.top for one or
two rows (more rows -> legend type 'scroll'), plus room for a y-axis name;
end_labels=True adds series endLabel ('{a}') colored with --text-secondary at
render time and widens grid.right to fit the longest name.

No colors live here - every value comes from tools/viz/palette.json.
"""
from __future__ import annotations

import base64
import copy
import csv
import html
import io
import json
import re
import struct
import sys
from pathlib import Path
from urllib.parse import quote

try:
    from tools.viz import palette
except ModuleNotFoundError:  # run as a script
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tools.viz import palette

KINDS = ("vega-lite", "echarts", "plotly")
MODE_DEFAULTS = ("auto", "light", "dark")
CDN = {
    "vega": "https://cdn.jsdelivr.net/npm/vega@6.4.0",
    "vega-lite": "https://cdn.jsdelivr.net/npm/vega-lite@6.4.3",
    "vega-embed": "https://cdn.jsdelivr.net/npm/vega-embed@7.3.0",
    "echarts": "https://cdnjs.cloudflare.com/ajax/libs/echarts/6.1.0/echarts.min.js",
    "plotly": "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@4.1.2",
}
SCRIPTS_FOR = {
    "vega-lite": ("vega", "vega-lite", "vega-embed"),
    "echarts": ("echarts",),
    "plotly": ("plotly",),
}
MAX_TABLE_ROWS = 500
MAX_INLINE_BYTES = 2 * 1024 * 1024
MAX_PAGE_BYTES = 16 * 1024 * 1024
DEFAULT_HEIGHT = 320
BAR_MAX_PX = 24
REQUIRED_CAPABILITIES = {"downloads": True}   # Download CSV -> claude.use("downloads")
LEGEND_STROKE = {"icon": "rect", "itemWidth": 16, "itemHeight": 2}
DUAL_AXIS_WARNING = ("Dual y-axis removed: two scales on one plot mislead. "
                     "Plot the second measure as its own chart or index both to a common base.")


# --------------------------------------------------------------------------- json


def _json_default(obj):
    if hasattr(obj, "tolist"):          # numpy arrays / scalars
        return obj.tolist()
    if hasattr(obj, "isoformat"):       # datetime, date, pandas Timestamp
        return obj.isoformat()
    if isinstance(obj, (set, tuple)):
        return list(obj)
    return str(obj)


def _plain(obj):
    """Deep copy as plain JSON types (numpy, datetimes and tuples normalized)."""
    return json.loads(json.dumps(obj, default=_json_default))


def _script_json(obj):
    """JSON safe inside <script>: no '<', '>', '&' or line separators survive raw."""
    text = json.dumps(obj, default=_json_default, ensure_ascii=True, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


# --------------------------------------------------------------------------- helpers


def _distinct(rows, field):
    seen = []
    for r in rows or []:
        if isinstance(r, dict) and field in r and r[field] not in seen:
            seen.append(r[field])
    return seen


def _vl_field_escape(name):
    return re.sub(r"([\\.\[\]])", r"\\\1", str(name))


def _vl_values(spec):
    """Inline rows of a Vega-Lite spec (data.values or altair-style datasets)."""
    data = spec.get("data")
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("values"), list):
        return data["values"]
    name = data.get("name")
    datasets = spec.get("datasets")
    if name and isinstance(datasets, dict) and isinstance(datasets.get(name), list):
        return datasets[name]
    return None


def _mark_type(mark):
    if isinstance(mark, str):
        return mark
    if isinstance(mark, dict):
        return mark.get("type")
    return None


def _mark_with(mark, **extra):
    out = {"type": mark} if isinstance(mark, str) else dict(mark)
    for k, v in extra.items():
        out.setdefault(k, v)
    return out


# --------------------------------------------------------------------------- vega-lite


def _vl_derived(transforms):
    """Fields a Vega-Lite transform list creates -> series names (fold key) or None.

    fold's key field (as[0], default 'key') maps to the folded field names, so
    its series are known without data; every other output (fold value, calculate,
    window, joinaggregate, aggregate, bin, timeUnit, lookup, ...) maps to None.
    """
    out = {}
    for t in transforms or []:
        if not isinstance(t, dict):
            continue
        if "fold" in t:
            as_ = [str(a) for a in _as_list(t.get("as"))] or ["key", "value"]
            out[as_[0]] = [str(f) for f in _as_list(t["fold"])]
            out[as_[1] if len(as_) > 1 else "value"] = None
            continue
        for key in ("window", "joinaggregate", "aggregate"):
            for item in _as_list(t.get(key)):
                if isinstance(item, dict) and item.get("as"):
                    out[str(item["as"])] = None
        for a in _as_list(t.get("as")):
            if isinstance(a, str):
                out[a] = None
    return out


def _vl_series(field, values, derived):
    """Series of a color field: list, None (transform output, unknown), [] (no data)."""
    if field in derived:
        return derived[field]
    return _distinct(values, field) if values else []


def _vl_crosshair(spec, values):
    """Single-view line/area -> layer [mark, hover points, pivot rule with tooltip]."""
    enc = dict(spec.get("encoding") or {})
    x, y, color = enc.get("x"), enc.get("y"), enc.get("color")
    if not (isinstance(x, dict) and x.get("field")):
        return spec
    mark = spec["mark"]
    kind = _mark_type(mark)
    base_enc = {k: v for k, v in enc.items() if k != "x"}
    x_tip = {k: x[k] for k in ("field", "type", "timeUnit", "title", "format") if k in x}
    y_plain = isinstance(y, dict) and y.get("field") and not y.get("aggregate")
    color_field = color.get("field") if isinstance(color, dict) else None
    derived = _vl_derived(spec.get("transform"))
    series = _vl_series(color_field, values, derived) if color_field else []
    rule = {
        "mark": {"type": "rule", "strokeWidth": 1},
        "encoding": {"opacity": {"condition": {"value": 1, "param": "hover", "empty": False},
                                 "value": 0}},
        "params": [{"name": "hover", "select": {
            "type": "point", "fields": [x["field"]], "nearest": True,
            "on": "pointerover", "clear": "pointerout"}}],
    }
    if color_field and y_plain and (series or series is None):
        rule["transform"] = [{"pivot": color_field, "value": y["field"],
                              "groupby": [x["field"]]}]
        if series:
            rule["encoding"]["tooltip"] = [x_tip] + [
                {"field": _vl_field_escape(s), "type": "quantitative", "title": str(s)}
                for s in series]
        else:   # transform output with unknown values: show every pivoted field
            rule["mark"]["tooltip"] = {"content": "data"}
    else:
        tips = [x_tip]
        if color_field:
            tips.append({"field": color_field, "type": color.get("type", "nominal"),
                         "title": color.get("title", color_field)})
        if isinstance(y, dict) and y.get("field"):
            tips.append({k: y[k] for k in ("field", "type", "aggregate", "title", "format")
                         if k in y})
        rule["encoding"]["tooltip"] = tips
    layers = [{"mark": mark, "encoding": base_enc}]
    if kind == "line" and y_plain:
        point_enc = {k: v for k, v in base_enc.items() if k in ("y", "color")}
        layers.append({"transform": [{"filter": {"param": "hover", "empty": False}}],
                       "mark": {"type": "point", "filled": True, "size": 64},
                       "encoding": point_enc})
    layers.append(rule)
    out = {k: v for k, v in spec.items() if k not in ("mark", "encoding")}
    out["encoding"] = {"x": x}
    out["layer"] = layers
    return out


def _vl_legend(spec, values):
    """Legend on for >= 2 series (by the color field), off for one.

    A color field created by a transform is counted from the transform (fold)
    and takes categorical slots in fold order (color.sort = the fold list unless
    the author set sort or scale.domain); when its values are unknown
    (calculate, ...) the author's legend is kept.
    """
    top = _vl_derived(spec.get("transform"))
    targets = [spec] + [lyr for lyr in spec.get("layer", []) if isinstance(lyr, dict)]
    for t in targets:
        color = (t.get("encoding") or {}).get("color")
        if not (isinstance(color, dict) and color.get("field")):
            continue
        derived = top if t is spec else {**top, **_vl_derived(t.get("transform"))}
        field = color["field"]
        if field in derived and derived[field] is None:
            color.setdefault("legend", {})
            continue
        if field in derived and "sort" not in color and                 "domain" not in (color.get("scale") or {}):
            color["sort"] = list(derived[field])  # slots follow the fold order
        if field not in derived and values is None:
            continue
        n = len(_vl_series(field, values, derived))
        if n < 2:
            color["legend"] = None
        elif not isinstance(color.get("legend"), dict):
            color["legend"] = {}


def _prepare_vega_lite(spec, warnings):
    values = _vl_values(spec)
    resolve = spec.get("resolve")
    if isinstance(resolve, dict) and (resolve.get("scale") or {}).get("y") == "independent":
        resolve["scale"].pop("y")
        if not resolve["scale"]:
            resolve.pop("scale")
        if not resolve:
            spec.pop("resolve")
        warnings.append(DUAL_AXIS_WARNING)
    single = "mark" in spec and not any(k in spec for k in (
        "layer", "params", "facet", "repeat", "concat", "hconcat", "vconcat"))
    if single:
        kind = _mark_type(spec["mark"])
        if kind in ("line", "area"):
            spec = _vl_crosshair(spec, values)
        elif kind and not (isinstance(spec["mark"], dict) and "tooltip" in spec["mark"]):
            spec["mark"] = _mark_with(spec["mark"], tooltip=True)
    _vl_legend(spec, values)
    if single and _mark_type(spec.get("mark") or spec["layer"][0].get("mark")) == "line":
        base = spec if "mark" in spec else spec["layer"][0]
        legend = ((base.get("encoding") or {}).get("color") or {}).get("legend")
        if isinstance(legend, dict):
            legend.setdefault("symbolType", "stroke")  # legend mirrors the mark
    if not any(k in spec for k in ("facet", "repeat", "concat", "hconcat", "vconcat")):
        spec["width"] = "container"
        spec["height"] = "container"
        spec["autosize"] = {"type": "fit", "contains": "padding"}
    return spec, values


# --------------------------------------------------------------------------- echarts


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _markerless(s):
    return s.get("type") == "line" and (s.get("showSymbol") is False
                                        or s.get("symbol") == "none")


def _prepare_echarts(opt, warnings, end_labels=False):
    """Patch an ECharts option; returns (option, rows, fit).

    fit is True when the grid is ours (author gave none): the bridge then sizes
    grid.top / grid.right to the measured legend and end labels at render time.
    """
    series = [s for s in _as_list(opt.get("series")) if isinstance(s, dict)]
    y_axes = _as_list(opt.get("yAxis"))
    if len(y_axes) > 1:
        opt["yAxis"] = y_axes[0]
        for s in series:
            s.pop("yAxisIndex", None)
        warnings.append(DUAL_AXIS_WARNING)
    x_axes = _as_list(opt.get("xAxis"))
    horizontal = bool(x_axes) and isinstance(x_axes[0], dict) and \
        x_axes[0].get("type") == "value" and bool(y_axes) and \
        isinstance(y_axes[0], dict) and y_axes[0].get("type") == "category"
    lineish = any(s.get("type") == "line" for s in series)
    if "tooltip" not in opt:
        opt["tooltip"] = ({"trigger": "axis", "axisPointer": {"type": "line"}} if lineish
                          else {"trigger": "item"})
    legend = opt.get("legend")
    show = len(series) >= 2
    if isinstance(legend, dict) or legend is None:
        legend = dict(legend or {})
        legend["show"] = show
        if show:
            legend.setdefault("top", 0)
            if all(_markerless(s) for s in series):   # key mirrors the stroke
                for k, v in LEGEND_STROKE.items():
                    legend.setdefault(k, v)
        opt["legend"] = legend
    fit = "grid" not in opt
    if fit:
        opt["grid"] = {"left": 8, "right": 16, "top": 40 if show else 16, "bottom": 8,
                       "containLabel": True}
    for s in series:
        if s.get("type") == "bar":
            s.setdefault("barMaxWidth", BAR_MAX_PX)
            item = s.setdefault("itemStyle", {})
            item.setdefault("borderRadius", [0, 4, 4, 0] if horizontal else [4, 4, 0, 0])
        elif s.get("type") == "line":
            s.setdefault("lineStyle", {}).setdefault("width", 2)
            s.setdefault("symbolSize", 8)
            if end_labels:      # color comes from --text-secondary in the bridge
                label = s.setdefault("endLabel", {})
                label.setdefault("show", True)
                label.setdefault("formatter", "{a}")
                s.setdefault("labelLayout", {"moveOverlap": "shiftY"})
    return opt, _echarts_rows(opt, series, x_axes), fit


def _echarts_rows(opt, series, x_axes):
    dataset = opt.get("dataset")
    source = dataset.get("source") if isinstance(dataset, dict) else None
    if isinstance(source, list) and source:
        if all(isinstance(r, dict) for r in source):
            return source
        if isinstance(source[0], list):
            header = [str(h) for h in source[0]]
            return [dict(zip(header, r)) for r in source[1:] if isinstance(r, list)]
    axis = x_axes[0] if x_axes and isinstance(x_axes[0], dict) else {}
    cats = axis.get("data")
    if not isinstance(cats, list):
        y_axes = _as_list(opt.get("yAxis"))
        axis = y_axes[0] if y_axes and isinstance(y_axes[0], dict) else {}
        cats = axis.get("data")
    if not isinstance(cats, list) or not series:
        return None
    rows = [{"category": c} for c in cats]
    for i, s in enumerate(series):
        name = str(s.get("name") or f"series {i + 1}")
        for j, v in enumerate(s.get("data") or []):
            if j < len(rows):
                rows[j][name] = v.get("value") if isinstance(v, dict) else v
    return rows


# --------------------------------------------------------------------------- plotly

_BDATA_TYPES = {"f8": "d", "f4": "f", "i1": "b", "u1": "B", "i2": "h", "u2": "H",
                "i4": "i", "u4": "I", "i8": "q", "u8": "Q"}


def _plotly_values(v):
    """Plain list from a plotly array (list, or {'dtype','bdata'} typed array)."""
    if isinstance(v, list):
        return v
    if isinstance(v, dict) and "bdata" in v and v.get("dtype") in _BDATA_TYPES:
        raw = base64.b64decode(v["bdata"])
        code = _BDATA_TYPES[v["dtype"]]
        return list(struct.unpack(f"<{len(raw) // struct.calcsize(code)}{code}", raw))
    return None


def _prepare_plotly(fig, warnings):
    data = [t for t in _as_list(fig.get("data")) if isinstance(t, dict)]
    layout = fig.setdefault("layout", {})
    dual = [k for k, v in layout.items()
            if re.fullmatch(r"yaxis\d+", k) and isinstance(v, dict) and v.get("overlaying")]
    if dual:
        for k in dual:
            layout.pop(k)
        names = {"y" + k[len("yaxis"):] for k in dual}
        for t in data:
            if t.get("yaxis") in names:
                t.pop("yaxis")
        warnings.append(DUAL_AXIS_WARNING)
    layout.pop("width", None)
    layout["autosize"] = True
    lines = any(t.get("type", "scatter") in ("scatter", "scattergl")
                and "lines" in str(t.get("mode", "lines")) for t in data)
    if lines:
        layout.setdefault("hovermode", "x unified")
    shown = [t for t in data if t.get("showlegend") is not False]
    layout["showlegend"] = len(shown) >= 2
    if layout["showlegend"]:
        legend = layout.setdefault("legend", {})
        legend.setdefault("orientation", "h")
        legend.setdefault("y", 1.02)
        legend.setdefault("yanchor", "bottom")
        legend.setdefault("x", 0)
    rows = []
    for i, t in enumerate(data):
        xs, ys = _plotly_values(t.get("x")), _plotly_values(t.get("y"))
        if xs is None or ys is None:
            continue
        name = str(t.get("name") or f"trace {i}")
        rows.extend({"series": name, "x": a, "y": b} for a, b in zip(xs, ys))
    fig["data"] = data
    return fig, rows or None


# --------------------------------------------------------------------------- charts


def prepare_chart(chart):
    """Normalize one chart dict: patched spec, rows, warnings, height (no I/O)."""
    if not isinstance(chart, dict):
        raise TypeError("a chart must be a dict with 'kind' and 'spec'")
    kind = chart.get("kind")
    if kind not in KINDS:
        raise ValueError(f"unknown chart kind {kind!r}; expected one of {', '.join(KINDS)}")
    spec = chart.get("spec")
    if not isinstance(spec, dict):
        raise TypeError(f"chart {chart.get('title')!r}: 'spec' must be a dict")
    spec = _plain(copy.deepcopy(spec))
    warnings = []
    fit = False
    if kind == "vega-lite":
        spec, rows = _prepare_vega_lite(spec, warnings)
    elif kind == "echarts":
        spec, rows, fit = _prepare_echarts(spec, warnings,
                                           end_labels=bool(chart.get("end_labels")))
    else:
        spec, rows = _prepare_plotly(spec, warnings)
    given = chart.get("rows")
    if given is not None:
        rows = _plain(list(given))
    height = int(chart.get("height") or DEFAULT_HEIGHT)
    return {"kind": kind, "spec": spec, "rows": rows, "warnings": warnings,
            "title": str(chart.get("title") or ""), "caption": chart.get("caption"),
            "height": max(120, height), "fit": fit}


def capabilities_for(charts):
    """Capabilities a page built from charts needs: {'downloads': True} when any
    card has rows (it gets a Download CSV link), else {}. Publish with these."""
    if any(prepare_chart(c)["rows"] for c in charts or []):
        return dict(REQUIRED_CAPABILITIES)
    return {}


def from_altair(chart, title="", caption=None, rows=None, height=DEFAULT_HEIGHT):
    """Altair chart -> chart dict (kind 'vega-lite')."""
    return {"kind": "vega-lite", "spec": chart.to_dict(), "title": title,
            "caption": caption, "rows": rows, "height": height}


def from_plotly(fig, title="", caption=None, rows=None, height=None):
    """Plotly figure -> chart dict (kind 'plotly'); height defaults to layout.height."""
    spec = _plain(fig.to_plotly_json())
    spec.pop("config", None)
    h = height or (spec.get("layout") or {}).get("height") or DEFAULT_HEIGHT
    return {"kind": "plotly", "spec": {"data": spec.get("data", []),
                                       "layout": spec.get("layout", {})},
            "title": title, "caption": caption, "rows": rows, "height": h}


# --------------------------------------------------------------------------- table + csv


def _columns(rows):
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    return cols


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _cell_text(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=_json_default)
    return str(v)


def _csv_safe(v):
    """Neutralize spreadsheet formulas in text cells (numbers pass through)."""
    text = _cell_text(v)
    if isinstance(v, str) and text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        try:
            float(text)
        except ValueError:
            return "'" + text
    return text


def _csv_href(rows, cols):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([str(c) for c in cols])
    for r in rows:
        writer.writerow([_csv_safe(r.get(c)) for c in cols])
    return "data:text/csv;charset=utf-8," + quote(buf.getvalue(), safe="")


def _slug(text, fallback):
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return s or fallback


def _table_html(rows, max_rows):
    rows = [r for r in rows if isinstance(r, dict)]
    cols = _columns(rows)
    shown = rows[:max_rows]
    esc = html.escape
    out = []
    if len(rows) > len(shown):
        out.append(f'<p class="table-note">Showing {len(shown)} of {len(rows)} rows; '
                   "the CSV download has all of them.</p>")
    out.append("<table>")
    numeric = {c for c in cols
               if all(_is_number(r.get(c)) for r in shown if r.get(c) is not None)}
    heads = []
    for c in cols:
        cls = ' class="num"' if c in numeric else ""
        heads.append(f'<th scope="col"{cls}>{esc(str(c))}</th>')
    out.append("<thead><tr>" + "".join(heads) + "</tr></thead>")
    out.append("<tbody>")
    for r in shown:
        cells = []
        for c in cols:
            v = r.get(c)
            cls = ' class="num"' if _is_number(v) else ""
            cells.append(f"<td{cls}>{esc(_cell_text(v))}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</tbody></table>")
    return "\n".join(out), cols


# --------------------------------------------------------------------------- css


def _font_stack():
    names = palette.font()["family_stack"]
    return ", ".join(f'"{n}"' if " " in n else n for n in names)


def _indent(text, pad):
    return "\n".join(pad + line for line in text.splitlines())


def _css():
    light = _indent(palette.css_tokens("light"), "  ")
    dark_media = _indent(palette.css_tokens("dark"), "    ")
    dark_attr = _indent(palette.css_tokens("dark"), "  ")
    return f"""
:root {{
  color-scheme: light;
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
[hidden] {{ display: none !important; }}
*, *::before, *::after {{ box-sizing: border-box; }}
html {{ -webkit-text-size-adjust: 100%; }}
body {{
  margin: 0;
  padding: 0 16px;
  background: var(--surface-2);
  color: var(--text-primary);
  font-family: {_font_stack()};
  font-size: 14px;
  line-height: 1.45;
  overflow-x: hidden;
}}
.page {{ max-width: 1100px; margin: 0 auto; padding: 20px 0 32px; }}
.page-head {{ display: flex; flex-wrap: wrap; align-items: flex-start;
  justify-content: space-between; gap: 8px 16px; margin-bottom: 16px; }}
.page-head h1 {{ font-size: 22px; font-weight: 600; margin: 0; }}
.description {{ color: var(--text-secondary); margin: 4px 0 0; max-width: 70ch; }}
button, .csv {{ font: inherit; font-size: 13px; color: var(--text-secondary);
  background: transparent; border: 1px solid var(--border); border-radius: 6px;
  padding: 4px 10px; min-height: 30px; cursor: pointer; text-decoration: none;
  display: inline-flex; align-items: center; white-space: nowrap; }}
button:hover, .csv:hover {{ color: var(--text-primary); background: var(--surface-2); }}
button:focus-visible, .csv:focus-visible {{ outline: 2px solid var(--series-1);
  outline-offset: 2px; }}
button[aria-pressed="true"] {{ color: var(--text-primary); border-color: var(--axis); }}
.charts {{ display: grid; gap: 16px;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 440px), 1fr)); }}
.card {{
  margin: 0;
  min-width: 0;
  overflow-x: hidden;
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 16px 14px;
}}
.card-head {{ display: flex; flex-wrap: wrap; align-items: center;
  justify-content: space-between; gap: 6px 12px; margin-bottom: 8px; }}
.card-title {{ font-size: 15px; font-weight: 600; margin: 0; }}
.card-tools {{ display: flex; gap: 6px; }}
div.chart {{ display: block; width: 100%; position: relative; }}
.chart-error {{ color: var(--text-secondary); font-size: 13px; padding: 8px 0; }}
.warning {{ margin: 0 0 8px; padding: 4px 8px; font-size: 13px; color: var(--text-primary);
  border-left: 3px solid var(--status-warning); }}
figcaption {{ color: var(--text-secondary); font-size: 13px; margin-top: 8px; }}
.csv-note {{ color: var(--text-secondary); font-size: 13px; align-self: center; }}
.table-wrap {{ max-height: 360px; overflow: auto; }}
.table-note {{ color: var(--text-secondary); font-size: 13px; margin: 0 0 6px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px;
  font-variant-numeric: tabular-nums; }}
th, td {{ text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--grid);
  white-space: nowrap; }}
td.num, th.num {{ text-align: right; }}
thead th {{
  position: sticky;
  top: 0;
  background: var(--surface-1);
  color: var(--text-secondary);
  font-weight: 600;
}}
#vg-tooltip-element.vg-tooltip.custom-theme {{ background: var(--surface-1);
  color: var(--text-primary); border: 1px solid var(--border); border-radius: 6px;
  font-family: inherit; font-size: 12px; padding: 6px 8px; box-shadow: none; }}
#vg-tooltip-element.custom-theme td.key {{ color: var(--text-secondary); font-weight: 400; }}
#vg-tooltip-element.custom-theme td.value {{ font-weight: 600; }}
"""


# --------------------------------------------------------------------------- js

# Head: the readiness hook exists before any external script loads. The 20 s
# fallback keeps a page whose bridge never ran from hanging a rasterizer.
_HEAD_JS = """window.__chartsErrors = [];
window.__chartsReady = new Promise(function (resolve) {
  window.__vizStart = resolve;
  setTimeout(function () {
    window.__chartsErrors.push("bridge did not start within 20 s");
    resolve();
  }, 20000);
});"""

# Body end: theme bridge, renderers, toggles. Plain constant - data arrives in
# the #viz-specs JSON element, never by string formatting.
_BRIDGE_JS = r"""(function () {
  "use strict";
  var root = document.documentElement;
  var specs = JSON.parse(document.getElementById("viz-specs").textContent);
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  var errors = window.__chartsErrors;
  var handles = specs.map(function () { return null; });
  var queue = Promise.resolve();
  var lastMode = null;

  function mode() {
    var t = root.getAttribute("data-theme");
    if (t === "light" || t === "dark") { return t; }
    return media && media.matches ? "dark" : "light";
  }
  function tok(name) { return getComputedStyle(root).getPropertyValue("--" + name).trim(); }
  function seriesColors() {
    var out = [];
    for (var i = 1; i <= 8; i++) { out.push(tok("series-" + i)); }
    return out;
  }
  function fontStack() { return getComputedStyle(document.body).fontFamily; }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }

  function vegaConfig() {
    var font = fontStack(), t2 = tok("text-secondary");
    return {
      background: tok("surface-1"), font: font, padding: 4, view: {stroke: null},
      axis: {domainColor: tok("axis"), tickColor: tok("axis"), gridColor: tok("grid"),
             gridWidth: 1, domainWidth: 1, labelColor: t2, titleColor: t2,
             labelFont: font, titleFont: font, titleFontWeight: "normal",
             labelFontSize: 12, titleFontSize: 12},
      axisX: {grid: false},
      axisBand: {grid: false},
      legend: {orient: "top", direction: "horizontal", labelColor: t2, titleColor: t2,
               labelFont: font, titleFont: font, titleFontWeight: "normal",
               labelFontSize: 12, titleFontSize: 12},
      title: {color: tok("text-primary"), subtitleColor: t2, font: font},
      range: {category: seriesColors()},
      line: {strokeWidth: 2, strokeCap: "round", strokeJoin: "round"},
      bar: {cornerRadiusEnd: 4},
      area: {fillOpacity: 0.1, line: {strokeWidth: 2}},
      point: {filled: true, size: 64, stroke: tok("surface-1"), strokeWidth: 2},
      rule: {color: tok("text-muted")},
      text: {color: t2, font: font}
    };
  }

  function echartsTheme() {
    var t2 = tok("text-secondary"), axis = tok("axis"), grid = tok("grid");
    var ax = {axisLine: {lineStyle: {color: axis}}, axisTick: {lineStyle: {color: axis}},
              axisLabel: {color: t2}, nameTextStyle: {color: t2},
              splitLine: {lineStyle: {color: grid, width: 1, type: "solid"}}};
    return {
      color: seriesColors(), backgroundColor: tok("surface-1"),
      textStyle: {color: t2, fontFamily: fontStack()},
      title: {textStyle: {color: tok("text-primary")}},
      legend: {textStyle: {color: t2}},
      tooltip: {backgroundColor: tok("surface-1"), borderColor: tok("border"),
                textStyle: {color: tok("text-primary")},
                axisPointer: {lineStyle: {color: tok("text-muted"), width: 1}}},
      categoryAxis: ax, valueAxis: ax, timeAxis: ax, logAxis: ax
    };
  }

  function plotlyLayout(layout, height) {
    var L = clone(layout || {});
    var s1 = tok("surface-1"), t1 = tok("text-primary"), t2 = tok("text-secondary");
    var patch = {paper_bgcolor: s1, plot_bgcolor: s1, colorway: seriesColors(),
                 font: {color: t2, family: fontStack()}};
    L.template = L.template || {};
    L.template.layout = Object.assign({}, L.template.layout || {}, patch);
    L.paper_bgcolor = s1;
    L.plot_bgcolor = s1;
    L.colorway = patch.colorway;
    L.font = Object.assign({}, L.font || {}, patch.font);
    L.xaxis = L.xaxis || {};
    L.yaxis = L.yaxis || {};
    Object.keys(L).forEach(function (k) {
      if (/^[xy]axis\d*$/.test(k)) {
        Object.assign(L[k], {gridcolor: tok("grid"), linecolor: tok("axis"),
                             zerolinecolor: tok("axis"), gridwidth: 1});
        if (k.charAt(0) === "x" && L[k].showgrid === undefined) { L[k].showgrid = false; }
      }
    });
    if (L.hovermode === "x unified") {
      Object.assign(L.xaxis, {showspikes: true, spikemode: "across", spikethickness: 1,
                              spikedash: "solid", spikecolor: tok("text-muted")});
    }
    L.hoverlabel = {bgcolor: s1, bordercolor: tok("border"), font: {color: t1}};
    L.legend = Object.assign(L.legend || {}, {font: {color: t2}});
    if (L.title && typeof L.title === "object") {
      L.title.font = Object.assign(L.title.font || {}, {color: t1});
    }
    L.margin = L.margin || {l: 48, r: 16, t: L.showlegend ? 40 : 24, b: 40};
    L.height = height;
    return L;
  }

  function renderVega(el, c) {
    return vegaEmbed(el, clone(c.spec), {actions: false, renderer: "svg",
                                         config: vegaConfig(), tooltip: {theme: "custom"}})
      .then(function (res) { return {dispose: function () { res.finalize(); }}; });
  }

  var measureCtx = null;
  function textWidth(text) {
    if (!measureCtx) { measureCtx = document.createElement("canvas").getContext("2d"); }
    measureCtx.font = "12px " + fontStack();
    return measureCtx.measureText(String(text)).width;
  }
  function asList(v) { return Array.isArray(v) ? v : (v ? [v] : []); }

  // End labels wear the text token; an auto grid (c.fit) is sized to the legend
  // rows at this width (1-2 rows, else a scroll legend), a y-axis name and the
  // longest end label. Returns a key that changes when the layout must change.
  function fitEcharts(opt, c, width) {
    var series = asList(opt.series), ends = [];
    series.forEach(function (s) {
      if (s && s.endLabel && s.endLabel.show) {
        s.endLabel = Object.assign({color: tok("text-secondary"), fontFamily: fontStack(),
                                    fontSize: 12}, s.endLabel);
        ends.push(s.name || "");
      }
    });
    var grid = opt.grid, legend = opt.legend;
    if (!c.fit || !grid || Array.isArray(grid) || !(width > 0)) { return ""; }
    var top = 16, rows = 0;
    if (legend && !Array.isArray(legend) && legend.show !== false) {
      var names = asList(legend.data).length ? asList(legend.data) : series.map(
        function (s, i) { return (s && s.name) || "series " + i; });
      var iw = legend.itemWidth == null ? 25 : legend.itemWidth;
      var gap = legend.itemGap == null ? 10 : legend.itemGap;
      var x = 0;
      rows = 1;
      names.forEach(function (n) {
        var w = iw + 5 + textWidth(n && typeof n === "object" ? n.name : n);
        if (x > 0 && x + w > width - 10) { rows += 1; x = 0; }
        x += w + gap;
      });
      if (rows > 2 && !legend.type) { legend.type = "scroll"; }
      if (legend.type === "scroll") { rows = 1; }
      top = 26 + rows * 14 + (rows - 1) * gap;
    }
    var y = asList(opt.yAxis)[0];
    if (y && y.name && (!y.nameLocation || y.nameLocation === "end")) { top += 18; }
    grid.top = top;
    if (ends.length) {
      grid.right = Math.ceil(16 + 8 + Math.max.apply(null, ends.map(textWidth)));
    }
    return [top, grid.right, legend && legend.type].join("|");
  }

  function renderEcharts(el, c, m) {
    var name = "viz-" + m;
    echarts.registerTheme(name, echartsTheme());
    var chart = echarts.init(el, name, {renderer: "svg"});
    var opt = clone(c.spec);
    var fitKey = fitEcharts(opt, c, el.clientWidth);
    var handle = {dispose: function () { chart.dispose(); },
                  resize: function () {
                    var next = clone(c.spec), key = fitEcharts(next, c, el.clientWidth);
                    if (key && key !== fitKey) { fitKey = key; chart.setOption(next, true); }
                    chart.resize();
                  }};
    return new Promise(function (resolve) {
      var done = false;
      function finish() { if (!done) { done = true; resolve(handle); } }
      chart.on("finished", finish);
      chart.setOption(opt);
      setTimeout(finish, 5000);
    });
  }

  function renderPlotly(el, c) {
    return Plotly.newPlot(el, clone(c.spec.data || []), plotlyLayout(c.spec.layout, c.height),
                          {responsive: true, displaylogo: false, displayModeBar: false})
      .then(function () { return {dispose: function () { Plotly.purge(el); }}; });
  }

  var RENDER = {"vega-lite": renderVega, "echarts": renderEcharts, "plotly": renderPlotly};
  var NEEDS = {"vega-lite": "vegaEmbed", "echarts": "echarts", "plotly": "Plotly"};

  function renderOne(i, m) {
    var c = specs[i];
    var el = document.getElementById(c.id);
    if (handles[i]) {
      try { handles[i].dispose(); } catch (e) { /* already gone */ }
      handles[i] = null;
    }
    el.textContent = "";
    if (typeof window[NEEDS[c.kind]] === "undefined") {
      return Promise.reject(new Error(NEEDS[c.kind] + " did not load (offline or CDN blocked)"));
    }
    return Promise.resolve().then(function () { return RENDER[c.kind](el, c, m); })
      .then(function (h) { handles[i] = h; });
  }

  function fail(i, err) {
    var msg = (err && err.message) || String(err);
    errors.push(specs[i].id + ": " + msg);
    var el = document.getElementById(specs[i].id);
    el.textContent = "";
    var p = document.createElement("p");
    p.className = "chart-error";
    p.textContent = "Chart could not be drawn: " + msg + ". The table view has the data.";
    el.appendChild(p);
  }

  function renderAll() {
    var m = mode();
    if (m === lastMode) { return window.__chartsReady; }
    lastMode = m;
    queue = queue.then(function () {
      return Promise.all(specs.map(function (c, i) {
        return renderOne(i, m).catch(function (e) { fail(i, e); });
      }));
    });
    window.__chartsReady = queue;
    return queue;
  }

  var themeBtn = document.getElementById("theme-toggle");
  function label() {
    var dark = mode() === "dark";
    themeBtn.textContent = dark ? "\u263C Light" : "\u263E Dark";
    themeBtn.setAttribute("aria-label", "Switch to " + (dark ? "light" : "dark") + " mode");
  }
  function themeChanged() { label(); renderAll(); }
  themeBtn.addEventListener("click", function () {
    root.setAttribute("data-theme", mode() === "dark" ? "light" : "dark");
  });
  new MutationObserver(themeChanged).observe(root, {attributes: true,
                                                    attributeFilter: ["data-theme"]});
  if (media) {
    if (media.addEventListener) { media.addEventListener("change", themeChanged); }
    else if (media.addListener) { media.addListener(themeChanged); }
  }

  Array.prototype.forEach.call(document.querySelectorAll(".table-toggle"), function (btn) {
    btn.addEventListener("click", function () {
      var card = btn.closest(".card");
      var on = btn.getAttribute("aria-pressed") !== "true";
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.textContent = on ? "Chart" : "Table";
      card.querySelector(".chart").hidden = on;
      card.querySelector(".table-wrap").hidden = !on;
      if (!on) { window.dispatchEvent(new Event("resize")); }
    });
  });
  window.addEventListener("resize", function () {
    handles.forEach(function (h) { if (h && h.resize) { h.resize(); } });
  });

  // Downloads: in the claude.ai viewer a data: link cannot download, so a granted
  // `downloads` capability takes the click. use() is never awaited by rendering;
  // save() runs only from a click; the data: href stays the fallback.
  var QUIET = {declined: 1, rate_limited: 1};
  var CALLER = {too_large: 1, bad_request: 1, transform_error: 1, rejected_extension: 1,
                request_unknown: 1};
  function csvLinks() { return Array.prototype.slice.call(document.querySelectorAll("a.csv")); }
  function csvText(a) {
    var href = a.getAttribute("href") || "";
    return decodeURIComponent(href.slice(href.indexOf(",") + 1));
  }
  function hideCsv(note) {
    csvLinks().forEach(function (a) {
      a.hidden = true;
      if (note) {
        var span = document.createElement("span");
        span.className = "csv-note";
        span.textContent = note;
        a.parentNode.insertBefore(span, a.nextSibling);
      }
    });
  }
  function wireDownloads(d) {
    csvLinks().forEach(function (a) {
      a.addEventListener("click", function (ev) {
        ev.preventDefault();
        if (a.getAttribute("data-save") === "pending") { return; }
        a.setAttribute("data-save", "pending");
        var req;
        try {
          req = d.save({filename: a.getAttribute("download") || "data.csv", data: csvText(a)});
        } catch (e) { req = Promise.reject(e); }
        Promise.resolve(req).then(function (res) {
          a.setAttribute("data-save", (res && res.status) || "saved");
        }, function (err) {
          var code = err && typeof err.code === "string" ? err.code : "unavailable";
          a.setAttribute("data-save", code);
          if (QUIET[code]) { return; }                  // the viewer said no / try later
          if (CALLER[code]) {
            if (window.console) { console.warn("downloads.save: " + code); }
            return;
          }
          if (code === "extension_not_enabled") {
            hideCsv("CSV download is not available in this view.");
            return;
          }
          hideCsv("");          // unavailable, not_granted, capability_*, unknown codes
        });
      });
      a.setAttribute("data-save", "ready");
    });
  }
  function setupDownloads() {
    var cl = window.claude, p;
    if (!csvLinks().length || !cl || typeof cl.use !== "function") { return; }
    try { p = cl.use("downloads"); } catch (e) { return; }
    Promise.resolve(p).then(function (d) {
      if (d && typeof d.save === "function") { wireDownloads(d); }
    }, function () { /* same as null: keep the data: link */ });
  }

  label();
  window.__vizStart(renderAll());
  setupDownloads();
})();"""


# --------------------------------------------------------------------------- page


def _check_title(title):
    words = str(title or "").split()
    if not 2 <= len(words) <= 4:
        raise ValueError(f"title must be 2-4 words for an Artifact page, got {len(words)}: "
                         f"{title!r}")
    return " ".join(words)


def _card_html(i, c, max_rows):
    esc = html.escape
    cid = f"chart-{i + 1}"
    title = c["title"] or f"Chart {i + 1}"
    rows = c["rows"] or []
    parts = [f'<figure class="card" id="{cid}">', '<div class="card-head">',
             f'<h2 class="card-title">{esc(title)}</h2>', '<div class="card-tools">',
             (f'<button type="button" class="table-toggle" aria-pressed="false" '
              f'aria-controls="{cid}-table">Table</button>')]
    table = '<p class="table-note">No underlying rows were supplied for this chart.</p>'
    if rows:
        table, cols = _table_html(rows, max_rows)
        name = _slug(c["title"], cid) + ".csv"
        parts.append(f'<a class="csv" download="{esc(name)}" '
                     f'href="{_csv_href(rows, cols)}">Download CSV</a>')
    parts.append("</div></div>")
    parts.extend(f'<p class="warning" role="note">Warning: {esc(w)}</p>' for w in c["warnings"])
    parts.append(f'<div class="chart" id="{cid}-plot" style="height: {c["height"]}px" '
                 f'role="img" aria-label="{esc(title)}"></div>')
    parts.append(f'<div class="table-wrap" id="{cid}-table" hidden>\n{table}\n</div>')
    if c["caption"]:
        parts.append(f"<figcaption>{esc(str(c['caption']))}</figcaption>")
    parts.append("</figure>")
    return "\n".join(parts)


def build_page(charts, title, description="", mode_default="auto", table_rows=None):
    """Self-contained Artifact-ready HTML for one or more chart dicts.

    table_rows caps the rows each table view renders (default 500); the CSV
    download always carries every row. The page uses the claude.ai `downloads`
    capability when granted: publish with capabilities_for(charts)
    (REQUIRED_CAPABILITIES is the full set). Raises ValueError on a bad title
    (not 2-4 words), unknown kind/mode, inline data over 2 MB (aggregate
    first) or a page over 16 MB.
    """
    if mode_default not in MODE_DEFAULTS:
        raise ValueError(f"mode_default must be one of {MODE_DEFAULTS}, got {mode_default!r}")
    page_title = _check_title(title)
    charts = list(charts or [])
    if not charts:
        raise ValueError("build_page needs at least one chart")
    max_rows = MAX_TABLE_ROWS if table_rows is None else max(1, int(table_rows))
    prepared = [prepare_chart(c) for c in charts]
    payload = [{"id": f"chart-{i + 1}-plot", "kind": c["kind"], "spec": c["spec"],
                "height": c["height"], "fit": c["fit"]} for i, c in enumerate(prepared)]
    payload_json = _script_json(payload)
    inline = len(payload_json) + sum(
        len(json.dumps(c["rows"], default=_json_default)) for c in prepared if c["rows"])
    if inline > MAX_INLINE_BYTES:
        raise ValueError(f"inline data is {inline / 1048576:.1f} MB (limit 2 MB): aggregate "
                         "first (group/bin in duckdb or polars) and pass the summary rows")
    kinds = [k for k in KINDS if any(c["kind"] == k for c in prepared)]
    scripts = "\n".join(f'<script src="{CDN[name]}"></script>'
                        for k in kinds for name in SCRIPTS_FOR[k])
    theme_attr = "" if mode_default == "auto" else f' data-theme="{mode_default}"'
    esc = html.escape
    desc = f'<p class="description">{esc(str(description))}</p>' if description else ""
    cards = "\n".join(_card_html(i, c, max_rows) for i, c in enumerate(prepared))
    doc = f"""<!DOCTYPE html>
<html lang="en"{theme_attr}>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(page_title)}</title>
<script>
{_HEAD_JS}
</script>
<style>{_css()}</style>
{scripts}
</head>
<body>
<div class="page">
<header class="page-head">
<div>
<h1>{esc(page_title)}</h1>
{desc}
</div>
<button type="button" id="theme-toggle" aria-label="Toggle dark mode">Theme</button>
</header>
<main class="charts">
{cards}
</main>
</div>
<script type="application/json" id="viz-specs">{payload_json}</script>
<script>
{_BRIDGE_JS}
</script>
</body>
</html>
"""
    size = len(doc.encode("utf-8"))
    if size >= MAX_PAGE_BYTES:
        raise ValueError(f"page is {size / 1048576:.1f} MB; Artifact pages must stay under "
                         "16 MB - aggregate first")
    return doc


def write_page(charts, path, **kw):
    """build_page(charts, **kw) written as UTF-8 to path; returns the absolute path."""
    doc = build_page(charts, **kw)
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8", newline="\n")
    return str(path)


def main(argv=None):
    """CLI: artifact_page.py charts.json out.html --title "Two Words" [--description ...]."""
    import argparse

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="artifact_page.py",
                                     description="Build an Artifact-ready chart page from a "
                                                 "JSON list of chart dicts.")
    parser.add_argument("charts", help="JSON file: list of {kind, spec, title, ...}")
    parser.add_argument("out", help="output .html path")
    parser.add_argument("--title", required=True, help="2-4 words")
    parser.add_argument("--description", default="")
    parser.add_argument("--mode", choices=MODE_DEFAULTS, default="auto")
    args = parser.parse_args(argv)
    try:
        with open(args.charts, encoding="utf-8") as fh:
            charts = json.load(fh)
        out = write_page(charts, args.out, title=args.title, description=args.description,
                         mode_default=args.mode)
    except (OSError, ValueError, TypeError) as exc:
        sys.stderr.write(f"artifact_page.py: {exc}\n")
        return 2
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
