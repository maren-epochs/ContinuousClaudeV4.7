"""Tests for tools/fleet/closing.py (fleet close): the tab script and processes are mocked."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.fleet import closing
from tools.fleet.model import FleetState, Session


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class CloseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cwd = Path(self._tmp.name) / "acorn"
        self.handoffs = self.cwd / "thoughts" / "shared" / "handoffs" / "s1"
        self.handoffs.mkdir(parents=True)
        self.now = time.time()
        self.session = Session(
            pid=4242,
            project="acorn",
            cwd=str(self.cwd),
            alive=True,
            last_activity=_iso(self.now),
        )
        self.state = FleetState(sessions=[self.session])
        self.calls = []
        self.screen = "done.\n> \n"
        self.alive = [True, False]  # alive at the first check, gone after /exit

    def handoff(self, name="2026-11-19_handoff.yaml", age_s=60):
        p = self.handoffs / name
        p.write_text("goal: x\n", encoding="utf-8")
        t = self.now - age_s
        os.utime(p, (t, t))

    def run_tab(self, req):
        self.calls.append(req)
        if req["mode"] == "read":
            return {"ok": True, "screen": self.screen}
        return {"ok": True}

    def close(self, **kw):
        alive = iter(self.alive + [self.alive[-1]] * 50)
        with (
            mock.patch.object(
                closing.collect, "process_alive", side_effect=lambda *a: next(alive)
            ),
            mock.patch.object(closing, "_kill") as kill,
        ):
            out = closing.close(
                self.state,
                "acorn",
                run=self.run_tab,
                sleep=lambda s: None,
                env=kw.pop("env", {}),
                **kw,
            )
        return out, kill

    def test_types_exit_then_shell_exit_and_reports_a_clean_close(self):
        self.handoff()
        out, kill = self.close()
        self.assertIn("closed 'acorn': typed /exit", out)
        kill.assert_not_called()
        keys = self.calls[1]["steps"]
        self.assertEqual([s["keys"] for s in keys], ["/exit", "{ENTER}", "exit{ENTER}"])
        self.assertTrue(keys[1]["last"])

    def test_an_open_question_box_is_cancelled_first(self):
        self.handoff()
        self.screen = "Which task?\n> 1. A\nEnter to select · Esc to cancel\n"
        self.close()
        self.assertEqual(
            [s["keys"] for s in self.calls[1]["steps"]],
            ["{ESC}", "/exit", "{ENTER}", "exit{ENTER}"],
        )

    def test_refuses_without_a_handoff_since_its_last_activity(self):
        self.handoff(age_s=closing.HANDOFF_SLACK_S + 600)
        with self.assertRaisesRegex(closing.CloseError, "no handoff written"):
            self.close()
        self.assertEqual(self.calls, [])

    def test_auto_handoffs_do_not_count(self):
        self.handoff(name="auto-handoff-2026-11-19.md")
        with self.assertRaisesRegex(closing.CloseError, "no handoff written"):
            self.close()

    def test_refuses_its_own_session_even_with_force(self):
        with self.assertRaisesRegex(closing.CloseError, "this session"):
            self.close(force=True, env={"CLAUDE_PID": "4242"})

    def test_refuses_a_busy_session(self):
        self.handoff()
        self.screen = "* Working... (12s · esc to interrupt)\n"
        with self.assertRaisesRegex(closing.CloseError, "still working"):
            self.close()
        self.assertEqual(len(self.calls), 1)  # read only, nothing typed

    def test_dry_run_types_nothing(self):
        self.handoff()
        out, _ = self.close(dry_run=True)
        self.assertIn("would close 'acorn'", out)
        self.assertEqual([c["mode"] for c in self.calls], ["read"])

    def test_force_skips_the_handoff_check(self):
        out, _ = self.close(force=True)
        self.assertIn("--force", out)

    def test_falls_back_to_stopping_it_and_closing_the_tab(self):
        self.handoff()
        self.alive = [True] * 21 + [False]  # survives /exit, gone after the kill
        out, kill = self.close()
        kill.assert_called_once_with(4242)
        self.assertEqual(self.calls[-1]["mode"], "closetab")
        self.assertIn("process stopped; tab closed", out)

    def test_no_live_session_for_the_tab(self):
        self.session.alive = False
        with self.assertRaisesRegex(closing.CloseError, "no live session"):
            self.close()


if __name__ == "__main__":
    unittest.main()
