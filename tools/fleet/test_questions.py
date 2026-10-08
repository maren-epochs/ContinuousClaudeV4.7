"""Tests for the fleet question queue (questions.py), fleet.py questions/answer and the
dashboard notice. Run from the repo root:  py -3.13 -m pytest -q tools/fleet
Every test uses a temp HOME/USERPROFILE; the real ~/.claude is never touched.
"""

import contextlib
import io
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

from tools.fleet import dashboard, fleet, model, questions
from tools.fleet.model import FleetState, Session


def _q(qid, text="Which date?", status="pending", created="2026-10-08T10:00:00Z"):
    return {
        "id": qid,
        "created_at": created,
        "header": "date",
        "question": text,
        "options": [
            {"label": "A. Monday (Recommended)", "description": "why"},
            {"label": "B. Friday", "description": ""},
        ],
        "multi_select": False,
        "status": status,
        "answer": "A" if status == "answered" else None,
        "answered_at": None,
        "answered_via": "session" if status == "answered" else None,
    }


class QueueHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)})
        env.start()
        self.addCleanup(env.stop)
        self.qdir = questions.questions_dir()
        self.qdir.mkdir(parents=True)
        self.write(
            "sessA",
            "alpha",
            [
                _q("q-aaaaaaa1", created="2026-10-08T10:00:00Z"),
                _q("q-aaaaaaa2", status="answered"),
            ],
        )
        self.write(
            "sessB",
            "beta",
            [_q("q-bbbbbbb1", "Ship now?", created="2026-10-08T09:00:00Z")],
        )
        model.save_state(
            FleetState(
                sessions=[Session(session_id="sessA", name="alpha-1", alive=True)]
            )
        )

    def write(self, sid, project, qs):
        data = {
            "schema_version": 1,
            "session_id": sid,
            "cwd": f"C:/work/{project}",
            "project": project,
            "questions": qs,
        }
        (self.qdir / f"{sid}.json").write_text(json.dumps(data), encoding="utf-8")

    def stored(self, sid):
        return json.loads((self.qdir / f"{sid}.json").read_text(encoding="utf-8"))

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = fleet.main(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()


class LoadAnswerTests(QueueHome):
    def test_load_pending_oldest_first_across_sessions(self):
        ids = [q.id for q in questions.load()]
        self.assertEqual(ids, ["q-bbbbbbb1", "q-aaaaaaa1"])

    def test_load_all_and_by_session(self):
        self.assertEqual(len(questions.load(include_answered=True)), 3)
        self.assertEqual(
            [q.id for q in questions.load(session_id="sessB")], ["q-bbbbbbb1"]
        )

    def test_corrupt_queue_file_skipped(self):
        (self.qdir / "bad.json").write_text("{", encoding="utf-8")
        self.assertEqual(len(questions.load()), 2)

    def test_answer_marks_answered_once(self):
        q = questions.answer("q-aaaaaaa1", " B ", "fleet")
        self.assertEqual(
            (q.status, q.answer, q.answered_via), ("answered", "B", "fleet")
        )
        raw = self.stored("sessA")["questions"][0]
        self.assertEqual(raw["status"], "answered")
        self.assertTrue(raw["answered_at"].endswith("Z"))
        with self.assertRaisesRegex(
            questions.QuestionError, "already answered via fleet: B"
        ):
            questions.answer("q-aaaaaaa1", "A", "session")

    def test_answer_refusals(self):
        for qid, text, via, msg in (
            ("nope", "A", "session", "not a question id"),
            ("q-aaaaaaa1", "A", "email", "via must be"),
            ("q-aaaaaaa1", "  ", "session", "empty answer"),
            ("q-ffffffff", "A", "session", "no question"),
        ):
            with (
                self.subTest(qid=qid, via=via),
                self.assertRaisesRegex(questions.QuestionError, msg),
            ):
                questions.answer(qid, text, via)

    def test_relay_text_carries_marker_the_hook_allows(self):
        q = questions.load(session_id="sessA")[0]
        self.assertTrue(
            questions.relay_text(q).startswith("[fleet q-aaaaaaa1] alpha: ")
        )


class CliTests(QueueHome):
    def test_questions_lists_pending_with_session_name_and_options(self):
        code, out, _ = self.run_main("questions")
        self.assertEqual(code, 0)
        self.assertIn("pending questions (2):", out)
        self.assertIn("q-aaaaaaa1  alpha  alpha-1  [date]", out)
        self.assertIn("q-bbbbbbb1  beta  sessB", out)
        self.assertIn("- A. Monday (Recommended): why", out)
        self.assertNotIn("q-aaaaaaa2", out)

    def test_questions_json_for_the_skill(self):
        code, out, _ = self.run_main("questions", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        first = next(d for d in data if d["id"] == "q-aaaaaaa1")
        self.assertEqual(first["session_name"], "alpha-1")
        self.assertTrue(first["relay_text"].startswith("[fleet q-aaaaaaa1]"))

    def test_answer_via_fleet_names_the_session_to_relay_to(self):
        code, out, _ = self.run_main(
            "answer", "q-aaaaaaa1", "A. Monday", "--via", "fleet"
        )
        self.assertEqual(code, 0)
        self.assertIn("relay to: alpha-1", out)
        code, out, _ = self.run_main("answer", "q-bbbbbbb1", "B", "--via", "fleet")
        self.assertIn("not live: kept in queue", out)

    def test_answer_twice_refused_exit_1(self):
        self.run_main("answer", "q-aaaaaaa1", "A")
        code, _, err = self.run_main("answer", "q-aaaaaaa1", "B")
        self.assertEqual(code, 1)
        self.assertIn("already answered via session: A", err)

    def test_no_pending(self):
        for sid in ("sessA", "sessB"):
            (self.qdir / f"{sid}.json").unlink()
        self.assertIn("no pending questions", self.run_main("questions")[1])


class AwayTests(QueueHome):
    def test_away_off_by_default_then_on_then_off(self):
        self.assertIn("away mode off", self.run_main("away")[1])
        code, out, _ = self.run_main("away", "on")
        self.assertEqual(code, 0)
        self.assertIn("away mode on", out)
        self.assertTrue(questions.away_path().is_file())
        self.assertTrue(questions.is_away())
        self.assertIn("away mode off", self.run_main("away", "off")[1])
        self.assertFalse(questions.away_path().exists())
        self.assertIn("away mode off", self.run_main("away", "off")[1])  # idempotent

    def test_bad_mode_is_usage_error(self):
        self.assertEqual(self.run_main("away", "maybe")[0], 2)


class DashboardNoticeTests(QueueHome):
    def test_notice_lists_pending_questions_first(self):
        page = dashboard.build_page(model.load_state() or FleetState())
        self.assertIn("Pending questions (2)", page)
        self.assertIn("Ship now?", page)
        self.assertLess(page.index('id="questions"'), page.index("<h2>Alerts</h2>"))

    def test_no_notice_without_pending(self):
        for sid in ("sessA", "sessB"):
            (self.qdir / f"{sid}.json").unlink()
        self.assertNotIn('id="questions"', dashboard.build_page(FleetState()))

    def test_question_text_is_escaped(self):
        self.write("sessC", "gamma", [_q("q-ccccccc1", "<script>x</script>")])
        page = dashboard.build_page(FleetState())
        self.assertNotIn("<script>x</script>", page)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", page)


if __name__ == "__main__":
    unittest.main()
