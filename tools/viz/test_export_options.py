#!/usr/bin/env python3
"""Call-form tests for export.save() / render_html() option objects (VAL-701).

Characterization (classes *CallForms): every call form of save() and
render_html() in harness/skills, CLAUDE.md, demo.py and the viz tests, positional
and keyword, pinned on HEAD 161e58b before the option objects existed. No
browser and no chart library: save() runs with a recording handler, and
render_html() with a fake Playwright browser that records every call.

Option objects (class OptionObjects): SaveOptions / RenderOptions are frozen
dataclasses with the documented defaults; each signature is <= 5 parameters
(tldr long_param_list) and keeps the first argument positional.

Run from the repo root:  py -3.13 tools/viz/test_export_options.py
"""

import dataclasses
import inspect
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.viz import export

FIG = object()


class _Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = tmp.name


class SaveCallForms(_Case):
    def save(self, *args, **kwargs):
        """(result, (target, stem, fmts, opts)) with a recording matplotlib handler."""
        calls = []

        def handler(target, stem, fmts, result, opts):
            calls.append((target, stem, fmts, dict(opts)))
            result["notes"].append("recorded")

        with (
            mock.patch.object(export, "_classify", return_value=("matplotlib", FIG)),
            mock.patch.dict(export._HANDLERS, {"matplotlib": handler}),
        ):
            result = export.save(*args, **kwargs)
        self.assertEqual(len(calls), 1)
        return result, calls[0]

    def stem(self, name):
        return os.path.join(self.out, name)

    def test_defaults(self):
        result, (target, stem, fmts, opts) = self.save(FIG, self.stem("x.png"))
        self.assertEqual(
            result, {"png": None, "svg": None, "html": None, "notes": ["recorded"]}
        )
        self.assertIs(target, FIG)
        self.assertEqual(stem, self.stem("x"))
        self.assertEqual(fmts, ("png", "svg", "html"))
        self.assertEqual(
            opts,
            {"mode": "light", "scale": 2, "bokeh_theme": None, "timeout_ms": 30000},
        )

    def test_formats_positional(self):  # CLAUDE.md save(fig, path, formats)
        _, (_, _, fmts, _) = self.save(FIG, self.stem("x"), ("svg", "png"))
        self.assertEqual(fmts, ("svg", "png"))

    def test_visualize_skill_and_demo_forms(self):
        _, (_, stem, fmts, opts) = self.save(
            FIG, pathlib.Path(self.out) / "s-dark", formats=("png", "svg"), mode="dark"
        )
        self.assertEqual(
            (stem, fmts, opts["mode"]), (self.stem("s-dark"), ("png", "svg"), "dark")
        )
        _, (_, _, _, opts) = self.save(FIG, self.stem("b"), bokeh_theme="T")
        self.assertEqual(opts["bokeh_theme"], "T")

    def test_all_keywords(self):
        _, (_, _, fmts, opts) = self.save(
            fig=FIG,
            path=self.stem("x"),
            formats=["html"],
            mode="dark",
            scale=3,
            bokeh_theme="T",
            timeout_ms=5,
        )
        self.assertEqual(fmts, ("html",))
        self.assertEqual(
            opts, {"mode": "dark", "scale": 3, "bokeh_theme": "T", "timeout_ms": 5}
        )

    def test_mode_is_keyword_only(self):
        with self.assertRaises(TypeError):
            self.save(FIG, self.stem("x"), ("png",), "dark")

    def test_bad_format_and_mode(self):
        with self.assertRaises(ValueError):
            self.save(FIG, self.stem("x"), formats=("gif",))
        with self.assertRaises(ValueError):
            self.save(FIG, self.stem("x"), mode="sepia")

    def test_unknown_keyword_names_it(self):
        with self.assertRaises(TypeError) as ctx:
            self.save(FIG, self.stem("x"), dpi=300)
        self.assertIn("dpi", str(ctx.exception))


class _FakeBrowser:
    """get_browser() stand-in: records new_context kwargs and page calls."""

    def __init__(self):
        self.context_kwargs = None
        self.page = mock.MagicMock(name="page")
        self.page.evaluate.return_value = None
        self.context = mock.MagicMock(name="context")
        self.context.new_page.return_value = self.page

    def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        return self.context


