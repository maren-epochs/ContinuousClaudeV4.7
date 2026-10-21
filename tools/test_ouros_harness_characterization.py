"""Characterization tests for ouros_harness.py functions refactored in VAL-613.

Pins current behavior (return values, call arguments, CLI output, exit codes)
of _call_nia_help, _call_research_package, _call_run_python, _call_llm,
execute_in_sandbox and main, so the debt refactor provably changes nothing.
External services, subprocesses and the ouros module are faked.

Run: py -3.13 tools/test_ouros_harness_characterization.py
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT / "tools" / "ouros_harness.py"

_spec = importlib.util.spec_from_file_location("ouros_harness_charac", HARNESS)
oh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oh)

NIA_HELP_SHA256 = "492a39ce5a486b7ecd3a8d6ed17ebbe80e49dadeacae40869ec6035d46c609a9"


def _digest(obj):
    """sha256 of the canonical JSON encoding of `obj`."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


class NiaHelpTests(unittest.TestCase):
    """nia_help returns the same help table, as a fresh object every call."""

    def test_snapshot(self):
        d = oh._call_nia_help()
        self.assertEqual(_digest(d), NIA_HELP_SHA256)
        self.assertEqual(next(iter(d)), "research_package")
        self.assertEqual(list(d)[-1], "nia_help")

    def test_fresh_object_each_call(self):
        d = oh._call_nia_help()
        d["injected"] = 1
        d["nia_web"]["args"]["query"] = "mutated"
        self.assertEqual(_digest(oh._call_nia_help()), NIA_HELP_SHA256)


class ResearchPackageTests(unittest.TestCase):
    """research_package: queries sent, noise filtered, truncation, section order."""

    DOCS = {
        "answer": "ANS",
        "results": [
            {
                "source": {"display_name": "Express Docs", "document_name": "d1"},
                "content": "C" * 700,
            },
            {"source": {"display_name": "random.blog"}, "content": "noise"},
            {"source": {"display_name": "github.com"}, "content": "gh"},
            {"source": {"display_name": "pypi.org"}, "content": "beyond"},
        ],
    }
    PKG = {
        "version_used": "5.1.0",
        "results": [
            {"document": "D" * 700},
            {"document": ""},
            {"document": "x"},
            {"document": "y"},
        ],
    }
    GREP = {
        "results": [
            {"result": {"content": "ab" * 150, "file_path": "f", "start_line": 3}},
            {"content": "c2", "file_path": "g"},
            {"result": {"content": ""}},
            {"content": "c4"},
            {"content": "c5", "start_line": 9},
            {"content": "c6"},
            {"content": "c7-beyond"},
        ]
    }
    EXA = {"results": [{"title": "T", "summary": "S" * 400}, {}]}

    def _run(self, docs, pkg, grep, exa, *args, **kwargs):
        """Call _call_research_package with the four bridges faked."""
        with (
            mock.patch.object(oh, "_call_nia_search_sync", return_value=docs) as s,
            mock.patch.object(oh, "_call_nia_package_sync", return_value=pkg) as p,
            mock.patch.object(
                oh, "_call_nia_package_grep_sync", return_value=grep
            ) as g,
            mock.patch.object(oh, "_call_exa_search_sync", return_value=exa) as e,
        ):
            out = oh._call_research_package(*args, **kwargs)
        return out, (s, p, g, e)

    def test_full_result(self):
        out, (s, p, g, e) = self._run(
            self.DOCS, self.PKG, self.GREP, self.EXA, "express", version="5.1.0"
        )
        s.assert_called_once_with("express v5.1.0 API breaking changes migration guide")
        p.assert_called_once_with(
            "express", "API usage patterns middleware routing", registry="npm"
        )
        g.assert_called_once_with("express", "deprecat", registry="npm")
        e.assert_called_once_with(
            "express v5.1.0 migration guide best practices breaking changes",
            num_results=3,
        )
        expected = {
            "package": "express",
            "version_requested": "5.1.0",
            "version_indexed": "5.1.0",
            "registry": "npm",
            "sources_used": 1 + 2 + 2 + 5 + 2,
            "sections": {
                "nia_answer": "ANS",
                "official_docs": [
                    {"source": "Express Docs", "doc": "d1", "content": "C" * 600},
                    {"source": "github.com", "doc": "", "content": "gh"},
                ],
                "source_patterns": ["D" * 600, "x"],
                "deprecations": [
                    {"file": "f", "line": 3, "content": ("ab" * 150)[:200]},
                    {"file": "g", "line": "", "content": "c2"},
                    {"file": "", "line": "", "content": "c4"},
                    {"file": "", "line": 9, "content": "c5"},
                    {"file": "", "line": "", "content": "c6"},
                ],
                "guides": [
                    {"title": "T", "summary": "S" * 300},
                    {"title": "", "summary": ""},
                ],
            },
        }
        self.assertEqual(out, expected)
        self.assertEqual(list(out), list(expected))
        self.assertEqual(list(out["sections"]), list(expected["sections"]))

    def test_max_results_and_chars(self):
        out, (_, _, _, e) = self._run(
            self.DOCS,
            self.PKG,
            self.GREP,
            self.EXA,
            "express",
            registry="py_pi",
            max_results=1,
            max_chars=5,
        )
        e.assert_called_once_with(
            "express migration guide best practices breaking changes", num_results=1
        )
        sec = out["sections"]
        self.assertEqual(
            sec["official_docs"],
            [{"source": "Express Docs", "doc": "d1", "content": "CCCCC"}],
        )
        self.assertEqual(sec["source_patterns"], ["DDDDD"])
        self.assertEqual(
            [d["content"] for d in sec["deprecations"]], [("ab" * 150)[:200], "c2"]
        )
        self.assertEqual(out["registry"], "py_pi")
        self.assertEqual(out["sources_used"], 1 + 1 + 1 + 2 + 2)

    def test_all_empty(self):
        out, (s, _, _, _) = self._run({}, {}, {}, {}, "p", registry="py_pi")
        s.assert_called_once_with("p API breaking changes migration guide")
        self.assertEqual(
            out,
            {
                "package": "p",
                "version_requested": None,
                "version_indexed": "unknown",
                "registry": "py_pi",
                "sources_used": 0,
                "sections": {},
            },
        )


