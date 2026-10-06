"""End-to-end /visualize demo on vega_datasets (seattle_weather + stocks).

Run from the repo root: `py -3.13 continuum/research/dataviz-suite/demo.py`.
Follows harness/skills/visualize/SKILL.md step by step (FORM, COLOR, VALIDATE,
MARKS, HOVER, ACCESSIBILITY, OUTPUT); LOOK is done by a human/agent reading the
PNGs. Writes every artifact into ./out (overwritten on re-run) and prints only
absolute paths, one per line.
"""
import sys, pathlib; sys.path.insert(0, str(next(p for p in (pathlib.Path.cwd(), pathlib.Path.home()/'.claude') if (p/'tools'/'viz'/'palette.json').exists())))  # noqa: I001 - skill PRELUDE, verbatim

import json
import os
import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from great_tables import GT, loc
from great_tables import style as gt_style
from vega_datasets import data

from tools.viz import artifact_page, export, palette, recommend, style

VIZ = Path(palette.PALETTE_PATH).parent          # tools/viz the prelude resolved
OUT = Path(__file__).resolve().parent / "out"
SYMBOLS = ("AAPL", "AMZN", "GOOG", "IBM", "MSFT")  # entity -> slot, fixed once (alphabetical)
MODES = ("light", "dark")


def emit(*paths):
    for p in paths:
        if p:
            print(str(Path(p).resolve()))


