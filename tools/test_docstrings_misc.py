#!/usr/bin/env python3
"""Every function, method and class in the misc tool modules carries a docstring.

Pins the VAL-613 missing_docs fix for tools/privacy_guard.py,
tools/lock_requirements.py, the tools/viz support modules and the dataviz demo,
so a new undocumented definition fails here instead of growing `tldr debt`.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    "tools/privacy_guard.py",
    "tools/lock_requirements.py",
    "tools/viz/export.py",
    "tools/viz/palette.py",
    "tools/viz/style.py",
    "tools/viz/__init__.py",
    "continuum/research/dataviz-suite/demo.py",
)
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def undocumented(path: Path) -> list[str]:
    """`name:line` of each def/class in `path` whose body has no docstring."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        f"{node.name}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, _DEFS) and not ast.get_docstring(node)
    ]


class Docstrings(unittest.TestCase):
    def test_every_definition_documented(self) -> None:
        for rel in FILES:
            with self.subTest(file=rel):
                self.assertEqual(undocumented(ROOT / rel), [])


if __name__ == "__main__":
    unittest.main()
