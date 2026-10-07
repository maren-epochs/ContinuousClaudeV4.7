#!/usr/bin/env python3
"""Characterization tests for tools/lock_requirements.py marker propagation and output.

Pinned before the VAL-613 refactor of _propagate: unconditional reachability,
marker unions along conditional paths, the rendered pin line and section order.
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lock_requirements as lr

WIN = 'sys_platform == "win32"'
LINUX = 'sys_platform == "linux"'


def graph(*specs: tuple[str, bool, list[tuple[str, str | None]]]) -> dict[str, lr.Node]:
    """Nodes keyed by name from (name, direct, edges) triples, all at version 1.0."""
    return {
        name: lr.Node(name=name, version="1.0", direct=direct, edges=list(edges))
        for name, direct, edges in specs
    }


class Propagate(unittest.TestCase):
    def test_unconditional_chain(self) -> None:
        nodes = graph(
            ("a", True, [("b", None)]), ("b", False, [("c", None)]), ("c", False, [])
        )
        lr._propagate(nodes)
        self.assertTrue(all(n.unconditional for n in nodes.values()))
        self.assertTrue(all(not n.markers for n in nodes.values()))

    def test_conditional_edge_marks_child_and_descendants(self) -> None:
        nodes = graph(
            ("a", True, [("b", WIN)]), ("b", False, [("c", None)]), ("c", False, [])
        )
        lr._propagate(nodes)
        self.assertFalse(nodes["b"].unconditional)
        self.assertEqual(nodes["b"].markers, {WIN})
        self.assertEqual(nodes["c"].markers, {WIN})

    def test_markers_union_across_paths(self) -> None:
        nodes = graph(
            ("a", True, [("b", WIN), ("c", LINUX)]),
            ("b", False, [("d", None)]),
            ("c", False, [("d", None)]),
            ("d", False, []),
        )
        lr._propagate(nodes)
        self.assertEqual(nodes["d"].markers, {WIN, LINUX})

    def test_unconditional_path_clears_markers(self) -> None:
        # d first gets WIN through b, then an unconditional path through c wins.
        nodes = graph(
            ("a", True, [("b", WIN)]),
            ("b", False, [("d", None)]),
            ("z", True, [("c", None)]),
            ("c", False, [("d", None)]),
            ("d", False, []),
        )
        lr._propagate(nodes)
        self.assertTrue(nodes["d"].unconditional)
        self.assertEqual(nodes["d"].markers, set())

    def test_cycle_terminates(self) -> None:
        nodes = graph(
            ("a", True, [("b", WIN)]),
            ("b", False, [("c", None)]),
            ("c", False, [("b", LINUX)]),
        )
        lr._propagate(nodes)
        self.assertEqual(nodes["b"].markers, {WIN, LINUX})
        self.assertEqual(nodes["c"].markers, {WIN, LINUX})


class Render(unittest.TestCase):
    def test_line_forms(self) -> None:
        n = lr.Node(name="x", version="2.0", unconditional=True, markers={WIN})
        self.assertEqual(lr._line(n), "x==2.0")
        n = lr.Node(name="x", version="2.0", markers={WIN})
        self.assertEqual(lr._line(n), f"x==2.0 ; {WIN}")
        n = lr.Node(name="x", version="2.0", markers={WIN, LINUX})
        self.assertEqual(lr._line(n), f"x==2.0 ; ({LINUX}) or ({WIN})")

    def test_sections_and_notes(self) -> None:
        nodes = graph(("bokeh", True, []), ("zeta", True, []), ("alpha", False, []))
        lr._propagate(nodes)
        body = lr.render(nodes).split("# --- direct: named in pyproject.toml ---\n")[1]
        lines = body.splitlines()
        self.assertEqual(lines[0], "# " + lr.NOTES["bokeh"][0])
        self.assertEqual(
            lines[2:],
            ["bokeh==1.0", "zeta==1.0", "", "# --- transitive ---", "alpha==1.0"],
        )

    def test_normalize(self) -> None:
        self.assertEqual(lr.normalize("Foo_Bar.baz--q"), "foo-bar-baz-q")


class Cli(unittest.TestCase):
    def run_main(self, *argv: str) -> tuple[int, str]:
        """main(argv) with collect stubbed to a two-node graph; (exit, stdout)."""
        nodes = graph(("a", True, []), ("b", False, []))
        lr._propagate(nodes)
        buf = io.StringIO()
        with (
            mock.patch.object(lr, "collect", return_value=nodes),
            mock.patch.object(lr, "roots_from_pyproject", return_value=["a"]),
            redirect_stdout(buf),
        ):
            code = lr.main(list(argv))
        return code, buf.getvalue()

    def test_write_then_check(self) -> None:
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "req.lock"
            code, text = self.run_main("--output", str(out))
            self.assertEqual((code, text), (0, f"wrote {out} (2 pins)\n"))
            self.assertEqual(
                self.run_main("--output", str(out), "--check"),
                (0, "req.lock is up to date\n"),
            )
            out.write_text("stale\n", encoding="utf-8")
            self.assertEqual(
                self.run_main("--output", str(out), "--check"),
                (1, "req.lock is stale; run: py -3.13 tools/lock_requirements.py\n"),
            )

    def test_check_missing_file_is_stale(self) -> None:
        with TemporaryDirectory() as tmp:
            code, _ = self.run_main("--output", str(Path(tmp) / "none.lock"), "--check")
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
