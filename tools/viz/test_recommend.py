"""Tests for tools/viz/recommend.py (VAL-103).

Covers:
  (a) every job in JOBS returns a named form from choosing-a-form.md
  (b) job inference from a column profile when job is None
  (c) refusals: dual-axis, pie past 5 slices, 2-slice pie, one-bar chart,
      categorical series past 8 (fold into Other / small multiples)
  (c2) color jobs: no value-ramp on nominal categories (one color, slot 1);
      sequential only where color carries magnitude or categories are ordinal
  (d) determinism: same profile -> same output
  (e) profile_frame(df) builds the plain-dict profile (needs pandas)
  (f) CLI: csv/parquet path, --job, --json, exit codes

Run: py -3.13 tools/viz/test_recommend.py
"""

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
MODULE = PROJECT / "tools" / "viz" / "recommend.py"

_spec = importlib.util.spec_from_file_location("viz_recommend", MODULE)
recommend_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recommend_mod)
recommend = recommend_mod.recommend
infer_job = recommend_mod.infer_job
JOBS = recommend_mod.JOBS
FORMS = recommend_mod.FORMS

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None


def col(name, kind, cardinality=None, **extra):
    c = {"name": name, "kind": kind, "cardinality": cardinality}
    c.update(extra)
    return c


def profile(columns, n_rows=100):
    measures = [c["name"] for c in columns if c["kind"] == "numeric"]
    dims = [c["name"] for c in columns if c["kind"] != "numeric"]
    return {"n_rows": n_rows, "columns": columns, "measures": measures, "dimensions": dims}


# --- canonical profiles -----------------------------------------------------
P_ONE_VALUE = profile([col("revenue", "numeric", 1)], n_rows=1)
P_KPI_ROW = profile([col("revenue", "numeric", 1), col("users", "numeric", 1),
                     col("churn", "numeric", 1)], n_rows=1)
P_CAT_MEASURE = profile([col("product", "categorical", 6), col("sales", "numeric", 100)])
P_GRID = profile([col("region", "categorical", 5), col("month", "categorical", 12),
                  col("sales", "numeric", 60)], n_rows=60)
P_TIME_ONE = profile([col("date", "temporal", 24, is_sorted_time=True),
                      col("sales", "numeric", 24, scale=3)], n_rows=24)
P_TIME_SERIES = profile([col("date", "temporal", 24, is_sorted_time=True),
                         col("region", "categorical", 4),
                         col("sales", "numeric", 96, scale=3)], n_rows=96)
P_TIME_TWO_SCALES = profile([col("date", "temporal", 24, is_sorted_time=True),
                             col("users", "numeric", 24, scale=4),
                             col("sessions", "numeric", 24, scale=6)], n_rows=24)
P_TIME_SAME_SCALE = profile([col("date", "temporal", 24, is_sorted_time=True),
                             col("users", "numeric", 24, scale=4),
                             col("signups", "numeric", 24, scale=4)], n_rows=24)
P_BEFORE_AFTER = profile([col("period", "temporal", 2), col("team", "categorical", 6),
                          col("score", "numeric", 12)], n_rows=12)
P_TWO_MEASURES = profile([col("height", "numeric", 100), col("weight", "numeric", 100)])
P_THREE_MEASURES = profile([col("height", "numeric", 100), col("weight", "numeric", 100),
                            col("age", "numeric", 60)])
P_FLOW = profile([col("source", "categorical", 4), col("target", "categorical", 5),
                  col("count", "numeric", 20)], n_rows=20)
P_GEO = profile([col("iso3", "geo", 50), col("population", "numeric", 50)], n_rows=50)
P_MANY_SERIES = profile([col("date", "temporal", 24, is_sorted_time=True),
                         col("product", "categorical", 12),
                         col("sales", "numeric", 288, scale=3)], n_rows=288)