class _FakePopen:
    """subprocess.Popen stand-in; `script` is a list of communicate() outcomes."""

    script: list = []
    instances: list = []

    def __init__(self, args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.pid = 4242
        self.returncode = self.kwargs.pop("_rc", None)
        self.timeouts = []
        _FakePopen.instances.append(self)

    def communicate(self, timeout=None):
        """Pop the next outcome: an exception is raised, a (out, rc) pair returned."""
        self.timeouts.append(timeout)
        step = _FakePopen.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        out, rc = step
        self.returncode = rc
        return out, None


class HostPythonCmdTests(unittest.TestCase):
    """_host_python_cmd resolution order (VAL-702: CI/venv interpreters win)."""

    def _cmd(self, env, version, has_py):
        which = "C:/py.exe" if has_py else None
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(oh.sys, "version_info", version),
            mock.patch("shutil.which", return_value=which),
        ):
            if "OUROS_HOST_PYTHON" not in env:
                os.environ.pop("OUROS_HOST_PYTHON", None)
            return oh._host_python_cmd()

    def test_env_override_wins(self):
        env = {"OUROS_HOST_PYTHON": "/opt/ds/bin/python"}
        self.assertEqual(self._cmd(env, (3, 13, 0), True), ["/opt/ds/bin/python"])

    def test_own_interpreter_when_313(self):
        self.assertEqual(self._cmd({}, (3, 13, 7), True), [sys.executable])

    def test_launcher_pin_when_not_313(self):
        self.assertEqual(self._cmd({}, (3, 14, 0), True), ["py", "-3.13"])

    def test_own_interpreter_without_launcher(self):
        self.assertEqual(self._cmd({}, (3, 14, 0), False), [sys.executable])


