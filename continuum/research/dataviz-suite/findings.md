# dataviz-suite demo (VAL-302) - findings

End-to-end run of `/visualize` (harness/skills/visualize/SKILL.md). One script, `continuum/research/dataviz-suite/demo.py`, produces everything in `continuum/research/dataviz-suite/out/`. It can be re-run, takes ~9 s, and prints only absolute paths plus one `capabilities_for:` line. All paths below are repo-relative (this directory is tracked in a public repo).

Refresh (second run): regenerated after the tool fixes in c547c9e (recommend.py, VAL-402) and 307b602 (artifact_page.py + SKILL.md, VAL-401/403). Changes: chart A follows the new raw + smoothed house rule; chart B uses `end_labels=True` and the manual legend workaround is removed; the static chart's IBM label was moved off the AMZN/IBM end; the recommend JSON was re-run with the fixed tool; `capabilities_for(charts)` is printed. Gaps fixed by those commits are marked FIXED below.

## Datasets

| Dataset | Source | Rows | Columns | Used for |
|---|---|---|---|---|
| seattle_weather | `vega_datasets.data.seattle_weather()` (local, v0.9.0) | 1461 daily, 2012-01-01..2015-12-31 | date, precipitation, temp_max, temp_min, wind, weather (sun 714 / fog 411 / rain 259 / drizzle 54 / snow 23) | page chart A (temp_max + 7-day mean), great_tables monthly table |
| stocks | `vega_datasets.data.stocks()` (local) | 560 monthly, 2000-01..2010-03 | symbol (AAPL, AMZN, GOOG, IBM, MSFT), date, price | static matplotlib chart, page chart B |

Step 0 HAND-OFF: both are clean and <= 5000 rows, so the charts need no aggregation. The table aggregates by calendar month in pandas.

## Procedure followed (step by step, with commands)

All steps run from the repo root with `py -3.13`. The skill PRELUDE line opens demo.py verbatim (cwd branch -> repo `tools/viz`).

| Step | What was done | Command / call |
|---|---|---|
| 0 HAND-OFF | datasets saved as CSV for the CLI | `df.to_csv(out/seattle-weather.csv)`, `out/stocks.csv` |
| 1 FORM | recommender on both CSVs (CLI) + Python API on chart A's slice | `py -3.13 tools/viz/recommend.py out/seattle-weather.csv --json`; same for `out/stocks.csv`; `recommend.recommend(recommend.profile_frame(weather[["date","temp_max"]]))` |
| 2 COLOR | stocks: `encoding.color` = `symbol` (a dimension) -> `palette.categorical(mode, 5)`. Each symbol gets one fixed slot (AAPL, AMZN, GOOG, IBM, MSFT = slots 1-5), used in BOTH the static chart and ECharts. Chart A: `single` -> slot 1 for the 7-day mean; the raw daily series is context and wears the muted text token (step 4 rule) | `palette.categorical(m, 5)` |
| 3 VALIDATE | exact hexes, per mode, adjacent pairs (lines) | `py -3.13 tools/viz/validate_palette.py "<5 hexes>" --mode light` / `--mode dark` |
| 4 MARKS | matplotlib `style.apply_matplotlib(mode)`. Chart A: raw 1px muted, mean 2px slot 1 (dense + raw/smoothed rules). great_tables: palette tokens mapped by hand into `tab_options` | `style.apply_matplotlib(m)` |
| 5 HOVER | chart A is a layered spec, so it gets its own nearest-x pointer param + rule + two-value tooltip (step 5: layered specs get no automatic crosshair). Chart B uses the artifact_page default axis tooltip | `artifact_page.write_page(charts, out/weather-and-stocks.html, title="Weather and Stocks", description=...)` |
| 6 ACCESSIBILITY | static chart: legend + 4 direct end labels. Chart B: legend + themed end labels. Chart A: one colored series, so no legend; the caption names both marks. Each card has a Table toggle + CSV. Dark mode rendered and read | - |
| 7a OUTPUT static | stocks multi-line, light + dark, PNG + SVG | `export.save(fig, out/stocks-{mode}, formats=("png","svg"), mode=mode)` |
| 7a OUTPUT table | GT -> PNG (export renders `as_raw_html` via Playwright, `selector="table"`) | `export.save(gt, out/weather-table, formats=("png",), mode="light")` |
| 7b OUTPUT page | two-chart page; publisher capabilities; light/dark/phone renders | `artifact_page.capabilities_for(charts)` -> `{"downloads": true}`; `export.render_html(html, out/page-{light,dark}.png, mode=m)`; `export.render_html(html, out/page-phone-light.png, width=390, mode="light")` |
| 8 LOOK | Read every PNG (2 static + table + 3 page), then and after each fix | Read tool |
| 9 REPORT | this file | - |