class SchemaTests(unittest.TestCase):
    def test_result_shape(self):
        out = recommend(P_CAT_MEASURE, "magnitude")
        self.assertEqual(set(out), {"form", "reason", "encoding", "warnings"})
        self.assertIn(out["form"], FORMS)
        self.assertIsInstance(out["reason"], str)
        self.assertTrue(out["reason"].strip())
        self.assertIsInstance(out["encoding"], dict)
        self.assertIn("x", out["encoding"])
        self.assertIn("y", out["encoding"])
        self.assertIsInstance(out["warnings"], list)

    def test_jobs_enumerated(self):
        self.assertEqual(JOBS, ("magnitude", "identity", "polarity", "headline",
                                "change-over-time", "distribution", "relationship",
                                "part-to-whole", "ranking", "flow", "spatial"))

    def test_unknown_job_raises(self):
        with self.assertRaises(ValueError):
            recommend(P_CAT_MEASURE, "pie")

    def test_every_job_returns_named_form(self):
        for job in JOBS:
            for prof in (P_ONE_VALUE, P_CAT_MEASURE, P_GRID, P_TIME_ONE, P_TIME_SERIES,
                         P_TWO_MEASURES, P_FLOW, P_GEO, P_MANY_SERIES):
                out = recommend(prof, job)
                self.assertIn(out["form"], FORMS, msg=f"{job}: {out}")

    def test_deterministic(self):
        a = recommend(copy.deepcopy(P_TIME_SERIES), "identity")
        b = recommend(copy.deepcopy(P_TIME_SERIES), "identity")
        self.assertEqual(a, b)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_input_not_mutated(self):
        prof = copy.deepcopy(P_TIME_SERIES)
        recommend(prof, None)
        self.assertEqual(prof, P_TIME_SERIES)


