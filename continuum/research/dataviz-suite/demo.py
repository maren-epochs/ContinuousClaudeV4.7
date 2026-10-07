import sys, pathlib, importlib.util as _u; _c = pathlib.Path.cwd(); _d = next((p/'tools'/'viz' for p in (_c, *_c.parents, pathlib.Path.home()/'.claude') if (p/'tools'/'viz'/'palette.json').exists()), None) or (_ for _ in ()).throw(ModuleNotFoundError('ccv_viz: no tools/viz/palette.json in cwd, its parents or ~/.claude - run py -3.13 install/sync_global.py --apply from the ccv47 repo')); _s = _u.spec_from_file_location('ccv_viz', _d/'__init__.py', submodule_search_locations=[str(_d)]); sys.modules['ccv_viz'] = _m = _u.module_from_spec(_s); _s.loader.exec_module(_m)  # noqa: I001 - skill PRELUDE, verbatim
# End-to-end /visualize demo on vega_datasets (seattle_weather + stocks).
#
# Run from the repo root: `py -3.13 continuum/research/dataviz-suite/demo.py`.
# Follows harness/skills/visualize/SKILL.md step by step (FORM, COLOR, VALIDATE,
# MARKS, HOVER, ACCESSIBILITY, OUTPUT); LOOK is done by a human/agent reading the
# PNGs. Writes every artifact into ./out (overwritten on re-run) and prints only
# absolute paths (one per line) plus one `capabilities_for:` line.
# Line 1 is the skill PRELUDE, verbatim: it registers tools/viz as `ccv_viz`.

import json
import os
import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from ccv_viz import artifact_page, export, palette, recommend, style
from great_tables import GT
from vega_datasets import data

VIZ = Path(palette.PALETTE_PATH).parent          # tools/viz the prelude resolved
OUT = Path(__file__).resolve().parent / "out"
SYMBOLS = ("AAPL", "AMZN", "GOOG", "IBM", "MSFT")  # entity -> slot, fixed once (alphabetical)
MODES = ("light", "dark")
LABELED = ("AAPL", "GOOG", "IBM", "MSFT")  # direct-labeled ends (see stock_charts)


def emit(*paths):
    """Print the absolute path of each non-empty argument, one per line."""
    for p in paths:
        if p:
            print(str(Path(p).resolve()))


