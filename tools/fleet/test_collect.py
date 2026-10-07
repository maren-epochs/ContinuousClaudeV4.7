"""Tests for tools/fleet/collect.py and the fleet.py CLI (VAL-805).

Run from the repo root:  py -3.13 -m pytest -q tools/fleet
Every test builds a synthetic ~/.claude under a temp HOME/USERPROFILE; the real
~/.claude is never read or written.
"""

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.fleet import collect, model
from tools.fleet.model import FleetState, Machine, Proposal

FLEET_PY = Path(REPO_ROOT) / "tools" / "fleet" / "fleet.py"
KEY_SECRET = "KEY-SECRET-BYTES-7f3a9c"
TS0 = "2026-10-07T10:00:00.000Z"


def assistant(model_name, usage=None, ts=TS0, content=None, sidechain=False):
    msg = {"model": model_name, "content": content or []}
    if usage is not None:
        msg["usage"] = usage
    return {
        "type": "assistant",
        "timestamp": ts,
        "isSidechain": sidechain,
        "message": msg,
    }


def usage(inp=0, read=0, create=0, out=10):
    return {
        "input_tokens": inp,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": create,
        "output_tokens": out,
    }


def spawn(tool_id, name="Agent", ts=TS0):
    block = {"type": "tool_use", "id": tool_id, "name": name, "input": {}}
    return assistant("claude-opus-5-5", usage(1), ts, [block])


def tool_result(tool_id, async_launched, ts=TS0):
    rec = {
        "type": "user",
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id}],
        },
        "toolUseResult": (
            {"status": "async_launched", "isAsync": True}
            if async_launched
            else {"status": "completed"}
        ),
    }
    return rec


def notification(tool_id, status="completed", ts=TS0):
    body = (
        f"<task-notification><task-id>a1</task-id><tool-use-id>{tool_id}"
        f"</tool-use-id><status>{status}</status></task-notification>"
    )
    return {
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": ts,
        "content": body,
    }