class JobTests(unittest.TestCase):
    """One test per job in the choosing-a-form table."""

    def test_magnitude_bar(self):
        out = recommend(P_CAT_MEASURE, "magnitude")
        self.assertEqual(out["form"], "bar")
        self.assertEqual(out["encoding"]["x"], "product")
        self.assertEqual(out["encoding"]["y"], "sales")
        self.assertEqual(out["encoding"]["sort"], "-y")
        self.assertEqual(out["encoding"]["color"], "single")
        self.assertIn("magnitude", out["reason"])

    def test_magnitude_grid_heatmap(self):
        out = recommend(P_GRID, "magnitude")
        self.assertEqual(out["form"], "heatmap")
        self.assertEqual(out["encoding"]["x"], "month")
        self.assertEqual(out["encoding"]["y"], "region")
        self.assertEqual(out["encoding"]["color"], "sales")

    def test_identity_grouped_bar(self):
        out = recommend(P_GRID, "identity")
        self.assertEqual(out["form"], "grouped bar")
        self.assertEqual(out["encoding"]["color"], "region")
        self.assertEqual(out["warnings"], [])

    def test_identity_multi_line_over_time(self):
        out = recommend(P_TIME_SERIES, "identity")
        self.assertEqual(out["form"], "multi-line")
        self.assertEqual(out["encoding"]["x"], "date")
        self.assertEqual(out["encoding"]["color"], "region")

    def test_identity_single_series_is_emphasis_not_categorical(self):
        out = recommend(P_CAT_MEASURE, "identity")
        self.assertEqual(out["form"], "emphasis")

    def test_polarity_diverging_bar(self):
        out = recommend(P_CAT_MEASURE, "polarity")
        self.assertEqual(out["form"], "diverging bar")
        self.assertEqual(out["encoding"]["color"], "diverging")

    def test_polarity_line_vs_baseline(self):
        out = recommend(P_TIME_ONE, "polarity")
        self.assertEqual(out["form"], "line vs baseline")

    def test_polarity_diverging_stacked_bar(self):
        out = recommend(P_GRID, "polarity")
        self.assertEqual(out["form"], "diverging stacked bar")

    def test_headline_stat_tile(self):
        out = recommend(P_ONE_VALUE, "headline")
        self.assertEqual(out["form"], "stat tile")
        self.assertEqual(out["encoding"]["y"], "revenue")

    def test_headline_kpi_row(self):
        out = recommend(P_KPI_ROW, "headline")
        self.assertEqual(out["form"], "KPI row")

    def test_headline_with_trend_is_stat_tile_with_sparkline(self):
        out = recommend(P_TIME_ONE, "headline")
        self.assertEqual(out["form"], "stat tile")
        self.assertEqual(out["encoding"]["x"], "date")

    def test_change_over_time_line(self):
        out = recommend(P_TIME_ONE, "change-over-time")
        self.assertEqual(out["form"], "line")
        self.assertEqual(out["encoding"]["x"], "date")
        self.assertEqual(out["encoding"]["y"], "sales")

    def test_change_over_time_series(self):
        out = recommend(P_TIME_SERIES, "change-over-time")
        self.assertEqual(out["form"], "multi-line")
        self.assertEqual(out["encoding"]["color"], "region")

    def test_change_over_time_same_scale_shares_one_axis(self):
        out = recommend(P_TIME_SAME_SCALE, "change-over-time")
        self.assertEqual(out["form"], "multi-line")
        self.assertEqual(out["encoding"]["y"], ["users", "signups"])
        self.assertNotIn("dual-axis", " ".join(out["warnings"]).lower())

    def test_change_over_time_before_after_dumbbell(self):
        out = recommend(P_BEFORE_AFTER, "change-over-time")
        self.assertEqual(out["form"], "dumbbell")
        self.assertEqual(out["encoding"]["y"], "team")

    def test_change_over_time_without_time_column_falls_back(self):
        out = recommend(P_CAT_MEASURE, "change-over-time")
        self.assertEqual(out["form"], "bar")
        self.assertTrue(any("no temporal column" in w for w in out["warnings"]))

    def test_distribution_histogram(self):
        out = recommend(profile([col("latency", "numeric", 1000)], n_rows=1000), "distribution")
        self.assertEqual(out["form"], "histogram")
        self.assertEqual(out["encoding"]["x"], "latency")

    def test_distribution_by_category_dot_plot(self):
        out = recommend(P_CAT_MEASURE, "distribution")
        self.assertEqual(out["form"], "dot plot")
        self.assertEqual(out["encoding"]["y"], "product")

    def test_relationship_scatter(self):
        out = recommend(P_TWO_MEASURES, "relationship")
        self.assertEqual(out["form"], "scatter")
        self.assertEqual(out["encoding"]["x"], "height")
        self.assertEqual(out["encoding"]["y"], "weight")

    def test_relationship_bubble(self):
        out = recommend(P_THREE_MEASURES, "relationship")
        self.assertEqual(out["form"], "bubble")
        self.assertEqual(out["encoding"]["size"], "age")

    def test_relationship_scatter_color_caps_at_three(self):
        prof = profile([col("height", "numeric", 100), col("weight", "numeric", 100),
                        col("team", "categorical", 5)])
        out = recommend(prof, "relationship")
        self.assertEqual(out["form"], "small multiples")
        self.assertEqual(out["encoding"]["facet"], "team")
        self.assertTrue(any("all-pairs" in w for w in out["warnings"]))

    def test_relationship_needs_two_measures(self):
        out = recommend(P_CAT_MEASURE, "relationship")
        self.assertEqual(out["form"], "table")

    def test_part_to_whole_stacked_bar(self):
        prof = profile([col("segment", "categorical", 4), col("share", "numeric", 4)], n_rows=4)
        out = recommend(prof, "part-to-whole")
        self.assertEqual(out["form"], "stacked bar")
        self.assertEqual(out["encoding"]["color"], "segment")

    def test_part_to_whole_horizontal_for_many(self):
        prof = profile([col("segment", "categorical", 8), col("share", "numeric", 8)], n_rows=8)
        out = recommend(prof, "part-to-whole")
        self.assertEqual(out["form"], "bar")
        self.assertEqual(out["encoding"]["orientation"], "horizontal")

    def test_ranking_sorted_bar(self):
        out = recommend(P_CAT_MEASURE, "ranking")
        self.assertEqual(out["form"], "bar")
        self.assertEqual(out["encoding"]["sort"], "-y")
        self.assertEqual(out["encoding"]["orientation"], "horizontal")

    def test_ranking_before_after_dumbbell(self):
        out = recommend(P_BEFORE_AFTER, "ranking")
        self.assertEqual(out["form"], "dumbbell")

    def test_flow_sankey(self):
        out = recommend(P_FLOW, "flow")
        self.assertEqual(out["form"], "sankey")
        self.assertEqual(out["encoding"]["x"], "source")
        self.assertEqual(out["encoding"]["y"], "target")

    def test_flow_needs_two_dimensions(self):
        out = recommend(P_CAT_MEASURE, "flow")
        self.assertEqual(out["form"], "table")

    def test_spatial_choropleth(self):
        out = recommend(P_GEO, "spatial")
        self.assertEqual(out["form"], "choropleth")
        self.assertEqual(out["encoding"]["color"], "population")

    def test_spatial_without_geo_falls_back(self):
        out = recommend(P_CAT_MEASURE, "spatial")
        self.assertEqual(out["form"], "bar")
        self.assertTrue(any("no geo column" in w for w in out["warnings"]))


