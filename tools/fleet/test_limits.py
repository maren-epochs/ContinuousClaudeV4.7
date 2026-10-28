"""Tests for limits.py (sessions stopped on the usage limit) and fleet.py limits.
Run from the repo root:  py -3.13 -m pytest -q tools/fleet
Every test uses a temp projects dir; the real ~/.claude is never touched.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.fleet import limits
from tools.fleet.model import FleetState, Session

SID = "sess-test-limit-1"
CWD = "C:\\Users\\x\\Documents\\proj"
LIMIT = {
    "type": "assistant",
    "apiError": "usage_limit_reached",
    "apiErrorParams": {
        "rate_limit_info": {"resetsAt": 1792755000, "rateLimitType": "five_hour"}
    },
    "message": {"content": [{"type": "text", "text": "You've hit your session limit"}]},
}
USER = {"type": "user", "message": {"content": "resume"}}
REPLY = {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}
META = {"type": "system", "subtype": "notice"}


class Stopped(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.projects = Path(self._tmp.name)
        self.slug = self.projects / "C--Users-x-Documents-proj"
        self.slug.mkdir()
        self.state = FleetState(
            sessions=[
                Session(
                    session_id=SID, name="proj-0e", project="proj", cwd=CWD, alive=True
                )
            ]
        )

    def write(self, *entries):
        (self.slug / f"{SID}.jsonl").write_text(
            "\n".join(json.dumps(e) for e in entries), encoding="utf-8"
        )

    def test_limit_last_is_stopped(self):
        self.write(USER, REPLY, LIMIT, META)
        rows = limits.stopped(self.state, self.projects)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0].resets_at, rows[0].limit_type), (1792755000.0, "five_hour")
        )
        self.assertFalse(rows[0].ready(1792754999))
        self.assertTrue(rows[0].ready(1792755000))

    def test_resumed_after_limit_not_stopped(self):
        self.write(LIMIT, USER, REPLY)
        self.assertEqual(limits.stopped(self.state, self.projects), [])

    def test_dead_session_skipped(self):
        self.write(LIMIT)
        self.state.sessions[0].alive = False
        self.assertEqual(limits.stopped(self.state, self.projects), [])

    def test_quota_fallback_and_unknown_reset(self):
        entry = {k: v for k, v in LIMIT.items() if k != "apiErrorParams"}
        self.write({**entry, "quotaLimits": {"resetsAt": 5}})
        self.assertEqual(limits.stopped(self.state, self.projects)[0].resets_at, 5.0)
        self.write(entry)
        row = limits.stopped(self.state, self.projects)[0]
        self.assertIsNone(row.resets_at)
        self.assertTrue(row.ready(0))

    def test_report_text_and_json(self):
        self.write(LIMIT)
        rows = limits.stopped(self.state, self.projects)
        text = limits.report(rows, 1792754000)
        self.assertIn("proj-0e", text)
        self.assertIn("next reset:", text)
        self.assertIn("ready: send 'resume'", limits.report(rows, 1792756000))
        data = json.loads(limits.report(rows, 1792756000, as_json=True))
        self.assertTrue(data[0]["ready"])
        self.assertEqual(
            limits.report([], 0), "no live session is stopped on the usage limit"
        )


class Usage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(
            os.environ, {"HOME": self._tmp.name, "USERPROFILE": self._tmp.name}
        )
        env.start()
        self.addCleanup(env.stop)

    def test_no_reading(self):
        self.assertIn("no usage reading yet", limits.usage_report(0))
        self.assertEqual(json.loads(limits.usage_report(0, as_json=True)), {})

    def test_reading_shown_with_age(self):
        path = limits.usage_path()
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "five_hour_pct": 91.6,
                    "five_hour_resets_at": 1792755000,
                    "seven_day_pct": 40,
                    "seven_day_resets_at": None,
                    "at": 1792750000 * 1000,
                }
            ),
            encoding="utf-8",
        )
        text = limits.usage_report(1792750600)
        self.assertIn("5-hour: 92% used, resets 20", text)
        self.assertIn("7-day: 40% used, resets unknown", text)
        self.assertIn("reading 10 min old", text)


if __name__ == "__main__":
    unittest.main()
