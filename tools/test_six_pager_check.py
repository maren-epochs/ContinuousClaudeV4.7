"""Tests for harness/skills/six-pager/scripts/check_memo.py.

Covers:
  (a) body/appendix split, word count and page estimate
  (b) bullets allowed under Goals/Tenets/Appendix, FAIL elsewhere
  (c) bullets without a final period are separate sentences (no false long sentence)
  (d) weasel words: up to 3 warn, more FAIL
  (e) number tracing: CSV commas vs thousands separators, K/M suffixes, currency,
      rounding within 0.5%, years and small counts skipped, placeholders ignored
  (f) CLI exit code 1 on FAIL, 0 otherwise

Run: py -3.13 -m pytest -q tools/test_six_pager_check.py
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools._testing import load_module

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "harness" / "skills" / "six-pager" / "scripts" / "check_memo.py"
cm = load_module("six_pager_check_memo", SCRIPT)

PROSE = "We cut median first response from 9.5 to 6.1 hours in four quarters. "


def memo(body: str, appendix: str = "## Appendix\n\nData.\n") -> str:
    return f"# Title\n\n{body}\n\n{appendix}"


class CheckMemoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        p = self.dir / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_body_split_and_pages(self) -> None:
        r = cm.check(
            self.write("m.md", memo(PROSE * 50, "## Appendix\n\n" + "word " * 5000)), []
        )
        self.assertTrue(r["appendix_present"])
        self.assertLess(r["body_words"], 1000)  # appendix words excluded
        self.assertEqual(r["pages_est"], round(r["body_words"] / 550, 1))

    def test_bullets_allowed_only_in_goals_tenets(self) -> None:
        ok = memo(
            "## Goals\n\n- Cut response to 4 hours by Q3\n\n## Tenets\n\n- Fix over deflect"
        )
        self.assertEqual(cm.check(self.write("a.md", ok), [])["stray_bullets"], [])
        bad = memo("## State of the business\n\n- volume up\n- costs up")
        r = cm.check(self.write("b.md", bad), [])
        self.assertEqual(len(r["stray_bullets"]), 2)
        self.assertTrue(any("bullet" in f for f in r["fail"]))

    def test_bullets_without_period_not_one_sentence(self) -> None:
        items = "\n".join(
            f"- Tenet number {i} says we prefer fixes over macros always"
            for i in range(6)
        )
        r = cm.check(self.write("t.md", memo("## Tenets\n\n" + items)), [])
        self.assertEqual(r["long_sentences"], 0)

    def test_weasel_threshold(self) -> None:
        few = cm.check(
            self.write("w1.md", memo("Volume grew significantly. " + PROSE)), []
        )
        self.assertFalse(any("weasel" in f for f in few["fail"]))
        many = memo(
            "It was very robust, seamless and significantly better for many teams. "
            + PROSE
        )
        self.assertTrue(
            any("weasel" in f for f in cm.check(self.write("w2.md", many), [])["fail"])
        )

    def test_number_tracing(self) -> None:
        src = self.write(
            "s.csv", "quarter,tickets,cost\nQ1,31700,1340000\nQ2,33900,1520000\n"
        )
        notes = self.write(
            "n.md", "Lease is £48,000/yr and the quote was $96K. Turned away 142."
        )
        text = memo(
            "Tickets rose from 31,700 to 33,900 and cost reached $1.52M. "
            "Rent is £48K and the list price $96,000; 142 were turned away. "
            "In 2027 we have 3 options. About 33,950 tickets is close enough. "
            "We invented 777 widgets. [TBD: 555 pages]"
        )
        r = cm.check(self.write("m.md", text), [src, notes])
        self.assertEqual(r["untraced_numbers"], ["777"])

    def test_cli_exit_codes(self) -> None:
        good = self.write("g.md", memo(PROSE * 40))
        bad = self.write("b.md", memo("## Options\n\n- a\n- b\n\n" + PROSE * 40))
        run = lambda p: subprocess.run(
            [sys.executable, str(SCRIPT), str(p)], capture_output=True, text=True
        )
        self.assertEqual(run(good).returncode, 0)
        self.assertEqual(run(bad).returncode, 1)


if __name__ == "__main__":
    unittest.main()