class RunPythonTests(unittest.TestCase):
    """run_python: validation, timeout clamp, truncation, exit/timeout markers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name) / "work"
        self.addCleanup(setattr, oh, "_SESSION_WORK_DIR", oh._SESSION_WORK_DIR)
        oh._SESSION_WORK_DIR = self.work
        _FakePopen.instances = []

    def _run(self, script, *args, **kwargs):
        """Call _call_run_python with Popen faked and the kill helper mocked."""
        _FakePopen.script = list(script)
        with (
            mock.patch("subprocess.Popen", _FakePopen),
            mock.patch.object(oh, "_host_python_cmd", return_value=["PY"]),
            mock.patch.object(oh, "_kill_process_tree") as kill,
        ):
            out = oh._call_run_python(*args, **kwargs)
        return out, kill

    def test_rejects_empty_or_non_string(self):
        for code in ("", "   ", None, 3):
            with self.subTest(code=code):
                out, _ = self._run([], code)
                self.assertEqual(
                    out, {"error": "run_python requires a non-empty code string"}
                )

    def test_success_argv_cwd_env(self):
        out, kill = self._run([("hello\n", 0)], "print(1)")
        self.assertEqual(out, "hello\n")
        kill.assert_not_called()
        p = _FakePopen.instances[0]
        self.assertEqual(p.args, ["PY", "-c", "print(1)"])
        self.assertEqual(p.kwargs["cwd"], str(self.work))
        self.assertTrue(self.work.is_dir())
        self.assertEqual(p.kwargs["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(p.kwargs["env"]["PYTHONUTF8"], "1")
        self.assertEqual(p.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(p.kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(p.timeouts, [60.0])

    def test_timeout_clamp_and_fallback(self):
        self._run([("", 0)], "x", timeout=10000)
        self.assertEqual(_FakePopen.instances[-1].timeouts, [600])
        self._run([("", 0)], "x", timeout="abc")
        self.assertEqual(_FakePopen.instances[-1].timeouts, [60])

    def test_nonzero_exit_and_none_output(self):
        out, _ = self._run([(None, 3)], "x")
        self.assertEqual(out, "\n[exit code: 3]")

    def test_truncation(self):
        big = "x" * 9000 + "END"
        out, _ = self._run([(big, 0)], "x")
        self.assertEqual(out, "[truncated]\n" + big[-8192:])

    def test_timeout_kills_tree(self):
        exp = subprocess.TimeoutExpired("PY", 0.5)
        out, kill = self._run([exp, ("partial", None)], "x", timeout=0.5)
        kill.assert_called_once_with(_FakePopen.instances[0])
        self.assertEqual(
            out, "partial\n[run_python timed out after 0.5s — process tree killed]"
        )
        self.assertEqual(_FakePopen.instances[0].timeouts, [0.5, 10])

    def test_timeout_second_wait_also_expires(self):
        exp = subprocess.TimeoutExpired("PY", 2)
        out, _ = self._run([exp, exp], "x", timeout=2)
        self.assertEqual(out, "\n[run_python timed out after 2s — process tree killed]")

    def test_popen_failure_and_lock_released(self):
        with (
            mock.patch("subprocess.Popen", side_effect=OSError("nope")),
            mock.patch.object(oh, "_host_python_cmd", return_value=["PY"]),
        ):
            out = oh._call_run_python("x")
        self.assertEqual(out, {"error": "run_python failed: nope"})
        self.assertTrue(oh._RUN_PYTHON_LOCK.acquire(blocking=False))
        oh._RUN_PYTHON_LOCK.release()

    def test_concurrent_call_denied(self):
        oh._RUN_PYTHON_LOCK.acquire()
        try:
            out, _ = self._run([], "x")
        finally:
            oh._RUN_PYTHON_LOCK.release()
        self.assertEqual(
            out,
            {
                "error": "run_python denied: another run_python call is in "
                "progress (max_concurrent: 1)"
            },
        )


class CallLlmTests(unittest.TestCase):
    """llm_call: request body/headers/timeout per backend, error returns."""

    ANTHROPIC_REPLY = {"content": [{"text": "A"}]}
    OPENAI_REPLY = {"choices": [{"message": {"content": "O"}}]}
    KEYS = {
        "ANTHROPIC_API_KEY": "ka",
        "OPENAI_API_KEY": "ko",
        "OPENROUTER_API_KEY": "kr",
    }

    def _run(self, reply, *args, **kwargs):
        """Call _call_llm with keys set and _post_json mocked; return (out, post)."""
        with (
            mock.patch.dict(os.environ, self.KEYS),
            mock.patch.object(oh, "_post_json", return_value=reply) as post,
        ):
            return oh._call_llm(*args, **kwargs), post

    def test_anthropic_system_no_temperature(self):
        out, post = self._run(self.ANTHROPIC_REPLY, "p", system="S")
        self.assertEqual(out, "A")
        post.assert_called_once_with(
            oh.LLM_ENDPOINTS["anthropic"],
            {
                "model": "claude-haiku-4-5-20251001",
                "messages": [{"role": "user", "content": "p"}],
                "max_tokens": 1000,
                "system": "S",
            },
            {"x-api-key": "ka", "anthropic-version": "2023-06-01"},
            120,
        )

    def test_anthropic_positive_temperature(self):
        _, post = self._run(self.ANTHROPIC_REPLY, "p", "m", 5, None, 0.7)
        body = post.call_args[0][1]
        self.assertEqual(body["temperature"], 0.7)
        self.assertNotIn("system", body)
        self.assertEqual((body["model"], body["max_tokens"]), ("m", 5))

    def test_openai_and_openrouter_always_send_temperature(self):
        for backend, key in (("openai", "ko"), ("openrouter", "kr")):
            with self.subTest(backend=backend):
                out, post = self._run(
                    self.OPENAI_REPLY, "p", system="S", backend=backend
                )
                self.assertEqual(out, "O")
                url, body, headers, timeout = post.call_args[0]
                self.assertEqual(url, oh.LLM_ENDPOINTS[backend])
                self.assertEqual(body["temperature"], 0.0)
                self.assertNotIn("system", body)
                self.assertEqual(headers, {"Authorization": f"Bearer {key}"})
                self.assertEqual(timeout, 120)

    def test_local_no_headers_short_timeout(self):
        _, post = self._run(self.OPENAI_REPLY, "p", backend="local")
        _, body, headers, timeout = post.call_args[0]
        self.assertEqual((headers, timeout, body["temperature"]), ({}, 60, 0.0))

    def test_missing_key_and_unknown_backend(self):
        env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                oh._call_llm("p", backend="openrouter"),
                {"error": "OPENROUTER_API_KEY not set"},
            )
        self.assertEqual(
            oh._call_llm("p", backend="x"),
            {
                "error": "Unknown backend: x. Use 'local', 'anthropic', 'openai', or 'openrouter'."
            },
        )

    def test_refused_url_becomes_error(self):
        with (
            mock.patch.dict(os.environ, self.KEYS),
            mock.patch.object(oh, "_post_json", side_effect=oh.UrlRefused("no")),
        ):
            self.assertEqual(oh._call_llm("p"), {"error": "no"})


class _FakeManager:
    """ouros.SessionManager stand-in that records every call."""

    def __init__(self, existing=(), load_error=None, save_error=None):
        self.calls = []
        self.existing = list(existing)
        self.load_error, self.save_error = load_error, save_error

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "load_session" and self.load_error:
                raise self.load_error
            if name == "save_session" and self.save_error:
                raise self.save_error
            if name == "list_sessions":
                return [{"id": i} for i in self.existing]
            return None

        return record


class _FakeSession:
    """ouros.Session stand-in replaying scripted execute/resume results."""

    def __init__(self, script, manager=None, session_id=None):
        self.script, self.manager, self.session_id = list(script), manager, session_id
        self.resumed = []

    def execute(self, code):
        """First scripted result."""
        self.code = code
        return self.script.pop(0)

    def resume(self, call_id, value):
        """Record the resume value; return the next scripted result."""
        self.resumed.append((call_id, value))
        return self.script.pop(0)


def _pause(name, call_id, args=(), kwargs=None, stdout=None):
    """A paused ouros result requesting external function `name`."""
    return {
        "stdout": stdout,
        "is_complete": False,
        "progress": {
            "status": "function_call",
            "function_name": name,
            "call_id": call_id,
            "args": list(args),
            "kwargs": kwargs or {},
        },
    }


class ExecuteInSandboxTests(unittest.TestCase):
    """execute_in_sandbox: session open mode, bridge loop, save, stdout, artifacts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = Path(self.tmp.name)
        self.addCleanup(setattr, oh, "_SESSION_WORK_DIR", oh._SESSION_WORK_DIR)

    def _run(self, manager, script, **kwargs):
        """Run execute_in_sandbox with a fake ouros module; return (result, session, err)."""
        holder = {}

        def session_factory(manager=None, session_id=None):
            holder["s"] = _FakeSession(script, manager, session_id)
            return holder["s"]

        fake = types.ModuleType("ouros")
        fake.SessionManager = lambda: manager
        fake.Session = session_factory
        extra = {
            "echo_fn": lambda *a, **k: {"a": list(a), "k": k},
            "boom": mock.Mock(side_effect=RuntimeError("bad")),
        }
        err = io.StringIO()
        with (
            mock.patch.dict(sys.modules, {"ouros": fake}),
            mock.patch.dict(oh.EXTERNAL_FUNCTIONS, extra),
            mock.patch.object(oh, "_snapshot_output_dir", return_value={"snap": 1}),
            mock.patch.object(oh, "_diff_output_dir", return_value=["ART"]) as diff,
            contextlib.redirect_stderr(err),
        ):
            result = oh.execute_in_sandbox("CODE", **kwargs)
            diff.assert_called_once_with({"snap": 1})
            ext = list(oh.EXTERNAL_FUNCTIONS)
        return result, holder["s"], err.getvalue(), ext

    def test_bridge_loop_fresh_default_session(self):
        mgr = _FakeManager()
        script = [
            _pause("echo_fn", 1, args=[1], kwargs={"x": 2}, stdout="a"),
            _pause("nope", 2, stdout="b"),
            _pause("boom", 3),
            {"stdout": "c", "is_complete": True, "extra": 7},
        ]
        result, sess, err, ext = self._run(mgr, script)
        self.assertEqual(
            result,
            {"stdout": "abc", "is_complete": True, "extra": 7, "artifacts": ["ART"]},
        )
        self.assertEqual(
            sess.resumed,
            [
                (1, {"a": [1], "k": {"x": 2}}),
                (2, {"error": "Unknown function: nope"}),
                (3, {"error": "boom failed: bad"}),
            ],
        )
        self.assertEqual(
            (sess.session_id, sess.manager, sess.code), ("default", mgr, "CODE")
        )
        self.assertEqual(
            mgr.calls,
            [
                ("list_sessions", (), {}),
                ("create_session", ("default",), {"external_functions": ext}),
            ],
        )
        self.assertEqual(oh._SESSION_WORK_DIR, oh.SANDBOX_OUTPUT_ROOT / "default")
        self.assertEqual(err, "")

    def test_non_function_pause_stops_loop(self):
        mgr = _FakeManager(existing=["default"])
        paused = {"stdout": "x", "is_complete": False, "progress": {"status": "other"}}
        result, sess, _, ext = self._run(mgr, [paused])
        self.assertEqual(result["stdout"], "x")
        self.assertFalse(result["is_complete"])
        self.assertEqual(sess.resumed, [])
        self.assertIn(
            ("reset", (), {"session_id": "default", "external_functions": ext}),
            mgr.calls,
        )

    def test_saved_session_loads_by_default_and_saves(self):
        (self.storage / "s1.bin").write_bytes(b"")
        mgr = _FakeManager()
        result, _, _, ext = self._run(
            mgr,
            [{"stdout": "", "is_complete": True}],
            session_id="s1",
            storage_dir=str(self.storage),
        )
        self.assertEqual(
            mgr.calls,
            [
                ("set_storage_dir", (str(self.storage),), {}),
                ("load_session", (), {"name": "s1", "session_id": "s1"}),
                ("register_external_functions", (ext,), {"session_id": "s1"}),
                ("save_session", (), {"session_id": "s1", "name": "s1"}),
            ],
        )
        self.assertEqual(
            result, {"stdout": "", "is_complete": True, "artifacts": ["ART"]}
        )
        self.assertEqual(oh._SESSION_WORK_DIR, oh.SANDBOX_OUTPUT_ROOT / "s1")

    def test_failed_load_warns_and_creates(self):
        mgr = _FakeManager(
            load_error=RuntimeError("corrupt"), save_error=OSError("disk")
        )
        _, _, err, ext = self._run(
            mgr,
            [{"is_complete": True}],
            session_id="s2",
            storage_dir=str(self.storage),
            load_session=True,
        )
        self.assertEqual(err, "Warning: could not load session 's2': corrupt\n")
        names = [c[0] for c in mgr.calls]
        self.assertEqual(
            names, ["set_storage_dir", "load_session", "create_session", "save_session"]
        )
        self.assertEqual(
            mgr.calls[2], ("create_session", ("s2",), {"external_functions": ext})
        )

    def test_reset_ignores_saved_session(self):
        (self.storage / "s3.bin").write_bytes(b"")
        mgr = _FakeManager(existing=["s3"])
        self._run(
            mgr,
            [{"is_complete": True}],
            session_id="s3",
            storage_dir=str(self.storage),
            reset_session=True,
        )
        names = [c[0] for c in mgr.calls]
        self.assertEqual(
            names, ["set_storage_dir", "list_sessions", "reset", "save_session"]
        )

    def test_session_without_storage_not_saved(self):
        mgr = _FakeManager()
        self._run(mgr, [{"is_complete": True}], session_id="s4")
        self.assertEqual([c[0] for c in mgr.calls], ["list_sessions", "create_session"])


