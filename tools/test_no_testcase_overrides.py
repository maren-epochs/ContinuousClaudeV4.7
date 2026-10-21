"""Guard: no test class may override a unittest.TestCase fail*/assert* attribute.

A helper named fail() on a TestCase subclass once made every assertion pass:
TestCase routes failed assertions through self.fail, so the override swallowed
them. This suite AST-scans every tracked test_*.py and names each class method
whose name is an existing TestCase attribute starting with 'fail' or 'assert'.
New helper names (assertSorted, ...) stay allowed. Scope is deliberately simple:
every ClassDef in a test_*.py counts, inheritance is not resolved.

Run: py -3.13 -m pytest -q tools/test_no_testcase_overrides.py
"""

import ast
import subprocess
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "node_modules"}

# Aliases removed in Python 3.12; overriding them still breaks suites on older
# interpreters and signals the same mistake, so keep them protected.
LEGACY_NAMES = {
    "failUnless",
    "failIf",
    "failUnlessEqual",
    "failIfEqual",
    "failUnlessAlmostEqual",
    "failIfAlmostEqual",
    "failUnlessRaises",
    "assert_",
    "assertEquals",
    "assertNotEquals",
    "assertAlmostEquals",
    "assertNotAlmostEquals",
    "assertRegexpMatches",
    "assertNotRegexpMatches",
    "assertRaisesRegexp",
    "assertDictContainsSubset",
}

PROTECTED = {
    name for name in dir(unittest.TestCase) if name.startswith(("fail", "assert"))
} | LEGACY_NAMES


def scan_source(source: str, label: str) -> list[str]:
    """Return 'label:line:Class.method' for each protected-name method defined."""
    hits = []
    for node in ast.walk(ast.parse(source, filename=label)):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if (
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name in PROTECTED
            ):
                hits.append(f"{label}:{item.lineno}:{node.name}.{item.name}")
    return hits


def _is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name.startswith("test_") and name.endswith(".py")


def test_files() -> list[Path]:
    """Tracked test_*.py files (git ls-files), else an rglob of the tree."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=PROJECT,
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8")
        rels = [r for r in out.split("\0") if r and _is_test_file(r)]
        return [PROJECT / r for r in rels if (PROJECT / r).is_file()]
    except (OSError, subprocess.CalledProcessError):
        return [
            p
            for p in PROJECT.rglob("test_*.py")
            if not SKIP_DIRS.intersection(p.relative_to(PROJECT).parts)
        ]


# Not collected as a test by pytest's function collector: unittest-style suite.
test_files.__test__ = False  # type: ignore[attr-defined]

OFFENDER = """
import unittest

class T(unittest.TestCase):
    def fail(self, msg=None):
        pass
"""

ALLOWED = """
import unittest

class T(unittest.TestCase):
    def assertSorted(self, xs):
        self.assertEqual(xs, sorted(xs))
"""


class ScannerSelfCheck(unittest.TestCase):
    def test_offender_detected(self):
        hits = scan_source(OFFENDER, "test_x.py")
        self.assertEqual(hits, ["test_x.py:5:T.fail"])

    def test_new_helper_allowed(self):
        self.assertEqual(scan_source(ALLOWED, "test_x.py"), [])

    def test_assert_override_and_async_detected(self):
        src = (
            "class A:\n"
            "    def assertEqual(self, a, b):\n"
            "        pass\n"
            "    async def assertTrue(self, x):\n"
            "        pass\n"
        )
        self.assertEqual(
            scan_source(src, "test_y.py"),
            ["test_y.py:2:A.assertEqual", "test_y.py:4:A.assertTrue"],
        )

    def test_legacy_alias_detected(self):
        src = "class B:\n    def failUnless(self, x):\n        pass\n"
        self.assertEqual(scan_source(src, "test_z.py"), ["test_z.py:2:B.failUnless"])


class NoTestCaseOverrides(unittest.TestCase):
    def test_finds_test_files(self):
        # A long-tracked suite: this file itself may be untracked before commit.
        names = {p.name for p in test_files()}
        self.assertIn("test_validate_report.py", names)

    def test_repo_has_no_overrides(self):
        hits: list[str] = []
        for path in test_files():
            rel = path.relative_to(PROJECT).as_posix()
            hits.extend(scan_source(path.read_text(encoding="utf-8"), rel))
        self.assertEqual(
            hits,
            [],
            "methods override unittest.TestCase fail*/assert* attributes "
            "(assertions silently break); rename them:\n" + "\n".join(hits),
        )


if __name__ == "__main__":
    unittest.main()
