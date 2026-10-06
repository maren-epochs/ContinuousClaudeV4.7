# dataviz-suite demo (VAL-302) - findings

End-to-end run of `/visualize` (harness/skills/visualize/SKILL.md). One script, `continuum/research/dataviz-suite/demo.py`, produces everything in `continuum/research/dataviz-suite/out/`. It can be re-run, takes ~9 s, and prints only absolute paths plus one `capabilities_for:` line. All paths below are repo-relative (this directory is tracked in a public repo).

Refresh (second run): regenerated after the tool fixes in c547c9e (recommend.py, VAL-402) and 307b602 (artifact_page.py + SKILL.md, VAL-401/403). Changes: chart A follows the new raw + smoothed house rule; chart B uses `end_labels=True` and the manual legend workaround is removed; the static chart's IBM label was moved off the AMZN/IBM end; the recommend JSON was re-run with the fixed tool; `capabilities_for(charts)` is printed. Gaps fixed by those commits are marked FIXED below.

Refresh (third run, VAL-506): regenerated after the m5 tool follow-ups (VAL-502 gray token, VAL-503 `end_labels` list form, VAL-504 `style.gt_style` + table margin in export, VAL-505 `ccv_viz` PRELUDE). Changes: demo.py opens with the current PRELUDE and imports `from ccv_viz import ...`; chart A draws the raw daily series as a plain 1px `line` whose color is `artifact_page.token("text-muted")` (the rule-segment / `lead` window workaround is gone); the table is styled by `style.gt_style(table, "light")` instead of hand-mapped `tab_options` / `tab_style` (the `great_tables.style` import is dropped, so no name clash remains); chart B passes `end_labels=["AAPL", "GOOG", "IBM", "MSFT"]` (the static chart's 4) instead of `True`.

## Datasets

| Dataset | Source | Rows | Columns | Used for |
|---|---|---|---|---|
| seattle_weather | `vega_datasets.data.seattle_weather()` (local, v0.9.0) | 1461 daily, 2012-01-01..2015-12-31 | date, precipitation, temp_max, temp_min, wind, weather (sun 714 / fog 411 / rain 259 / drizzle 54 / snow 23) | page chart A (temp_max + 7-day mean), great_tables monthly table |
| stocks | `vega_datasets.data.stocks()` (local) | 560 monthly, 2000-01..2010-03 | symbol (AAPL, AMZN, GOOG, IBM, MSFT), date, price | static matplotlib chart, page chart B |

Step 0 HAND-OFF: both are clean and <= 5000 rows, so the charts need no aggregation. The table aggregates by calendar month in pandas.

## Procedure followed (step by step, with commands)

All steps run from the repo root with `py -3.13`. The skill PRELUDE line is demo.py's first line, verbatim (cwd branch -> repo `tools/viz`, registered as `ccv_viz`; trailing `# noqa: I001`, see gap 13). Run: `py -3.13 continuum/research/dataviz-suite/demo.py` (exit 0, ~10 s).

| Step | What was done | Command / call |
|---|---|---|
| 0 HAND-OFF | datasets saved as CSV for the CLI | `df.to_csv(out/seattle-weather.csv)`, `out/stocks.csv` |
| 1 FORM | recommender on both CSVs (CLI) + Python API on chart A's slice | `py -3.13 tools/viz/recommend.py out/seattle-weather.csv --json`; same for `out/stocks.csv`; `recommend.recommend(recommend.profile_frame(weather[["date","temp_max"]]))` |
| 2 COLOR | stocks: `encoding.color` = `symbol` (a dimension) -> `palette.categorical(mode, 5)`. Each symbol gets one fixed slot (AAPL, AMZN, GOOG, IBM, MSFT = slots 1-5), used in BOTH the static chart and ECharts. Chart A: `single` -> slot 1 for the 7-day mean; the raw daily series is context and wears the muted text token (step 4 rule) | `palette.categorical(m, 5)` |
| 3 VALIDATE | exact hexes, per mode, adjacent pairs (lines) | `py -3.13 tools/viz/validate_palette.py "<5 hexes>" --mode light` / `--mode dark` |
| 4 MARKS | matplotlib `style.apply_matplotlib(mode)`. Chart A: raw `line` 1px in `token("text-muted")`, mean `line` 2px slot 1 (dense + raw/smoothed rules). great_tables: house style helper | `style.apply_matplotlib(m)`; `{"mark": {"type": "line", "strokeWidth": 1, "color": artifact_page.token("text-muted")}}`; `style.gt_style(GT(..., id="weather-table"), "light")` |
| 5 HOVER | chart A is a layered spec, so it gets its own nearest-x pointer param + rule + two-value tooltip (step 5: layered specs get no automatic crosshair). Chart B uses the artifact_page default axis tooltip | `artifact_page.write_page(charts, out/weather-and-stocks.html, title="Weather and Stocks", description=...)` |
| 6 ACCESSIBILITY | static chart: legend + 4 direct end labels. Chart B: legend + 4 themed end labels (AMZN legend only, as in the static chart). Chart A: one colored series, so no legend; the caption names both marks. Each card has a Table toggle + CSV. Dark mode rendered and read | `"end_labels": ["AAPL", "GOOG", "IBM", "MSFT"]` |
| 7a OUTPUT static | stocks multi-line, light + dark, PNG + SVG | `export.save(fig, out/stocks-{mode}, formats=("png","svg"), mode=mode)` |
| 7a OUTPUT table | GT -> PNG (export rasterizes the GT container via Playwright, so the `gt_style` 16 px margin band is in the PNG) | `export.save(gt, out/weather-table, formats=("png",), mode="light")` |
| 7b OUTPUT page | two-chart page; publisher capabilities; light/dark/phone renders | `artifact_page.capabilities_for(charts)` -> `{"downloads": true}`; `export.render_html(html, out/page-{light,dark}.png, mode=m)`; `export.render_html(html, out/page-phone-light.png, width=390, mode="light")` |
| 8 LOOK | Read every PNG (2 static + table + 3 page), then and after each fix | Read tool |
| 9 REPORT | this file | - |

Hover verified live (Playwright pointer move from a scratch script, nothing added to out/), third run:
- Chart A tooltip: `Date Apr 11, 2014 | Daily max (C) 17.2 | 7-day mean (C) 15.6` (light and dark).
- Chart A resolved strokes: raw line `stroke-width 1` = the page's `--text-muted` in each mode; mean line `stroke-width 2` = slot 1 of that mode.
- Chart B tooltip lists all 5 series (`2009-10-01 AAPL 188.5 | AMZN 118.81 | GOOG 536.12 | IBM 119.54 | MSFT 27.48`), AMZN included although it has no end label.
- `window.__chartsErrors` = `[]` in both modes; no page warnings (`end_labels` list accepted).
- Chart A Table toggle header: `date | temp_max | temp_max_7d`.

## Artifacts

| Path | Bytes | Note |
|---|---|---|
| continuum/research/dataviz-suite/demo.py | 12333 | generator |
| continuum/research/dataviz-suite/out/stocks-light.png | 87500 | (1) static, light, dpi 144 |
| continuum/research/dataviz-suite/out/stocks-light.svg | 30640 | (1) static, light |
| continuum/research/dataviz-suite/out/stocks-dark.png | 83256 | (1) static, dark |
| continuum/research/dataviz-suite/out/stocks-dark.svg | 30640 | (1) static, dark |
| continuum/research/dataviz-suite/out/weather-table.png | 82582 | (2) great_tables, light, scale 2, `gt_style` |
| continuum/research/dataviz-suite/out/weather-and-stocks.html | 226492 | (3) interactive page |
| continuum/research/dataviz-suite/out/page-light.png | 107485 | (3) 1200 px, light |
| continuum/research/dataviz-suite/out/page-dark.png | 106711 | (3) 1200 px, dark |
| continuum/research/dataviz-suite/out/page-phone-light.png | 81673 | (3) 390 px, light |
| continuum/research/dataviz-suite/out/validator.txt | 1615 | (4) validator output, both modes |
| continuum/research/dataviz-suite/out/recommend-seattle-weather.json | 1764 | (5) CLI |
| continuum/research/dataviz-suite/out/recommend-stocks.json | 854 | (5) CLI |
| continuum/research/dataviz-suite/out/recommend-seattle-temp-max.json | 258 | (5) API, chart A slice |
| continuum/research/dataviz-suite/out/seattle-weather.csv | 49300 | recommend input |
| continuum/research/dataviz-suite/out/stocks.csv | 12833 | recommend input |

Total out/ ~925 KB (the page HTML grew ~29 KB: chart A now inlines both columns per day instead of computing the mean in a window transform). The page loads only pinned CDNs: jsdelivr vega 6.4.0 / vega-lite 6.4.3 / vega-embed 7.3.0, cdnjs echarts 6.1.0. No absolute paths or user names inside out/ (grep clean).

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

WARN consequence (binding per skill step 3): light slots 3-5 (GOOG, IBM, MSFT) are below 3:1. The static chart therefore direct-labels exactly those three plus AAPL. Page chart B end-labels the same four (`end_labels` list), and every card keeps its table view.

## Recommender output and whether it was followed

| Input | job | form | encoding | Followed? |
|---|---|---|---|---|
| out/stocks.csv (CLI) | change-over-time (`job_inferred` true) | multi-line | x date, y price, color symbol, label direct | Yes. Static: multi-line with legend + 4 direct end labels. Page chart B: multi-line with legend + themed end labels on the same 4 (`end_labels=["AAPL", "GOOG", "IBM", "MSFT"]`) |
| out/seattle-weather.csv (CLI) | change-over-time (`job_inferred` true) | small multiples | x date, y value, facet measure (no color). Warnings: only wind (1e0) differs in scale from precipitation/temp_max/temp_min (1e1); `weather` is a per-row attribute, not a series | Not as a 4-panel chart: the deliverables fix chart A to temp_max only, plus a monthly table. The dual-axis refusal is honored (no measures mixed on one axis). The table uses `weather` as an aggregated per-month attribute (most common value), which the warning allows ("aggregate") |
| weather[date, temp_max] (API) | change-over-time (`job_inferred` true) | line | x date, y temp_max, color single | Yes: slot 1 on the colored series (the 7-day mean). The raw daily series is context in the muted token (skill step 4 raw + smoothed rule), so there is no second categorical slot and no legend |

## Deviations / gaps in the skill or tools

1. FIXED (307b602): `tools/viz/artifact_page.py` `_vl_legend` / `_vl_crosshair` forced `legend: null` and a one-row tooltip on fold-derived color fields. Fold series are now counted from the transform (legend, every-series tooltip, slots in fold order); calculate outputs keep the author's legend. Not used for chart A, because fold gives each folded series a categorical slot and the raw + smoothed rule needs the raw series in the muted token at 1px with the mean at 2px. A single fold line mark cannot do both; chart A uses two line layers instead (gap 2).
2. FIXED (token references in artifact_page specs; applied to chart A in VAL-506): a spec string `artifact_page.token("text-muted")` (= `"token:text-muted"`) is resolved by the page bridge to the current `--text-muted` on every render and theme change. Chart A's raw series is now a plain `line` layer (`strokeWidth: 1`, `color: token("text-muted")`) beside the mean `line` (`color.datum`, slot 1, 2px). The day-to-day `rule` segments and their `lead` window are removed. The spec still carries its own nearest-x pointer param, because it is layered (gap 3).
3. FIXED (307b602): SKILL.md step 5 now says the automatic crosshair applies to single-view specs only, and that layered/params specs need their own pointer params.
4. FIXED (307b602): the ECharts legend icon is a stroke for markerless lines, grid.top is sized to the measured legend (+ y-axis name), and `end_labels=True` adds themed end labels (`--text-secondary`, `moveOverlap: shiftY`). The manual legend workaround is removed from demo.py. FIXED (VAL-503) the remaining observation that `end_labels=True` labelled all 5 series, past the skill's "<= 4 also direct-labeled" guidance: `end_labels` now also takes a list of series names, and chart B passes `["AAPL", "GOOG", "IBM", "MSFT"]`, the static chart's 4. AMZN is legend + tooltip only, in both outputs.
5. FIXED (VAL-504): great_tables styling. `style.gt_style(gt, mode)` applies the house style (palette surface/ink, left heading, 1px axis-ink rules, grid-ink body hlines, secondary ink on labels/notes, tabular nums, 16 px margin band). demo.py's hand-mapped `tab_options` / `tab_style` block and its `from great_tables import style as gt_style` import are removed; the table gets a fixed id (`GT(..., id="weather-table")`) so the scoped CSS is stable across runs.
6. FIXED (c547c9e): `tools/viz/recommend.py`. `weather` is no longer a color (per-row attribute warning instead); the dual-axis warning names only wind as differing; `recommend()` returns `job` / `job_inferred`.
7. FIXED (VAL-504): `tools/viz/export.py` now rasterizes the GT container, so the `gt_style` margin (16 px) surrounds the table in the PNG; the last column header no longer runs to the edge.
8. FIXED (307b602): SKILL.md step 4 now has the dense-series rule (> ~500 points -> 1px) and the raw + smoothed rule (raw muted, smoothed in a slot). Both are applied to chart A.
9. OPEN: Vega-Lite daily dates. `timeUnit: "utcyearmonthdate"` is needed so days do not shift west of UTC, but it renders axis labels as "Jan 01, 2012"; `axis: {format: "%Y", tickCount: "year"}` fixes them. SKILL.md documents only the monthly case.
10. FIXED (307b602): SKILL.md step 9 / 7b now say repo-relative paths in findings and absolute paths in stdout/chat only.
11. OPEN: the PRELUDE resolves on `Path.cwd()`, so demo.py must run from the repo root. Elsewhere it falls back to `~/.claude/tools/viz`, which is only current after `install/sync_global.py --apply`. (VAL-505 changed the import name to `ccv_viz`, which stops a project's own `tools/` package from shadowing the viz modules; the cwd-first lookup is unchanged.)
12. OPEN: the light-mode validator CVD line reports `tritan 5.8` (below the 6-8 floor band) under PASS, because the status grades only the protan worst. Unclear whether intended.
13. OPEN: under this repo's ruff config the verbatim PRELUDE line still needs `# noqa: I001` (re-checked with the `ccv_viz` PRELUDE; `ruff check` passes with it).
14. OPEN (new): `export.save(fig, ..., formats=("svg",))` output is not reproducible: every run writes a new `dc:date` and new random `<path id>` values, so a re-run with no chart change still shows an SVG diff. matplotlib supports `metadata={"Date": None}` and `rcParams["svg.hashsalt"]` for stable output.

## LOOK - anti-pattern self-check (from the rendered PNGs)

Read with the Read tool after the third run: out/stocks-light.png, out/stocks-dark.png, out/weather-table.png, out/page-light.png, out/page-dark.png, out/page-phone-light.png. No layout fix was needed.

What changed in the third run, as seen in the renders:
- Chart A: gray 1px raw line under a blue 2px 7-day mean, in both modes and at 390 px; it looks as the rule-segment version did. The gray is `--text-muted` resolved per theme (the palette gives it the same value in light and dark; it reads lighter against the dark surface).
- Chart B: end labels GOOG / AAPL / IBM / MSFT only; AMZN has no label, so IBM's label sits alone at the AMZN/IBM end. Stroke legend keys, one legend row at 1200 and 390 px, unchanged.
- Table: `gt_style` output - left heading, secondary-ink subtitle / column labels / source note, axis-ink hairline under the labels and at the body bottom, grid-ink row hlines, and a surface margin band on all four sides.
- Static chart: unchanged from the second run (PNGs byte-identical; the SVGs differ only in matplotlib's `dc:date` metadata and random path ids, gap 14).

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
| Label clipped / overflowing | PASS | static and ECharts end labels inside their plots at 1200 and 390 px; table has a 16 px margin on every side |
| Label collision | PASS | static IBM label separated from the AMZN/IBM ends with a leader; ECharts labels only IBM at that end (AMZN unlabeled), no overlap |
| Container height excludes x-axis band | PASS | x labels visible in all 3 page renders, no nested scroll |
| Text wears text tokens | PASS | static labels text-secondary; ECharts end labels `--text-secondary`; titles text-primary; both modes |
| Legend for >= 2 series, none for one | PASS | stocks charts: legend; chart A: one colored series + a muted context series -> no legend, the caption names both ("Gray hairline: daily max. Blue line: 7-day rolling mean.") |
| <= 4 direct labels (skill step 6) | PASS | static and page chart B: AAPL, GOOG, IBM, MSFT (the light WARN slots + AAPL), AMZN legend only (gap 4) |
| Tooltip as the only way to read a value | PASS | per-card table view incl. `temp_max_7d` (rows passed explicitly); static values read off the axis |
| Pinpoint hover targets | PASS | nearest-x crosshair (chart A), axis trigger (chart B) |
| Table view on interactive page | PASS | Table + Download CSV on both cards; `capabilities_for` -> `{"downloads": true}` |
| Dark mode: own validated slots, rendered and read | PASS | dark slots validated; page-dark and stocks-dark read |
| Phone width: no horizontal scroll, no overlap | PASS | page-phone-light.png: cards stack, legend one row, end labels fit |
| Display face / tabular-nums on hero, texture, filters, skeleton flash | N/A | not present |
