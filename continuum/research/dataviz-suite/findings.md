# dataviz-suite demo (VAL-302) - findings

First real run of `/visualize` (harness/skills/visualize/SKILL.md) end to end. One script, `continuum/research/dataviz-suite/demo.py`, produces everything in `continuum/research/dataviz-suite/out/`; re-runnable, ~9 s, prints only absolute paths. All paths below are repo-relative (this directory is tracked in a public repo).

## Datasets

| Dataset | Source | Rows | Columns | Used for |
|---|---|---|---|---|
| seattle_weather | `vega_datasets.data.seattle_weather()` (local, v0.9.0) | 1461 daily, 2012-01-01..2015-12-31 | date, precipitation, temp_max, temp_min, wind, weather (sun 714 / fog 411 / rain 259 / drizzle 54 / snow 23) | page chart A (temp_max + 7-day mean), great_tables monthly table |
| stocks | `vega_datasets.data.stocks()` (local) | 560 monthly, 2000-01..2010-03 | symbol (AAPL, AMZN, GOOG, IBM, MSFT), date, price | static matplotlib chart, page chart B |

Step 0 HAND-OFF: both clean and <= 5000 rows; no aggregation needed for charts. Table aggregates by calendar month in pandas.

## Procedure followed (step by step, with commands)

All run from the repo root with `py -3.13`; the skill PRELUDE line opens demo.py verbatim (cwd branch -> repo `tools/viz`).

| Step | What was done | Command / call |
|---|---|---|
| 0 HAND-OFF | datasets saved as CSV for the CLI | `df.to_csv(out/seattle-weather.csv)`, `out/stocks.csv` |
| 1 FORM | recommender on both CSVs (CLI) + Python API on chart A's slice | `py -3.13 tools/viz/recommend.py out/seattle-weather.csv --json`; same for `out/stocks.csv`; `recommend.recommend(recommend.profile_frame(weather[["date","temp_max"]]))` |
| 2 COLOR | stocks `encoding.color` = `symbol` (a dimension) -> `palette.categorical(mode, 5)`, entity -> slot fixed once (AAPL, AMZN, GOOG, IBM, MSFT = slots 1-5) in BOTH the static chart and ECharts; chart A: `single` for one measure, the 7-day mean adds a 2nd series -> slots 1-2 | `palette.categorical(m, 5)` |
| 3 VALIDATE | exact hexes, per mode, adjacent pairs (lines) | `py -3.13 tools/viz/validate_palette.py "<5 hexes>" --mode light` / `--mode dark` |
| 4 MARKS | matplotlib `style.apply_matplotlib(mode)`; great_tables: no style helper exists -> palette tokens mapped by hand into `tab_options` | `style.apply_matplotlib(m)` |
| 5 HOVER | chart A: hand-authored crosshair (gap 1); chart B: artifact_page default axis tooltip | `artifact_page.write_page(charts, out/weather-and-stocks.html, title="Weather and Stocks", description=...)` |
| 6 ACCESSIBILITY | legend on every >= 2-series chart; static: 4 direct end labels; page: Table toggle + CSV per card; dark mode rendered and read | - |
| 7a OUTPUT static | stocks multi-line, light + dark, PNG + SVG | `export.save(fig, out/stocks-{mode}, formats=("png","svg"), mode=mode)` |
| 7a OUTPUT table | GT -> PNG (export renders `as_raw_html` via Playwright, `selector="table"`) | `export.save(gt, out/weather-table, formats=("png",), mode="light")` |
| 7b OUTPUT page | two-chart page; light/dark/phone renders | `export.render_html(html, out/page-{light,dark}.png, mode=m)`; `export.render_html(html, out/page-phone-light.png, width=390, mode="light")` |
| 8 LOOK | Read every PNG (2 static + table + 3 page); 5 defects found, fixed in demo.py, re-rendered, re-read | Read tool |
| 9 REPORT | this file | - |

Hover verified live (Playwright pointer move from a scratch script, nothing added to out/): chart A tooltip `Date Apr 11, 2014 | Daily max (C) 17.2 | 7-day mean (C) 15.6`; chart B tooltip lists all 5 series (`AAPL 75.51 | AMZN 44.82 | GOOG 432.66 | IBM 75.89 | MSFT 26.14`); `window.__chartsErrors` = `[]`; chart A Table toggle header `date | temp_max | temp_max_7d`.

## Artifacts

