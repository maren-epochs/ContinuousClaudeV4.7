"""Simulated global install + the /visualize PRELUDE from a shadowing project.

sync_global --apply --target <tmp>/home/.claude builds the install (conftest
`simulated_install`). A sibling project owns a regular `tools` package
(tools/__init__.py). The PRELUDE, extracted from the INSTALLED visualize
SKILL.md, runs there with HOME/USERPROFILE -> <tmp>/home and must load
ccv_viz.palette from the install while `import tools` still yields the
project's own package.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# One PRELUDE extractor for both suites (VAL-615: no copied helper).
from tools.viz.test_import import prelude_of

pytestmark = pytest.mark.integration

Runner = Callable[..., subprocess.CompletedProcess[str]]

PROBE = """
import json, sys
from ccv_viz import palette
import tools
print("PROBE" + json.dumps({
    "palette": str(palette.__file__),
    "tools_file": str(tools.__file__),
    "tools_mark": getattr(tools, "MARK", None),
    "slot1": palette.categorical("light", 1)[0],
}))
"""


def test_prelude_from_shadowing_project_uses_install(
    simulated_install: dict, python: str, run: Runner
) -> None:
    install: Path = simulated_install["install"]
    proj: Path = simulated_install["proj"]
    env: dict[str, str] = simulated_install["env"]

    skill = install / "skills" / "visualize" / "SKILL.md"
    assert skill.is_file(), f"install missing {skill}"
    assert (install / "tools" / "viz" / "palette.json").is_file()
    prelude = prelude_of(skill.read_text(encoding="utf-8"))
    assert "ccv_viz" in prelude

    (proj / "tools").mkdir(exist_ok=True)
    (proj / "tools" / "__init__.py").write_text(
        'MARK = "user-tools"\n', encoding="utf-8"
    )
    script = proj / "probe.py"
    script.write_text(prelude + "\n" + PROBE, encoding="utf-8")

    p = run([python, str(script)], cwd=proj, env=env)
    assert p.returncode == 0, f"probe exit {p.returncode}\n{p.stdout}\n{p.stderr}"
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("PROBE")), None)
    assert line, f"no PROBE line\n{p.stdout}\n{p.stderr}"
    out = json.loads(line[len("PROBE") :])

    viz = (install / "tools" / "viz").resolve()
    assert Path(out["palette"]).resolve().is_relative_to(viz), out["palette"]
    assert out["tools_mark"] == "user-tools", out
    assert (
        Path(out["tools_file"]).resolve() == (proj / "tools" / "__init__.py").resolve()
    )
    assert re.fullmatch(r"#[0-9A-Fa-f]{6}", out["slot1"]), out["slot1"]