class InferenceTests(unittest.TestCase):
    def test_single_value_is_headline(self):
        self.assertEqual(infer_job(P_ONE_VALUE), "headline")
        self.assertEqual(recommend(P_ONE_VALUE)["form"], "stat tile")

    def test_kpi_row_is_headline(self):
        self.assertEqual(infer_job(P_KPI_ROW), "headline")

    def test_time_column_is_change_over_time(self):
        self.assertEqual(infer_job(P_TIME_ONE), "change-over-time")
        self.assertEqual(infer_job(P_TIME_SERIES), "change-over-time")

    def test_geo_column_is_spatial(self):
        self.assertEqual(infer_job(P_GEO), "spatial")

    def test_source_target_is_flow(self):
        self.assertEqual(infer_job(P_FLOW), "flow")

    def test_two_measures_no_dims_is_relationship(self):
        self.assertEqual(infer_job(P_TWO_MEASURES), "relationship")

    def test_one_measure_no_dims_is_distribution(self):
        self.assertEqual(infer_job(profile([col("latency", "numeric", 1000)], 1000)),
                         "distribution")

    def test_category_plus_measure_is_magnitude(self):
        self.assertEqual(infer_job(P_CAT_MEASURE), "magnitude")
        self.assertEqual(infer_job(P_GRID), "magnitude")

    def test_dimensions_only_is_magnitude_of_counts(self):
        prof = profile([col("status", "categorical", 4)], n_rows=40)
        self.assertEqual(infer_job(prof), "magnitude")
        out = recommend(prof)
        self.assertEqual(out["form"], "bar")
        self.assertEqual(out["encoding"]["y"], "count")

    def test_empty_profile_is_table(self):
        out = recommend({"n_rows": 0, "columns": [], "measures": [], "dimensions": []})
        self.assertEqual(out["form"], "table")