| Path | Bytes | Note |
|---|---|---|
| continuum/research/dataviz-suite/demo.py | 13317 | generator |
| continuum/research/dataviz-suite/out/stocks-light.png | 86301 | (1) static, light, dpi 144 |
| continuum/research/dataviz-suite/out/stocks-light.svg | 30441 | (1) static, light |
| continuum/research/dataviz-suite/out/stocks-dark.png | 82224 | (1) static, dark |
| continuum/research/dataviz-suite/out/stocks-dark.svg | 30441 | (1) static, dark |
| continuum/research/dataviz-suite/out/weather-table.png | 81572 | (2) great_tables, light, scale 2 |
| continuum/research/dataviz-suite/out/weather-and-stocks.html | 191456 | (3) interactive page, published (see below) |
| continuum/research/dataviz-suite/out/page-light.png | 107442 | (3) 1200 px, light |
| continuum/research/dataviz-suite/out/page-dark.png | 106476 | (3) 1200 px, dark |
| continuum/research/dataviz-suite/out/page-phone-light.png | 82294 | (3) 390 px, light |
| continuum/research/dataviz-suite/out/validator.txt | 1615 | (4) validator output, both modes |
| continuum/research/dataviz-suite/out/recommend-seattle-weather.json | 1553 | (5) CLI |
| continuum/research/dataviz-suite/out/recommend-stocks.json | 826 | (5) CLI |
| continuum/research/dataviz-suite/out/recommend-seattle-temp-max.json | 205 | (5) API, chart A slice |
| continuum/research/dataviz-suite/out/seattle-weather.csv | 49300 | recommend input |
| continuum/research/dataviz-suite/out/stocks.csv | 12833 | recommend input |

Total out/ ~885 KB. The page loads only pinned CDNs: jsdelivr vega 6.4.0 / vega-lite 6.4.3 / vega-embed 7.3.0, cdnjs echarts 6.1.0. No absolute paths or user names inside out/ (grep clean).

PUBLISHED (orchestrator, Artifact tool, private): https://claude.ai/artifact/BB3VuPmVxfjuyXqhFbrzGf

Publish-time finding: the per-card "Download CSV" links are plain data: URLs, which the claude.ai artifact viewer never lets a page download. Fix in artifact_page.py: declare the `downloads` capability and save through the viewer's downloads API, falling back to the data: link outside the viewer.

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

Default `--surface` equals `palette.surface(mode)["surface"]` in both modes (checked), so the skill command validates against the real chart surface.

WARN consequence (binding per skill step 3): light slots 3-5 (GOOG, IBM, MSFT) < 3:1 -> the static chart direct-labels exactly those three plus AAPL; the page relies on the per-card table view.

## Recommender output and whether it was followed

| Input | job | form | encoding | Followed? |
|---|---|---|---|---|
| out/stocks.csv (CLI) | change-over-time (inferred) | multi-line | x date, y price, color symbol, label direct | Yes: static multi-line with legend + 4 direct end labels; page chart B multi-line with legend + table view, no direct labels (gap 3c) |
| out/seattle-weather.csv (CLI) | change-over-time (inferred) | small multiples | x date, y value, facet measure, color weather; dual-axis warning | Not as a 4-panel chart: the deliverables fix chart A to temp_max only + a monthly table. Dual-axis refusal honored (no measures mixed on one axis). `color: weather` not usable (gap 5a) |
| weather[date, temp_max] (API) | (API returns no job) | line | x date, y temp_max, color single | Form yes. Color overridden for one stated reason: the 7-day rolling mean is a 2nd series, so slots 1-2 + legend instead of `single` |

## Deviations / gaps in the skill or tools

1. `tools/viz/artifact_page.py` `_vl_legend` / `_vl_crosshair`: a single-view Vega-Lite line whose series come from a transform (window + `fold` -> color field `series`) gets `color.legend` forced to `null` (field absent from inline rows -> `_distinct` = 0 < 2; an explicit legend dict is overwritten) and a one-row tooltip instead of the every-series pivot. Probe: `prepare_chart(...)` returned `{"field": "series", "type": "nominal", "legend": null}`. Workaround in demo.py: hand-authored layered spec (two line layers with `color.datum`, hover points, rule with a `nearest` point param on `x`, tooltip listing both fields). Fix route: skip legend/series counting when the color field is not in the inline rows (or comes from `fold`/`calculate`), keep the author's legend.
2. SKILL.md step 5/7b says "artifact_page adds both" (crosshair + tooltip) without saying this applies only to single-view line/area specs; any `layer`/`params` spec gets nothing. The known-gaps list should name this and gap 1.
3. `artifact_page.py` ECharts path: (a) the line legend icon stays ECharts' default circle+line although lines draw no symbols (the Vega-Lite path sets `symbolType: stroke`); (b) default `grid.top` 40 assumes one legend row - with 5 series + a `yAxis.name` the legend wrapped at 390 px and overlapped the "USD" axis name and plot top; (c) no theme-aware direct (end) labels - ECharts `endLabel` color would need a per-mode text token the bridge does not expose, so recommend's `label: direct` cannot be met on the page. Workaround: `legend: {icon: "rect", itemWidth: 16, itemHeight: 2}`, unit moved to the card title.
4. great_tables styling: `tools/viz/style.py` has no great_tables helper and SKILL.md step 4 lists none, although `export.save` supports GT. demo.py maps `palette.surface/text/font` into `tab_options` by hand (GT defaults are 2px rules; set 1px hairlines and `table_body_border_top_width="0px"`, else it stacks under the label rule). `export.save(gt, ..., mode=...)` changes only the browser color scheme, not the table colors, so a dark table needs its own token mapping.
5. `tools/viz/recommend.py` on seattle_weather: (a) the small-multiples line form returns `color: weather` - a per-day attribute, not a series key; a line cannot be colored by it; (b) the dual-axis warning lists precipitation (1e1), temp_max (1e1), temp_min (1e1), wind (1e0) as differing in scale - three of four share 1e1, only wind differs; (c) `recommend.recommend()` returns no `job` / `job_inferred` key (only the CLI adds them), so API callers cannot see the inferred job without `infer_job()`.
6. `tools/viz/export.py` great_tables PNG: the crop is exactly the `<table>` box (`selector="table"`), zero margin - the last column header ends ~10 px from the right edge.
7. House line weight: 2px (`style.LINE_PX`) on 1461 daily points in a ~460 px plot read as a solid saturated block over the 7-day mean. SKILL.md has no dense-series rule below the 5000-row resampler threshold. demo.py sets the raw daily layer to `strokeWidth: 1`.
8. Vega-Lite daily dates: `timeUnit: "utcyearmonthdate"` (needed so ISO days are not shifted a day west of UTC; the skill documents only the monthly case) turns axis labels into "Jan 01, 2012"; `axis: {format: "%Y", tickCount: "year"}` fixes it. The skill could note the daily case + axis format.
9. SKILL.md step 9 says findings carry absolute artifact paths; for tracked/public research dirs that leaks user paths. This run uses repo-relative paths (orchestrator override). The skill should say "repo-relative when the findings file is tracked".
10. PRELUDE resolves on `Path.cwd()`: demo.py must run from the repo root; from elsewhere it falls back to `~/.claude/tools/viz` (reported stale by W201b until `install/sync_global.py --apply`).
11. Validator light-mode CVD line reports `tritan 5.8` (below the 6-8 floor band) under PASS; the status grades the protan worst only. Unclear whether intended.
12. Lint: the verbatim PRELUDE line needs `# noqa: I001` under this repo's ruff config (E401/E702/E402 are not enabled).

