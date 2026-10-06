#!/usr/bin/env python3
"""Tests for tools/viz/palette.{json,py} and the vendored tools/viz/validate_palette.py.

Run from the repo root:  py -3.13 tools/viz/test_palette.py
"""
import json
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.viz import palette, validate_palette

VIZ_DIR = os.path.join(REPO_ROOT, "tools", "viz")
PALETTE_JSON = os.path.join(VIZ_DIR, "palette.json")
VENDORED = os.path.join(VIZ_DIR, "validate_palette.py")
VENDOR_SOURCE = os.path.join(
    REPO_ROOT, "continuum", "autonomous", "dataviz-suite", "context",
    "dataviz-skill", "scripts", "validate_palette.py")

# matplotlib's default categorical cycle (tab10), in its native order.
TAB10 = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
         "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]

STATUS_KEYS = ("good", "warning", "serious", "critical")
MODES = ("light", "dark")


def raw_json():
    with open(PALETTE_JSON, encoding="utf-8") as fh:
        return json.load(fh)


def statuses(report):
    return {name: c["status"] for name, c in report["checks"].items()}


class ReferencePaletteValidates(unittest.TestCase):
    """The reference categorical palette passes the vendored validator in both modes."""

    def test_light_passes(self):
        rep = validate_palette.validate(palette.categorical("light"), mode="light")
        self.assertTrue(rep["ok"], statuses(rep))
        self.assertEqual(rep["checks"]["CVD separation"]["status"], "PASS")
        self.assertEqual(rep["checks"]["Normal-vision floor"]["status"], "PASS")
        self.assertNotIn("FAIL", statuses(rep).values())
        self.assertEqual(rep["surface"], palette.surface("light")["surface"])

    def test_dark_passes_on_dark_surface_from_json(self):
        dark_surface = raw_json()["modes"]["dark"]["surface"]
        rep = validate_palette.validate(palette.categorical("dark"), mode="dark", surface=dark_surface)
        self.assertTrue(rep["ok"], statuses(rep))
        self.assertEqual(rep["checks"]["CVD separation"]["status"], "PASS")
        self.assertEqual(rep["checks"]["Contrast vs surface"]["status"], "PASS")
        self.assertEqual(rep["surface"], dark_surface)

    def test_first_three_slots_pass_all_pairs_both_modes(self):
        # palette.md: the first three slots validate all-pairs in both modes.
        for mode in MODES:
            rep = validate_palette.validate(palette.categorical(mode, 3), mode=mode, pairs="all")
            self.assertTrue(rep["ok"], (mode, statuses(rep)))
            self.assertEqual(rep["checks"]["CVD separation"]["status"], "PASS", mode)

    def test_tab10_fails_cvd_separation(self):
        rep = validate_palette.validate(TAB10, mode="light")
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["checks"]["CVD separation"]["status"], "FAIL")
        self.assertEqual(rep["checks"]["Chroma floor"]["status"], "FAIL")

    def test_report_shape_and_ascii(self):
        rep = validate_palette.validate(palette.categorical("light"))
        self.assertIsInstance(rep["ok"], bool)
        self.assertEqual(rep["mode"], "light")
        for name, check in rep["checks"].items():
            self.assertIn(check["status"], ("PASS", "WARN", "FAIL"), name)
            self.assertIsInstance(check["detail"], str)
            self.assertTrue(check["detail"].isascii(), (name, check["detail"]))
        self.assertEqual(set(rep["checks"]), {
            "Lightness band", "Chroma floor", "CVD separation",
            "Normal-vision floor", "Contrast vs surface"})

    def test_validate_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            validate_palette.validate(["#2a78d6", "not-a-hex"])
        with self.assertRaises(ValueError):
            validate_palette.validate(["#2a78d6"], mode="sepia")
        with self.assertRaises(ValueError):
            validate_palette.validate([])

    def test_validate_accepts_comma_string(self):
        rep = validate_palette.validate(",".join(palette.categorical("light")))
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["palette"], palette.categorical("light"))