class RenderCallForms(_Case):
    def render(self, *args, **kwargs):
        browser = _FakeBrowser()
        with mock.patch.object(export, "get_browser", return_value=browser):
            path = export.render_html(*args, **kwargs)
        return path, browser

    def png(self, name="p.png"):
        return os.path.join(self.out, name)

    def test_defaults(self):
        path, b = self.render("<p>x</p>", self.png())
        self.assertEqual(path, os.path.abspath(self.png()))
        self.assertEqual(
            b.context_kwargs,
            {
                "viewport": {"width": 1200, "height": 800},
                "device_scale_factor": 1,
                "color_scheme": "light",
            },
        )
        b.page.set_content.assert_called_once_with(
            "<p>x</p>", wait_until="networkidle", timeout=30000
        )
        b.page.screenshot.assert_called_once_with(
            path=path, full_page=True, timeout=30000
        )
        b.page.emulate_media.assert_called_once_with(color_scheme="light")

    def test_positional_viewport_and_mode(self):
        _, b = self.render("<p>x</p>", self.png(), 300, 200, "dark")
        self.assertEqual(
            b.context_kwargs,
            {
                "viewport": {"width": 300, "height": 200},
                "device_scale_factor": 1,
                "color_scheme": "dark",
            },
        )

    def test_demo_and_test_forms(self):
        _, b = self.render(
            "<p>x</p>", pathlib.Path(self.png()), width=390, mode="light"
        )
        self.assertEqual(b.context_kwargs["viewport"], {"width": 390, "height": 800})
        _, b = self.render("<p>x</p>", self.png(), width=200, height=100)
        self.assertEqual(b.context_kwargs["viewport"], {"width": 200, "height": 100})

    def test_keyword_only_options(self):
        path, b = self.render(
            "<p>x</p>",
            self.png(),
            mode="dark",
            scale=2,
            timeout_ms=45000,
            selector="#c",
        )
        self.assertEqual(b.context_kwargs["device_scale_factor"], 2)
        b.page.set_content.assert_called_once_with(
            "<p>x</p>", wait_until="networkidle", timeout=45000
        )
        b.page.locator.assert_called_once_with("#c")
        b.page.locator.return_value.first.screenshot.assert_called_once_with(
            path=path, timeout=45000
        )
        b.page.screenshot.assert_not_called()

    def test_all_keywords(self):
        _, b = self.render(
            html_or_path="<p>x</p>",
            png_path=self.png(),
            width=10,
            height=20,
            mode="dark",
            scale=3,
            timeout_ms=7,
            selector=None,
        )
        self.assertEqual(
            b.context_kwargs,
            {
                "viewport": {"width": 10, "height": 20},
                "device_scale_factor": 3,
                "color_scheme": "dark",
            },
        )

    def test_scale_is_keyword_only(self):
        with self.assertRaises(TypeError):
            self.render("<p>x</p>", self.png(), 300, 200, "dark", 2)

    def test_bad_mode(self):
        with self.assertRaises(ValueError):
            self.render("<p>x</p>", self.png(), mode="sepia")

    def test_alias(self):
        self.assertIs(export.html_to_png, export.render_html)

    def test_unknown_keyword_names_it(self):
        with self.assertRaises(TypeError) as ctx:
            self.render("<p>x</p>", self.png(), full_page=False)
        self.assertIn("full_page", str(ctx.exception))


class OptionObjects(unittest.TestCase):
    CASES = (
        (
            "save",
            lambda: export.save,
            lambda: export.SaveOptions,
            {
                "formats": ("png", "svg", "html"),
                "mode": "light",
                "scale": 2,
                "bokeh_theme": None,
                "timeout_ms": 30000,
            },
        ),
        (
            "render_html",
            lambda: export.render_html,
            lambda: export.RenderOptions,
            {
                "width": 1200,
                "height": 800,
                "mode": "light",
                "scale": 1,
                "timeout_ms": 30000,
                "selector": None,
            },
        ),
    )

    def test_frozen_dataclass_with_documented_defaults(self):
        for name, _, cls, defaults in self.CASES:
            with self.subTest(name):
                opts_cls = cls()
                self.assertTrue(dataclasses.is_dataclass(opts_cls))
                fields = {f.name: f.default for f in dataclasses.fields(opts_cls)}
                self.assertEqual(fields, defaults)
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    opts_cls().__setattr__("mode", "dark")

    def test_short_signature_first_argument_positional(self):
        for name, fn, _, _ in self.CASES:
            with self.subTest(name):
                params = list(inspect.signature(fn()).parameters.values())
                self.assertLessEqual(len(params), 5)
                self.assertIs(params[0].kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)


if __name__ == "__main__":
    unittest.main()