class RefusalTests(unittest.TestCase):
    """Each refusal maps to an anti-patterns.md entry, named in warnings."""

    def test_never_dual_axis(self):
        out = recommend(P_TIME_TWO_SCALES, "change-over-time")
        self.assertEqual(out["form"], "small multiples")
        self.assertEqual(out["encoding"]["facet"], "measure")
        self.assertTrue(any(w.startswith("Dual-axis charts") for w in out["warnings"]))
        self.assertNotIn("dual", out["form"])

    def test_unknown_scales_are_treated_as_different(self):
        prof = profile([col("date", "temporal", 24), col("users", "numeric", 24),
                        col("sessions", "numeric", 24)], n_rows=24)
        out = recommend(prof, "change-over-time")
        self.assertEqual(out["form"], "small multiples")
        self.assertTrue(any(w.startswith("Dual-axis charts") for w in out["warnings"]))

    def test_part_to_whole_past_five_slices_is_bar_not_pie(self):
        prof = profile([col("segment", "categorical", 6), col("share", "numeric", 6)], n_rows=6)
        out = recommend(prof, "part-to-whole")
        self.assertEqual(out["form"], "bar")
        self.assertTrue(any(w.startswith("A donut/pie for comparing close values")
                            for w in out["warnings"]))

    def test_part_to_whole_never_pie(self):
        for n in (2, 3, 5, 6, 9, 20):
            prof = profile([col("segment", "categorical", n), col("share", "numeric", n)], n_rows=n)
            out = recommend(prof, "part-to-whole")
            self.assertNotIn("pie", out["form"])
            self.assertNotIn("donut", out["form"])

    def test_two_slice_pie_is_a_meter(self):
        prof = profile([col("segment", "categorical", 2), col("share", "numeric", 2)], n_rows=2)
        out = recommend(prof, "part-to-whole")
        self.assertEqual(out["form"], "meter")
        self.assertTrue(any(w.startswith("A one-bar bar chart, or a 2-slice pie")
                            for w in out["warnings"]))

    def test_one_bar_chart_is_a_stat_tile(self):
        prof = profile([col("product", "categorical", 1), col("sales", "numeric", 1)], n_rows=1)
        out = recommend(prof, "magnitude")
        self.assertEqual(out["form"], "stat tile")
        self.assertTrue(any(w.startswith("A one-bar bar chart, or a 2-slice pie")
                            for w in out["warnings"]))

    def test_headline_single_value_is_not_a_chart(self):
        out = recommend(P_ONE_VALUE, "headline")
        self.assertEqual(out["form"], "stat tile")
        self.assertEqual(out["warnings"], [])

    def test_categorical_series_past_eight_folds(self):
        out = recommend(P_MANY_SERIES, "identity")
        self.assertEqual(out["form"], "small multiples")
        self.assertEqual(out["encoding"]["facet"], "product")
        joined = " ".join(out["warnings"])
        self.assertIn("Cycling / generating hues past 8", joined)
        self.assertIn("fold into Other / small multiples", joined)

    def test_categorical_series_past_eight_folds_over_time(self):
        out = recommend(P_MANY_SERIES, "change-over-time")
        self.assertEqual(out["form"], "small multiples")
        self.assertIn("Cycling / generating hues past 8", " ".join(out["warnings"]))

    def test_grouped_bar_past_eight_series_folds(self):
        prof = profile([col("region", "categorical", 10), col("product", "categorical", 9),
                        col("sales", "numeric", 90)], n_rows=90)
        out = recommend(prof, "identity")
        self.assertEqual(out["form"], "small multiples")
        self.assertEqual(out["encoding"]["facet"], "product")
        self.assertIn("fold into Other / small multiples", " ".join(out["warnings"]))

    def test_grouped_bar_series_is_the_lower_cardinality_dimension(self):
        prof = profile([col("region", "categorical", 5), col("product", "categorical", 9),
                        col("sales", "numeric", 45)], n_rows=45)
        out = recommend(prof, "identity")
        self.assertEqual(out["form"], "grouped bar")
        self.assertEqual(out["encoding"]["x"], "product")
        self.assertEqual(out["encoding"]["color"], "region")
        self.assertEqual(out["encoding"]["label"], "direct")

    def test_eight_series_is_the_ceiling_not_past_it(self):
        prof = profile([col("date", "temporal", 24), col("product", "categorical", 8),
                        col("sales", "numeric", 192, scale=3)], n_rows=192)
        out = recommend(prof, "identity")
        self.assertEqual(out["form"], "multi-line")
        self.assertNotIn("Cycling / generating hues past 8", " ".join(out["warnings"]))

    def test_more_than_seven_color_classes_is_a_table(self):
        prof = profile([col("region", "categorical", 5), col("tier", "categorical", 9),
                        col("sales", "numeric", 45)], n_rows=45)
        out = recommend(prof, "part-to-whole")
        self.assertEqual(out["form"], "table")
        self.assertIn("More than ~7 color classes carrying meaning", " ".join(out["warnings"]))


P_ORDINAL = profile([col("tier", "categorical", 4, ordered=True),
                     col("sales", "numeric", 100)])
P_COUNTS = profile([col("status", "categorical", 4)], n_rows=40)


