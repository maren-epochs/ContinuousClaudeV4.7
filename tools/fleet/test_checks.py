"""Tests for tools/fleet/checks.py (VAL-806).

Run from the repo root:  py -3.13 -m pytest -q tools/fleet
Every test builds synthetic transcripts and a synthetic ~/.claude under a temp
HOME/USERPROFILE; the real ~/.claude is never read or written.
"""

import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.fleet import checks, collect, model
from tools.fleet.model import FleetState, Session

NOW = float(int(time.time()))


def iso(minutes_ago: float) -> str:
    stamp = dt.datetime.fromtimestamp(NOW - minutes_ago * 60, dt.UTC)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def prompt(text, ago=60):
    return {
        "type": "user",
        "timestamp": iso(ago),
        "message": {"role": "user", "content": text},
    }


def say(*blocks, ago=59, model_name="claude-opus-5-5", sidechain=False):
    return {
        "type": "assistant",
        "timestamp": iso(ago),
        "isSidechain": sidechain,
        "message": {
            "model": model_name,
            "content": list(blocks),
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }


def text(t):
    return {"type": "text", "text": t}


def use(tool_id, name, **inp):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}


def result(tool_id, ago=58, error=False, content="ok", meta=None):
    rec = {
        "type": "user",
        "timestamp": iso(ago),
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": error,
                    "content": content,
                }
            ],
        },
    }
    if meta is not None:
        rec["toolUseResult"] = meta
    return rec


def notification(tool_id, status, summary="", ago=40):
    body = (
        f"<task-notification><task-id>a1</task-id><tool-use-id>{tool_id}"
        f"</tool-use-id><status>{status}</status><summary>{summary}</summary>"
        "</task-notification>"
    )
    return {
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": iso(ago),
        "content": body,
    }