class FleetHome(unittest.TestCase):
    """Base: temp HOME with ~/.claude, every pid treated as alive."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        env = mock.patch.dict(
            os.environ, {"HOME": str(self.root), "USERPROFILE": str(self.root)}
        )
        env.start()
        os.environ.pop("CLAUDE_CONTEXT_WINDOW", None)
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)
        self.claude = self.root / ".claude"
        (self.claude / "sessions").mkdir(parents=True)
        alive = mock.patch.object(collect, "process_alive", return_value=True)
        self.alive = alive.start()
        self.addCleanup(alive.stop)
        self.real_git_head = collect.git_head
        git = mock.patch.object(collect, "git_head", return_value=None)
        git.start()
        self.addCleanup(git.stop)
        self.pct_tmp = self.root / "tmp"
        self.pct_tmp.mkdir()
        pct = mock.patch.object(collect, "pct_dir", return_value=self.pct_tmp)
        pct.start()
        self.addCleanup(pct.stop)

    def pct_file(self, sid8, value, age_s=0.0) -> Path:
        path = self.pct_tmp / f"claude-context-pct-{sid8}.txt"
        path.write_text(value, encoding="utf-8")
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
        return path

    def project(self, name="project-A") -> Path:
        cwd = self.root / "work" / name
        cwd.mkdir(parents=True, exist_ok=True)
        return cwd

    def session(self, pid=4242, sid="sess-a1", cwd=None, **fields) -> Path:
        cwd = cwd or self.project()
        data = {
            "pid": pid,
            "sessionId": sid,
            "cwd": str(cwd),
            "startedAt": 1791230842108,
            "procStart": "134357044404838550",
            "status": "idle",
            "kind": "interactive",
            "version": "2.1.292",
            "updatedAt": 1791230900000,
            "name": "work on A",
        }
        data.update(fields)
        data = {k: v for k, v in data.items() if v is not None}
        path = self.claude / "sessions" / f"{pid}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def transcript(self, cwd, sid, records, slug=None, prefix="") -> Path:
        slug = slug or re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
        path = self.claude / "projects" / slug / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = "".join(json.dumps(r) + "\n" for r in records)
        path.write_text(prefix + lines, encoding="utf-8")
        return path

    def collect_one(self):
        state = collect.collect()
        self.assertEqual(len(state.sessions), 1, state.extra.get("warnings"))
        return state.sessions[0]


class SessionFileTests(FleetHome):
    def test_reads_only_digit_json_never_key(self):
        self.session(4242)
        sessions = self.claude / "sessions"
        (sessions / "4242.0a1b2c3d4e.key").write_text(KEY_SECRET, encoding="utf-8")
        for name in ("notes.json", "12a.json", "4242.json.bak", "77.key"):
            fake = {"pid": 77, "sessionId": KEY_SECRET, "cwd": KEY_SECRET}
            (sessions / name).write_text(json.dumps(fake), encoding="utf-8")
        opened = []
        real = collect.read_text

        def spy(path, *a, **kw):
            opened.append(Path(path).name)
            return real(path, *a, **kw)

        with mock.patch.object(collect, "read_text", side_effect=spy):
            state = collect.collect()
        path = model.save_state(state)
        self.assertEqual([s.pid for s in state.sessions], [4242])
        self.assertNotIn(KEY_SECRET, state.to_json())
        self.assertNotIn(KEY_SECRET, path.read_text(encoding="utf-8"))
        self.assertFalse([n for n in opened if n.endswith(".key")], opened)
        self.assertIn("4242.json", opened)

    def test_session_fields(self):
        cwd = self.project("project-A")
        self.session(4242, "sess-a1", cwd, status="waiting", waitingFor="input")
        s = self.collect_one()
        self.assertEqual(s.pid, 4242)
        self.assertEqual(s.session_id, "sess-a1")
        self.assertEqual(s.project, "project-A")
        self.assertEqual(s.cwd, str(cwd))
        self.assertEqual(s.status, "waiting")
        self.assertEqual(s.kind, "interactive")
        self.assertEqual(s.version, "2.1.292")
        self.assertEqual(s.name, "work on A")
        self.assertTrue(s.alive)
        self.assertEqual(s.started_at, "2026-10-05T20:07:22Z")
        self.assertEqual(s.updated_at, "2026-10-05T20:08:20Z")
        self.assertEqual(s.extra.get("waiting_for"), "input")
        self.assertEqual([a for a in s.alerts if a.kind == "schema_unknown"], [])
        self.alive.assert_called_with(4242, "134357044404838550")

    def test_string_timestamps_keep_source_form(self):
        self.session(5, startedAt="2026-10-07T09:00:00Z", updatedAt=None)
        s = self.collect_one()
        self.assertEqual(s.started_at, "2026-10-07T09:00:00Z")
        self.assertIsNone(s.updated_at)

    def test_dead_pid_is_not_alive(self):
        self.session(4242)
        self.alive.return_value = False
        self.assertFalse(self.collect_one().alive)

    def test_missing_keys_are_null_with_schema_unknown_alert(self):
        path = self.claude / "sessions" / "31.json"
        path.write_text(json.dumps({"pid": 31, "cwd": str(self.project())}), "utf-8")
        s = self.collect_one()
        self.assertIsNone(s.session_id)
        self.assertIsNone(s.status)
        self.assertIsNone(s.version)
        self.assertEqual(len(s.alerts), 1)
        alert = s.alerts[0]
        self.assertEqual(alert.kind, "schema_unknown")
        self.assertIn("sessionId", alert.detail)
        self.assertIn("version", alert.detail)
        self.assertEqual(alert.evidence, "31.json")

    def test_wrong_types_become_null(self):
        self.session(8, sid=12, status=["x"], startedAt=True, version=2.1)
        s = self.collect_one()
        self.assertEqual(s.pid, 8)
        self.assertIsNone(s.session_id)
        self.assertIsNone(s.status)
        self.assertIsNone(s.started_at)
        self.assertIsNone(s.version)

    def test_pid_falls_back_to_file_name(self):
        path = self.claude / "sessions" / "123.json"
        path.write_text(json.dumps({"pid": "x", "cwd": "/w/p"}), encoding="utf-8")
        self.assertEqual(self.collect_one().pid, 123)

    def test_invalid_json_is_a_warning_not_a_crash(self):
        self.session(4242)
        (self.claude / "sessions" / "99.json").write_text("{oops", encoding="utf-8")
        (self.claude / "sessions" / "98.json").write_text("[1, 2]", encoding="utf-8")
        state = collect.collect()
        self.assertEqual([s.pid for s in state.sessions], [4242])
        warnings = state.extra["warnings"]
        self.assertEqual(len(warnings), 2, warnings)
        self.assertTrue(any("99.json" in w for w in warnings))

    def test_no_sessions_dir(self):
        (self.claude / "sessions").rmdir()
        state = collect.collect()
        self.assertEqual(state.sessions, [])
        self.assertEqual(state.extra["warnings"], [])


class ProcessAliveTests(unittest.TestCase):
    def test_own_pid_is_alive(self):
        self.assertTrue(collect.process_alive(os.getpid(), None))

    def test_bad_pids_are_dead(self):
        for pid in (None, 0, -5, "12"):
            self.assertFalse(collect.process_alive(pid, None), pid)

    def test_exited_process_is_dead(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(collect.process_alive(proc.pid, None))

    @unittest.skipUnless(sys.platform == "win32", "procStart is a Windows FILETIME")
    def test_proc_start_must_match(self):
        start = collect.process_start(os.getpid())
        self.assertIsInstance(start, int)
        self.assertTrue(collect.process_alive(os.getpid(), str(start)))
        self.assertFalse(collect.process_alive(os.getpid(), str(start + 1)))
        self.assertTrue(collect.process_alive(os.getpid(), "not-a-number"))


class TranscriptTests(FleetHome):
    def test_model_context_and_last_activity(self):
        cwd = self.project()
        self.session(4242, "sess-a1", cwd)
        self.transcript(
            cwd,
            "sess-a1",
            [
                assistant("claude-old", usage(5), "2026-10-07T10:00:00.000Z"),
                {"type": "user", "timestamp": "2026-10-07T10:01:00.000Z"},
                assistant(
                    "claude-opus-5-5",
                    usage(1000, 50000, 9000),
                    "2026-10-07T10:02:00.000Z",
                ),
                assistant(
                    "claude-side", usage(190000), "2026-10-07T10:03:00.000Z", None, True
                ),
                assistant("<synthetic>", None, "2026-10-07T10:03:30.000Z"),
                {"type": "attachment", "timestamp": "2026-10-07T10:04:00.000Z"},
            ],
        )
        s = self.collect_one()
        self.assertEqual(s.model, "claude-opus-5-5")
        self.assertEqual(s.context_pct, 30.0)
        self.assertEqual(s.last_activity, "2026-10-07T10:04:00.000Z")
        self.assertEqual(s.agents_running, 0)
        self.assertEqual(s.alerts, [])
        self.assertTrue(s.extra["transcript"].endswith("sess-a1.jsonl"))

    def test_context_window_rule(self):
        self.assertEqual(collect.context_pct(usage(1000, 59000), {}), 30.0)
        self.assertEqual(collect.context_pct(usage(199999), {}), 99.0)
        self.assertEqual(collect.context_pct(usage(250000), {}), 25.0)
        self.assertEqual(collect.context_pct(usage(5_000_000), {}), 100.0)
        env = {"CLAUDE_CONTEXT_WINDOW": "500000"}
        self.assertEqual(collect.context_pct(usage(100000), env), 20.0)
        self.assertEqual(
            collect.context_pct(usage(1), {"CLAUDE_CONTEXT_WINDOW": "x"}), 0
        )
        self.assertEqual(collect.context_pct({"input_tokens": "9"}, {}), 0.0)
        self.assertIsNone(collect.context_pct("nope", {}))

    def test_spawning_session_window_env_is_never_applied(self):
        cwd = self.project()
        self.session(4242, "sess-a1", cwd)
        self.transcript(cwd, "sess-a1", [assistant("m", usage(100000))])
        with mock.patch.dict(os.environ, {"CLAUDE_CONTEXT_WINDOW": "1000000"}):
            s = self.collect_one()
        self.assertEqual(s.context_pct, 50.0)
        self.assertEqual(s.extra["context_source"], "transcript")


class ContextSourceTests(FleetHome):
    """Fresh statusline pct file (status.mjs used_percentage) beats the transcript."""

    SID = "abcdefgh-0000-4000-8000-tail"

    def one_m_session(self):
        cwd = self.project()
        self.session(4242, self.SID, cwd)
        # 172K tokens: the transcript rule assumes a 200K window -> 86%.
        self.transcript(cwd, self.SID, [assistant("m", usage(2000, 170000))])

    def test_fresh_statusline_pct_is_preferred(self):
        self.one_m_session()
        self.pct_file("abcdefgh", "17", age_s=30)
        s = self.collect_one()
        self.assertEqual(s.context_pct, 17.0)
        self.assertEqual(s.extra["context_source"], "statusline")

    def test_stale_pct_file_falls_back_to_transcript(self):
        self.one_m_session()
        self.pct_file("abcdefgh", "17", age_s=collect.PCT_FRESH_S + 60)
        s = self.collect_one()
        self.assertEqual(s.context_pct, 86.0)
        self.assertEqual(s.extra["context_source"], "transcript")

    def age_transcript(self, age_s):
        path = self.claude / "projects"
        (transcript,) = path.glob(f"*/{self.SID}.jsonl")
        stamp = time.time() - age_s
        os.utime(transcript, (stamp, stamp))
        return transcript

    def test_stale_pct_file_is_trusted_while_session_idle(self):
        self.one_m_session()
        stale = collect.PCT_FRESH_S + 3600
        self.age_transcript(stale + 60)
        self.pct_file("abcdefgh", "17", age_s=stale)
        s = self.collect_one()
        self.assertEqual(s.context_pct, 17.0)
        self.assertEqual(s.extra["context_source"], "statusline-idle")

    def test_idle_rule_accepts_equal_mtimes(self):
        self.one_m_session()
        transcript = self.age_transcript(collect.PCT_FRESH_S + 120)
        pct = self.pct_file("abcdefgh", "17")
        stamp = transcript.stat().st_mtime
        os.utime(pct, (stamp, stamp))
        self.assertEqual(self.collect_one().extra["context_source"], "statusline-idle")

    def test_stale_pct_file_loses_to_newer_transcript(self):
        self.one_m_session()
        self.age_transcript(collect.PCT_FRESH_S + 60)
        self.pct_file("abcdefgh", "17", age_s=collect.PCT_FRESH_S + 120)
        s = self.collect_one()
        self.assertEqual(s.context_pct, 86.0)
        self.assertEqual(s.extra["context_source"], "transcript")

    def test_stale_pct_file_without_transcript_is_not_trusted(self):
        self.session(4242, self.SID, self.project())
        self.pct_file("abcdefgh", "42", age_s=collect.PCT_FRESH_S + 60)
        s = self.collect_one()
        self.assertIsNone(s.context_pct)
        self.assertNotIn("context_source", s.extra)

    def test_statusline_reading_direct(self):
        now = time.time()
        self.pct_file("abcdefgh", "33", age_s=collect.PCT_FRESH_S + 60)
        old = now - collect.PCT_FRESH_S - 120
        new = now - 10
        read = collect.statusline_reading
        self.assertEqual(
            read("abcdefgh-1", now, self.pct_tmp, old), (33.0, "statusline-idle")
        )
        self.assertIsNone(read("abcdefgh-1", now, self.pct_tmp, new))
        self.assertIsNone(read("abcdefgh-1", now, self.pct_tmp, None))
        self.pct_file("abcdefgh", "33", age_s=5)
        self.assertEqual(
            read("abcdefgh-1", now, self.pct_tmp, new), (33.0, "statusline")
        )

    def test_future_mtime_pct_file_is_not_fresh(self):
        self.one_m_session()
        self.pct_file("abcdefgh", "17", age_s=-3600)
        self.assertEqual(self.collect_one().extra["context_source"], "transcript")

    def test_pct_file_is_keyed_by_the_8_char_session_prefix(self):
        self.one_m_session()
        self.pct_file("abcdefgX", "17")
        self.assertEqual(self.collect_one().context_pct, 86.0)

    def test_garbage_pct_file_falls_back(self):
        self.one_m_session()
        for body in ("", "x", "NaN", "1e3", "-4", "12.5"):
            with self.subTest(body=body):
                self.pct_file("abcdefgh", body)
                self.assertEqual(self.collect_one().context_pct, 86.0)

    def test_pct_is_capped_at_100(self):
        self.one_m_session()
        self.pct_file("abcdefgh", "250")
        self.assertEqual(self.collect_one().context_pct, 100.0)

    def test_statusline_pct_without_transcript(self):
        self.session(4242, self.SID, self.project())
        self.pct_file("abcdefgh", "42")
        s = self.collect_one()
        self.assertEqual(s.context_pct, 42.0)
        self.assertEqual(s.extra["context_source"], "statusline")

    def test_no_source_records_nothing(self):
        self.session(4242, self.SID, self.project())
        s = self.collect_one()
        self.assertIsNone(s.context_pct)
        self.assertNotIn("context_source", s.extra)

    def test_statusline_pct_direct(self):
        now = time.time()
        self.pct_file("abcdefgh", "33\n", age_s=5)
        self.assertEqual(collect.statusline_pct("abcdefgh-1", now, self.pct_tmp), 33.0)
        self.assertIsNone(collect.statusline_pct("../../x", now, self.pct_tmp))
        self.assertIsNone(collect.statusline_pct(None, now, self.pct_tmp))
        self.assertIsNone(collect.statusline_pct("zzzzzzzz", now, self.pct_tmp))
        self.assertEqual(collect.PCT_FRESH_S, 600)

    def test_agents_running_is_spawned_minus_finished(self):
        cwd = self.project()
        self.session(4242, "sess-a1", cwd)
        self.transcript(
            cwd,
            "sess-a1",
            [
                spawn("tu-a"),
                tool_result("tu-a", async_launched=True),
                spawn("tu-b", name="Task"),
                tool_result("tu-b", async_launched=False),
                spawn("tu-c"),
                tool_result("tu-c", async_launched=True),
                spawn("tu-d"),
                tool_result("tu-d", async_launched=True),
                notification("tu-a"),
                notification("tu-a"),
                notification("tu-d", status="failed"),
                notification("tu-old"),
                {"type": "attachment", "content": notification("tu-c", "running")},
                assistant("m", usage(1), content=[{"type": "tool_use", "id": "x"}]),
            ],
        )
        self.assertEqual(self.collect_one().agents_running, 1)

    def test_sidechain_spawns_are_not_counted(self):
        cwd = self.project()
        self.session(4242, "sess-a1", cwd)
        rec = spawn("tu-s")
        rec["isSidechain"] = True
        self.transcript(cwd, "sess-a1", [rec])
        self.assertEqual(self.collect_one().agents_running, 0)

    def test_reads_only_the_tail(self):
        cwd = self.project()
        self.session(4242, "sess-a1", cwd)
        early = assistant("claude-early", usage(150000))
        filler = {"type": "user", "message": {"content": "x" * 4000}}
        n = collect.TAIL_BYTES // 4000 + 50
        path = self.transcript(cwd, "sess-a1", [early] + [filler] * n)
        self.assertGreater(path.stat().st_size, collect.TAIL_BYTES)
        calls = []
        real = collect.read_tail

        def spy(p, max_bytes):
            calls.append((Path(p).name, max_bytes))
            return real(p, max_bytes)

        with mock.patch.object(collect, "read_tail", side_effect=spy):
            s = self.collect_one()
        self.assertIsNone(s.model)
        self.assertIsNone(s.context_pct)
        self.assertIn(("sess-a1.jsonl", collect.TAIL_BYTES), calls)
        self.assertEqual(s.alerts, [])

    def test_read_tail_drops_partial_first_line(self):
        path = Path(self._tmp.name) / "t.jsonl"
        path.write_bytes(b'{"a": 1}\n{"b": 2}\n{"c": 3}\n')
        self.assertEqual(collect.read_tail(path, 12), '{"c": 3}\n')
        self.assertEqual(collect.read_tail(path, 1000), path.read_text())
        self.assertEqual(collect.read_tail(path.with_name("missing"), 10), "")

    def test_transcript_found_outside_slug_dir(self):
        cwd = self.project()
        self.session(4242, "sess-a1", cwd)
        self.transcript(
            cwd, "sess-a1", [assistant("claude-x", usage(1))], slug="other-slug"
        )
        self.assertEqual(self.collect_one().model, "claude-x")

    def test_unsafe_session_id_is_not_globbed(self):
        cwd = self.project()
        self.session(4242, "*", cwd)
        self.transcript(cwd, "anything", [assistant("claude-x", usage(1))])
        s = self.collect_one()
        self.assertIsNone(s.model)
        self.assertNotIn("transcript", s.extra)

    def test_missing_transcript_leaves_nulls(self):
        self.session(4242)
        s = self.collect_one()
        self.assertIsNone(s.model)
        self.assertIsNone(s.context_pct)
        self.assertIsNone(s.last_activity)
        self.assertEqual(s.agents_running, 0)

    def test_garbage_lines_are_skipped(self):
        cwd = self.project()
        self.session(4242, "sess-a1", cwd)
        good = json.dumps(assistant("claude-x", usage(2000)))
        path = self.transcript(cwd, "sess-a1", [])
        path.write_text(f"not json\n[1]\n{good}\n{{broken", encoding="utf-8")
        s = self.collect_one()
        self.assertEqual(s.model, "claude-x")
        self.assertEqual(s.context_pct, 1.0)

    def test_schema_unknown_when_usage_vanishes(self):
        cwd = self.project()
        self.session(4242, "sess-a1", cwd)
        self.transcript(cwd, "sess-a1", [assistant("claude-x"), assistant("claude-y")])
        s = self.collect_one()
        self.assertEqual(s.model, "claude-y")
        kinds = [a.kind for a in s.alerts if a.kind != "compliance"]
        self.assertEqual(kinds, ["schema_unknown"])
        self.assertIn("usage", s.alerts[0].detail)
        self.assertEqual(s.alerts[0].evidence, "sess-a1.jsonl")

    def test_schema_unknown_when_type_vanishes(self):
        cwd = self.project()
        self.session(4242, "sess-a1", cwd)
        self.transcript(cwd, "sess-a1", [{"kind": "assistant"}, {"kind": "user"}])
        s = self.collect_one()
        self.assertEqual([a.kind for a in s.alerts], ["schema_unknown"])
        self.assertIn("type", s.alerts[0].detail)


class HandoffTests(FleetHome):
    def write(self, path: Path, text: str, age_h: float) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        t = time.time() - age_h * 3600
        os.utime(path, (t, t))
        return path

    def test_project_root_newest_by_mtime(self):
        cwd = self.project()
        root = cwd / "thoughts" / "shared" / "handoffs"
        self.write(root / "old.yaml", "goal: old goal\nnow: old\n", 10)
        newest = self.write(
            root / "sub" / "new.yml", 'goal: "ship collector"\nnow: tests\n', 2
        )
        self.write(root / "notes.txt", "goal: not a handoff\n", 0)
        home_root = self.claude / "handoffs" / "project-A"
        self.write(home_root / "home.yaml", "goal: home\n", 0)
        self.session(4242, cwd=cwd)
        h = self.collect_one().handoff
        self.assertEqual(h.path, str(newest))
        self.assertEqual(h.goal, "ship collector")
        self.assertEqual(h.now, "tests")
        self.assertAlmostEqual(h.age_h, 2.0, delta=0.05)

    def test_home_root_fallback_and_heading_goal(self):
        cwd = self.project("project-B")
        root = self.claude / "handoffs" / "project-B"
        self.write(root / "h.md", "# Handoff: Build fleet\n\nbody\n", 1)
        self.session(4242, cwd=cwd)
        h = self.collect_one().handoff
        self.assertEqual(h.goal, "Build fleet")
        self.assertIsNone(h.now)

    def test_topic_is_a_goal_fallback(self):
        cwd = self.project()
        root = cwd / "thoughts" / "shared" / "handoffs"
        self.write(root / "h.yaml", "topic: research\nnow: reading\n", 1)
        self.session(4242, cwd=cwd)
        self.assertEqual(self.collect_one().handoff.goal, "research")

    def test_no_handoff_is_null(self):
        self.session(4242)
        self.assertIsNone(self.collect_one().handoff)


class HarnessTests(FleetHome):
    def manifest(self, **fields):
        data = {
            "schema_version": 1,
            "generated_at": "2026-10-07T12:00:00Z",
            "repo": str(self.root / "repo"),
            "head_sha": "a" * 40,
            "dirty": False,
            "files": {},
        }
        data.update(fields)
        (self.root / "repo").mkdir(exist_ok=True)
        model.manifest_path().write_text(json.dumps(data), encoding="utf-8")

    def test_in_sync(self):
        self.manifest()
        with mock.patch.object(collect, "git_head", return_value="a" * 40) as head:
            h = collect.collect().harness
        head.assert_called_once_with(str(self.root / "repo"))
        self.assertEqual(h.repo, str(self.root / "repo"))
        self.assertEqual(h.head_sha, "a" * 40)
        self.assertEqual(h.installed_sha, "a" * 40)
        self.assertEqual(h.drift, [])
        self.assertIs(h.extra["in_sync"], True)
        self.assertIs(h.extra["dirty"], False)
        self.assertEqual(h.extra["synced_at"], "2026-10-07T12:00:00Z")

    def test_behind(self):
        self.manifest(dirty=True)
        with mock.patch.object(collect, "git_head", return_value="b" * 40):
            h = collect.collect().harness
        self.assertIs(h.extra["in_sync"], False)
        self.assertIs(h.extra["dirty"], True)

    def test_no_manifest(self):
        h = collect.collect().harness
        self.assertIsNone(h.repo)
        self.assertIsNone(h.installed_sha)
        self.assertIsNone(h.head_sha)
        self.assertIsNone(h.extra["in_sync"])

    def test_corrupt_manifest(self):
        model.manifest_path().write_text('{"repo": 5, "head_sha": [', "utf-8")
        state = collect.collect()
        self.assertIsNone(state.harness.repo)
        self.assertTrue(any("manifest" in w for w in state.extra["warnings"]))

    def test_git_head_of_this_repo(self):
        sha = self.real_git_head(REPO_ROOT)
        self.assertRegex(sha or "", r"^[0-9a-f]{40}$")
        self.assertIsNone(self.real_git_head(str(self.root / "nope")))


class AuditTests(FleetHome):
    def events(self, n, path=None):
        path = path or model.audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            fh.write("not json\n\n")
            for i in range(n):
                ev = {"ts": f"t{i:05d}", "category": "force-push", "tool": "Bash"}
                fh.write(json.dumps(ev) + "\n")
        return path

    def test_recent_events_newest_last(self):
        self.events(200)
        recent = collect.collect().audit_recent
        self.assertEqual(len(recent), collect.AUDIT_RECENT)
        self.assertEqual(recent[-1].ts, "t00199")
        self.assertEqual(recent[0].ts, f"t{200 - collect.AUDIT_RECENT:05d}")

    def test_read_is_bounded(self):
        self.events(10)
        calls = []
        real = collect.read_tail

        def spy(p, max_bytes):
            calls.append((Path(p).name, max_bytes))
            return real(p, max_bytes)

        with (
            mock.patch.object(collect, "read_tail", side_effect=spy),
            mock.patch.object(Path, "read_text", side_effect=AssertionError),
        ):
            recent = collect.audit_recent(model.audit_path())
        self.assertEqual(len(recent), 10)
        self.assertEqual(calls, [("audit.jsonl", collect.AUDIT_TAIL_BYTES)])

    def test_rotates_over_5_mb(self):
        path = self.events(100000)
        size = path.stat().st_size
        self.assertGreater(size, 5 * 1024 * 1024)
        state = collect.collect()
        self.assertEqual(state.audit_recent[-1].ts, "t99999")
        rotated = path.with_name("audit.jsonl.1")
        self.assertEqual(rotated.stat().st_size, size)
        self.assertFalse(path.exists())

    def test_small_log_is_not_rotated(self):
        path = self.events(10)
        collect.collect()
        self.assertTrue(path.exists())
        self.assertFalse(path.with_name("audit.jsonl.1").exists())

    def test_no_audit_log(self):
        self.assertEqual(collect.collect().audit_recent, [])


class StateTests(FleetHome):
    def test_inbox_counts_pending_only(self):
        for i, status in enumerate(("pending", "pending", "rejected")):
            model.save_proposal(Proposal(id=f"p{i}", status=status))
        self.assertEqual(collect.collect().inbox_count, 2)

    def test_never_emits_nan_or_infinity(self):
        cwd = self.project()
        self.session(4242, "sess-a1", cwd)
        self.transcript(cwd, "sess-a1", [assistant("m", usage(10))])
        bad = Machine(mem_total_gb=math.nan, mem_free_gb=math.inf)
        with (
            mock.patch.object(collect, "machine_memory", return_value=bad),
            mock.patch.object(collect, "context_pct", return_value=math.nan),
        ):
            path = collect.collect_and_save()

        def reject(name):
            raise AssertionError(name)

        data = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject)
        self.assertIsNone(data["machine"]["mem_total_gb"])
        self.assertIsNone(data["machine"]["mem_free_gb"])
        self.assertIsNone(data["sessions"][0]["context_pct"])

    def test_save_is_atomic_and_round_trips(self):
        self.session(4242)
        with mock.patch.object(model, "write_atomic", wraps=model.write_atomic) as wa:
            path = collect.collect_and_save()
        self.assertEqual(path, model.state_path())
        wa.assert_called_once()
        self.assertEqual(list(path.parent.glob("*.tmp")), [])
        state = model.load_state()
        self.assertEqual(state.schema_version, 1)
        self.assertEqual([s.pid for s in state.sessions], [4242])
        self.assertRegex(state.generated_at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIsInstance(state.extra["collect_s"], float)

    def test_sessions_sorted_alive_first_then_newest(self):
        self.session(1, "s-old", startedAt=1791000000000)
        self.session(2, "s-new", startedAt=1791300000000)
        self.session(3, "s-dead", startedAt=1791400000000)
        self.alive.side_effect = lambda pid, start: pid != 3
        ids = [s.session_id for s in collect.collect().sessions]
        self.assertEqual(ids, ["s-new", "s-old", "s-dead"])

    def test_full_collect_under_5_seconds(self):
        filler = {"type": "user", "message": {"content": "y" * 2000}}
        records = [assistant("m", usage(1000))] + [filler] * 1000
        for i in range(15):
            cwd = self.project(f"project-{i}")
            self.session(1000 + i, f"sess-{i}", cwd)
            self.transcript(cwd, f"sess-{i}", records + [spawn(f"tu-{i}")])
        t = time.perf_counter()
        state = collect.collect()
        elapsed = time.perf_counter() - t
        self.assertEqual(len(state.sessions), 15)
        self.assertLess(elapsed, 5.0)


class DeadlineTests(FleetHome):
    """collect aborts after COLLECT_DEADLINE_S and never replaces a good state.json."""

    def good_state(self) -> str:
        path = model.save_state(FleetState(generated_at="2026-01-01T00:00:00Z"))
        return path.read_text(encoding="utf-8")

    def test_default_deadline_is_20_s(self):
        self.assertEqual(collect.COLLECT_DEADLINE_S, 20.0)

    def test_slow_collect_raises_and_keeps_last_good_state(self):
        before = self.good_state()
        for i in range(5):
            self.session(1000 + i, f"sess-{i}", self.project(f"project-{i}"))
        real = collect.build_session

        def slow(*args, **kwargs):
            time.sleep(0.1)
            return real(*args, **kwargs)

        with (
            mock.patch.object(collect, "build_session", side_effect=slow),
            mock.patch.object(collect, "_hard_exit") as hard,
            self.assertRaises(collect.CollectTimeout),
        ):
            collect.collect_and_save(deadline_s=0.15)
        hard.assert_not_called()
        self.assertEqual(model.state_path().read_text(encoding="utf-8"), before)

    def test_watchdog_is_cancelled_after_a_normal_collect(self):
        self.session(4242)
        with mock.patch.object(collect, "_hard_exit") as hard:
            collect.collect(deadline_s=5)
            for t in threading.enumerate():
                if t.name == collect.WATCHDOG_NAME:
                    t.join(2)
            alive = [
                t
                for t in threading.enumerate()
                if t.name == collect.WATCHDOG_NAME and t.is_alive()
            ]
        self.assertEqual(alive, [])
        hard.assert_not_called()

    def test_hung_collect_is_hard_killed_by_the_watchdog(self):
        before = self.good_state()
        script = (
            "import sys, time\n"
            f"sys.path.insert(0, {REPO_ROOT!r})\n"
            "from tools.fleet import collect\n"
            "collect.WATCHDOG_GRACE_S = 0.1\n"
            "collect.collect_sessions = lambda *a, **k: time.sleep(60)\n"
            "collect.collect_and_save(deadline_s=0.3)\n"
            "print('not reached')\n"
        )
        t = time.perf_counter()
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=dict(os.environ),
            timeout=30,
        )
        elapsed = time.perf_counter() - t
        self.assertEqual(out.returncode, collect.WATCHDOG_EXIT, out.stderr)
        self.assertNotIn("not reached", out.stdout)
        self.assertLess(elapsed, 15)
        self.assertEqual(model.state_path().read_text(encoding="utf-8"), before)


class MachineTests(unittest.TestCase):
    @unittest.skipUnless(
        sys.platform == "win32" or Path("/proc/meminfo").exists(), "no memory source"
    )
    def test_memory_snapshot(self):
        m = collect.machine_memory()
        self.assertGreater(m.mem_total_gb, 0)
        self.assertGreaterEqual(m.mem_free_gb, 0)
        self.assertLessEqual(m.mem_free_gb, m.mem_total_gb)


class CliTests(FleetHome):
    def run_cli(self, *args):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, str(FLEET_PY), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=60,
        )

    def test_collect_json_report(self):
        cwd = self.project("project-A")
        self.session(os.getpid(), "sess-a1", cwd, procStart=None)
        (self.claude / "sessions" / "1.0a1b.key").write_text(KEY_SECRET, "utf-8")
        out = self.run_cli("collect")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("1 session", out.stdout)
        self.assertIn("1 alive", out.stdout)
        self.assertNotIn(KEY_SECRET, out.stdout + out.stderr)
        self.assertTrue(model.state_path().exists())

        out = self.run_cli("json")
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)
        self.assertEqual(data["sessions"][0]["session_id"], "sess-a1")

        out = self.run_cli("report")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("project-A", out.stdout)
        self.assertIn("sessions: 1 (1 alive)", out.stdout)

    def test_report_collects_when_state_missing(self):
        out = self.run_cli("report")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("sessions: 0", out.stdout)
        self.assertTrue(model.state_path().exists())

    def test_bad_command(self):
        out = self.run_cli("bogus")
        self.assertEqual(out.returncode, 2)


if __name__ == "__main__":
    unittest.main()