## LOOK - anti-pattern self-check (from the rendered PNGs)

Read with the Read tool: out/stocks-light.png, out/stocks-dark.png, out/weather-table.png, out/page-light.png, out/page-dark.png, out/page-phone-light.png (each re-read after the fixes).

Defects seen on first render and fixed before this report: chart A ticks "Jan 01, 2012" (-> years); chart A 2px daily line a solid block (-> 1px); chart B legend circle icons for markerless lines (-> stroke); chart B at 390 px: 2-row legend overlapping axis name and plot top (-> one row, no axis name); table 2px header rule stacked on the body top rule (-> 1px hairline).

| Entry (anti-patterns.md / skill step 8) | Result | Evidence |
|---|---|---|
| Dual-axis charts | PASS | one y axis everywhere; no dual-axis warning on the page |
| Recolor-on-filter / color follows entity | PASS (mapping) | SYMBOLS -> slot fixed once; same hue per symbol in static and page. Legend-toggle repaint not exercised |
| Cycling hues past 8 | PASS | 5 slots max |
| Eyeballing colorblind-safety | PASS | validator run both modes, exit 0 |
| Value ramp on nominal categories | PASS | no ramps used |
| Rainbow sequential / diverging midpoint | N/A | none used |
| Status color for a non-status series | PASS | no status tokens used |
| Eight hues when the story is one number | PASS | story is 5 trajectories |
| One-bar bar / 2-slice pie / pie for close values | N/A | none |
| More than ~7 color classes | PASS | 5 |
| Thick saturated blocks, heavy grid | PASS after fix | chart A daily line was a block at 2px |
| Dashed gridlines / axis rules | PASS | solid hairlines in all renders |
| Number on every data point | PASS | static: 4 end labels only; page: none |
| Border around marks | N/A | lines only |
| Label clipped / overflowing | PASS | end labels inside the PNG; table header ~10 px from the edge (gap 6), not clipped |
| Container height excludes x-axis band | PASS | x labels visible in all 3 page renders, no nested scroll seen |
| Text wears text tokens | PASS | end labels text-secondary, titles text-primary, both modes |
| Legend for >= 2 series, none for one | PASS after workaround | the fold-derived legend would have been removed (gap 1) |
| <= 4 direct labels (skill step 6) | PASS | AAPL, GOOG, IBM, MSFT labeled; AMZN (5th, slot 2, passes 3:1) legend only - its 2010 end (128.8) is 3 USD above IBM (125.6); IBM label dodged 5 pt down; slight residual ambiguity at the shared end, the legend resolves it |
| Tooltip as the only way to read a value | PASS | per-card table view incl. `temp_max_7d` (rows passed explicitly); static values read off the axis |
| Pinpoint hover targets | PASS | nearest-x crosshair (chart A), axis trigger (chart B) |
| Table view on interactive page | PASS | Table + Download CSV on both cards |
| Dark mode: own validated slots, rendered and read | PASS | dark slots validated; page-dark and stocks-dark read |
| Phone width: no horizontal scroll, no overlap | PASS after fix | page-phone-light.png: cards stack, legend one row |
| Display face / tabular-nums on hero, texture, filters, skeleton flash | N/A | not present |
