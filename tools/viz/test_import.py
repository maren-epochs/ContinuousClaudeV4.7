#!/usr/bin/env python3
"""VAL-505: the /visualize PRELUDE survives a project that owns a `tools` package.

A regular package (tools/__init__.py) in the cwd beats the namespace
~/.claude/tools regardless of sys.path order, so `from tools.viz import ...`
cannot work there. The PRELUDE registers tools/viz under its own import name
(`ccv_viz`) and leaves `tools` alone. These tests build, in a
TemporaryDirectory:

  home/.claude    a simulated global install (install/sync_global.py --apply --target)
  proj/tools/     the user's own regular `tools` package

and assert, with USERPROFILE/HOME pointing at home/:

  (a) the PRELUDE (taken verbatim from the INSTALLED visualize SKILL.md, i.e.
      after sync_global rewrites) imports palette, style, artifact_page, export
      and recommend from the install, as a script and via `-c`; each __file__
      is under the install; `import tools` still yields the user's package;
      intra-package imports resolve through the alias (no `tools.viz` loaded)
  (b) from the repo cwd the same PRELUDE resolves to the repo's tools/viz
  (c) in-repo `from tools.viz import ...` keeps working
  (d) every documented CLI runs from the shadowing project against the install
  (e) skill snippets: no `tools.viz` import left; every python block / `-c`
      line that imports `ccv_viz` opens with the PRELUDE; analyze-data's
      chart snippet carries it

Run from the repo root:  py -3.13 tools/viz/test_import.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SYNC = REPO / "install" / "sync_global.py"
SKILLS = {name: REPO / "harness" / "skills" / name / "SKILL.md" for name in ("visualize", "analyze-data")}
ALIAS = "ccv_viz"
MODULES = ("palette", "style", "artifact_page", "export", "recommend")
TIMEOUT = 180

PYTHON_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)


def prelude_of(md: str) -> str:
    """First line of the first ```python block - the PRELUDE the skill quotes verbatim."""
    m = PYTHON_BLOCK.search(md)
    if not m:
        raise AssertionError("no ```python block in SKILL.md")
    return m.group(1).splitlines()[0]


def run(args, cwd, env, check=True):
    p = subprocess.run(args, cwd=str(cwd), env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=TIMEOUT, check=False)
    if check and p.returncode != 0:
        raise AssertionError(f"{args!r} in {cwd} -> exit {p.returncode}\n"
                             f"stdout:\n{p.stdout}\nstderr:\n{p.stderr}")
    return p


PROBE = """
import json, sys
from {alias} import {mods}
import tools
out = {{m: str(sys.modules["{alias}." + m].__file__) for m in {mods_t!r}}}
out["pkg"] = str(sys.modules["{alias}"].__file__)
out["tools"] = getattr(tools, "MARK", None)
out["tools_viz_loaded"] = sorted(k for k in sys.modules if k == "tools.viz" or k.startswith("tools.viz."))
out["style_palette_is_alias"] = style.palette is palette
out["page_palette_is_alias"] = artifact_page.palette is palette
out["slot1"] = palette.categorical("light", 1)[0]
print("PROBE" + json.dumps(out))
"""