Hover verified live (Playwright pointer move from a scratch script, nothing added to out/):
- Chart A tooltip: `Date Apr 11, 2014 | Daily max (C) 17.2 | 7-day mean (C) 15.6`.
- Chart B tooltip lists all 5 series (`AAPL 67.85 | AMZN 30.83 | GOOG 378.53 | IBM 76.35 | MSFT 24.13`).
- `window.__chartsErrors` = `[]`.
- Chart A Table toggle header: `date | temp_max | temp_max_7d`.

## Artifacts

| Path | Bytes | Note |
|---|---|---|
| continuum/research/dataviz-suite/demo.py | 14243 | generator |
| continuum/research/dataviz-suite/out/stocks-light.png | 87500 | (1) static, light, dpi 144 |
| continuum/research/dataviz-suite/out/stocks-light.svg | 30640 | (1) static, light |
| continuum/research/dataviz-suite/out/stocks-dark.png | 83256 | (1) static, dark |
| continuum/research/dataviz-suite/out/stocks-dark.svg | 30640 | (1) static, dark |
| continuum/research/dataviz-suite/out/weather-table.png | 81572 | (2) great_tables, light, scale 2 |
| continuum/research/dataviz-suite/out/weather-and-stocks.html | 197112 | (3) interactive page |
| continuum/research/dataviz-suite/out/page-light.png | 109384 | (3) 1200 px, light |
| continuum/research/dataviz-suite/out/page-dark.png | 108657 | (3) 1200 px, dark |
| continuum/research/dataviz-suite/out/page-phone-light.png | 83567 | (3) 390 px, light |
| continuum/research/dataviz-suite/out/validator.txt | 1615 | (4) validator output, both modes |
| continuum/research/dataviz-suite/out/recommend-seattle-weather.json | 1764 | (5) CLI |
| continuum/research/dataviz-suite/out/recommend-stocks.json | 854 | (5) CLI |
| continuum/research/dataviz-suite/out/recommend-seattle-temp-max.json | 258 | (5) API, chart A slice |
| continuum/research/dataviz-suite/out/seattle-weather.csv | 49300 | recommend input |
| continuum/research/dataviz-suite/out/stocks.csv | 12833 | recommend input |

Total out/ ~897 KB. The page loads only pinned CDNs: jsdelivr vega 6.4.0 / vega-lite 6.4.3 / vega-embed 7.3.0, cdnjs echarts 6.1.0. No absolute paths or user names inside out/ (grep clean).

PUBLISH-PENDING (republish to the existing URL): continuum/research/dataviz-suite/out/weather-and-stocks.html

Existing URL (first publish, orchestrator, private): https://claude.ai/artifact/BB3VuPmVxfjuyXqhFbrzGf. Republish with `capabilities` = `{"downloads": true}` (printed by demo.py from `artifact_page.capabilities_for(charts)`).

Publish-time finding from the first run: the per-card "Download CSV" links were plain data: URLs, and the claude.ai viewer never lets a page download those. FIXED in 307b602: artifact_page.py asks for the `downloads` capability and saves through the viewer's downloads API, with the data: link as the fallback; `capabilities_for(charts)` tells the publisher what to declare.

## Validator output (out/validator.txt, scope footer lines trimmed)

```
$ py -3.13 tools/viz/validate_palette.py "#2a78d6,#eb6834,#1baf7a,#eda100,#e87ba4" --mode light
Palette (light, surface #fcfcfb, categorical): 5 slots
  [PASS] Lightness band         all 5 inside L 0.43–0.77
  [PASS] Chroma floor           all 5 >= 0.1
  [PASS] CVD separation         worst adjacent #eda100↔#1baf7a ΔE 9.1 (protan) · tritan 5.8
  [PASS] Normal-vision floor    worst adjacent #e87ba4↔#eda100 ΔE 19.6 (normal)
  [WARN] Contrast vs surface    below 3:1 — relief required (visible labels or table view): [["#1baf7a",2.74],["#eda100",2.11],["#e87ba4",2.62]]

  → ALL CHECKS PASS  (CVD in the 6–8 floor band is legal ONLY with secondary encoding: direct labels, gaps, or texture)
exit 0

$ py -3.13 tools/viz/validate_palette.py "#3987e5,#d95926,#199e70,#c98500,#d55181" --mode dark
Palette (dark, surface #1a1a19, categorical): 5 slots
  [PASS] Lightness band         all 5 inside L 0.48–0.67
  [PASS] Chroma floor           all 5 >= 0.1
  [PASS] CVD separation         worst adjacent #c98500↔#199e70 ΔE 8.4 (protan) · tritan 8.7
  [PASS] Normal-vision floor    worst adjacent #d55181↔#c98500 ΔE 19.3 (normal)
  [PASS] Contrast vs surface    all 5 >= 3:1

  → ALL CHECKS PASS  (CVD in the 6–8 floor band is legal ONLY with secondary encoding: direct labels, gaps, or texture)
exit 0
```