class ColorJobTests(unittest.TestCase):
    """anti-patterns.md 'A value-ramp on nominal categories': a single-measure bar
    over nominal categories takes ONE color (slot 1); bar length carries magnitude.
    'sequential' only where the color channel itself carries magnitude (heatmap,
    choropleth - encoded as the measure name) or the categories are ordinal."""

    def test_nominal_magnitude_bar_is_single_color(self):
        out = recommend(P_CAT_MEASURE, "magnitude")
        self.assertEqual(out["form"], "bar")
        self.assertEqual(out["encoding"]["color"], "single")
        self.assertNotIn("sequential", out["reason"])

    def test_nominal_count_bar_is_single_color(self):
        for job in ("magnitude", "identity"):
            out = recommend(P_COUNTS, job)
            self.assertEqual(out["form"], "bar", job)
            self.assertEqual(out["encoding"]["color"], "single", job)

    def test_ordinal_magnitude_bar_takes_the_ramp_in_natural_order(self):
        out = recommend(P_ORDINAL, "magnitude")
        self.assertEqual(out["form"], "bar")
        self.assertEqual(out["encoding"]["color"], "sequential")
        # a value sort would scramble the ramp; ordinal bars keep category order
        self.assertNotIn("sort", out["encoding"])

    def test_heatmap_color_carries_the_measure(self):
        out = recommend(P_GRID, "magnitude")
        self.assertEqual(out["form"], "heatmap")
        self.assertEqual(out["encoding"]["color"], "sales")
        self.assertIn("sequential", out["reason"])

    def test_choropleth_color_carries_the_measure(self):
        out = recommend(P_GEO, "spatial")
        self.assertEqual(out["form"], "choropleth")
        self.assertEqual(out["encoding"]["color"], "population")

    def test_one_series_forms_are_single_color(self):
        hist = profile([col("latency", "numeric", 1000)], n_rows=1000)
        many = profile([col("segment", "categorical", 8), col("share", "numeric", 8)],
                       n_rows=8)
        cases = [(P_CAT_MEASURE, "ranking", "bar"),
                 (P_CAT_MEASURE, "distribution", "dot plot"),
                 (hist, "distribution", "histogram"),
                 (many, "part-to-whole", "bar"),
                 (P_TIME_ONE, "change-over-time", "line")]
        for prof, job, form in cases:
            out = recommend(prof, job)
            self.assertEqual(out["form"], form, job)
            self.assertEqual(out["encoding"]["color"], "single", f"{job}: {out}")

    def test_ranking_stays_single_even_for_ordinal(self):
        # ranking sorts by value, which would scramble an ordinal ramp
        out = recommend(P_ORDINAL, "ranking")
        self.assertEqual(out["encoding"]["color"], "single")
        self.assertEqual(out["encoding"]["sort"], "-y")

    def test_no_return_path_value_ramps_nominal_categories(self):
        nominal = (P_ONE_VALUE, P_KPI_ROW, P_CAT_MEASURE, P_GRID, P_TIME_ONE,
                   P_TIME_SERIES, P_TIME_TWO_SCALES, P_TIME_SAME_SCALE, P_BEFORE_AFTER,
                   P_TWO_MEASURES, P_THREE_MEASURES, P_FLOW, P_GEO, P_MANY_SERIES,
                   P_COUNTS, profile([col("latency", "numeric", 1000)], n_rows=1000),
                   {"n_rows": 0, "columns": [], "measures": [], "dimensions": []})
        for prof in nominal:
            for job in JOBS:
                out = recommend(prof, job)
                self.assertNotEqual(out["encoding"].get("color"), "sequential",
                                    f"{job}: {out}")

    def test_color_job_names_documented(self):
        doc = recommend_mod.__doc__
        self.assertIn("single", doc)
        self.assertIn("value-ramp on nominal categories", doc)


