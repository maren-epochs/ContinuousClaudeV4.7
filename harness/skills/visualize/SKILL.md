---
name: visualize
description: Chart procedure on tools/viz - form, color by job, validated palette, house style, render and LOOK - "chart this", "plot", "graph", "visualize", "dashboard", "make a figure"
user-invocable: true
---

Order is fixed: FORM, COLOR, VALIDATE, MARKS, HOVER, ACCESSIBILITY, RENDER + LOOK, ANTI-PATTERN
CHECK - color never first. Host CPython (`py -3.13`) through `tools/viz`; every color/font token
lives in `tools/viz/palette.json` - no hex literals in chart code, never hand-set colors.

**Where it runs.** `tools/viz/...` is relative to the ccv47 repo (elsewhere `~/.claude/tools/viz/`).
Python imports `from tools.viz import ...` with the parent of `tools/` on `sys.path`; inside
`/analyze-data` the same code goes through `run_python` (stateless, cwd = session work dir).

**0 HAND-OFF.** Data not clean or not aggregated -> `/analyze-data` steps 1-4 first; return with
a parquet. Aggregate with duckdb/polars; send <= 5000 rows to any chart. Exceptions: millions of
points, static -> datashader raster; long interactive time series -> plotly-resampler
`FigureResampler`; altair past 5000 rows -> `alt.data_transformers.enable("vegafusion")`.

**1 FORM.** Name the reader's job (magnitude, identity, polarity, headline, change-over-time,
distribution, relationship, part-to-whole, ranking, flow, spatial), then ask the tool:

```bash
py -3.13 tools/viz/recommend.py agg.parquet --json      # job inferred; --job ranking to set it
```

Python: `recommend.recommend(df_or_profile, job=None)`. Accept the form, or override it with one
stated reason in the report. Its refusals bind: dual axis -> small multiples; pie past 5 slices
-> bar; one bar / 2-slice pie -> stat tile; over 8 series -> fold into "Other" or small
multiples; over 7 meaningful color classes -> table. The answer may be a stat tile.

**2 COLOR BY JOB.** One rule per job, all from `tools.viz.palette`:

| Job | Call | Rule |
|-----|------|------|
| identity | `palette.categorical(mode, n)` | fixed slot order, never cycled; n > 8 raises - fold |
| magnitude | `palette.sequential()` | one hue, light -> dark |
| polarity | `palette.diverging(mode)` -> (low, mid, high) | warm/cool poles, neutral gray mid |
| state | `palette.status(mode)` | good/warning/serious/critical only, with icon + label |

One series -> slot 1 on every bar; no value ramp on nominal categories, even when `recommend`
says `"color": "sequential"`. Ordered categories -> ordinal ramp, validated with `--ordinal`.
Color follows the entity, not its rank: map entity -> slot once, so filters never repaint.

**3 VALIDATE.** Run the validator on the exact hexes the chart uses, per mode. FAIL (exit 1)
stops the procedure: cut series, facet, or reorder until PASS. A WARN obligates direct labels
or a table view - not dismissable. `--pairs all` (scatter/bubble/maps/facets) passes for the
first 3 slots only: more series there -> small multiples.

```bash
HEX=$(py -3.13 -c "from tools.viz import palette; print(','.join(palette.categorical('light', 4)))")
py -3.13 tools/viz/validate_palette.py "$HEX" --mode light       # then dark slots, --mode dark
py -3.13 tools/viz/validate_palette.py "$HEX" --mode light --pairs all
```

**4 MARKS.** Apply the house style, then build: matplotlib `style.apply_matplotlib(mode)` (bars
`**style.bar_kwargs(mode)`, areas `alpha=style.AREA_OPACITY`); seaborn `style.set_seaborn(mode)`;
plotly `fig.update_layout(template=style.plotly_template(mode))` (scatter: `hovermode="closest"`);
altair `with alt.theme.enable(style.altair_theme(mode)):` around build + save; bokeh
`export.save(p, stem, bokeh_theme=style.bokeh_theme(mode))`. The style carries 2px lines, 8px
markers with a 2px surface ring, 2px surface gaps, solid hairline grid. Yours to enforce: bars
<= `style.BAR_MAX_PX` (24px) thick (size the figure, see 7a), selective direct labels only.

**5 HOVER.** Interactive = hover by default: crosshair + every-series tooltip on line/area,
per-mark on bar/dot (`artifact_page` adds both). Tooltips never gate; filters in one top row.

**6 ACCESSIBILITY.** >= 2 series -> legend always, <= 4 also direct-labeled; one series -> no
legend, the title names it. Interactive pages ship a table view. Dark mode is its own validated
slots (step 3), rendered and looked at - not an automatic flip. Text wears `palette.text(mode)`.