class MainCliTests(unittest.TestCase):
    """main(): flag validation, session commands, code sources, output format."""

    def _main(self, argv, stdin=None, result=None):
        """Run main() with argv; return (exit_code, stdout, stderr, mocks)."""
        out, err = io.StringIO(), io.StringIO()
        stdin = stdin or mock.Mock(isatty=mock.Mock(return_value=True))
        mocks = {}
        with contextlib.ExitStack() as st:
            st.enter_context(
                mock.patch.object(sys, "argv", ["ouros_harness.py", *argv])
            )
            st.enter_context(mock.patch.object(sys, "stdin", stdin))
            st.enter_context(mock.patch.object(oh, "_load_env"))
            st.enter_context(mock.patch.object(oh, "_apply_data_roots"))
            for name in ("list_variables", "get_variable", "fork_session"):
                mocks[name] = st.enter_context(mock.patch.object(oh, name))
            mocks["execute_in_sandbox"] = st.enter_context(
                mock.patch.object(oh, "execute_in_sandbox", return_value=result or {})
            )
            st.enter_context(contextlib.redirect_stdout(out))
            st.enter_context(contextlib.redirect_stderr(err))
            code = 0
            try:
                oh.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue(), mocks

    def test_reset_and_load_exclusive(self):
        code, out, err, m = self._main(["--reset", "--load", "--code", "x"])
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "Error: --reset and --load are mutually exclusive\n")
        m["execute_in_sandbox"].assert_not_called()

    def test_session_commands_require_session(self):
        for argv, flag in (
            (["--list-vars"], "--list-vars"),
            (["--get-var", "v"], "--get-var"),
            (["--fork", "b"], "--fork"),
        ):
            with self.subTest(flag=flag):
                code, _, err, _ = self._main(argv)
                self.assertEqual(code, 1)
                self.assertEqual(err, f"Error: {flag} requires --session\n")

    def test_session_commands_dispatch(self):
        _, _, _, m = self._main(["--session", "s", "--list-vars", "--get-var", "v"])
        m["list_variables"].assert_called_once_with("s", "thoughts/shared/dives")
        m["get_variable"].assert_not_called()
        _, _, _, m = self._main(
            ["-s", "s", "--get-var", "v", "--fork", "f", "--storage", "st"]
        )
        m["get_variable"].assert_called_once_with("s", "v", "st")
        m["fork_session"].assert_not_called()
        code, _, _, m = self._main(["-s", "s", "--fork", "f"])
        m["fork_session"].assert_called_once_with("s", "f", "thoughts/shared/dives")
        self.assertEqual(code, 0)
        m["execute_in_sandbox"].assert_not_called()

    def test_no_code(self):
        code, _, err, _ = self._main([])
        self.assertEqual(code, 1)
        self.assertEqual(err, "Error: provide code via --code, --file, or stdin\n")

    def test_code_sources_and_execute_kwargs(self):
        _, _, _, m = self._main(["--code", "C", "-s", "s", "--load"])
        m["execute_in_sandbox"].assert_called_once_with(
            code="C",
            session_id="s",
            storage_dir="thoughts/shared/dives",
            load_session=True,
            reset_session=False,
        )
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "c.py"
            f.write_text("FILE", encoding="utf-8")
            _, _, _, m = self._main(["--file", str(f), "--reset"])
        kw = m["execute_in_sandbox"].call_args.kwargs
        self.assertEqual(
            (kw["code"], kw["reset_session"], kw["session_id"]), ("FILE", True, None)
        )
        pipe = mock.Mock(
            isatty=mock.Mock(return_value=False), read=mock.Mock(return_value="STDIN")
        )
        _, _, _, m = self._main([], stdin=pipe)
        self.assertEqual(m["execute_in_sandbox"].call_args.kwargs["code"], "STDIN")

    def test_stdout_and_artifacts_output(self):
        arts = [f"/a/{i}" for i in range(12)]
        code, out, _, _ = self._main(
            ["-c", "x"], result={"stdout": "hi", "artifacts": arts}
        )
        lines = [f"  /a/{i}" for i in range(10)]
        self.assertEqual(code, 0)
        self.assertEqual(
            out, "hiartifacts:\n" + "\n".join(lines) + "\n  ... and 2 more\n"
        )
        _, out, _, _ = self._main(
            ["-c", "x"], result={"stdout": "", "artifacts": ["/z"]}
        )
        self.assertEqual(out, "artifacts:\n  /z\n")
        _, out, _, _ = self._main(["-c", "x"], result={"stdout": "only\n"})
        self.assertEqual(out, "only\n")


if __name__ == "__main__":
    unittest.main(verbosity=1)