The default `--surface` equals `palette.surface(mode)["surface"]` in both modes (checked), so the skill command validates against the real chart surface. Chart A's one categorical slot is slot 1, which this 5-slot run already covers; the muted token is text, not a categorical slot.

WARN consequence (binding per skill step 3): light slots 3-5 (GOOG, IBM, MSFT) are below 3:1. The static chart therefore direct-labels exactly those three plus AAPL. Page chart B now end-labels all five series, and every card keeps its table view.

## Recommender output and whether it was followed

| Input | job | form | encoding | Followed? |
|---|---|---|---|---|
| out/stocks.csv (CLI) | change-over-time (`job_inferred` true) | multi-line | x date, y price, color symbol, label direct | Yes. Static: multi-line with legend + 4 direct end labels. Page chart B: multi-line with legend + themed end labels on all 5 (`end_labels=True`) |
| out/seattle-weather.csv (CLI) | change-over-time (`job_inferred` true) | small multiples | x date, y value, facet measure (no color). Warnings: only wind (1e0) differs in scale from precipitation/temp_max/temp_min (1e1); `weather` is a per-row attribute, not a series | Not as a 4-panel chart: the deliverables fix chart A to temp_max only, plus a monthly table. The dual-axis refusal is honored (no measures mixed on one axis). The table uses `weather` as an aggregated per-month attribute (most common value), which the warning allows ("aggregate") |
| weather[date, temp_max] (API) | change-over-time (`job_inferred` true) | line | x date, y temp_max, color single | Yes: slot 1 on the colored series (the 7-day mean). The raw daily series is context in the muted token (skill step 4 raw + smoothed rule), so there is no second categorical slot and no legend |

## Deviations / gaps in the skill or tools

1. FIXED (307b602): `tools/viz/artifact_page.py` `_vl_legend` / `_vl_crosshair` forced `legend: null` and a one-row tooltip on fold-derived color fields. Fold series are now counted from the transform (legend, every-series tooltip, slots in fold order); calculate outputs keep the author's legend. Not used for chart A, because fold gives each folded series a categorical slot and the raw + smoothed rule needs the raw series in the muted token at 1px with the mean at 2px. A single fold line mark cannot do both.
2. OPEN (new): there is no way to name a palette token inside a page spec. A hex picked per mode in Python does not follow the page's light/dark toggle. Chart A works around this with a minimal layered spec: the raw series is drawn as day-to-day `rule` segments (x/y -> x2/y2 via a `lead` window), and the bridge themes the `rule` mark with `--text-muted` in both modes. The mean is a `line` with `color.datum` (slot 1, 2px), and the spec carries its own crosshair. Fix route: a token reference in specs (e.g. `{"token": "text-muted"}` resolved by the bridge), or an artifact_page option `context_series=[...]` that draws named series muted and 1px.
3. FIXED (307b602): SKILL.md step 5 now says the automatic crosshair applies to single-view specs only, and that layered/params specs need their own pointer params.
4. FIXED (307b602): the ECharts legend icon is a stroke for markerless lines, grid.top is sized to the measured legend (+ y-axis name), and `end_labels=True` adds themed end labels (`--text-secondary`, `moveOverlap: shiftY`). The manual legend workaround is removed from demo.py. Remaining observation: `end_labels` labels every series. With 5 series that is past the skill's "<= 4 also direct-labeled" guidance; AMZN/IBM labels stack legibly via shiftY. A per-series opt-out would allow the 4-label choice the static chart makes.
5. OPEN: great_tables styling. `tools/viz/style.py` has no great_tables helper and SKILL.md step 4 lists none, so demo.py maps tokens into `tab_options` by hand (1px hairlines, `table_body_border_top_width="0px"`). `export.save(gt, mode=...)` does not theme the table colors.
6. FIXED (c547c9e): `tools/viz/recommend.py`. `weather` is no longer a color (per-row attribute warning instead); the dual-axis warning names only wind as differing; `recommend()` returns `job` / `job_inferred`.
7. OPEN: `tools/viz/export.py` crops the great_tables PNG exactly to the `<table>` box with zero margin; the last column header ends ~10 px from the right edge.
8. FIXED (307b602): SKILL.md step 4 now has the dense-series rule (> ~500 points -> 1px) and the raw + smoothed rule (raw muted, smoothed in a slot). Both are applied to chart A.
9. OPEN: Vega-Lite daily dates. `timeUnit: "utcyearmonthdate"` is needed so days do not shift west of UTC, but it renders axis labels as "Jan 01, 2012"; `axis: {format: "%Y", tickCount: "year"}` fixes them. SKILL.md documents only the monthly case.
10. FIXED (307b602): SKILL.md step 9 / 7b now say repo-relative paths in findings and absolute paths in stdout/chat only.
11. OPEN: the PRELUDE resolves on `Path.cwd()`, so demo.py must run from the repo root. Elsewhere it falls back to `~/.claude/tools/viz`, which is only current after `install/sync_global.py --apply`.
12. OPEN: the light-mode validator CVD line reports `tritan 5.8` (below the 6-8 floor band) under PASS, because the status grades only the protan worst. Unclear whether intended.
13. OPEN: under this repo's ruff config the verbatim PRELUDE line needs `# noqa: I001`.