@unittest.skipIf(pd is None, "pandas not installed")
class ProfileFrameTests(unittest.TestCase):
    def test_profile_frame_kinds(self):
        df = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=6, freq="MS"),
            "region": ["n", "s", "n", "s", "n", "s"],
            "flag": [True, False, True, False, True, False],
            "sales": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
            "note": ["alpha one", "beta two", "gamma three", "delta four", "eps five", "zeta six"],
        })
        prof = recommend_mod.profile_frame(df)
        kinds = {c["name"]: c["kind"] for c in prof["columns"]}
        self.assertEqual(kinds["date"], "temporal")
        self.assertEqual(kinds["region"], "categorical")
        self.assertEqual(kinds["flag"], "boolean")
        self.assertEqual(kinds["sales"], "numeric")
        self.assertEqual(kinds["note"], "text")
        self.assertEqual(prof["n_rows"], 6)
        self.assertEqual(prof["measures"], ["sales"])
        self.assertEqual(prof["dimensions"], ["date", "region", "flag"])
        date = next(c for c in prof["columns"] if c["name"] == "date")
        self.assertTrue(date["is_sorted_time"])
        self.assertEqual(date["cardinality"], 6)
        sales = next(c for c in prof["columns"] if c["name"] == "sales")
        self.assertEqual(sales["scale"], 1)

    def test_profile_frame_geo_and_year(self):
        df = pd.DataFrame({"iso3": ["USA", "CAN"], "year": [2020, 2021], "pop": [1e6, 3e7]})
        prof = recommend_mod.profile_frame(df)
        kinds = {c["name"]: c["kind"] for c in prof["columns"]}
        self.assertEqual(kinds["iso3"], "geo")
        self.assertEqual(kinds["year"], "temporal")
        self.assertEqual(prof["measures"], ["pop"])

    def test_profile_frame_marks_ordered_categoricals(self):
        df = pd.DataFrame({
            "tier": pd.Categorical(["S", "M", "L", "XL"], categories=["S", "M", "L", "XL"],
                                   ordered=True),
            "team": ["a", "b", "c", "d"],
            "sales": [1.0, 2.0, 3.0, 4.0],
        })
        prof = recommend_mod.profile_frame(df)
        by = {c["name"]: c for c in prof["columns"]}
        self.assertTrue(by["tier"]["ordered"])
        self.assertNotIn("ordered", by["team"])
        out = recommend(df[["tier", "sales"]], "magnitude")
        self.assertEqual(out["encoding"]["color"], "sequential")
        out = recommend(df[["team", "sales"]], "magnitude")
        self.assertEqual(out["encoding"]["color"], "single")

    def test_recommend_accepts_frame(self):
        df = pd.DataFrame({"product": list("abcdef"), "sales": [1, 2, 3, 4, 5, 6]})
        out = recommend(df)
        self.assertEqual(out["form"], "bar")


@unittest.skipIf(pd is None, "pandas not installed")
class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.csv = self.dir / "sales.csv"
        pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=12, freq="MS").strftime("%Y-%m-%d"),
            "sales": range(12),
        }).to_csv(self.csv, index=False)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(MODULE), *args], check=False,
                              capture_output=True, text=True, cwd=str(PROJECT))

    def test_csv_text(self):
        r = self.run_cli(str(self.csv))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("form: line", r.stdout)
        self.assertIn("job: change-over-time", r.stdout)

    def test_csv_json(self):
        r = self.run_cli(str(self.csv), "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["form"], "line")
        self.assertEqual(out["job"], "change-over-time")
        self.assertEqual(out["encoding"]["x"], "date")
        self.assertIn("profile", out)

    def test_job_override(self):
        r = self.run_cli(str(self.csv), "--job", "distribution", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["form"], "histogram")

    def test_bad_job(self):
        r = self.run_cli(str(self.csv), "--job", "pie")
        self.assertEqual(r.returncode, 2)

    def test_missing_file(self):
        r = self.run_cli(str(self.dir / "nope.csv"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("nope.csv", r.stderr)

    def test_parquet(self):
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            self.skipTest("pyarrow not installed")
        pq = self.dir / "sales.parquet"
        pd.read_csv(self.csv).to_parquet(pq)
        r = self.run_cli(str(pq), "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["form"], "line")


if __name__ == "__main__":
    unittest.main()