def run_cli(args):
    """Run a tools/viz CLI with this interpreter; save stdout+stderr; return (exit, text)."""
    proc = subprocess.run([sys.executable, *args], capture_output=True, check=False,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    text = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    return proc.returncode, text


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    OUT.mkdir(parents=True, exist_ok=True)

    # 0 HAND-OFF: both datasets ship clean and small (1461 + 560 rows <= 5000): no aggregation.
    weather = data.seattle_weather()
    stocks = data.stocks()
    w_csv, s_csv = OUT / "seattle-weather.csv", OUT / "stocks.csv"
    weather.to_csv(w_csv, index=False, date_format="%Y-%m-%d")
    stocks.to_csv(s_csv, index=False, date_format="%Y-%m-%d")
    emit(w_csv, s_csv)

    # 1 FORM: recommend.py --json on each saved csv (CLI, as the skill shows).
    rec = {}
    for name, csv in (("seattle-weather", w_csv), ("stocks", s_csv)):
        code, text = run_cli([str(VIZ / "recommend.py"), str(csv), "--json"])
        if code != 0:
            raise SystemExit(f"recommend.py failed on {csv}: {text}")
        path = OUT / f"recommend-{name}.json"
        path.write_text(text, encoding="utf-8", newline="\n")
        rec[name] = json.loads(text)
        emit(path)
    # Chart A charts one measure of the weather frame: ask the Python API about that slice too.
    temp = weather[["date", "temp_max"]]
    rec["seattle-temp-max"] = recommend.recommend(recommend.profile_frame(temp))
    path = OUT / "recommend-seattle-temp-max.json"
    path.write_text(json.dumps(rec["seattle-temp-max"], indent=2) + "\n", encoding="utf-8",
                    newline="\n")
    emit(path)

    # 2 COLOR BY JOB: stocks -> encoding.color 'symbol' (a dimension) -> categorical(mode, 5);
    # chart A -> 'single' for one measure; the 7-day mean is a second series -> slots 1-2.
    assert rec["stocks"]["encoding"]["color"] == "symbol", rec["stocks"]["encoding"]
    slots = {m: palette.categorical(m, len(SYMBOLS)) for m in MODES}

    # 3 VALIDATE: the exact hexes, per mode (lines -> adjacent pairs, the default).
    report = []
    for m in MODES:
        hexes = ",".join(slots[m])
        cmd = [str(VIZ / "validate_palette.py"), hexes, "--mode", m]
        code, text = run_cli(cmd)
        report.append(f"$ py -3.13 tools/viz/validate_palette.py \"{hexes}\" --mode {m}\n"
                      f"{text.strip()}\nexit {code}\n")
        if code != 0:
            raise SystemExit(f"validator FAIL ({m}): stop and cut/reorder series\n{text}")
    path = OUT / "validator.txt"
    path.write_text("\n".join(report), encoding="utf-8", newline="\n")
    emit(path)

    # 4 MARKS + 6 ACCESSIBILITY + 7a OUTPUT: stocks multi-line, matplotlib, both modes.
    # Light-mode validator WARNs slots 3-5 (GOOG, IBM, MSFT) under 3:1 -> they must be
    # direct-labeled. Labels: AAPL, GOOG, IBM, MSFT (4 = the method's cap). AMZN (slot 2,
    # passes 3:1) is the 5th: legend only - its 2010 end (128.8) sits 3 USD above IBM's.
    labeled = ("AAPL", "GOOG", "IBM", "MSFT")
    for m in MODES:
        style.apply_matplotlib(m)
        color = dict(zip(SYMBOLS, slots[m]))
        fig, ax = plt.subplots(figsize=(8, 4.5))
        for sym in SYMBOLS:
            s = stocks[stocks["symbol"] == sym].sort_values("date")
            ax.plot(s["date"], s["price"], color=color[sym], label=sym)
            if sym in labeled:
                end = s.iloc[-1]
                dy = -5 if sym == "IBM" else 0          # IBM label dodges below AMZN's end
                ax.annotate(sym, xy=(end["date"], end["price"]), xytext=(6, dy),
                            textcoords="offset points", va="center",
                            color=palette.text(m)["secondary"], annotation_clip=False)
        ax.set_ylim(bottom=0)
        ax.set_ylabel("Price (USD)")
        ax.set_title("Monthly stock price, 2000-2010", loc="left")
        ax.legend(loc="upper left", ncol=1)
        res = export.save(fig, OUT / f"stocks-{m}", formats=("png", "svg"), mode=m)
        plt.close(fig)
        emit(res["png"], res["svg"])

    # Table: seattle weather by calendar month (2012-2015). great_tables has no style.py
    # helper (skill step 4 lists none), so the palette tokens are mapped by hand here.
    w = weather.assign(year=weather["date"].dt.year, month=weather["date"].dt.month)
    monthly_total = w.groupby(["year", "month"])["precipitation"].sum().groupby("month").mean()
    summary = pd.DataFrame({
        "month": pd.to_datetime(sorted(w["month"].unique()), format="%m").strftime("%b"),
        "temp_max": w.groupby("month")["temp_max"].mean().to_numpy(),
        "precip": monthly_total.to_numpy(),
        "weather": w.groupby("month")["weather"].agg(lambda s: s.value_counts().index[0])
                    .to_numpy(),
    })
    surf, text = palette.surface("light"), palette.text("light")
    table = (
        GT(summary, rowname_col="month")
        .tab_header(title="Seattle weather by month",
                    subtitle="2012-2015 averages; precipitation is the mean monthly total")
        .cols_label(temp_max="Mean daily max (C)", precip="Precipitation (mm)",
                    weather="Most common weather")
        .fmt_number(columns=["temp_max", "precip"], decimals=1)
        .tab_source_note("Source: vega_datasets seattle_weather")
        .tab_style(gt_style.text(color=text["secondary"]),
                   [loc.column_labels(), loc.subtitle(), loc.source_notes()])
        .tab_options(
            table_background_color=surf["surface"], table_font_color=text["primary"],
            table_font_names=palette.font()["family_stack"], heading_align="left",
            table_border_top_color=surf["surface"], table_border_bottom_color=surf["surface"],
            heading_border_bottom_color=surf["surface"],
            column_labels_border_top_color=surf["surface"],
            column_labels_border_bottom_color=surf["axis"],
            table_body_hlines_color=surf["grid"], table_body_border_bottom_color=surf["axis"],
            table_body_border_top_color=surf["axis"], stub_border_width="0px",
            # LOOK fix: GT default rules are 2px; hairlines per the house style.
            column_labels_border_bottom_width="1px", table_body_border_bottom_width="1px",
            table_body_border_top_width="0px",   # else it stacks under the label rule
        )
    )
    res = export.save(table, OUT / "weather-table", formats=("png",), mode="light")
    emit(res["png"])

    # 5 HOVER + 7b OUTPUT: two-chart Artifact page.
    # Chart A (Vega-Lite): daily temp_max + 7-day rolling mean via a window transform.
    # WORKAROUND (artifact_page gap): a single-view line whose series come from a fold
    # transform gets legend forced to null (the color field is not in the inline rows)
    # and a one-row tooltip, so the layered crosshair is authored here instead.
    temp_rows = temp.assign(temp_max_7d=temp["temp_max"].rolling(7, min_periods=1).mean().round(2),
                            date=temp["date"].dt.strftime("%Y-%m-%d")).to_dict("records")
    x = {"field": "date", "type": "temporal", "timeUnit": "utcyearmonthdate", "title": None,
         "axis": {"format": "%Y", "tickCount": "year"}}   # LOOK fix: was "Jan 01, 2012" ticks
    hover = {"filter": {"param": "hover", "empty": False}}
    vl = {
        "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
        "data": {"values": [{"date": r["date"], "temp_max": r["temp_max"]} for r in temp_rows]},
        "transform": [{"window": [{"op": "mean", "field": "temp_max", "as": "temp_max_7d"}],
                       "frame": [-6, 0], "sort": [{"field": "date"}]}],
        "encoding": {"x": x},
        "layer": [
            # LOOK fix: 1461 daily points in ~460px at the house 2px stroke read as a solid
            # block over the mean; the noisy raw series drops to a 1px hairline.
            {"mark": {"type": "line", "strokeWidth": 1},
             "encoding": {"y": {"field": "temp_max", "type": "quantitative",
                                "title": "Max temperature (C)"},
                          "color": {"datum": "Daily max",
                                    "legend": {"title": None, "symbolType": "stroke"}}}},
            {"mark": {"type": "line"},
             "encoding": {"y": {"field": "temp_max_7d", "type": "quantitative"},
                          "color": {"datum": "7-day mean"}}},
            {"transform": [hover], "mark": {"type": "point", "filled": True, "size": 64},
             "encoding": {"y": {"field": "temp_max", "type": "quantitative"},
                          "color": {"datum": "Daily max"}}},
            {"transform": [hover], "mark": {"type": "point", "filled": True, "size": 64},
             "encoding": {"y": {"field": "temp_max_7d", "type": "quantitative"},
                          "color": {"datum": "7-day mean"}}},
            {"mark": {"type": "rule", "strokeWidth": 1},
             "params": [{"name": "hover", "select": {
                 "type": "point", "encodings": ["x"], "nearest": True,
                 "on": "pointerover", "clear": "pointerout"}}],
             "encoding": {
                 "opacity": {"condition": {"value": 1, "param": "hover", "empty": False},
                             "value": 0},
                 "tooltip": [{"field": "date", "type": "temporal",
                              "timeUnit": "utcyearmonthdate", "title": "Date"},
                             {"field": "temp_max", "type": "quantitative",
                              "title": "Daily max (C)", "format": ".1f"},
                             {"field": "temp_max_7d", "type": "quantitative",
                              "title": "7-day mean (C)", "format": ".1f"}]}},
        ],
    }
    # Chart B (ECharts): stocks, 5 series in SYMBOLS order -> same slots as the static chart.
    wide = (stocks.pivot(index="date", columns="symbol", values="price")[list(SYMBOLS)]
            .sort_index())
    echarts = {
        "xAxis": {"type": "time"},
        "yAxis": {"type": "value", "min": 0},   # unit lives in the card title
        # LOOK fixes (artifact_page gaps): the default ECharts legend icon for a line is a
        # circle+line although no symbols are drawn -> thin rect (stroke); with the
        # default icons + a yAxis name the legend wrapped to 2 rows at 390px and hit the
        # plot (default grid top 40 = one row). Thin icons + no axis name fit one row.
        "legend": {"icon": "rect", "itemWidth": 16, "itemHeight": 2},
        "series": [{"name": sym, "type": "line", "showSymbol": False,
                    "data": [[d.strftime("%Y-%m-%d"), v] for d, v in wide[sym].dropna().items()]}
                   for sym in SYMBOLS],
    }
    stock_rows = [{"date": d.strftime("%Y-%m-%d"),
                   **{s: (None if pd.isna(r[s]) else float(r[s])) for s in SYMBOLS}}
                  for d, r in wide.iterrows()]
    charts = [
        {"kind": "vega-lite", "spec": vl, "title": "Seattle daily max temperature",
         "caption": "2012-2015, daily max with a 7-day rolling mean", "rows": temp_rows},
        {"kind": "echarts", "spec": echarts, "title": "Monthly stock price (USD)",
         "caption": "2000-2010, five symbols on one axis", "rows": stock_rows},
    ]
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


if __name__ == "__main__":
    main()
