"""Memory review: flags, weekly timing, report only, collect never fails on it."""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from tools.fleet import fleet, lessons, memreview

NOW = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.UTC)
TEXT = "gateway broker session clock market ledger orders signal"


def memory(name: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: d\n---\n\n{body}\n"


class ReviewHome(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        env = mock.patch.dict(
            os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        )
        env.start()
        self.addCleanup(env.stop)
        self.cwd = self.home / "work"
        (self.cwd / "proj").mkdir(parents=True)
        cwds = mock.patch.object(
            memreview.launch,
            "slug_cwds",
            side_effect=lambda slug: [str(self.cwd / "proj")] if slug == "proj" else [],
        )
        cwds.start()
        self.addCleanup(cwds.stop)
        mask = mock.patch.object(lessons, "mask", side_effect=lambda text, _guard: text)
        mask.start()
        self.addCleanup(mask.stop)

    def project(
        self, slug: str, files: dict[str, str], index: str | None = None
    ) -> Path:
        folder = self.home / ".claude" / "projects" / slug / "memory"
        folder.mkdir(parents=True)
        for name, text in files.items():
            (folder / name).write_text(text, encoding="utf-8")
        if index is None:
            index = "\n".join(f"- [{n}]({n}) - hook" for n in files)
        (folder / "MEMORY.md").write_text(index, encoding="utf-8")
        return folder

    def details(self, kind: str) -> list[str]:
        return [f.detail for f in memreview.scan() if f.kind == kind]


class FlagTests(ReviewHome):
    def test_a_clean_project_has_no_flags(self):
        (self.cwd / "proj" / "src").mkdir()
        (self.cwd / "proj" / "src" / "a.py").write_text("", encoding="utf-8")
        self.project(
            "proj",
            {
                "a.md": memory("a", "See [[b]] and `src/a.py` and `src/`."),
                "b.md": memory("b", "Unrelated topic entirely, written differently."),
            },
        )
        self.assertEqual(memreview.scan(), [])

    def test_index_mismatch_both_ways(self):
        self.project(
            "proj", {"a.md": memory("a", "x")}, index="- [gone](gone.md) - hook"
        )
        details = self.details("index")
        self.assertIn("MEMORY.md points to a missing file: gone.md", details)
        self.assertIn("no MEMORY.md line for a.md", details)

    def test_broken_link_matches_name_or_file_stem(self):
        self.project(
            "proj",
            {
                "file-a.md": memory("a", "[[file-a]] [[a]] [[nothing-yet]]"),
            },
        )
        self.assertEqual(
            self.details("link"), ["file-a.md: [[nothing-yet]] matches no memory"]
        )

    def test_stale_reference_only_for_paths(self):
        (self.home / ".claude" / "kept.json").parent.mkdir(parents=True)
        (self.home / ".claude" / "kept.json").write_text("{}", encoding="utf-8")
        body = (
            "`tools/gone.py:12` `~/.claude/kept.json` `~/.claude/gone.json` "
            "`/low-priority` `\\\\` `owner/repo` `https://x.io/a.py` `<slug>/a.py` "
            "`py -3.13 tools/x.py` `old/dir/` `--flag`"
        )
        self.project("proj", {"a.md": memory("a", body)})
        self.assertEqual(
            self.details("stale"),
            [
                "a.md: `old/dir/` not found",
                "a.md: `tools/gone.py` not found",
                "a.md: `~/.claude/gone.json` not found",
            ],
        )

    def test_a_path_written_from_the_folder_holding_the_projects_is_found(self):
        (self.cwd / "other").mkdir()
        (self.cwd / "other" / "PLAN.md").write_text("", encoding="utf-8")
        self.project("proj", {"a.md": memory("a", "`other/PLAN.md` `other/GONE.md`")})
        self.assertEqual(self.details("stale"), ["a.md: `other/GONE.md` not found"])

    def test_near_duplicates_within_and_across_projects(self):
        self.project(
            "proj",
            {"a.md": memory("a", TEXT), "b.md": memory("b", TEXT + " thirteen")},
        )
        self.project("other", {"c.md": memory("c", TEXT)})
        details = self.details("duplicate")
        self.assertTrue(any(d.startswith("a.md and b.md:") for d in details))
        self.assertTrue(any("(across projects: lesson)" in d for d in details))

    def test_old_project_reported_once_and_relative_paths_skipped(self):
        self.project("gone", {"a.md": memory("a", "`src/a.py`")})
        flags = [f for f in memreview.scan() if f.kind == "old_project"]
        self.assertEqual(
            [(f.project, f.detail) for f in flags],
            [("gone", "project folder no longer exists")],
        )
        self.assertEqual(self.details("stale"), [])

    def test_archive_folder_is_ignored(self):
        folder = self.project("proj", {"a.md": memory("a", "x")})
        (folder / ".archive").mkdir()
        (folder / ".archive" / "old.md").write_text(memory("a", TEXT), encoding="utf-8")
        self.assertEqual(memreview.scan(), [])


class WeeklyTests(ReviewHome):
    def test_writes_when_due_and_leaves_memories_alone(self):
        folder = self.project("proj", {"a.md": memory("a", "`gone/a.py`")})
        before = {p.name: p.read_bytes() for p in folder.iterdir()}
        path = memreview.write_if_due(NOW)
        self.assertEqual(path.name, "2026-11-05.md")
        text = path.read_text(encoding="utf-8")
        self.assertIn("Flags: 1", text)
        self.assertIn("## Stale reference (1)", text)
        self.assertEqual({p.name: p.read_bytes() for p in folder.iterdir()}, before)

    def test_not_due_for_seven_days(self):
        self.project("proj", {"a.md": memory("a", "x")})
        memreview.write(NOW)
        self.assertIsNone(memreview.write_if_due(NOW + dt.timedelta(days=6)))
        self.assertIsNotNone(memreview.write_if_due(NOW + dt.timedelta(days=7)))

    def test_notice_counts_flags_and_stays_quiet_at_zero(self):
        self.project("proj", {"a.md": memory("a", "x")})
        memreview.write(NOW)
        self.assertIsNone(memreview.notice())
        (self.home / ".claude/projects/proj/memory/MEMORY.md").write_text(
            "", encoding="utf-8"
        )
        memreview.write(NOW + dt.timedelta(days=7))
        self.assertIn("memory review: 1 flags in", memreview.notice())

    def test_a_failed_write_is_recorded_and_the_next_success_clears_it(self):
        with mock.patch.object(memreview, "scan", side_effect=OSError("no")):
            fleet._weekly_memory_review()
        self.assertIn("last weekly write failed", memreview.notice())
        fleet._weekly_memory_review()
        self.assertFalse(memreview.err_path().exists())


if __name__ == "__main__":
    unittest.main()
