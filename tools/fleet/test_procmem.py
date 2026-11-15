"""Tests for tools/fleet/procmem.py: parsing the counter lines, ranking, describing."""

from __future__ import annotations

import unittest
from unittest import mock

from tools.fleet import procmem

GB = 1024**3
LINES = "\n".join(
    [
        f"_Total\t{9 * GB}\t{9 * GB}",
        f"Idle\t{GB}\t{GB}",
        f"brave\t{GB}\t{2 * GB}",
        f"brave#1\t{GB // 2}\t{GB}",
        f"claude\t{GB // 4}\t{GB}",
        f"GGnet\t{GB + GB // 4}\t{3 * GB}",
        "garbage line",
        "bad\tx\t1",
    ]
)


class ParseTests(unittest.TestCase):
    def test_instances_group_by_program_and_rank_by_ram(self):
        top = procmem.parse(LINES)
        self.assertEqual([e["name"] for e in top], ["brave", "GGnet", "claude"])
        self.assertEqual(
            top[0], {"name": "brave", "ram_gb": 1.5, "reserved_gb": 3.0, "count": 2}
        )

    def test_totals_idle_and_malformed_lines_are_dropped(self):
        names = {e["name"] for e in procmem.parse(LINES)}
        self.assertFalse(names & {"_Total", "Idle", "bad", "garbage line"})

    def test_n_limits_the_list(self):
        self.assertEqual(len(procmem.parse(LINES, n=1)), 1)


class TopProgramsTests(unittest.TestCase):
    def test_off_windows_is_unknown(self):
        with mock.patch.object(procmem.sys, "platform", "linux"):
            self.assertIsNone(procmem.top_programs(run=lambda _: LINES))

    def test_failed_or_empty_query_is_unknown(self):
        with mock.patch.object(procmem.sys, "platform", "win32"):
            self.assertIsNone(procmem.top_programs(run=lambda _: None))
            self.assertIsNone(procmem.top_programs(run=lambda _: ""))
            top = procmem.top_programs(n=2, run=lambda _: LINES)
        self.assertEqual([e["name"] for e in top or []], ["brave", "GGnet"])


class DescribeTests(unittest.TestCase):
    def test_names_ram_and_reserved_with_a_count_when_several(self):
        text = procmem.describe(procmem.parse(LINES), n=2)
        self.assertEqual(
            text, "brave x2 1.50 GB (3.00 reserved), GGnet 1.25 GB (3.00 reserved)"
        )

    def test_nothing_read_is_empty(self):
        self.assertEqual(procmem.describe(None), "")
        self.assertEqual(procmem.describe([]), "")


if __name__ == "__main__":
    unittest.main()