class ShadowingProject(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory(prefix="ccv47-viz-import-")
        root = Path(cls._td.name)
        cls.home = root / "home"
        cls.install = cls.home / ".claude"
        cls.proj = root / "proj"
        (cls.proj / "tools").mkdir(parents=True)
        (cls.proj / "tools" / "__init__.py").write_text('MARK = "user-tools"\n', encoding="utf-8")
        cls.env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        cls.env.update(USERPROFILE=str(cls.home), HOME=str(cls.home), PYTHONIOENCODING="utf-8")
        run([sys.executable, str(SYNC), "--apply", "--target", str(cls.install)], REPO, cls.env)
        cls.viz = (cls.install / "tools" / "viz").resolve()
        cls.installed_skill = (cls.install / "skills" / "visualize" / "SKILL.md").read_text(encoding="utf-8")
        cls.prelude = prelude_of(cls.installed_skill)
        cls.slot1 = run([sys.executable, "-c", "from tools.viz import palette; print(palette.categorical('light', 1)[0])"],
                        REPO, cls.env).stdout.strip()

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def probe(self, cwd, how):
        code = self.prelude + "\n" + PROBE.format(alias=ALIAS, mods=", ".join(MODULES), mods_t=MODULES)
        if how == "script":
            script = Path(cwd) / "viz_probe.py"
            script.write_text(code, encoding="utf-8")
            p = run([sys.executable, script.name], cwd, self.env)
        else:
            p = run([sys.executable, "-c", code], cwd, self.env)
        line = next(ln for ln in p.stdout.splitlines() if ln.startswith("PROBE"))
        return json.loads(line[len("PROBE"):])

    def assert_from(self, out, viz_dir):
        for key in (*MODULES, "pkg"):
            with self.subTest(module=key):
                self.assertEqual(Path(out[key]).resolve().parent, viz_dir, f"{key} loaded from {out[key]}")
        self.assertEqual(out["tools_viz_loaded"], [], "the alias must not load tools.viz")
        self.assertTrue(out["style_palette_is_alias"], "style.palette must be the alias's palette")
        self.assertTrue(out["page_palette_is_alias"], "artifact_page.palette must be the alias's palette")

    def test_prelude_imports_install_from_shadowing_project_script(self):
        out = self.probe(self.proj, "script")
        self.assert_from(out, self.viz)
        self.assertEqual(out["tools"], "user-tools", "user's `import tools` must get THEIR package")
        self.assertEqual(out["slot1"], self.slot1, "the real palette, not a stub")

    def test_prelude_imports_install_from_shadowing_project_dash_c(self):
        out = self.probe(self.proj, "-c")
        self.assert_from(out, self.viz)
        self.assertEqual(out["tools"], "user-tools")

    def test_tools_viz_is_shadowed_there(self):
        """Precondition (the 2026-10-06 probe): `tools.viz` cannot be reached from this project,
        whatever sys.path order - the reason the PRELUDE uses its own import name."""
        code = (f"import sys; sys.path.insert(0, {str(self.install)!r}); from tools.viz import palette")
        p = run([sys.executable, "-c", code], self.proj, self.env, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("No module named 'tools.viz'", p.stderr)

    def test_prelude_from_repo_cwd_uses_repo(self):
        code = self.prelude + "\nimport sys\nfrom ccv_viz import palette, style\nprint(palette.__file__)\n"
        p = run([sys.executable, "-c", code], REPO, self.env)
        self.assertEqual(Path(p.stdout.strip()).resolve().parent, (REPO / "tools" / "viz").resolve())

    def test_in_repo_tools_viz_import(self):
        code = ("from tools.viz import palette, style, artifact_page, export, recommend\n"
                "assert style.palette is palette and artifact_page.palette is palette\n"
                "print(palette.__file__)\n")
        p = run([sys.executable, "-c", code], REPO, self.env)
        self.assertEqual(Path(p.stdout.strip()).resolve().parent, (REPO / "tools" / "viz").resolve())

    def test_clis_from_shadowing_project_against_install(self):
        viz, proj = self.viz, self.proj
        csv = proj / "agg.csv"
        csv.write_text("team,hours\na,3\nb,5\nc,2\n", encoding="utf-8")
        charts = proj / "charts.json"
        charts.write_text(json.dumps([{"kind": "vega-lite", "title": "Hours", "spec": {
            "data": {"values": [{"team": "a", "hours": 3}]}, "mark": "bar",
            "encoding": {"x": {"field": "team", "type": "nominal"},
                         "y": {"field": "hours", "type": "quantitative"}}}}]), encoding="utf-8")
        py = sys.executable
        p = run([py, str(viz / "palette.py")], proj, self.env)
        self.assertIn("--", p.stdout)
        p = run([py, str(viz / "style.py"), "dark"], proj, self.env)
        json.loads(p.stdout)
        p = run([py, str(viz / "recommend.py"), str(csv), "--json"], proj, self.env)
        self.assertIn("job", json.loads(p.stdout))
        out_html = proj / "out.html"
        run([py, str(viz / "artifact_page.py"), str(charts), str(out_html), "--title", "Team Hours"], proj, self.env)
        self.assertIn("<title>Team Hours</title>", out_html.read_text(encoding="utf-8"))
        p = run([py, str(viz / "export.py"), "--help"], proj, self.env)
        self.assertIn("render", p.stdout)
        hexes = run([py, "-c", self.prelude + "; from ccv_viz import palette; "
                     "print(','.join(palette.categorical('light', 3)))"], proj, self.env).stdout.strip()
        run([py, str(viz / "validate_palette.py"), hexes, "--mode", "light"], proj, self.env)

    def test_in_repo_clis(self):
        py = sys.executable
        run([py, "tools/viz/palette.py"], REPO, self.env)
        json.loads(run([py, "tools/viz/style.py", "light"], REPO, self.env).stdout)
        run([py, "tools/viz/artifact_page.py", "--help"], REPO, self.env)


class SkillSnippets(unittest.TestCase):
    def setUp(self):
        self.text = {n: p.read_text(encoding="utf-8") for n, p in SKILLS.items()}
        self.prelude = prelude_of(self.text["visualize"])

    def test_prelude_registers_alias(self):
        self.assertIn(f"'{ALIAS}'", self.prelude)
        self.assertNotIn("tools.viz", self.prelude)

    def test_no_tools_viz_import_left(self):
        for name, md in self.text.items():
            with self.subTest(skill=name):
                self.assertIsNone(re.search(r"(from|import) tools\.viz\b", md), name)

    def test_every_viz_snippet_opens_with_prelude(self):
        for name, md in self.text.items():
            for block in PYTHON_BLOCK.findall(md):
                if ALIAS not in block:
                    continue
                with self.subTest(skill=name, block=block[:60]):
                    lines = [ln.strip() for ln in block.splitlines()]
                    self.assertIn(self.prelude, lines, "python block imports ccv_viz without the PRELUDE")
                    first = lines.index(self.prelude)
                    use = next((i for i, ln in enumerate(lines) if ALIAS in ln and ln != self.prelude), None)
                    if use is not None:  # the PRELUDE block itself has no import after it
                        self.assertLess(first, use, "PRELUDE must precede the first ccv_viz import")
            for line in re.findall(r'py -3\.13 -c "([^"]*)"', md):
                if ALIAS in line:
                    with self.subTest(skill=name, c=line[:60]):
                        self.assertTrue(line.startswith(self.prelude), "-c line must open with the PRELUDE")

    def test_analyze_data_chart_snippet_uses_prelude(self):
        blocks = [b for b in PYTHON_BLOCK.findall(self.text["analyze-data"]) if "savefig" in b]
        self.assertTrue(blocks, "analyze-data step 5 chart snippet missing")
        for b in blocks:
            self.assertIn(self.prelude, [ln.strip() for ln in b.splitlines()])


if __name__ == "__main__":
    unittest.main()