class RampsAreLightnessMonotonic(unittest.TestCase):

    def test_every_ramp_strictly_decreasing_in_oklab_L(self):
        data = raw_json()
        self.assertTrue(data["ramps"], "no ramps in palette.json")
        for hue, steps in data["ramps"].items():
            ordered = [steps[k] for k in sorted(steps, key=int)]
            ls = [validate_palette.oklch(c)[0] for c in ordered]
            for i in range(1, len(ls)):
                self.assertLess(ls[i], ls[i - 1], (hue, ordered[i - 1], ordered[i]))
            self.assertEqual(palette.sequential(hue), ordered)

    def test_ordinal_bounds_match_doc(self):
        # palette.md: light ordinal start no lighter than step 250 (2.06:1);
        # dark go no darker than step 600 (2.15:1).
        bounds = raw_json()["ordinal_bounds"]
        step250 = palette.sequential("blue", steps=[bounds["light"]["min_step"]])[0]
        step600 = palette.sequential("blue", steps=[bounds["dark"]["max_step"]])[0]
        self.assertAlmostEqual(validate_palette.contrast(step250, palette.surface("light")["surface"]), 2.06, places=2)
        self.assertAlmostEqual(validate_palette.contrast(step600, palette.surface("dark")["surface"]), 2.15, places=2)
        self.assertGreaterEqual(validate_palette.contrast(step250, palette.surface("light")["surface"]),
                                validate_palette.ORDINAL_LIGHT_FLOOR)

    def test_ordinal_subrange_validates_as_ramp(self):
        # A coarse ordinal pick (every third step) must read as one-hue ramp.
        picks = palette.sequential("blue", steps=[250, 400, 550, 700])
        rep = validate_palette.validate(picks, mode="light", ordinal=True)
        self.assertTrue(rep["ok"], statuses(rep))
        self.assertEqual(rep["checks"]["Single hue"]["status"], "PASS")


class JsonAndPythonStayInSync(unittest.TestCase):

    def test_categorical_matches_json_and_is_eight_slots(self):
        data = raw_json()
        for mode in MODES:
            self.assertEqual(palette.categorical(mode), data["modes"][mode]["categorical"])
            self.assertEqual(len(palette.categorical(mode)), 8)
            self.assertEqual(palette.categorical(mode, 6), data["modes"][mode]["categorical"][:6])
        self.assertEqual(len(data["categorical_hues"]), 8)
        self.assertEqual(data["categorical_hues"][0], "blue")

    def test_categorical_n_over_8_raises_fold_into_other(self):
        with self.assertRaises(ValueError) as cm:
            palette.categorical("light", 9)
        self.assertIn("Other", str(cm.exception))
        with self.assertRaises(ValueError):
            palette.categorical("light", 0)
        with self.assertRaises(ValueError):
            palette.categorical("sepia")

    def test_status_matches_json_and_is_mode_invariant(self):
        data = raw_json()
        for mode in MODES:
            self.assertEqual(palette.status(mode), data["modes"][mode]["status"])
            self.assertEqual(tuple(palette.status(mode)), STATUS_KEYS)
        self.assertEqual(palette.status("light"), palette.status("dark"))

    def test_surface_and_text_match_json(self):
        data = raw_json()
        for mode in MODES:
            m = data["modes"][mode]
            self.assertEqual(palette.surface(mode)["surface"], m["surface"])
            self.assertEqual(palette.surface(mode)["surface_alt"], m["surface_alt"])
            self.assertEqual(palette.surface(mode)["grid"], m["grid"])
            self.assertEqual(palette.surface(mode)["axis"], m["axis"])
            self.assertEqual(palette.text(mode), m["text"])
            for key in ("primary", "secondary", "muted"):
                self.assertIn(key, palette.text(mode))

    def test_diverging_matches_json(self):
        data = raw_json()
        for mode in MODES:
            low, mid, high = palette.diverging(mode)
            d = data["diverging"][mode]
            self.assertEqual((low, mid, high), (d["low"], d["mid"], d["high"]))
        self.assertEqual(data["diverging"]["low_hue"], "blue")
        self.assertEqual(data["diverging"]["high_hue"], "red")
        self.assertNotEqual(palette.diverging("light")[1], palette.diverging("dark")[1])

    def test_sequential_default_and_steps(self):
        data = raw_json()
        default_hue = data["sequential_default"]
        self.assertEqual(palette.sequential(), palette.sequential(default_hue))
        self.assertEqual(palette.sequential(steps=[100, "700"]),
                         [data["ramps"][default_hue]["100"], data["ramps"][default_hue]["700"]])
        with self.assertRaises(ValueError):
            palette.sequential("no-such-hue")
        with self.assertRaises(ValueError):
            palette.sequential(steps=[999])

    def test_texture_and_font_present(self):
        data = raw_json()
        self.assertEqual(data["texture"]["angles"], [45, 135])
        self.assertEqual(data["texture"]["kind"], "lines")
        self.assertEqual(data["font"]["family_stack"][0], "system-ui")
        self.assertEqual(palette.texture(), data["texture"])
        self.assertEqual(palette.font(), data["font"])

    def test_load_is_cached_and_copy_safe(self):
        a = palette.load()
        a["modes"]["light"]["categorical"][0] = "#000000"
        self.assertEqual(palette.categorical("light")[0], raw_json()["modes"]["light"]["categorical"][0])

    def test_css_tokens(self):
        for mode in MODES:
            css = palette.css_tokens(mode)
            lines = [ln for ln in css.splitlines() if ln.strip()]
            self.assertTrue(lines)
            for ln in lines:
                self.assertRegex(ln, r"^--[a-z0-9-]+: [^;]+;$")
            self.assertIn(f"--surface-1: {palette.surface(mode)['surface']};", lines)
            self.assertIn(f"--text-primary: {palette.text(mode)['primary']};", lines)
            self.assertIn(f"--series-1: {palette.categorical(mode)[0]};", lines)
            self.assertIn(f"--series-8: {palette.categorical(mode)[7]};", lines)
            self.assertIn(f"--status-critical: {palette.status(mode)['critical']};", lines)
            self.assertIn(f"--div-mid: {palette.diverging(mode)[1]};", lines)
            self.assertIn(f"--seq-100: {palette.sequential()[0]};", lines)
            self.assertTrue(css.isascii())

    def test_all_hex_values_are_well_formed(self):
        data = raw_json()
        for mode in MODES:
            m = data["modes"][mode]
            for c in m["categorical"] + list(m["status"].values()) + list(m["text"].values()):
                self.assertTrue(validate_palette.is_hex_color(c), c)
        for steps in data["ramps"].values():
            for c in steps.values():
                self.assertTrue(validate_palette.is_hex_color(c), c)


