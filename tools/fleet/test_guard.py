"""Memory guard: away test, browser close, session candidates, one run, reporting."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from tools.fleet import guard, model

NOW = 1_800_000_000.0


class GuardHome(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        env = mock.patch.dict(
            os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        )
        env.start()
        self.addCleanup(env.stop)
        (self.home / ".claude" / "fleet").mkdir(parents=True)

    def config(self, **values):
        guard.config_path().write_text(json.dumps(values), encoding="utf-8")

    def readings(self, free=0.5, idle=120.0, hour=5, processes=(30, 2)):
        for target, value in (
            ("free_gb", free),
            ("idle_minutes", idle),
            ("local_hour", hour),
            ("browser_processes", processes),
            ("window_closing", False),
        ):
            patcher = mock.patch.object(guard, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)


class ConfigTests(GuardHome):
    def test_defaults_without_a_file(self):
        cfg = guard.load_config()
        self.assertEqual((cfg.hold_gb, cfg.browser_gb), (1.0, 2.4))
        self.assertEqual(cfg.protected, ())

    def test_file_overrides_and_bad_values_are_ignored(self):
        self.config(hold_gb=2, protected=["Amber"], browser="x; rm", idle_min="soon")
        cfg = guard.load_config()
        self.assertEqual(cfg.hold_gb, 2.0)
        self.assertEqual(cfg.protected, ("amber",))
        self.assertEqual(cfg.browser, "brave")
        self.assertEqual(cfg.idle_min, 60.0)


class AwayTests(unittest.TestCase):
    cfg = guard.Config()

    def test_window_edges(self):
        self.assertFalse(guard.user_away(120, 3, False, self.cfg))
        self.assertTrue(guard.user_away(120, 4, False, self.cfg))
        self.assertTrue(guard.user_away(120, 9, False, self.cfg))
        self.assertFalse(guard.user_away(120, 10, False, self.cfg))

    def test_away_mode_replaces_the_window_not_the_idle_test(self):
        self.assertTrue(guard.user_away(120, 15, True, self.cfg))
        self.assertFalse(guard.user_away(10, 15, True, self.cfg))

    def test_recent_or_unknown_input_means_present(self):
        self.assertFalse(guard.user_away(59, 5, False, self.cfg))
        self.assertFalse(guard.user_away(None, 5, True, self.cfg))


class HoldTests(GuardHome):
    def test_reason_only_under_the_level(self):
        with mock.patch.object(guard, "free_gb", return_value=0.5):
            self.assertIn("0.50 GB free", guard.hold_reason())
        with mock.patch.object(guard, "free_gb", return_value=1.0):
            self.assertIsNone(guard.hold_reason())
        with mock.patch.object(guard, "free_gb", return_value=None):
            self.assertIsNone(guard.hold_reason())


class CloseBrowserTests(unittest.TestCase):
    def close(self, seen):
        calls, sleeps = [], []
        result = guard.close_browser(
            "brave",
            processes=mock.Mock(side_effect=seen),
            run=lambda script: calls.append(script),
            sleep=sleeps.append,
        )
        return result, calls, sleeps

    def test_closes_windows_then_ends_leftovers(self):
        result, calls, sleeps = self.close([(30, 2), (5, 0), (5, 0)])
        self.assertEqual(result, {"outcome": "closed", "leftovers": 5})
        self.assertIn("CloseMainWindow", calls[0])
        self.assertIn("Stop-Process", calls[1])
        self.assertIn(guard.LEFTOVER_GRACE_S, sleeps)

    def test_clean_exit_stops_nothing(self):
        result, calls, _ = self.close([(30, 1), (0, 0)])
        self.assertEqual(result, {"outcome": "closed", "leftovers": 0})
        self.assertEqual(len(calls), 1)

    def test_a_window_that_stays_is_never_killed(self):
        rounds = int(guard.CLOSE_WAIT_S / guard.CLOSE_ROUND_S) + 1
        result, calls, _ = self.close([(30, 1)] * rounds)
        self.assertEqual(result, {"outcome": "windows_left", "windows": 1})
        self.assertFalse([c for c in calls if "Stop-Process" in c])

    def test_a_window_reopened_during_the_grace_stops_the_cleanup(self):
        result, calls, _ = self.close([(30, 1), (5, 0), (9, 1)])
        self.assertEqual(result["outcome"], "windows_left")
        self.assertFalse([c for c in calls if "Stop-Process" in c])

    def test_failed_check_does_nothing_more(self):
        result, calls, _ = self.close([None])
        self.assertEqual(result, {"outcome": "check_failed"})
        self.assertEqual(calls, [])


def _session(**kw):
    values = {
        "alive": True,
        "project": "alpha",
        "kind": "interactive",
        "status": "idle",
        "pid": 1,
        "session_id": "s1",
        "last_activity": "2020-01-01T00:00:00Z",
    }
    return model.Session(**{**values, **kw})


class BrowserUseTests(GuardHome):
    def session_with(self, *entries):
        path = self.home / "s.jsonl"
        text = "".join(json.dumps(e) + "\n" for e in entries)
        path.write_text(text, encoding="utf-8")
        session = _session()
        session.extra["transcript"] = str(path)
        return session, path

    @staticmethod
    def tool_use(name, stamp):
        return {
            "type": "assistant",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)),
            "message": {"content": [{"type": "tool_use", "name": name}]},
        }

    def test_recent_browser_tool_counts(self):
        now = time.time()
        session, _ = self.session_with(
            self.tool_use("mcp__claude-in-chrome__navigate", now - 60)
        )
        self.assertEqual(
            guard.browser_in_use([session], now, guard.Config()), ["alpha"]
        )

    def test_old_use_other_tools_and_mere_mentions_do_not_count(self):
        now = time.time()
        session, _ = self.session_with(
            self.tool_use("mcp__claude-in-chrome__navigate", now - 3600),
            self.tool_use("Bash", now - 60),
            {"type": "user", "timestamp": "x", "text": "mcp__claude-in-chrome__x"},
        )
        self.assertEqual(guard.browser_in_use([session], now, guard.Config()), [])

    def test_subagent_use_counts(self):
        now = time.time()
        session, path = self.session_with(self.tool_use("Bash", now - 60))
        sub = path.with_suffix("") / "subagents"
        sub.mkdir(parents=True)
        (sub / "a.jsonl").write_text(
            json.dumps(self.tool_use("mcp__claude-in-chrome__find", now - 60)) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(
            guard.browser_in_use([session], now, guard.Config()), ["alpha"]
        )


class CandidateTests(unittest.TestCase):
    def rows(self, sessions, children=None, **cfg):
        return guard.session_candidates(
            sessions,
            NOW,
            guard.Config(**cfg),
            children=children or (lambda pid: ["node.exe"]),
        )

    def test_idle_and_waiting_sessions_longest_idle_first(self):
        would, skipped = self.rows(
            [
                _session(project="newer", last_activity="2027-01-15T00:00:00Z"),
                _session(project="older", status="waiting"),
            ]
        )
        self.assertEqual([r["project"] for r in would], ["older", "newer"])
        self.assertEqual(skipped, [])

    def test_every_reason_to_skip(self):
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 60))
        _, skipped = self.rows(
            [
                _session(project="Amber"),
                _session(project="bg", kind="bg"),
                _session(project="busy", status="busy"),
                _session(project="agents", agents_running=1),
                _session(project="recent", last_activity=recent),
                _session(project="nopid", pid=None),
                _session(alive=False, project="dead"),
            ],
            protected=("amber",),
        )
        self.assertEqual(
            [(r["project"], r["why"]) for r in skipped],
            [
                ("Amber", "protected"),
                ("bg", "kind bg"),
                ("busy", "status busy"),
                ("agents", "agents running"),
                ("recent", "recently active"),
                ("nopid", "no pid"),
            ],
        )

    def test_children_beyond_mcp_servers_or_a_failed_check_block_it(self):
        _, skipped = self.rows([_session()], children=lambda pid: ["pytest.exe"])
        self.assertEqual(skipped[0]["why"], "running pytest.exe")
        _, skipped = self.rows([_session()], children=lambda pid: None)
        self.assertEqual(skipped[0]["why"], "child check failed")


class TickTests(GuardHome):
    def test_present_user_changes_nothing_and_logs_nothing(self):
        self.readings(idle=5.0)
        with mock.patch.object(guard, "close_browser") as close:
            record = guard.tick(NOW, sessions=[])
        close.assert_not_called()
        self.assertEqual((record["action"], record["why"]), ("none", "user present"))
        self.assertFalse(guard.log_path().exists())
        self.assertTrue(guard.state_path().exists())

    def test_enough_memory_changes_nothing(self):
        self.readings(free=5.0)
        with mock.patch.object(guard, "close_browser") as close:
            record = guard.tick(NOW, sessions=[])
        close.assert_not_called()
        self.assertEqual(record["why"], "memory ok")

    def test_away_and_low_closes_the_browser_and_logs_it(self):
        self.readings(free=2.0)
        with mock.patch.object(
            guard, "close_browser", return_value={"outcome": "closed", "leftovers": 0}
        ) as close:
            record = guard.tick(NOW, sessions=[])
        close.assert_called_once_with("brave")
        self.assertEqual(record["action"], "close_browser")
        logged = json.loads(guard.log_path().read_text(encoding="utf-8"))
        self.assertEqual(logged["outcome"], "closed")

    def test_dry_run_acts_on_nothing_and_writes_nothing(self):
        self.readings(free=2.0)
        with mock.patch.object(guard, "close_browser") as close:
            record = guard.tick(NOW, dry_run=True, sessions=[])
        close.assert_not_called()
        self.assertEqual(record["action"], "would_close_browser")
        self.assertFalse(guard.state_path().exists())
        self.assertFalse(guard.log_path().exists())

    def test_browser_in_use_is_left_open(self):
        self.readings(free=2.0)
        with (
            mock.patch.object(guard, "browser_in_use", return_value=["alpha"]),
            mock.patch.object(guard, "close_browser") as close,
        ):
            record = guard.tick(NOW, sessions=[])
        close.assert_not_called()
        self.assertEqual(record["browser_in_use_by"], ["alpha"])

    def test_sessions_are_only_logged_after_two_low_readings(self):
        self.readings(free=0.5, processes=(0, 0))
        sessions = [_session()]
        with (
            mock.patch.object(guard, "child_names", return_value=[]),
            mock.patch.object(guard.subprocess, "run") as run,
        ):
            first = guard.tick(NOW, sessions=sessions)
            second = guard.tick(NOW, sessions=sessions)
        run.assert_not_called()
        self.assertEqual(first["action"], "none")
        self.assertEqual(second["action"], "would_close_sessions")
        self.assertEqual(second["would_close"][0]["project"], "alpha")

    def test_recovered_memory_resets_the_count(self):
        self.readings(free=0.5, processes=(0, 0))
        guard.tick(NOW, sessions=[])
        guard.free_gb.return_value = 5.0
        guard.tick(NOW, sessions=[])
        guard.free_gb.return_value = 0.5
        record = guard.tick(NOW, sessions=[])
        self.assertEqual(record["low_readings"], 1)

    def test_unknown_memory_does_nothing(self):
        self.readings(free=None)
        record = guard.tick(NOW, sessions=[])
        self.assertEqual(record["why"], "memory unknown")

    def test_failed_browser_check_closes_nothing(self):
        self.readings(free=0.5, processes=None)
        with mock.patch.object(guard, "close_browser") as close:
            record = guard.tick(NOW, sessions=[])
        close.assert_not_called()
        self.assertEqual(record["why"], "browser check failed")


class ReportTests(GuardHome):
    @staticmethod
    def entry(stamp, **kw):
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))
        return json.dumps({"ts": ts, **kw}) + "\n"

    def test_recent_actions_skip_old_idle_and_repeats(self):
        repeat = {"action": "would_close_sessions", "would_close": [{"project": "a"}]}
        guard.log_path().write_text(
            self.entry(NOW - 90000, action="close_browser", outcome="closed")
            + self.entry(NOW - 500, action="none")
            + self.entry(NOW - 400, action="close_browser", outcome="closed")
            + self.entry(NOW - 300, **repeat)
            + self.entry(NOW - 200, **repeat),
            encoding="utf-8",
        )
        lines = guard.recent_actions(NOW)
        self.assertEqual(len(lines), 2)
        self.assertIn("closed the browser", lines[0])
        self.assertIn("would close sessions: a", lines[1])

    def test_log_is_trimmed_past_its_limit(self):
        with mock.patch.object(guard, "LOG_MAX_BYTES", 200):
            for _ in range(10):
                guard._log({"action": "none", "pad": "x" * 40})
        self.assertLess(guard.log_path().stat().st_size, 200)

    def test_status_names_thresholds_and_last_run(self):
        self.readings(free=3.0, idle=2.0)
        self.config(protected=["amber"])
        guard.tick(NOW, sessions=[])
        text = guard.status(NOW)
        self.assertIn("hold new sessions under 1 GB", text)
        self.assertIn("protected: amber", text)
        self.assertIn("user present", text)
        self.assertIn("no actions", text)


if __name__ == "__main__":
    unittest.main()
