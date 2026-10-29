"""Tests for boxes.py (question-box answering plan and SendKeys escaping).
Run from the repo root:  py -3.13 -m pytest -q tools/fleet
No keys are sent: only the pure planning and escaping are tested.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.fleet import boxes

BOX = """
● Some earlier output
 ☐ Prod webhook
│ How should it get set?
❯ 1. A. Give Claude the key once (Recommended)
     description
  2. B. You set it yourself
  3. C. Defer to launch
  4. Type something.
─────────────
  5. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel
"""


class Plan(unittest.TestCase):
    def test_pointer_and_type_option(self):
        p = boxes.plan(BOX)
        self.assertEqual((p.current, p.target), (1, 4))
        self.assertEqual(p.keys, "{DOWN}{DOWN}{DOWN}")

    def test_pointer_below_target_moves_up(self):
        screen = BOX.replace("❯ 1.", "  1.").replace("  5. Chat", "❯ 5. Chat")
        self.assertEqual(boxes.plan(screen).keys, "{UP}")

    def test_already_on_target(self):
        screen = BOX.replace("❯ 1.", "  1.").replace("  4. Type", "❯ 4. Type")
        self.assertEqual(boxes.plan(screen).keys, "")

    def test_no_box_refused(self):
        with self.assertRaises(boxes.BoxError):
            boxes.plan("● working...\n❯ \n  status line")

    def test_scrollback_list_ignored(self):
        old = "❯ 1. stale item\n  7. Type something.\n" * 3
        p = boxes.plan(old + BOX)
        self.assertEqual((p.current, p.target), (1, 4))

    def test_box_without_type_option_refused(self):
        with self.assertRaises(boxes.BoxError):
            boxes.plan(BOX.replace("Type something.", "Other"))


class SendKeys(unittest.TestCase):
    def test_specials_braced(self):
        self.assertEqual(
            boxes.sendkeys_literal("a+b (c) {d} ~%^[e]"),
            "a{+}b {(}c{)} {{}d{}} {~}{%}{^}{[}e{]}",
        )

    def test_multiline_refused(self):
        with self.assertRaises(boxes.BoxError):
            boxes.sendkeys_literal("one\ntwo")

    def test_default_answer_is_one_line_and_names_create_handoff(self):
        self.assertIn("/create-handoff", boxes.HANDOFF_ANSWER)
        boxes.sendkeys_literal(boxes.HANDOFF_ANSWER)

    def test_away_answer_is_one_sendkeys_safe_line(self):
        boxes.sendkeys_literal(boxes.AWAY_ANSWER)


class ClearOpenBoxes(unittest.TestCase):
    """away on answers boxes opened before it; no keys are sent (boxes.answer is stubbed)."""

    def test_answers_only_live_waiting_sessions_and_reports_refusals(self):
        from unittest import mock

        from tools.fleet import fleet
        from tools.fleet.model import FleetState, Session

        state = FleetState(
            sessions=[
                Session(project="privacy", status="waiting", alive=True),
                Session(project="oaken", status="waiting", alive=True),
                Session(project="amber", status="idle", alive=True),
                Session(project="gone", status="waiting", alive=False),
            ]
        )

        def fake(tab, text):
            self.assertEqual(text, boxes.AWAY_ANSWER)
            if tab == "oaken":
                raise boxes.BoxError("2 Windows Terminal tabs are named 'oaken'")
            return "answered"

        with mock.patch.object(boxes, "answer", side_effect=fake) as answer:
            lines = fleet._clear_open_boxes(state)
        self.assertEqual(
            [c.args[0] for c in answer.call_args_list], ["privacy", "oaken"]
        )
        self.assertEqual(lines[0], "sessions already waiting on a box: 2")
        self.assertIn("privacy: open box answered", lines[1])
        self.assertIn("oaken: open box NOT answered: 2 Windows Terminal tabs", lines[2])

    def test_no_waiting_session_adds_nothing(self):
        from tools.fleet import fleet
        from tools.fleet.model import FleetState

        self.assertEqual(fleet._clear_open_boxes(FleetState()), [])


if __name__ == "__main__":
    unittest.main()