## LOOK - anti-pattern self-check (from the rendered PNGs)

Read with the Read tool after the refresh: out/stocks-light.png, out/stocks-dark.png, out/weather-table.png, out/page-light.png, out/page-dark.png, out/page-phone-light.png.

What changed in the refresh, as seen in the renders:
- Chart A: gray 1px raw hairline under a blue 2px 7-day mean, in both modes. The gray follows the theme: mid-gray on the light surface, lighter gray on the dark one.
- Chart B: stroke legend keys, one legend row at 1200 and 390 px, and end labels GOOG / AAPL / AMZN / IBM / MSFT in the text color. AMZN and IBM are stacked without overlap.
- Static chart: the IBM label now sits clearly below the AMZN/IBM ends, with a hairline leader to IBM's end point.

First-run defects (fixed then, still fixed): years on the chart A axis, the stroke legend, one legend row at 390 px, and 1px table rules.

| Entry (anti-patterns.md / skill step 8) | Result | Evidence |
|---|---|---|
| Dual-axis charts | PASS | one y axis everywhere; no dual-axis warning on the page |
| Recolor-on-filter / color follows entity | PASS (mapping) | each symbol mapped to its slot once; same hue per symbol in static and page. Legend-toggle repaint not exercised |
| Cycling hues past 8 | PASS | 5 slots max |
| Eyeballing colorblind-safety | PASS | validator run in both modes, exit 0 |
| Value ramp on nominal categories | PASS | no ramps used |
| Rainbow sequential / diverging midpoint | N/A | none used |
| Status color for a non-status series | PASS | no status tokens used; the raw series uses the muted text token, not a status token |
| Eight hues when the story is one number | PASS | stocks: 5 trajectories; chart A: one hue |
| One-bar bar / 2-slice pie / pie for close values | N/A | none |
| More than ~7 color classes | PASS | 5 |
| Thick saturated blocks, heavy grid | PASS | chart A raw 1px muted, mean 2px; grids hairline |
| Dashed gridlines / axis rules | PASS | solid hairlines in all renders |
| Number on every data point | PASS | end labels only (names, no values) |
| Border around marks | N/A | lines only |
| Label clipped / overflowing | PASS | static and ECharts end labels inside their plots at 1200 and 390 px; table header ~10 px from the edge (gap 7), not clipped |
| Label collision | PASS | static IBM label separated from the AMZN/IBM ends with a leader; ECharts AMZN/IBM labels stacked by shiftY, no overlap |
| Container height excludes x-axis band | PASS | x labels visible in all 3 page renders, no nested scroll |
| Text wears text tokens | PASS | static labels text-secondary; ECharts end labels `--text-secondary`; titles text-primary; both modes |
| Legend for >= 2 series, none for one | PASS | stocks charts: legend; chart A: one colored series + a muted context series -> no legend, the caption names both ("Gray hairline: daily max. Blue line: 7-day rolling mean.") |
| <= 4 direct labels (skill step 6) | static PASS / page note | static: AAPL, GOOG, IBM, MSFT (the light WARN slots + AAPL), AMZN legend only. Page chart B: `end_labels` labels all 5 (gap 4) |
| Tooltip as the only way to read a value | PASS | per-card table view incl. `temp_max_7d` (rows passed explicitly); static values read off the axis |
| Pinpoint hover targets | PASS | nearest-x crosshair (chart A), axis trigger (chart B) |
| Table view on interactive page | PASS | Table + Download CSV on both cards; `capabilities_for` -> `{"downloads": true}` |
| Dark mode: own validated slots, rendered and read | PASS | dark slots validated; page-dark and stocks-dark read |
| Phone width: no horizontal scroll, no overlap | PASS | page-phone-light.png: cards stack, legend one row, end labels fit |
| Display face / tabular-nums on hero, texture, filters, skeleton flash | N/A | not present |
