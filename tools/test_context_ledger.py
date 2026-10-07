"""Tests for tools/context_ledger.py.

Covers:
  (a) spans, per-skill totals, peak, output tokens, version (--json shape)
  (b) isSidechain entries and assistant entries without usage are not turns
  (c) malformed lines are skipped and counted
  (d) absent / null attributionSkill -> '(none)'
  (e) a >20000-token drop is a compaction: listed, excluded from deltas
  (f) text output is ASCII-only with SPANS / SKILLS tables
  (g) --session and no-arg (newest mtime) resolution under a temp HOME
  (h) exit codes: 1 no usage turns, 2 missing file / bad args
  (i) a generated 50000-line fixture parses in under 10s (streaming)

Run: py -3.13 tools/test_context_ledger.py

Tests go through the CLI via subprocess - same path create-handoff uses.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
LEDGER = PROJECT / "tools" / "context_ledger.py"

TS = "2026-10-06T02:12:30.511Z"


def _assistant(
    ctx, out=10, label="autonomous", sidechain=False, usage=True, version="2.1.290"
):
    """One assistant entry whose context (input+cache_read+cache_creation) sums to ctx."""
    e = {
        "type": "assistant",
        "isSidechain": sidechain,
        "timestamp": TS,
        "version": version,
        "uuid": "u",
        "parentUuid": "p",
        "sessionId": "s",
        "message": {"role": "assistant", "model": "m", "content": []},
    }
    if label != "ABSENT":
        e["attributionSkill"] = label
    if usage:
        a = ctx // 3
        e["message"]["usage"] = {
            "input_tokens": a,
            "cache_read_input_tokens": a,
            "cache_creation_input_tokens": ctx - 2 * a,
            "output_tokens": out,
            "service_tier": "standard",
        }
    return e


def _user(text="hi"):
    return {
        "type": "user",
        "isSidechain": False,
        "timestamp": TS,
        "version": "2.1.290",
        "message": {"role": "user", "content": text},
    }


class ContextLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, entries):
        """entries: list of dicts (json-encoded) or raw strings (written verbatim)."""
        p = self.dir / name
        lines = [e if isinstance(e, str) else json.dumps(e) for e in entries]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(p)

    def _run(self, *args, env=None, cwd=None):
        proc = subprocess.run(
            [sys.executable, str(LEDGER), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env=env,
            cwd=cwd,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def _json(self, *args, **kw):
        code, out, err = self._run(*args, "--json", **kw)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    # (a)
    def test_spans_totals_peak_version(self):
        p = self._write(
            "t.jsonl",
            [
                _user(),
                _assistant(100, out=5, label="resume-handoff"),
                _user(),
                _assistant(150, out=5, label="resume-handoff"),
                _user(),
                _assistant(160, out=7, label="autonomous"),
                _user(),
                _assistant(200, out=7, label="autonomous"),
                _user(),
                _assistant(210, out=1, label="premortem"),
                _user(),
                _assistant(230, out=1, label="autonomous"),
            ],
        )
        d = self._json(p)
        self.assertEqual(d["transcript"], p)
        self.assertEqual(d["version"], "2.1.290")
        self.assertEqual(d["turns"], 6)
        self.assertEqual(d["skipped_lines"], 0)
        self.assertEqual(d["peak_context"], 230)
        self.assertEqual(d["total_output_tokens"], 26)
        self.assertEqual(d["compactions"], [])
        self.assertEqual(
            d["spans"],
            [
                {
                    "label": "resume-handoff",
                    "first_turn": 1,
                    "last_turn": 2,
                    "turns": 2,
                    "start_context": 100,
                    "end_context": 150,
                    "delta": 150,
                },
                {
                    "label": "autonomous",
                    "first_turn": 3,
                    "last_turn": 4,
                    "turns": 2,
                    "start_context": 160,
                    "end_context": 200,
                    "delta": 50,
                },
                {
                    "label": "premortem",
                    "first_turn": 5,
                    "last_turn": 5,
                    "turns": 1,
                    "start_context": 210,
                    "end_context": 210,
                    "delta": 10,
                },
                {
                    "label": "autonomous",
                    "first_turn": 6,
                    "last_turn": 6,
                    "turns": 1,
                    "start_context": 230,
                    "end_context": 230,
                    "delta": 20,
                },
            ],
        )
        self.assertEqual(
            d["skills"],
            {
                "resume-handoff": {"delta": 150, "spans": 1, "turns": 2},
                "autonomous": {"delta": 70, "spans": 2, "turns": 3},
                "premortem": {"delta": 10, "spans": 1, "turns": 1},
            },
        )

    # (b)
    def test_sidechain_and_missing_usage_are_not_turns(self):
        p = self._write(
            "t.jsonl",
            [
                _assistant(100, sidechain=True),
                _assistant(100),
                _assistant(500, usage=False),
                {"type": "system", "subtype": "x"},
                {"type": "progress", "data": {}},
                _assistant(120),
                _assistant(900, sidechain=True),
            ],
        )
        d = self._json(p)
        self.assertEqual(d["turns"], 2)
        self.assertEqual(d["skipped_lines"], 0)
        self.assertEqual(d["peak_context"], 120)
        self.assertEqual(d["spans"][0]["first_turn"], 1)
        self.assertEqual(d["spans"][0]["last_turn"], 2)

    # (c)
    def test_malformed_lines_skipped_and_counted(self):
        p = self._write(
            "t.jsonl",
            [
                _assistant(100),
                "not json at all",
                '{"type": "assistant", "message": {"usage": {"input_tokens": 1',
                "[1, 2, 3]",
                _assistant(130),
            ],
        )
        d = self._json(p)
        self.assertEqual(d["skipped_lines"], 3)
        self.assertEqual(d["turns"], 2)
        self.assertEqual(d["peak_context"], 130)

    # (d)
    def test_absent_or_null_attribution_is_none(self):
        p = self._write(
            "t.jsonl",
            [
                _assistant(100, label="ABSENT"),
                _assistant(110, label=None),
                _assistant(120, label="review"),
                _assistant(125, label=None),
            ],
        )
        d = self._json(p)
        self.assertEqual(
            [s["label"] for s in d["spans"]], ["(none)", "review", "(none)"]
        )
        self.assertEqual(d["spans"][0]["turns"], 2)
        self.assertEqual(d["skills"]["(none)"], {"delta": 115, "spans": 2, "turns": 3})

    # (e)
    def test_compaction_listed_and_excluded_from_delta(self):
        p = self._write(
            "t.jsonl",
            [
                _assistant(100000, label="a"),
                _assistant(150000, label="a"),
                _assistant(160000, label="b"),
                _assistant(200000, label="b"),
                _assistant(50000, label="b"),  # drop 150000 -> compaction
                _assistant(70000, label="b"),
                _assistant(
                    55000, label="b"
                ),  # drop 15000 -> not a compaction (<= 20000)
            ],
        )
        d = self._json(p)
        self.assertEqual(d["compactions"], [{"turn": 5, "from": 200000, "to": 50000}])
        self.assertEqual(d["peak_context"], 200000)
        self.assertEqual(
            [s["delta"] for s in d["spans"]], [150000, 10000 + 40000 + 20000 - 15000]
        )
        # invariant: sum of span deltas minus compaction drops == final context
        drops = sum(c["from"] - c["to"] for c in d["compactions"])
        self.assertEqual(sum(s["delta"] for s in d["spans"]) - drops, 55000)
        self.assertEqual(d["skills"]["b"]["delta"], 55000)

    # (f)
    def test_text_output_ascii_tables(self):
        p = self._write(
            "t.jsonl",
            [
                _assistant(100000, label="resume-handoff"),
                _assistant(150000, label="autonomous"),
                _assistant(100, label="premortem"),
            ],
        )
        code, out, err = self._run(p)
        self.assertEqual(code, 0, err)
        self.assertTrue(all(ord(ch) < 128 for ch in out), "non-ASCII in text output")
        for frag in (
            "SPANS",
            "SKILLS",
            "resume-handoff",
            "autonomous",
            "premortem",
            "version",
            "peak",
            "compactions",
        ):
            self.assertIn(frag, out)
        self.assertIn("2.1.290", out)
        self.assertIn("150000", out.replace(",", ""))
        self.assertIn("150,000 -> 100", out)

    # (g)
    def _home_env(self, home):
        env = dict(os.environ)
        env.pop("CLAUDE_CONFIG_DIR", None)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        return env

    def test_session_resolution_via_temp_home(self):
        home = self.dir / "home"
        cwd = self.dir / "proj"
        cwd.mkdir()
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
        folder = home / ".claude" / "projects" / slug
        folder.mkdir(parents=True)
        old = folder / "11111111-aaaa-bbbb-cccc-222222222222.jsonl"
        new = folder / "33333333-aaaa-bbbb-cccc-444444444444.jsonl"
        old.write_text(
            json.dumps(_assistant(100, label="old")) + "\n", encoding="utf-8"
        )
        new.write_text(
            json.dumps(_assistant(200, label="new")) + "\n", encoding="utf-8"
        )
        t = time.time()
        os.utime(old, (t - 1000, t - 1000))
        os.utime(new, (t, t))
        env = self._home_env(home)

        # Reported paths are resolved (8.3 short names in TEMP expand).
        d = self._json("--session", old.stem, env=env, cwd=str(cwd))
        self.assertEqual(d["transcript"], str(old.resolve()))
        self.assertEqual(d["spans"][0]["label"], "old")

        d = self._json(env=env, cwd=str(cwd))
        self.assertEqual(d["transcript"], str(new.resolve()))
        self.assertEqual(d["spans"][0]["label"], "new")

        code, out, err = self._run("--session", "does-not-exist", env=env, cwd=str(cwd))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("does-not-exist", err)

    def test_no_arg_with_no_slug_folder_exits_2(self):
        home = self.dir / "home2"
        home.mkdir()
        cwd = self.dir / "proj2"
        cwd.mkdir()
        code, _out, err = self._run(env=self._home_env(home), cwd=str(cwd))
        self.assertEqual(code, 2)
        self.assertIn("no transcript", err.lower())

    # (h)
    def test_exit_codes(self):
        self.assertEqual(self._run(str(self.dir / "nope.jsonl"))[0], 2)
        empty = self._write(
            "empty.jsonl", [_user(), _assistant(1, usage=False), "garbage"]
        )
        code, out, err = self._run(empty)
        self.assertEqual(code, 1)
        self.assertIn("no assistant usage turns", err.lower())
        code, out, _err = self._run(empty, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        both = self._write("t.jsonl", [_assistant(1)])
        self.assertEqual(self._run(both, "--session", "x")[0], 2)
        self.assertEqual(self._run("--bogus-flag")[0], 2)
        self.assertEqual(self._run(str(self.dir))[0], 2)  # a directory, not a file

    # (i)
    def test_50000_line_fixture_under_10s(self):
        p = self.dir / "big.jsonl"
        labels = ["resume-handoff", "autonomous", "premortem", "review"]
        with p.open("w", encoding="utf-8") as fh:
            ctx = 20000
            for i in range(50000):
                if i % 2 == 0:
                    fh.write(json.dumps(_user("x" * 200)) + "\n")
                else:
                    ctx = ctx + 300 if i % 4001 else 30000
                    fh.write(
                        json.dumps(_assistant(ctx, label=labels[(i // 2000) % 4]))
                        + "\n"
                    )
        t0 = time.perf_counter()
        d = self._json(str(p))
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 10.0, f"took {elapsed:.1f}s")
        self.assertEqual(d["turns"], 25000)
        self.assertEqual(d["skipped_lines"], 0)
        self.assertGreater(len(d["compactions"]), 0)
        self.assertEqual(d["spans"][-1]["last_turn"], 25000)
        self.assertEqual(sum(s["turns"] for s in d["spans"]), 25000)


def _load_ledger():
    import importlib.util

    spec = importlib.util.spec_from_file_location("context_ledger_under_test", LEDGER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PathContainmentTests(unittest.TestCase):
    """(j) env-derived config root and --session ids stay inside the projects root."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name).resolve()
        self.home = self.dir / "home"
        self.cwd = self.dir / "proj"
        self.cwd.mkdir()
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(self.cwd))
        self.projects = self.home / ".claude" / "projects"
        self.folder = self.projects / slug
        self.folder.mkdir(parents=True)
        self.sid = "11111111-aaaa-bbbb-cccc-222222222222"
        self.mine = self.folder / f"{self.sid}.jsonl"
        self.mine.write_text(json.dumps(_assistant(100)) + "\n", encoding="utf-8")
        # A transcript that exists but belongs elsewhere: in a sibling project and
        # outside the config root entirely.
        sibling = self.projects / "other-project"
        sibling.mkdir()
        self.sibling = sibling / "x.jsonl"
        self.sibling.write_text(json.dumps(_assistant(5)) + "\n", encoding="utf-8")
        self.outside = self.dir / "loose.jsonl"
        self.outside.write_text(json.dumps(_assistant(7)) + "\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _env(self, **over):
        env = dict(os.environ)
        env.pop("CLAUDE_CONFIG_DIR", None)
        env["HOME"] = str(self.home)
        env["USERPROFILE"] = str(self.home)
        env.update(over)
        return env

    def _run(self, *args, env=None):
        proc = subprocess.run(
            [sys.executable, str(LEDGER), *args, "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env=env or self._env(),
            cwd=str(self.cwd),
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_plain_session_id_accepted(self):
        code, out, err = self._run("--session", self.sid)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["transcript"], str(self.mine))

    def test_traversal_session_ids_refused(self):
        rel_outside = os.path.relpath(self.outside.with_suffix(""), self.folder)
        for sid in (
            "../other-project/x",
            "..\\other-project\\x",
            f"{self.sid}/../../other-project/x",
            rel_outside,
            str(self.outside.with_suffix("")),
            str(self.sibling.with_suffix("")),
        ):
            with self.subTest(sid=sid):
                code, out, err = self._run("--session", sid)
                self.assertEqual(code, 2, out)
                self.assertEqual(out, "")
                self.assertIn("outside", err)

    def test_config_dir_env_resolved(self):
        dotted = self.home / "x" / ".." / ".claude"
        env = self._env(CLAUDE_CONFIG_DIR=str(dotted))
        code, out, err = self._run("--session", self.sid, env=env)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["transcript"], str(self.mine))

    def test_relative_config_dir_refused(self):
        code, out, err = self._run(env=self._env(CLAUDE_CONFIG_DIR="rel/.claude"))
        self.assertEqual(code, 2, out)
        self.assertIn("CLAUDE_CONFIG_DIR", err)
        self.assertIn("absolute", err)

    def test_relative_home_refused(self):
        code, out, err = self._run(env=self._env(HOME="relhome", USERPROFILE="relhome"))
        self.assertEqual(code, 2, out)
        self.assertIn("HOME", err)
        self.assertIn("absolute", err)

    def test_contained_helper(self):
        cl = _load_ledger()
        root = self.projects
        self.assertEqual(
            cl.contained(root / "a" / "b.jsonl", root), root / "a" / "b.jsonl"
        )
        self.assertEqual(cl.contained(root / "a" / ".." / "b", root), root / "b")
        self.assertEqual(cl.contained(root, root), root)
        for bad in (root / "..", root / "a" / ".." / ".." / "x", self.outside):
            with self.subTest(bad=str(bad)), self.assertRaises(cl.PathError):
                cl.contained(bad, root)


class ReaderCharacterization(unittest.TestCase):
    """VAL-612: Reader.__iter__ yields, skip counting and version capture,
    pinned on HEAD 2aff883 before the per-line parsing moved into helpers."""

    def test_reader_yields_and_counters(self):
        cl = _load_ledger()
        a1 = _assistant(300, out=7, label="review", version="")
        a2 = _assistant(90, out=None, label="", version="2.0.1")
        a2["timestamp"] = 12
        a3 = _assistant(60, label=None)
        a3["message"]["usage"]["input_tokens"] = "x"
        a3.pop("timestamp")
        no_msg = _assistant(10)
        no_msg["message"] = "text"
        lines = [
            "",
            "   ",
            "not json",
            "[1, 2]",
            "42",
            json.dumps({"type": "user", "version": 3}),
            json.dumps(a1),
            json.dumps(_assistant(50, sidechain=True, version="9.9.9")),
            json.dumps(no_msg),
            json.dumps(_assistant(10, usage=False)),
            json.dumps({"type": "assistant", "message": {"usage": []}}),
            json.dumps(a2),
            json.dumps(a3),
            json.dumps(_assistant(30, label="ABSENT", version="3.0")),
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "t.jsonl"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            reader = cl.Reader(path)
            rows = list(reader)
        self.assertEqual(
            rows,
            [
                (TS, 300, 7, "review"),
                ("", 90, 0, cl.NONE_LABEL),
                ("", 40, 10, cl.NONE_LABEL),
                (TS, 30, 10, cl.NONE_LABEL),
            ],
        )
        self.assertEqual(reader.skipped, 3)
        # first non-empty string version wins, even from a sidechain entry
        self.assertEqual(reader.version, "9.9.9")

    def test_reader_replaces_undecodable_bytes(self):
        cl = _load_ledger()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "t.jsonl"
            path.write_bytes(
                b"\xff\xfe garbage\n" + json.dumps(_assistant(9)).encode() + b"\n"
            )
            reader = cl.Reader(path)
            rows = list(reader)
        self.assertEqual(rows, [(TS, 9, 10, "autonomous")])
        self.assertEqual(reader.skipped, 1)
        self.assertEqual(reader.version, "2.1.290")


if __name__ == "__main__":
    unittest.main()
