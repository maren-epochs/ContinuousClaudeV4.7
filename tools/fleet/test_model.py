"""Tests for tools/fleet/model.py (fleet data contract, see tools/fleet/schema.md).

Run from the repo root:  py -3.13 -m pytest -q tools/fleet
Every test runs under a temp HOME/USERPROFILE; the real ~/.claude is never touched.
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

from tools.fleet import model
from tools.fleet.model import (
    Alert,
    AuditEvent,
    Change,
    Collision,
    DriftEntry,
    FleetState,
    Handoff,
    Harness,
    Machine,
    Proposal,
    ProposalSource,
    ProposalTarget,
    Session,
)


def full_state() -> FleetState:
    alert = Alert(
        kind="stuck",
        severity="warn",
        session="sess-a1",
        detail="waiting on a user reply for 25 min",
        evidence="transcript.jsonl:120",
    )
    return FleetState(
        generated_at="2026-10-07T12:00:00Z",
        harness=Harness(
            repo="repo-root",
            head_sha="abc1234",
            installed_sha="def5678",
            drift=[
                DriftEntry(
                    installed_path="hooks/status.mjs",
                    repo_path=".claude/hooks/status.mjs",
                    expected_sha="111",
                    actual_sha="222",
                    status="modified",
                )
            ],
        ),
        sessions=[
            Session(
                pid=4242,
                session_id="sess-a1",
                name="project-A work",
                project="project-A",
                cwd="/work/project-A",
                status="waiting",
                kind="interactive",
                version="2.1.292",
                alive=True,
                started_at="2026-10-07T10:00:00Z",
                updated_at="2026-10-07T11:59:00Z",
                model="claude-opus-5-5",
                context_pct=41.5,
                last_activity="2026-10-07T11:58:00Z",
                handoff=Handoff(
                    path="thoughts/shared/handoffs/h.yaml",
                    age_h=3.25,
                    goal="ship it",
                    now="tests",
                ),
                agents_running=2,
                alerts=[alert],
            ),
            Session(pid=99, session_id="sess-b2", project="project-B", status="shell"),
        ],
        inbox_count=3,
        audit_recent=[
            AuditEvent(
                ts="2026-10-07T11:00:00Z",
                project="project-B",
                session_id="sess-b2",
                category="force-push",
                command="git push --force",
                tool="Bash",
            )
        ],
        collisions=[
            Collision(
                kind="path",
                target="/work/project-A/x.py",
                sessions=["sess-a1", "sess-b2"],
                detail="both edited within 30 min",
            )
        ],
        machine=Machine(mem_total_gb=32.0, mem_free_gb=7.5),
    )


def edit_proposal() -> Proposal:
    return Proposal(
        id="20261007T120000Z-0a1b2c3d",
        created_at="2026-10-07T12:00:00Z",
        kind="edit",
        source=ProposalSource(
            project="project-A", session_id="sess-a1", cwd="/work/project-A"
        ),
        target=ProposalTarget(
            installed_path="hooks/status.mjs", repo_path=".claude/hooks/status.mjs"
        ),
        change=Change(tool="Edit", old_string="a = 1", new_string="a = 2"),
        reason="fix the context percentage",
        status="pending",
    )


class TempHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        patcher = mock.patch.dict(
            os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)


class RoundTripTests(unittest.TestCase):
    def assert_round_trip(self, rec):
        text = json.dumps(rec.to_dict())
        again = type(rec).from_dict(json.loads(text))
        self.assertEqual(again, rec)
        self.assertEqual(type(rec).from_json(rec.to_json()), rec)

    def test_fleet_state_round_trip(self):
        self.assert_round_trip(full_state())

    def test_edit_proposal_round_trip(self):
        self.assert_round_trip(edit_proposal())

    def test_lesson_and_write_proposals_round_trip(self):
        lesson = Proposal(
            id="lesson-1",
            kind="lesson",
            change=Change(tool="lesson", content="Prefer X over Y."),
            status="rejected",
        )
        self.assert_round_trip(lesson)
        cmd = Proposal(id="cmd-1", change=Change(tool="Bash", command="cp a b"))
        self.assert_round_trip(cmd)

    def test_audit_event_round_trip(self):
        self.assert_round_trip(full_state().audit_recent[0])

    def test_session_status_is_a_free_string(self):
        s = Session.from_dict({"status": "something-new", "kind": "bg"})
        self.assertEqual(s.status, "something-new")
        self.assertEqual(s.kind, "bg")

    def test_to_dict_is_plain_json(self):
        d = full_state().to_dict()
        self.assertEqual(d["sessions"][0]["handoff"]["age_h"], 3.25)
        self.assertEqual(d["sessions"][0]["alerts"][0]["kind"], "stuck")
        self.assertEqual(d["machine"], {"mem_total_gb": 32.0, "mem_free_gb": 7.5})
        self.assertNotIn("extra", d)
        self.assertNotIn("extra", d["sessions"][0])


class TolerantFromDictTests(unittest.TestCase):
    def test_missing_keys_give_defaults(self):
        st = FleetState.from_dict({})
        self.assertEqual(st, FleetState())
        self.assertIsNone(st.generated_at)
        self.assertEqual(st.sessions, [])
        self.assertEqual(st.harness, Harness())
        self.assertEqual(st.machine, Machine())
        p = Proposal.from_dict({})
        self.assertEqual(p.kind, "edit")
        self.assertEqual(p.status, "pending")
        self.assertEqual(p.change, Change())
        self.assertEqual(AuditEvent.from_dict({}), AuditEvent())

    def test_partial_nested_session(self):
        st = FleetState.from_dict({"sessions": [{"pid": 7, "handoff": {"goal": "g"}}]})
        s = st.sessions[0]
        self.assertEqual(s.pid, 7)
        self.assertIsNone(s.session_id)
        self.assertFalse(s.alive)
        self.assertEqual(s.agents_running, 0)
        self.assertEqual(s.handoff, Handoff(goal="g"))

    def test_extra_keys_are_kept_and_written_back(self):
        data = full_state().to_dict()
        data["future_top"] = {"x": 1}
        data["sessions"][0]["peerProtocol"] = 3
        data["harness"]["drift"][0]["note"] = "n"
        st = FleetState.from_dict(data)
        self.assertEqual(st.extra, {"future_top": {"x": 1}})
        self.assertEqual(st.sessions[0].extra, {"peerProtocol": 3})
        self.assertEqual(st.to_dict(), data)
        p = Proposal.from_dict({**edit_proposal().to_dict(), "content_hash": "h1"})
        self.assertEqual(p.extra["content_hash"], "h1")
        self.assertEqual(p.to_dict()["content_hash"], "h1")
        ev = AuditEvent.from_dict({"ts": "t", "cwd": "/w"})
        self.assertEqual(ev.to_dict(), {**AuditEvent(ts="t").to_dict(), "cwd": "/w"})

    def test_wrong_types_fall_back_to_defaults(self):
        st = FleetState.from_dict(
            {
                "inbox_count": "three",
                "sessions": "nope",
                "harness": [1, 2],
                "machine": {"mem_total_gb": "big", "mem_free_gb": 4},
                "collisions": [{"sessions": "a"}, "junk", 5],
            }
        )
        self.assertEqual(st.inbox_count, 0)
        self.assertEqual(st.sessions, [])
        self.assertEqual(st.harness, Harness())
        self.assertIsNone(st.machine.mem_total_gb)
        self.assertEqual(st.machine.mem_free_gb, 4.0)
        self.assertIsInstance(st.machine.mem_free_gb, float)
        self.assertEqual(st.collisions, [Collision()])

    def test_bool_is_not_a_number_and_number_is_not_a_bool(self):
        s = Session.from_dict({"pid": True, "alive": 1, "context_pct": False})
        self.assertIsNone(s.pid)
        self.assertFalse(s.alive)
        self.assertIsNone(s.context_pct)

    def test_non_mapping_input_gives_defaults(self):
        self.assertEqual(FleetState.from_dict(None), FleetState())  # type: ignore[arg-type]
        self.assertEqual(Proposal.from_dict([1]), Proposal())  # type: ignore[arg-type]
        self.assertEqual(AuditEvent.from_json("[]"), AuditEvent())

    def test_defaults_are_not_shared(self):
        a, b = FleetState(), FleetState()
        a.sessions.append(Session())
        self.assertEqual(b.sessions, [])


class PathTests(TempHome):
    def test_home_dir_prefers_userprofile_on_windows(self):
        env = {"HOME": "/h", "USERPROFILE": "/u"}
        self.assertEqual(model.home_dir(env=env, platform="win32"), Path("/u"))
        self.assertEqual(model.home_dir(env=env, platform="linux"), Path("/h"))

    def test_home_dir_falls_back(self):
        self.assertEqual(
            model.home_dir(env={"HOME": "", "USERPROFILE": "/u"}, platform="linux"),
            Path("/u"),
        )
        self.assertEqual(
            model.home_dir(env={"HOME": "/h"}, platform="win32"), Path("/h")
        )
        self.assertEqual(model.home_dir(env={}, platform="linux"), Path.home())

    def test_locations_resolve_under_temp_home(self):
        c = self.home / ".claude"
        self.assertEqual(model.home_dir(), self.home)
        self.assertEqual(model.claude_dir(), c)
        self.assertEqual(model.fleet_dir(), c / "fleet")
        self.assertEqual(model.state_path(), c / "fleet" / "state.json")
        self.assertEqual(model.audit_path(), c / "fleet" / "audit.jsonl")
        self.assertEqual(model.guard_errors_path(), c / "fleet" / "guard-errors.log")
        self.assertEqual(model.inbox_dir(), c / "harness-inbox")
        self.assertEqual(model.manifest_path(), c / ".ccv47-manifest.json")
        self.assertEqual(model.proposal_path("p-1"), c / "harness-inbox" / "p-1.json")

    def test_proposal_path_rejects_unsafe_ids(self):
        for bad in ("", "..", "../x", "a/b", "a\\b", "c:x", ".hidden", "a b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                model.proposal_path(bad)

    def test_new_proposal_id_is_safe_and_unique(self):
        ids = {model.new_proposal_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)
        for i in ids:
            model.proposal_path(i)


class FileIOTests(TempHome):
    def test_state_save_load(self):
        st = full_state()
        path = model.save_state(st)
        self.assertEqual(path, model.state_path())
        self.assertEqual(model.load_state(), st)
        self.assertEqual([p.name for p in model.fleet_dir().iterdir()], ["state.json"])

    def test_load_state_missing_or_corrupt_is_none(self):
        self.assertIsNone(model.load_state())
        model.fleet_dir().mkdir(parents=True)
        model.state_path().write_text("{not json", encoding="utf-8")
        self.assertIsNone(model.load_state())

    def test_proposal_save_load_list(self):
        p1 = edit_proposal()
        p2 = Proposal(id="p-0", created_at="2026-10-06T00:00:00Z", kind="lesson")
        model.save_proposal(p1)
        model.save_proposal(p2)
        (model.inbox_dir() / "broken.json").write_text("[", encoding="utf-8")
        (model.inbox_dir() / "notes.txt").write_text("x", encoding="utf-8")
        self.assertEqual(model.load_proposal(p1.id), p1)
        self.assertIsNone(model.load_proposal("absent"))
        self.assertEqual(model.list_proposals(), [p2, p1])

    def test_audit_append_and_read(self):
        events = [AuditEvent(ts=f"t{i}", category="c", command="x") for i in range(3)]
        for ev in events:
            model.append_audit(ev)
        with model.audit_path().open("a", encoding="utf-8") as fh:
            fh.write("garbage\n\n")
        model.append_audit(AuditEvent(ts="t3"))
        lines = model.audit_path().read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[0]), events[0].to_dict())
        got = model.read_audit()
        self.assertEqual([e.ts for e in got], ["t0", "t1", "t2", "t3"])
        self.assertEqual([e.ts for e in model.read_audit(limit=2)], ["t2", "t3"])

    def test_read_audit_missing_is_empty(self):
        self.assertEqual(model.read_audit(), [])


if __name__ == "__main__":
    unittest.main()
