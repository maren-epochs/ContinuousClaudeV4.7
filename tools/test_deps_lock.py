"""Consistency checks for pyproject.toml dependency groups and requirements.lock.

Stdlib only. Run directly (`py -3.13 tools/test_deps_lock.py`) or under pytest.

What is asserted:
- requirements.lock exists at the repo root (scripts/readiness.sh deps_pinned
  accepts that name) and every requirement line is an exact `name==version` pin.
- Every package in tools/requirements-viz.txt is pinned in the lock at the same
  version, and the pyproject `viz` group names exactly those packages.
- The pyproject `dev` group is pytest, coverage, mypy, ruff, pre-commit and each
  is pinned in the lock.
- Every pyproject requirement (runtime + all groups) is pinned in the lock at a
  version that satisfies its pyproject specifier.
- bokeh stays 3.9.2 with its reason, plotly-resampler is documented as a
  --no-deps install and is NOT a lock requirement (it declares plotly<7).
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "requirements.lock"
PYPROJECT = ROOT / "pyproject.toml"
VIZ_REQS = ROOT / "tools" / "requirements-viz.txt"

DEV_GROUP = {"pytest", "coverage", "mypy", "ruff", "pre-commit"}

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")
_PIN_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][0-9A-Za-z.+!-]*)\s*(?:;\s*(.+?))?\s*$"
)


def normalize(name: str) -> str:
    """PEP 503 name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def strip_comment(line: str) -> str:
    return line.split("#", 1)[0].strip()


def parse_requirement(text: str) -> tuple[str, str]:
    """Return (normalized name, specifier text without markers) of a PEP 508 string."""
    body = text.split(";", 1)[0].strip()
    m = _NAME_RE.match(body)
    if not m:
        raise ValueError(f"unparseable requirement: {text!r}")
    return normalize(m.group(1)), m.group(3).strip()


def read_lock() -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        line = strip_comment(raw)
        if not line:
            continue
        m = _PIN_RE.match(line)
        if not m:
            raise AssertionError(f"requirements.lock line is not an exact pin: {raw!r}")
        name = normalize(m.group(1))
        if name in pins:
            raise AssertionError(f"requirements.lock pins {name} twice")
        pins[name] = m.group(2)
    return pins


def read_viz_requirements() -> dict[str, str]:
    """Name -> pinned version (or '' when unpinned) from tools/requirements-viz.txt."""
    out: dict[str, str] = {}
    for raw in VIZ_REQS.read_text(encoding="utf-8").splitlines():
        line = strip_comment(raw)
        if not line:
            continue
        name, spec = parse_requirement(line)
        out[name] = spec[2:].strip() if spec.startswith("==") else ""
    return out


def read_pyproject() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _release(version: str) -> tuple[int, ...]:
    """Numeric release segment (PEP 440 public version, pre/post/dev ignored)."""
    head = re.match(r"^(\d+(?:\.\d+)*)", version)
    if not head:
        raise ValueError(f"no numeric release in {version!r}")
    parts = [int(p) for p in head.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def satisfies(version: str, spec: str) -> bool:
    """Minimal PEP 440 check for the operators this repo's pyproject uses."""
    for clause in filter(None, (c.strip() for c in spec.split(","))):
        m = re.match(r"^(==|>=|<=|<|>|!=)\s*(\S+)$", clause)
        if not m:
            raise ValueError(f"unsupported specifier clause {clause!r}")
        op, bound = m.groups()
        if op == "==":
            ok = version == bound
        elif op == "!=":
            ok = version != bound
        else:
            v, b = _release(version), _release(bound)
            ok = {">=": v >= b, "<=": v <= b, "<": v < b, ">": v > b}[op]
        if not ok:
            return False
    return True


class TestDepsLock(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not LOCK.is_file():
            raise AssertionError(f"missing {LOCK.name} at repo root")
        cls.lock = read_lock()
        cls.lock_text = LOCK.read_text(encoding="utf-8")
        cls.viz = read_viz_requirements()
        cls.pyproject = read_pyproject()

    def test_readiness_accepts_lock_name(self) -> None:
        readiness = (ROOT / "scripts" / "readiness.sh").read_text(encoding="utf-8")
        self.assertIn('"requirements.lock"', readiness)

    def test_lock_header_says_how_to_regenerate(self) -> None:
        header = [ln for ln in self.lock_text.splitlines() if ln.startswith("#")]
        self.assertTrue(
            any("tools/lock_requirements.py" in ln for ln in header),
            "lock header must name the regeneration command",
        )

    def test_viz_requirements_pinned_and_agree(self) -> None:
        self.assertTrue(self.viz, "tools/requirements-viz.txt parsed empty")
        for name, version in self.viz.items():
            with self.subTest(package=name):
                self.assertIn(
                    name, self.lock, f"{name} not pinned in requirements.lock"
                )
                if version:
                    self.assertEqual(self.lock[name], version, f"{name} version drift")

    def test_dev_group_pinned(self) -> None:
        for name in DEV_GROUP:
            with self.subTest(package=name):
                self.assertIn(name, self.lock)

    def test_pyproject_groups(self) -> None:
        project = self.pyproject["project"]
        groups = project.get("optional-dependencies", {})
        self.assertIn("viz", groups)
        self.assertIn("dev", groups)
        viz_names = {parse_requirement(r)[0] for r in groups["viz"]}
        self.assertEqual(
            viz_names, set(self.viz), "pyproject viz != requirements-viz.txt"
        )
        dev_names = {parse_requirement(r)[0] for r in groups["dev"]}
        self.assertEqual(dev_names, DEV_GROUP)
        self.assertIsInstance(project.get("dependencies"), list)

    def test_every_pyproject_requirement_locked_and_satisfied(self) -> None:
        project = self.pyproject["project"]
        reqs = list(project.get("dependencies", []))
        for group in project.get("optional-dependencies", {}).values():
            reqs.extend(group)
        for req in reqs:
            name, spec = parse_requirement(req)
            with self.subTest(requirement=req):
                self.assertIn(name, self.lock)
                self.assertTrue(
                    satisfies(self.lock[name], spec),
                    f"lock {name}=={self.lock[name]} violates pyproject {spec!r}",
                )

    def test_bokeh_pin_and_reason(self) -> None:
        self.assertEqual(self.lock.get("bokeh"), "3.9.2")
        self.assertRegex(self.lock_text, r"(?im)^#.*bokeh.*panel|^#.*panel.*bokeh")

    def test_plotly_resampler_documented_not_locked(self) -> None:
        self.assertNotIn("plotly-resampler", self.lock)
        self.assertRegex(
            self.lock_text, r"(?m)^#.*--no-deps plotly-resampler==0\.11\.1"
        )
        for dep in ("dash", "orjson", "tsdownsample", "plotly"):
            self.assertIn(dep, self.lock)

    def test_satisfies_helper(self) -> None:
        self.assertTrue(satisfies("3.9.2", ">=3.9,<3.10"))
        self.assertFalse(satisfies("3.10.0", ">=3.9,<3.10"))
        self.assertTrue(satisfies("1.9.0.post1", "<2"))
        self.assertTrue(satisfies("0.16.8", "==0.16.8"))
        self.assertTrue(satisfies("7.16.1", ""))


if __name__ == "__main__":
    unittest.main()