class VendoredValidator(unittest.TestCase):

    def test_header_then_verbatim_source(self):
        with open(VENDORED, "rb") as fh:
            vendored = fh.read()
        head = vendored.split(b"\n", 4)[:4]
        self.assertEqual(len(head), 4)
        self.assertTrue(all(ln.startswith(b"#") for ln in head), head)
        joined = b"\n".join(head).lower()
        self.assertIn(b"vendored", joined)
        self.assertIn(b"do not edit", joined)
        if not os.path.exists(VENDOR_SOURCE):
            self.skipTest("vendor source not present in this checkout")
        with open(VENDOR_SOURCE, "rb") as fh:
            source = fh.read()
        self.assertIn(source, vendored, "vendored block is not byte-identical to the source")

    def test_cli_still_works_and_import_is_silent(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        ref = ",".join(palette.categorical("light"))
        ok = subprocess.run([sys.executable, VENDORED, ref, "--mode", "light"],
                            capture_output=True, text=True, env=env, cwd=REPO_ROOT, check=False)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("ALL CHECKS PASS", ok.stdout)
        bad = subprocess.run([sys.executable, VENDORED, ",".join(TAB10), "--mode", "light"],
                             capture_output=True, text=True, env=env, cwd=REPO_ROOT, check=False)
        self.assertEqual(bad.returncode, 1)
        quiet = subprocess.run([sys.executable, "-c", "import tools.viz.validate_palette as v; print(callable(v.validate))"],
                               capture_output=True, text=True, env=env, cwd=REPO_ROOT, check=False)
        self.assertEqual(quiet.returncode, 0, quiet.stderr)
        self.assertEqual(quiet.stdout.strip(), "True")

    def test_main_wrapper_uses_vendored_tuple_validate(self):
        argv = sys.argv
        sys.argv = ["validate_palette.py", ",".join(palette.categorical("dark")), "--mode", "dark"]
        try:
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
                validate_palette.main()
            self.assertEqual(cm.exception.code, 0)
        finally:
            sys.argv = argv


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    unittest.main(verbosity=2)