**7 OUTPUT - one of three paths.**

(a) Static PNG/SVG - reports, findings, files. `py -3.13 chart.py`, or the body inside
`run_python(r'''...''')`. Both modes when the chart will be shared.

```python
import sys; sys.path.insert(0, "C:/Users/me/ccv47")      # parent of tools/ (or ~/.claude)
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt, pandas as pd
from tools.viz import export, style
df = pd.read_parquet("agg.parquet").sort_values("hours")
for mode in ("light", "dark"):
    style.apply_matplotlib(mode)
    fig, ax = plt.subplots(figsize=(7, 1 + 0.4 * len(df)))      # 0.4in/row * 0.4 -> ~16px bars
    ax.barh(df["team"], df["hours"], height=0.4, **style.bar_kwargs(mode))   # slot 1
    ax.grid(axis="y", visible=False); ax.grid(axis="x", visible=True)    # grid on the value axis
    ax.set_title("Hours by team", loc="left")
    out = export.save(fig, f"hours-{mode}", formats=("png", "svg"), mode=mode)
    print(out["png"], out["svg"], out["notes"])                 # paths only
    plt.close(fig)
```

`export.save` covers matplotlib/plotly/altair/bokeh/great_tables/holoviews; `notes` say which
path ran (plotly PNG falls back from kaleido to Playwright); bokeh/great_tables give no SVG.

(b) Interactive page - claude.ai Artifact, from Vega-Lite (altair), Plotly or ECharts specs:

```python
from tools.viz import artifact_page
charts = [artifact_page.from_altair(chart, title="Hours by team"),
          artifact_page.from_plotly(fig, title="Weekly trend")]
html = artifact_page.write_page(charts, "hours.html", title="Team Hours", description="...")
```

CLI: `py -3.13 tools/viz/artifact_page.py charts.json out.html --title "Two Words"` (list of
`{kind: vega-lite|plotly|echarts, spec, title, caption, rows, height}`). Title 2-4 words. The page
carries tokens, dark mode, hover, the legend rule, a Table toggle + CSV link per card, and strips
dual axes with a visible warning. Preview light, dark, phone (waits on `window.__chartsReady`):
`py -3.13 tools/viz/export.py render out.html out.png --mode dark --width 390`. Monthly
Vega-Lite data: `timeUnit: "utcyearmonth"` (else local time shows "Feb 28" for March).
Known gaps - warn or work around: facet/concat/repeat Vega-Lite specs are not resized at phone
width (one chart per card); Vega-Lite bars have no 24px cap (set `size`, or ECharts/Plotly);
Vega-Lite area has no hover dots.
PUBLISHING: only the orchestrator (main session) has the Artifact tool. Workers cannot publish:
leave the absolute HTML path + `PUBLISH-PENDING` in findings.md. The orchestrator publishes it
private (default), declares downloads only when the user wants the CSV export, records the URL.

(c) Render static site: only after the user names the workspace (never pick one); else report "not deployed".

**Geo.** Static: geopandas `.plot()` after `apply_matplotlib(mode)`. Interactive: altair
`mark_geoshape` / plotly choropleth via `artifact_page`; folium loads non-allowlisted CDNs.

**8 LOOK.** Read every PNG with the Read tool - light AND dark, plus phone width for pages. The
validator checks color, not layout: look for label collisions, clipped labels, a cut-off axis
band, overflow. Then the hard rules - any match is wrong, fix before reporting:

- One axis. Never two y-scales; different scales -> small multiples or index to 100 at t0.
- Fixed hue order, 8 max; a 9th series folds into "Other" or small multiples.
- No rainbow: sequential is one hue; diverging midpoint is neutral gray.
- Status colors reserved for state, never "series 4"; icon + label, never color alone.
- Text wears text tokens, never series color. Legend for >= 2 series, none for one.
- Every interactive page has a table view; no value readable only by tooltip.
- Thin marks, solid hairline grid, no borders around marks, no number on every point.
- No one-bar bar chart, no 2-slice pie, no pie for close values (bar instead).

**9 REPORT.** Token doctrine: print paths, never data. `continuum/research/<topic>/findings.md`
(Write tool): absolute artifact paths, validator output, form + reason, publish status (URL /
`PUBLISH-PENDING` / "not deployed"). Reusable gotcha -> `bloks new rule "<text>" --tags viz,<lib>`.

Install: `tools/requirements-viz.txt` header. Live copy: `py -3.13 install/sync_global.py --apply`.