class ChecksHome(unittest.TestCase):
    """Temp HOME with ~/.claude; sessions are built directly as Session records."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        env = mock.patch.dict(
            os.environ, {"HOME": str(self.root), "USERPROFILE": str(self.root)}
        )
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)
        self.claude = self.root / ".claude"
        (self.claude / "sessions").mkdir(parents=True)

    def repo(self, name, git_file=False) -> Path:
        path = self.root / "work" / name
        path.mkdir(parents=True, exist_ok=True)
        if git_file:
            (path / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        else:
            (path / ".git").mkdir(exist_ok=True)
        return path

    def plain_dir(self, name) -> Path:
        path = self.root / "scratch" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def transcript(self, sid, records, cwd=None, prefix="") -> Path:
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd or "x"))
        path = self.claude / "projects" / slug / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = "".join(json.dumps(r) + "\n" for r in records)
        path.write_text(prefix + lines, encoding="utf-8")
        return path

    def sess(self, sid, cwd, records=None, alive=True, **fields) -> Session:
        s = Session(
            session_id=sid,
            pid=abs(hash(sid)) % 90000 + 10,
            cwd=str(cwd),
            project=Path(cwd).name,
            alive=alive,
            status=fields.pop("status", "idle"),
            **fields,
        )
        if records is not None:
            s.extra["transcript"] = str(self.transcript(sid, records, cwd))
        return s

    def run_checks(self, *sessions) -> FleetState:
        state = FleetState(sessions=list(sessions))
        return checks.run_checks(state, now=NOW)

    def alerts(self, session, kind=None):
        return [a for a in session.alerts if kind is None or a.kind == kind]

    def line_of(self, session, needle) -> int:
        lines = Path(session.extra["transcript"]).read_text(encoding="utf-8")
        for n, line in enumerate(lines.split("\n"), start=1):
            if needle in line:
                return n
        raise AssertionError(needle)


class TailLineTests(ChecksHome):
    def test_line_numbers_are_absolute_past_the_tail(self):
        cwd = self.repo("project-A")
        filler = [prompt(f"filler {i} " + "x" * 200, ago=600) for i in range(40)]
        records = [*filler, say(use("t1", "Bash", command="ls"), ago=5)]
        s = self.sess("sess-a1", cwd, records)
        path = Path(s.extra["transcript"])
        with mock.patch.object(checks, "TAIL_BYTES", 1500):
            lines = checks.tail_lines(path, checks.TAIL_BYTES)
        self.assertLess(len(lines), 41)
        self.assertEqual(lines[-1][0], 41)
        self.assertIn('"t1"', lines[-1][1])
        whole = path.read_text(encoding="utf-8").split("\n")
        for number, line in lines:
            self.assertEqual(whole[number - 1], line)

    def test_unicode_line_separator_does_not_shift_numbers(self):
        cwd = self.repo("project-A")
        records = [prompt("a b"), say(text("done"))]
        s = self.sess("sess-a1", cwd, records)
        path = Path(s.extra["transcript"])
        path.write_text(
            json.dumps(records[0], ensure_ascii=False)
            + "\n"
            + json.dumps(records[1])
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual([n for n, _ in checks.tail_lines(path, 10_000)], [1, 2])

    def test_garbage_and_missing_transcripts_do_not_raise(self):
        cwd = self.repo("project-A")
        s = self.sess("sess-a1", cwd, [prompt("hi")])
        Path(s.extra["transcript"]).write_text(
            'not json\n[1,2]\n{"type": 5, "message": "x"}\n'
            '{"type": "assistant", "message": {"content": [7, {"type": "tool_use"}]}}\n'
            '{"type": "user", "message": {"content": [{"type": "tool_result"}]}}\n',
            encoding="utf-8",
        )
        gone = self.sess("sess-b1", cwd)
        gone.extra["transcript"] = str(self.root / "missing.jsonl")
        state = self.run_checks(s, gone, self.sess("sess-c1", cwd))
        self.assertEqual(len(state.sessions), 3)


class CollisionTests(ChecksHome):
    def test_two_alive_sessions_write_the_same_path_within_30_min(self):
        a_dir = self.repo("project-A")
        target = a_dir / "src" / "main.py"
        other = str(target).replace("\\", "/")
        if sys.platform == "win32":
            other = other.upper()
        a = self.sess(
            "sess-a1",
            a_dir,
            [prompt("go"), say(use("w1", "Edit", file_path=str(target)), ago=10)],
        )
        b = self.sess(
            "sess-b1",
            self.plain_dir("other"),
            [prompt("go"), say(use("w2", "Write", file_path=other), ago=20)],
        )
        state = self.run_checks(a, b)
        paths = [c for c in state.collisions if c.kind == "path"]
        self.assertEqual(len(paths), 1)
        self.assertEqual(sorted(paths[0].sessions), ["sess-a1", "sess-b1"])
        for s, tool_id in ((a, "w1"), (b, "w2")):
            found = self.alerts(s, "collision")
            self.assertEqual(len(found), 1, s.alerts)
            self.assertEqual(found[0].session, s.session_id)
            line = self.line_of(s, f'"{tool_id}"')
            self.assertEqual(found[0].evidence, f"{s.extra['transcript']}:{line}")

    def test_notebook_edit_counts_and_old_writes_do_not(self):
        a_dir = self.repo("project-A")
        nb = str(a_dir / "nb.ipynb")
        old = str(a_dir / "old.py")
        a = self.sess(
            "sess-a1",
            a_dir,
            [
                say(use("n1", "NotebookEdit", notebook_path=nb), ago=5),
                say(use("o1", "Write", file_path=old), ago=45),
            ],
        )
        b = self.sess(
            "sess-b1",
            self.plain_dir("b"),
            [
                say(use("n2", "NotebookEdit", notebook_path=nb), ago=6),
                say(use("o2", "Write", file_path=old), ago=5),
            ],
        )
        state = self.run_checks(a, b)
        self.assertEqual(
            [os.path.basename(c.target) for c in state.collisions if c.kind == "path"],
            ["nb.ipynb"],
        )

    def test_dead_session_never_collides(self):
        a_dir = self.repo("project-A")
        f = str(a_dir / "x.py")
        a = self.sess("sess-a1", a_dir, [say(use("w1", "Write", file_path=f), ago=1)])
        b = self.sess(
            "sess-b1",
            a_dir,
            [say(use("w2", "Write", file_path=f), ago=2)],
            alive=False,
        )
        state = self.run_checks(a, b)
        self.assertEqual(state.collisions, [])
        self.assertEqual(self.alerts(a, "collision"), [])

    def test_subagent_transcript_writes_count(self):
        a_dir = self.repo("project-A")
        f = str(a_dir / "x.py")
        a = self.sess("sess-a1", a_dir, [prompt("spawn a worker", ago=30)])
        sub = Path(a.extra["transcript"]).with_suffix("") / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-1.jsonl").write_text(
            json.dumps(say(use("w9", "Edit", file_path=f), ago=3, sidechain=True))
            + "\n",
            encoding="utf-8",
        )
        b = self.sess(
            "sess-b1",
            self.plain_dir("b"),
            [say(use("w2", "Write", file_path=f), ago=2)],
        )
        state = self.run_checks(a, b)
        self.assertEqual(len(state.collisions), 1)
        self.assertEqual(
            self.alerts(a, "collision")[0].evidence, f"{sub / 'agent-1.jsonl'}:1"
        )

    def test_two_alive_sessions_in_the_same_repo(self):
        repo = self.repo("project-A")
        (repo / "sub").mkdir()
        a = self.sess("sess-a1", repo, [prompt("hi")], last_activity=iso(5))
        b = self.sess("sess-b1", repo / "sub", [prompt("hi")], last_activity=iso(29))
        c = self.sess(
            "sess-c1", self.repo("project-B"), [prompt("hi")], last_activity=iso(1)
        )
        state = self.run_checks(a, b, c)
        repos = [x for x in state.collisions if x.kind == "repo"]
        self.assertEqual(len(repos), 1)
        self.assertEqual(sorted(repos[0].sessions), ["sess-a1", "sess-b1"])
        self.assertEqual(os.path.normcase(repos[0].target), os.path.normcase(str(repo)))
        self.assertEqual(len(self.alerts(a, "collision")), 1)
        self.assertEqual(self.alerts(c, "collision"), [])

    def test_repo_collision_needs_two_members_active_within_30_min(self):
        repo = self.repo("project-A")
        idle = [
            self.sess("sess-i1", repo, [prompt("hi")], last_activity=iso(31)),
            self.sess("sess-i2", repo, [prompt("hi")], kind="bg"),
            self.sess("sess-i3", repo, [prompt("hi")], last_activity="garbage"),
        ]
        one = self.sess("sess-a1", repo, [prompt("hi")], last_activity=iso(2))
        state = self.run_checks(one, *idle)
        self.assertEqual(state.collisions, [])
        for s in (one, *idle):
            self.assertEqual(self.alerts(s, "collision"), [])

    def test_repo_collision_lists_and_alerts_only_active_members(self):
        repo = self.repo("project-A")
        a = self.sess("sess-a1", repo, [prompt("hi")], last_activity=iso(2))
        b = self.sess("sess-b1", repo, [prompt("hi")], last_activity=iso(10))
        idle = self.sess("sess-i1", repo, [prompt("hi")], last_activity=iso(600))
        state = self.run_checks(a, b, idle)
        repos = [x for x in state.collisions if x.kind == "repo"]
        self.assertEqual(len(repos), 1)
        self.assertEqual(sorted(repos[0].sessions), ["sess-a1", "sess-b1"])
        self.assertEqual(len(self.alerts(a, "collision")), 1)
        self.assertNotIn("sess-i1", self.alerts(a, "collision")[0].detail)
        self.assertEqual(self.alerts(idle, "collision"), [])

    def test_path_collisions_ignore_activity(self):
        a_dir = self.repo("project-A")
        f = str(a_dir / "x.py")
        a = self.sess("sess-a1", a_dir, [say(use("w1", "Write", file_path=f), ago=3)])
        b = self.sess(
            "sess-b1",
            self.plain_dir("b"),
            [say(use("w2", "Write", file_path=f), ago=4)],
        )
        state = self.run_checks(a, b)
        self.assertEqual([c.kind for c in state.collisions], ["path"])

    def test_subagent_reads_are_capped_newest_first(self):
        a_dir = self.repo("project-A")
        a = self.sess("sess-a1", a_dir, [prompt("spawn", ago=5)])
        sub = Path(a.extra["transcript"]).with_suffix("") / "subagents"
        sub.mkdir(parents=True)
        cap = checks.SUBAGENT_MAX_FILES
        files = []
        for i in range(cap + 5):
            path = sub / f"agent-{i:02d}.jsonl"
            rec = say(use(f"w{i}", "Edit", file_path=str(a_dir / f"f{i}.py")), ago=3)
            path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
            stamp = NOW - 600 + i
            os.utime(path, (stamp, stamp))
            files.append(path)
        with mock.patch.object(checks, "_scan", wraps=checks._scan) as scan:
            self.run_checks(a)
        read = [c.args[0] for c in scan.call_args_list if c.args[0].parent == sub]
        self.assertEqual(sorted(read), sorted(files[-cap:]))

    def test_subagent_window_is_not_widened_to_the_tail_start(self):
        a_dir = self.repo("project-A")
        a = self.sess("sess-a1", a_dir, [prompt("long session", ago=300)])
        sub = Path(a.extra["transcript"]).with_suffix("") / "subagents"
        sub.mkdir(parents=True)
        old = sub / "agent-old.jsonl"
        rec = say(use("w1", "Edit", file_path=str(a_dir / "x.py")), ago=60)
        old.write_text(json.dumps(rec) + "\n", encoding="utf-8")
        os.utime(old, (NOW - 3600, NOW - 3600))
        with mock.patch.object(checks, "_scan", wraps=checks._scan) as scan:
            self.run_checks(a)
        self.assertNotIn(old, [c.args[0] for c in scan.call_args_list])

    def test_subagent_writes_are_read_once_per_session(self):
        a_dir = self.repo("project-A")
        a = self.sess("sess-a1", a_dir, [prompt("spawn", ago=5)])
        sub = Path(a.extra["transcript"]).with_suffix("") / "subagents"
        sub.mkdir(parents=True)
        rec = say(use("w1", "Edit", file_path=str(a_dir / "x.py")), ago=3)
        (sub / "agent-1.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
        with mock.patch.object(checks, "_scan", wraps=checks._scan) as scan:
            self.run_checks(a)
        read = [c for c in scan.call_args_list if c.args[0].parent == sub]
        self.assertEqual(len(read), 1)

    def test_worktrees_non_repos_and_dead_sessions_are_not_repo_collisions(self):
        main = self.repo("project-A")
        tree = self.repo("project-A-wt", git_file=True)
        scratch = self.plain_dir("s")
        state = self.run_checks(
            self.sess("sess-a1", main),
            self.sess("sess-b1", tree),
            self.sess("sess-c1", scratch),
            self.sess("sess-d1", scratch),
            self.sess("sess-e1", main, alive=False),
        )
        self.assertEqual(state.collisions, [])


class StuckTests(ChecksHome):
    def test_status_waiting_over_20_minutes(self):
        cwd = self.repo("project-A")
        s = self.sess("sess-a1", cwd, [prompt("hi"), say(text("ok"))], status="waiting")
        s.extra["status_updated_at"] = iso(25)
        s.extra["waiting_for"] = "permission"
        fresh = self.sess("sess-b1", cwd, [prompt("hi")], status="waiting")
        fresh.extra["status_updated_at"] = iso(10)
        dead = self.sess("sess-c1", cwd, [prompt("hi")], status="waiting", alive=False)
        dead.extra["status_updated_at"] = iso(90)
        self.run_checks(s, fresh, dead)
        found = self.alerts(s, "stuck")
        self.assertEqual(len(found), 1)
        self.assertIn("permission", found[0].detail)
        self.assertEqual(self.alerts(fresh, "stuck"), [])
        self.assertEqual(self.alerts(dead, "stuck"), [])

    def test_unanswered_ask_user_question(self):
        cwd = self.repo("project-A")
        ask = use("q1", "AskUserQuestion", questions=[{"question": "A or B?"}])
        s = self.sess("sess-a1", cwd, [prompt("hi", ago=40), say(ask, ago=30)])
        answered = self.sess(
            "sess-b1",
            cwd,
            [prompt("hi", ago=40), say(ask, ago=30), result("q1", ago=29)],
        )
        recent = self.sess("sess-c1", cwd, [prompt("hi"), say(ask, ago=5)])
        self.run_checks(s, answered, recent)
        found = self.alerts(s, "stuck")
        self.assertEqual(len(found), 1)
        self.assertIn("AskUserQuestion", found[0].detail)
        line = self.line_of(s, '"q1"')
        self.assertEqual(found[0].evidence, f"{s.extra['transcript']}:{line}")
        self.assertEqual(self.alerts(answered, "stuck"), [])
        self.assertEqual(self.alerts(recent, "stuck"), [])

    def test_prose_question_unanswered(self):
        cwd = self.repo("project-A")
        records = [prompt("hi", ago=40), say(text("Should I commit?"), ago=30)]
        s = self.sess("sess-a1", cwd, records)
        waiting_on_agents = self.sess("sess-b1", cwd, records, agents_running=1)
        busy = self.sess("sess-c1", cwd, records, status="busy")
        self.run_checks(s, waiting_on_agents, busy)
        self.assertEqual(len(self.alerts(s, "stuck")), 1)
        self.assertEqual(self.alerts(waiting_on_agents, "stuck"), [])
        self.assertEqual(self.alerts(busy, "stuck"), [])

    def fails(self, *calls):
        records = [prompt("go", ago=30)]
        for n, (cmd, error) in enumerate(calls):
            tid = f"b{n}"
            records += [
                say(use(tid, "Bash", command=cmd), ago=20 - n),
                result(tid, ago=20 - n, error=error, content="exit 1"),
            ]
        return records

    def test_same_failing_call_three_times_in_a_row(self):
        cwd = self.repo("project-A")
        s = self.sess("sess-a1", cwd, self.fails(("ok", False), *[("make", True)] * 3))
        two = self.sess("sess-b1", cwd, self.fails(*[("make", True)] * 2))
        broken = self.sess(
            "sess-c1",
            cwd,
            self.fails(("make", True), ("make", False), ("make", True), ("make", True)),
        )
        recovered = self.sess(
            "sess-d1", cwd, self.fails(*[("make", True)] * 3, ("ls", False))
        )
        varied = self.sess(
            "sess-e1",
            cwd,
            self.fails(("make a", True), ("make b", True), ("make", True)),
        )
        self.run_checks(s, two, broken, recovered, varied)
        found = self.alerts(s, "stuck")
        self.assertEqual(len(found), 1)
        self.assertIn("3", found[0].detail)
        self.assertEqual(
            found[0].evidence, f"{s.extra['transcript']}:{len(s_lines(s))}"
        )
        for other in (two, broken, recovered, varied):
            self.assertEqual(self.alerts(other, "stuck"), [], other.session_id)

    def test_subagent_stopped_at_turn_limit_without_report(self):
        cwd = self.repo("project-A")
        reports = cwd / "reports"
        reports.mkdir()
        (reports / "w2.json").write_text("{}", encoding="utf-8")
        missing = json.dumps({"output": str(reports / "w1.json")})
        present = json.dumps(
            {
                "prior_report": str(reports / "w1.json"),
                "output": str(reports / "w2.json"),
            }
        )
        limit = {"status": "error_max_turns", "content": []}
        records = [
            prompt("go", ago=50),
            say(use("a1", "Agent", prompt=missing), ago=49),
            result("a1", ago=40, content="partial", meta=limit),
            say(use("a2", "Agent", prompt=present), ago=39),
            result("a2", ago=35, content="partial", meta=limit),
            say(use("a3", "Agent", prompt=missing), ago=34),
            result("a3", ago=30, content="all done", meta={"status": "completed"}),
            say(use("a4", "Agent", prompt="no report path here"), ago=29),
            result("a4", ago=29, meta={"status": "async_launched", "isAsync": True}),
            notification("a4", "completed", "Agent reached max turns (60)", ago=20),
            notification("a4", "completed", "Agent reached max turns (60)", ago=20),
            say(text("Waiting."), ago=19),
        ]
        s = self.sess("sess-a1", cwd, records)
        self.run_checks(s)
        found = [a for a in self.alerts(s, "stuck") if "turn limit" in a.detail]
        lines = sorted(int(a.evidence.rsplit(":", 1)[1]) for a in found)
        expected = [self.line_of(s, '"a1"') + 1, self.line_of(s, "a4</tool-use-id>")]
        self.assertEqual(lines, expected)

    def test_unc_report_paths_are_rejected_before_any_stat(self):
        for raw in (
            "\\\\server\\share\\reports\\w1.json",
            "//server/share/reports/w1.json",
            "\\\\?\\C:\\x\\reports\\w1.json",
            "/\\server\\share\\reports\\w1.json",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(checks._report_path(f"write to {raw} now"))
                self.assertIsNone(checks._report_path(json.dumps({"output": raw})))
        self.assertEqual(
            checks._report_path("write to C:/x/reports/w1.json now"),
            "C:/x/reports/w1.json",
        )
        self.assertEqual(
            checks._report_path('{"output": "C:\\\\x\\\\reports\\\\w1.json"}'),
            "C:\\x\\reports\\w1.json",
        )

    def test_unc_report_path_never_reaches_is_file(self):
        cwd = self.repo("project-A")
        unc = json.dumps({"output": "\\\\server\\share\\reports\\w1.json"})
        limit = {"status": "error_max_turns", "content": []}
        records = [
            prompt("go", ago=50),
            say(use("a1", "Agent", prompt=unc), ago=49),
            result("a1", ago=40, content="partial", meta=limit),
        ]
        s = self.sess("sess-a1", cwd, records)
        real = Path.is_file

        def guarded(path):
            if str(path).startswith(("\\\\", "//")):
                raise AssertionError(f"UNC stat: {path}")
            return real(path)

        with mock.patch.object(checks.Path, "is_file", guarded):
            self.run_checks(s)
        found = [a for a in self.alerts(s, "stuck") if "turn limit" in a.detail]
        self.assertEqual(len(found), 1)
        self.assertNotIn("server", found[0].detail)


def s_lines(session):
    text_ = Path(session.extra["transcript"]).read_text(encoding="utf-8")
    return [line for line in text_.split("\n") if line]


class ComplianceTests(ChecksHome):
    def test_model_other_than_opus_without_user_request(self):
        cwd = self.repo("project-A")
        s = self.sess(
            "sess-a1",
            cwd,
            [prompt("hi"), say(text("ok"), model_name="claude-sonnet-5")],
        )
        asked = self.sess(
            "sess-b1",
            cwd,
            [
                prompt("switch to Sonnet for this one, cheaper"),
                say(text("ok"), model_name="claude-sonnet-5"),
            ],
        )
        reminder = self.sess(
            "sess-c1",
            cwd,
            [
                prompt("hi <system-reminder>sonnet haiku</system-reminder>"),
                say(text("ok"), model_name="claude-haiku-5"),
            ],
        )
        opus = self.sess(
            "sess-d1",
            cwd,
            [prompt("hi"), say(text("ok"), model_name="claude-opus-5-5")],
        )
        self.run_checks(s, asked, reminder, opus)
        found = [a for a in self.alerts(s, "compliance") if "model" in a.detail]
        self.assertEqual(len(found), 1)
        self.assertIn("claude-sonnet-5", found[0].detail)
        self.assertEqual(found[0].evidence, f"{s.extra['transcript']}:2")
        self.assertEqual(self.alerts(asked, "compliance"), [])
        self.assertEqual(len(self.alerts(reminder, "compliance")), 1)
        self.assertEqual(self.alerts(opus, "compliance"), [])

    def test_turn_ending_in_question_without_ask_user_question(self):
        cwd = self.repo("project-A")
        ask = use("q1", "AskUserQuestion", questions=[])
        records = [
            prompt("one", ago=50),
            say(text("Want me to continue?**"), ago=49),
            prompt("two", ago=48),
            say(text("Which one?"), ask, ago=47),
            result("q1", ago=46),
            say(text("Done."), ago=45),
            prompt("three", ago=44),
            say(text("Is this ok?"), use("t1", "Bash", command="ls"), ago=43),
            result("t1", ago=42),
            say(text("Finished; shall I push?"), ago=4),
        ]
        s = self.sess("sess-a1", cwd, records)
        busy = self.sess("sess-b1", cwd, records, status="busy")
        self.run_checks(s, busy)
        found = [a for a in self.alerts(s, "compliance") if "question" in a.detail]
        self.assertEqual(len(found), 1)
        self.assertIn("2", found[0].detail)
        self.assertEqual(found[0].evidence, f"{s.extra['transcript']}:{len(records)}")
        busy_found = [
            a for a in self.alerts(busy, "compliance") if "question" in a.detail
        ]
        self.assertEqual(len(busy_found), 1)
        self.assertIn("1", busy_found[0].detail)
        self.assertEqual(busy_found[0].evidence, f"{busy.extra['transcript']}:2")

    def test_write_into_another_projects_folder(self):
        a_dir = self.repo("project-A")
        b_dir = self.repo("project-B")
        scratch = self.plain_dir("tmp")
        records = [
            prompt("go"),
            say(use("w1", "Write", file_path=str(a_dir / "own.py")), ago=50),
            say(use("w2", "Edit", file_path=str(b_dir / "theirs.py")), ago=49),
            say(use("w3", "Write", file_path=str(scratch / "t.txt")), ago=48),
            say(use("w4", "Write", file_path=str(self.claude / "x.md")), ago=47),
            say(use("w5", "Write", file_path="rel/own2.py"), ago=46),
        ]
        s = self.sess("sess-a1", a_dir, records, alive=False)
        self.run_checks(s)
        found = [a for a in self.alerts(s, "compliance") if "project" in a.detail]
        self.assertEqual(len(found), 1, s.alerts)
        self.assertIn("project-B", found[0].detail)
        self.assertEqual(found[0].evidence, f"{s.extra['transcript']}:3")

    def test_subagent_write_into_another_projects_folder(self):
        a_dir = self.repo("project-A")
        b_dir = self.repo("project-B")
        s = self.sess("sess-a1", a_dir, [prompt("spawn", ago=10)])
        sub = Path(s.extra["transcript"]).with_suffix("") / "subagents"
        sub.mkdir(parents=True)
        rec = say(
            use("w1", "Edit", file_path=str(b_dir / "theirs.py")), ago=3, sidechain=True
        )
        (sub / "agent-1.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
        self.run_checks(s)
        found = [a for a in self.alerts(s, "compliance") if "project" in a.detail]
        self.assertEqual(len(found), 1, s.alerts)
        self.assertIn("project-B", found[0].detail)
        self.assertEqual(found[0].evidence, f"{sub / 'agent-1.jsonl'}:1")

    def test_write_into_a_session_folder_without_git(self):
        a_dir = self.repo("project-A")
        other = self.plain_dir("project-C")
        s = self.sess(
            "sess-a1",
            a_dir,
            [say(use("w1", "Write", file_path=str(other / "f.py")), ago=5)],
        )
        c = self.sess("sess-c1", other, alive=False)
        self.run_checks(s, c)
        self.assertEqual(
            len([a for a in self.alerts(s, "compliance") if "project" in a.detail]), 1
        )


class DriftTests(ChecksHome):
    def install(self, files, head="abc1234", marker="abc1234", generated=None):
        generated = generated or iso(60)[:19] + "Z"
        manifest = {
            "schema_version": 1,
            "generated_at": generated,
            "repo": "repo-root",
            "head_sha": head,
            "dirty": False,
            "eol": "crlf",
            "files": files,
        }
        model.manifest_path().write_text(json.dumps(manifest), encoding="utf-8")
        if marker is not None:
            mark = self.claude / ".ccv47-installed"
            mark.write_text(f"{marker} (dirty)\n", encoding="utf-8")
            when = NOW - 60 * 60
            os.utime(mark, (when, when))

    def put(self, rel, data: bytes) -> str:
        path = self.claude / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def entry(self, repo_path, sha, kept=False):
        return {"repo_path": repo_path, "sha256": sha, "kept": kept}

    def test_modified_and_missing_files_are_drift_kept_are_not(self):
        same = self.put("hooks/a.mjs", b"same\r\n")
        self.put("hooks/b.mjs", b"edited\r\n")
        kept = self.put("skills/k/SKILL.md", b"mine\r\n")
        (self.claude / "sessions" / "1.abc.key").write_text("SECRET", encoding="utf-8")
        self.put("hooks/x.key", b"SECRET")
        (self.root / "escape.txt").write_text("outside", encoding="utf-8")
        self.install(
            {
                "hooks/a.mjs": self.entry(".claude/hooks/a.mjs", same),
                "hooks/b.mjs": self.entry(".claude/hooks/b.mjs", "0" * 64),
                "hooks/gone.mjs": self.entry(".claude/hooks/gone.mjs", "1" * 64),
                "skills/k/SKILL.md": self.entry(
                    "harness/skills/k/SKILL.md", "2" * 64, True
                ),
                "skills/n/SKILL.md": self.entry(
                    "harness/skills/n/SKILL.md", None, True
                ),
                "../escape.txt": self.entry("x", "3" * 64),
                "sessions/1.abc.key": self.entry("x", "4" * 64),
                "hooks/x.key": self.entry("x", "5" * 64),
            }
        )
        self.assertTrue(kept)
        opened = []
        real_open = Path.open

        def spy(path, *a, **kw):
            opened.append(str(path))
            return real_open(path, *a, **kw)

        with mock.patch.object(Path, "open", spy):
            state = self.run_checks()
        drift = {Path(d.installed_path).name: d for d in state.harness.drift}
        self.assertEqual(sorted(drift), ["b.mjs", "gone.mjs"])
        self.assertEqual(drift["b.mjs"].status, "modified")
        self.assertEqual(drift["b.mjs"].expected_sha, "0" * 64)
        self.assertEqual(
            drift["b.mjs"].actual_sha, hashlib.sha256(b"edited\r\n").hexdigest()
        )
        self.assertEqual(drift["b.mjs"].repo_path, ".claude/hooks/b.mjs")
        self.assertEqual(drift["gone.mjs"].status, "missing")
        self.assertIsNone(drift["gone.mjs"].actual_sha)
        self.assertFalse([p for p in opened if p.endswith(".key")], opened)
        self.assertFalse([p for p in opened if p.endswith("escape.txt")], opened)
        self.assertTrue([p for p in opened if p.endswith("b.mjs")], opened)

    def test_manifest_stale_when_marker_sha_differs(self):
        self.put("hooks/b.mjs", b"edited\r\n")
        self.install(
            {"hooks/b.mjs": self.entry(".claude/hooks/b.mjs", "0" * 64)},
            head="abc1234",
            marker="def5678",
        )
        state = self.run_checks()
        self.assertEqual(len(state.harness.drift), 1)
        stale = state.harness.drift[0]
        self.assertEqual(stale.status, "stale")
        self.assertIn("manifest stale", stale.extra.get("detail", ""))
        self.assertEqual(stale.expected_sha, "def5678")
        self.assertEqual(stale.actual_sha, "abc1234")

    def test_manifest_stale_when_marker_is_newer_than_generated_at(self):
        self.put("hooks/b.mjs", b"edited\r\n")
        self.install(
            {"hooks/b.mjs": self.entry(".claude/hooks/b.mjs", "0" * 64)},
            generated=iso(120)[:19] + "Z",
        )
        state = self.run_checks()
        self.assertEqual([d.status for d in state.harness.drift], ["stale"])

    def test_dot_keys_are_skipped_not_fatal(self):
        self.put("hooks/b.mjs", b"edited\r\n")
        self.install(
            {
                ".": self.entry("x", "6" * 64),
                "./": self.entry("x", "7" * 64),
                "hooks/b.mjs": self.entry(".claude/hooks/b.mjs", "0" * 64),
            }
        )
        for key in (".", "./"):
            with self.subTest(key=key):
                self.assertIsNone(checks._safe_rel(key))
        state = self.run_checks()
        self.assertEqual(
            [Path(d.installed_path).name for d in state.harness.drift], ["b.mjs"]
        )

    def test_no_manifest_no_drift(self):
        self.assertEqual(self.run_checks().harness.drift, [])
        model.manifest_path().write_text("{broken", encoding="utf-8")
        self.assertEqual(self.run_checks().harness.drift, [])

    def test_session_started_before_last_sync(self):
        self.install({})
        cwd = self.repo("project-A")
        old = self.sess("sess-a1", cwd, [prompt("hi")], started_at=iso(90)[:19] + "Z")
        new = self.sess("sess-b1", cwd, [prompt("hi")], started_at=iso(30)[:19] + "Z")
        dead = self.sess(
            "sess-c1", cwd, [prompt("hi")], started_at=iso(90)[:19] + "Z", alive=False
        )
        self.run_checks(old, new, dead)
        found = self.alerts(old, "drift")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].evidence, f"{old.extra['transcript']}:1")
        self.assertEqual(self.alerts(new, "drift"), [])
        self.assertEqual(self.alerts(dead, "drift"), [])


class CollectWiringTests(ChecksHome):
    def test_collect_runs_checks(self):
        alive = mock.patch.object(collect, "process_alive", return_value=True)
        alive.start()
        self.addCleanup(alive.stop)
        git = mock.patch.object(collect, "git_head", return_value=None)
        git.start()
        self.addCleanup(git.stop)
        cwd = self.repo("project-A")
        for pid, sid in ((4242, "sess-a1"), (4243, "sess-b1")):
            data = {
                "pid": pid,
                "sessionId": sid,
                "cwd": str(cwd),
                "startedAt": int((NOW - 3600) * 1000),
                "status": "waiting",
                "statusUpdatedAt": int((NOW - 1800) * 1000),
                "kind": "interactive",
                "version": "2.1.292",
                "updatedAt": int((NOW - 1800) * 1000),
            }
            (self.claude / "sessions" / f"{pid}.json").write_text(
                json.dumps(data), encoding="utf-8"
            )
            self.transcript(sid, [prompt("hi"), say(text("ok"), ago=5)], cwd)
        self.put_manifest()
        state = collect.collect()
        kinds = sorted({a.kind for s in state.sessions for a in s.alerts})
        self.assertEqual(kinds, ["collision", "stuck"])
        self.assertEqual([c.kind for c in state.collisions], ["repo"])
        self.assertEqual([d.status for d in state.harness.drift], ["missing"])
        saved = model.load_state(model.save_state(state))
        self.assertEqual(len(saved.harness.drift), 1)

    def put_manifest(self):
        manifest = {
            "generated_at": "2026-01-01T00:00:00Z",
            "head_sha": None,
            "files": {"hooks/gone.mjs": {"repo_path": "r", "sha256": "1" * 64}},
        }
        model.manifest_path().write_text(json.dumps(manifest), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