def run_cli(args):
    """Run a tools/viz CLI with this interpreter; save stdout+stderr; return (exit, text)."""
    proc = subprocess.run([sys.executable, *args], capture_output=True, check=False,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    text = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    return proc.returncode, text


def write_text(path, text):
    """Write UTF-8 text with LF endings to path and print its absolute path."""
    path.write_text(text, encoding="utf-8", newline="\n")
    emit(path)


def hand_off():
    """Step 0: load both datasets and save them as csv; return (weather, stocks, csvs)."""
    # Both datasets ship clean and small (1461 + 560 rows <= 5000): no aggregation.
    weather = data.seattle_weather()
    stocks = data.stocks()
    w_csv, s_csv = OUT / "seattle-weather.csv", OUT / "stocks.csv"
    weather.to_csv(w_csv, index=False, date_format="%Y-%m-%d")
    stocks.to_csv(s_csv, index=False, date_format="%Y-%m-%d")
    emit(w_csv, s_csv)
    return weather, stocks, (("seattle-weather", w_csv), ("stocks", s_csv))


def form(csvs, temp):
    """Step 1: recommend.py --json per csv (CLI) plus the Python API on chart A's slice."""
    rec = {}
    for name, csv in csvs:
        code, text = run_cli([str(VIZ / "recommend.py"), str(csv), "--json"])
        if code != 0:
            raise SystemExit(f"recommend.py failed on {csv}: {text}")
        write_text(OUT / f"recommend-{name}.json", text)
        rec[name] = json.loads(text)
    # Chart A charts one measure of the weather frame: ask the Python API about that slice too.
    rec["seattle-temp-max"] = recommend.recommend(recommend.profile_frame(temp))
    write_text(OUT / "recommend-seattle-temp-max.json",
               json.dumps(rec["seattle-temp-max"], indent=2) + "\n")
    return rec


def validate(slots):
    """Step 3: run the validator on the exact hexes per mode; stop on any FAIL."""
    report = []
    for m in MODES:
        hexes = ",".join(slots[m])
        cmd = [str(VIZ / "validate_palette.py"), hexes, "--mode", m]
        code, text = run_cli(cmd)
        report.append(f"$ py -3.13 tools/viz/validate_palette.py \"{hexes}\" --mode {m}\n"
                      f"{text.strip()}\nexit {code}\n")
        if code != 0:
            raise SystemExit(f"validator FAIL ({m}): stop and cut/reorder series\n{text}")
    write_text(OUT / "validator.txt", "\n".join(report))


def label_end(ax, sym, series, m):
    """Direct label at a series' last point; IBM's is nudged down with a hairline leader."""
    end = series.iloc[-1]
    lead = {}
    if sym == "IBM":                        # clear of AMZN's end, leader to IBM
        dx, dy = 10, -18
        lead = {"arrowprops": {"arrowstyle": "-", "lw": 0.75, "shrinkA": 1,
                               "shrinkB": 2, "color": palette.surface(m)["axis"]}}
    else:
        dx, dy = 6, 0
    ax.annotate(sym, xy=(end["date"], end["price"]), xytext=(dx, dy),
                textcoords="offset points", va="center",
                color=palette.text(m)["secondary"], annotation_clip=False, **lead)


def stock_charts(stocks, slots):
    """Steps 4 + 6 + 7a: the stocks multi-line chart (matplotlib) in both modes."""
    # Light-mode validator WARNs slots 3-5 (GOOG, IBM, MSFT) under 3:1 -> they must be
    # direct-labeled. Labels: AAPL, GOOG, IBM, MSFT (4 = the method's cap). AMZN (slot 2,
    # passes 3:1) is the 5th: legend only - its 2010 end (128.8) sits 3 USD above IBM's,
    # so the IBM label is nudged well below both ends with a hairline leader to IBM's end.
    for m in MODES:
        style.apply_matplotlib(m)
        color = dict(zip(SYMBOLS, slots[m]))
        fig, ax = plt.subplots(figsize=(8, 4.5))
        for sym in SYMBOLS:
            s = stocks[stocks["symbol"] == sym].sort_values("date")
            ax.plot(s["date"], s["price"], color=color[sym], label=sym)
            if sym in LABELED:
                label_end(ax, sym, s, m)
        ax.set_ylim(bottom=0)
        ax.set_ylabel("Price (USD)")
        ax.set_title("Monthly stock price, 2000-2010", loc="left")
        ax.legend(loc="upper left", ncol=1)
        res = export.save(fig, OUT / f"stocks-{m}", formats=("png", "svg"), mode=m)
        plt.close(fig)
        emit(res["png"], res["svg"])


def weather_table(weather):
    """Table: seattle weather by calendar month (2012-2015), house style via style.gt_style."""
    w = weather.assign(year=weather["date"].dt.year, month=weather["date"].dt.month)
    monthly_total = w.groupby(["year", "month"])["precipitation"].sum().groupby("month").mean()
    summary = pd.DataFrame({
        "month": pd.to_datetime(sorted(w["month"].unique()), format="%m").strftime("%b"),
        "temp_max": w.groupby("month")["temp_max"].mean().to_numpy(),
        "precip": monthly_total.to_numpy(),
        "weather": w.groupby("month")["weather"].agg(lambda s: s.value_counts().index[0])
                    .to_numpy(),
    })
    table = (
        GT(summary, rowname_col="month", id="weather-table")   # fixed id: stable output
        .tab_header(title="Seattle weather by month",
                    subtitle="2012-2015 averages; precipitation is the mean monthly total")
        .cols_label(temp_max="Mean daily max (C)", precip="Precipitation (mm)",
                    weather="Most common weather")
        .fmt_number(columns=["temp_max", "precip"], decimals=1)
        .tab_source_note("Source: vega_datasets seattle_weather")
    )
    table = style.gt_style(table, "light")
    res = export.save(table, OUT / "weather-table", formats=("png",), mode="light")
    emit(res["png"])


def temperature_chart(temp):
    """Chart A (Vega-Lite): daily temp_max + 7-day rolling mean; the Artifact chart dict."""
    # House rule (skill step 4): raw + smoothed pair -> raw at 1px in the muted text token
    # (dense: 1461 points), only the smoothed mean in categorical slot 1 at 2px. The raw
    # line names the token (artifact_page.token), so it follows the light/dark toggle.
    # Layered spec -> no automatic crosshair (skill step 5): one nearest-x pointer param on
    # the rule, which carries the tooltip listing both values.
    temp_rows = temp.assign(temp_max_7d=temp["temp_max"].rolling(7, min_periods=1).mean().round(2),
                            date=temp["date"].dt.strftime("%Y-%m-%d")).to_dict("records")
    tu = "utcyearmonthdate"   # ISO days stay on their UTC date west of UTC
    mean = {"y": {"field": "temp_max_7d", "type": "quantitative"},
            "color": {"datum": "7-day mean", "legend": None}}
    vl = {
        "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
        "data": {"values": temp_rows},
        "encoding": {"x": {"field": "date", "type": "temporal", "timeUnit": tu, "title": None,
                           "axis": {"format": "%Y", "tickCount": "year"}}},
        "layer": [
            {"mark": {"type": "line", "strokeWidth": 1,
                      "color": artifact_page.token("text-muted")},
             "encoding": {"y": {"field": "temp_max", "type": "quantitative",
                                "title": "Max temperature (C)"}}},
            {"mark": {"type": "line", "strokeWidth": 2}, "encoding": mean},
            {"transform": [{"filter": {"param": "hover", "empty": False}}],
             "mark": {"type": "point", "filled": True, "size": 64}, "encoding": mean},
            {"mark": {"type": "rule", "strokeWidth": 1},
             "params": [{"name": "hover", "select": {
                 "type": "point", "encodings": ["x"], "nearest": True,
                 "on": "pointerover", "clear": "pointerout"}}],
             "encoding": {
                 "opacity": {"condition": {"value": 1, "param": "hover", "empty": False},
                             "value": 0},
                 "tooltip": [{"field": "date", "type": "temporal", "timeUnit": tu,
                              "title": "Date"},
                             {"field": "temp_max", "type": "quantitative",
                              "title": "Daily max (C)", "format": ".1f"},
                             {"field": "temp_max_7d", "type": "quantitative",
                              "title": "7-day mean (C)", "format": ".1f"}]}},
        ],
    }
    return {"kind": "vega-lite", "spec": vl, "title": "Seattle daily max temperature",
            "caption": "2012-2015. Gray hairline: daily max. Blue line: 7-day rolling mean.",
            "rows": temp_rows}


def stock_chart_spec(stocks):
    """Chart B (ECharts): stocks, 5 series in SYMBOLS order; the Artifact chart dict."""
    # Same slots as the static chart.
    wide = (stocks.pivot(index="date", columns="symbol", values="price")[list(SYMBOLS)]
            .sort_index())
    echarts = {
        "xAxis": {"type": "time"},
        "yAxis": {"type": "value", "min": 0},   # unit lives in the card title
        # legend icon + grid.top come from artifact_page (stroke key, legend-sized grid);
        # end_labels on the chart dict meets recommend's label: direct; the list keeps the
        # skill's <= 4 cap with the static chart's 4 (AMZN: legend only, see step 4 above).
        "series": [{"name": sym, "type": "line", "showSymbol": False,
                    "data": [[d.strftime("%Y-%m-%d"), v] for d, v in wide[sym].dropna().items()]}
                   for sym in SYMBOLS],
    }
    stock_rows = [{"date": d.strftime("%Y-%m-%d"),
                   **{s: (None if pd.isna(r[s]) else float(r[s])) for s in SYMBOLS}}
                  for d, r in wide.iterrows()]
    return {"kind": "echarts", "spec": echarts, "title": "Monthly stock price (USD)",
            "caption": "2000-2010, five symbols on one axis", "rows": stock_rows,
            "end_labels": list(LABELED)}


def artifact(charts):
    """Steps 5 + 7b + 8 inputs: the two-chart Artifact page and its light/dark/phone PNGs."""
    # Publisher input for the orchestrator's Artifact call (skill 7b).
    print("capabilities_for:", json.dumps(artifact_page.capabilities_for(charts)))
    html = artifact_page.write_page(
        charts, OUT / "weather-and-stocks.html", title="Weather and Stocks",
        description="Seattle daily maximum temperature (2012-2015) and five monthly stock "
                    "prices (2000-2010). Data: vega_datasets.")
    emit(html)
    # 8 LOOK inputs: light, dark, phone width (light).
    for m in MODES:
        emit(export.render_html(html, OUT / f"page-{m}.png", mode=m))
    emit(export.render_html(html, OUT / "page-phone-light.png", width=390, mode="light"))
    export.close_browser()


def main():
    """Run the /visualize steps on vega_datasets and write every artifact to ./out."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    OUT.mkdir(parents=True, exist_ok=True)
    weather, stocks, csvs = hand_off()
    temp = weather[["date", "temp_max"]]
    rec = form(csvs, temp)
    # 2 COLOR BY JOB: stocks -> encoding.color 'symbol' (a dimension) -> categorical(mode, 5);
    # chart A -> 'single' for one measure; the 7-day mean is a second series -> slots 1-2.
    assert rec["stocks"]["encoding"]["color"] == "symbol", rec["stocks"]["encoding"]
    slots = {m: palette.categorical(m, len(SYMBOLS)) for m in MODES}
    validate(slots)
    stock_charts(stocks, slots)
    weather_table(weather)
    artifact([temperature_chart(temp), stock_chart_spec(stocks)])


if __name__ == "__main__":
    main()
