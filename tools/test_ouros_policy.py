"""Security-policy tests for ouros_harness.py bridge functions.

Covers:
  (a) run_command: shell chaining/redirection/substitution denied, commands run
      with shell=False, exec/write-capable args denied, claude/codex removed
  (b) run_command: legitimate commands still work (quoted metachars, backslash
      paths, argv lists)
  (c) read_file: secrets denied under allowed roots, traversal denied, "." is
      dropped when cwd is home
  (d) glob_files: root checked, secrets and out-of-root matches filtered
  (e) agent_call: claude -p always carries --max-turns
  (f) llm_call: _post_json refuses every 30x redirect, so the API key and the
      prompt never reach the redirect target (loopback http.server, no network)

Run: py -3.13 tools/test_ouros_policy.py

Unit-level: imports the harness module and calls the bridge functions directly
(the sandbox dispatches to these same functions).
"""

import http.server
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT / "tools" / "ouros_harness.py"

_spec = importlib.util.spec_from_file_location("ouros_harness_under_test", HARNESS)
oh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oh)


def _denied(result):
    return (
        isinstance(result, dict) and "error" in result and "denied" in result["error"]
    )


class RunCommandBypassTests(unittest.TestCase):
    """Every one of these executed something extra under shell=True."""

    BYPASSES = [
        "git log & whoami",
        "git log -1 && whoami",
        "git log -1 || whoami",
        "grep x | powershell -c ls",
        r"echo hi && type %USERPROFILE%\.claude\.env",
        "tldr --help;whoami",
        "echo $(whoami)",
        "git log\nwhoami",
        "wc -l > out.txt",
        "grep x < secrets.txt",
        "claude -p hi --dangerously-skip-permissions",
        "codex exec hi",
        "powershell -c ls",
        "rg --pre cmd x .",
        "rg --pre=cmd x .",
        "git diff --output=C:/tmp/pwn.txt",
        "git -c core.pager=whoami log",
        "git log --ext-diff",
        "git show --textconv HEAD",
        "git push origin main",
        "`whoami`",
    ]

    def test_bypasses_denied(self):
        for cmd in self.BYPASSES:
            with self.subTest(cmd=cmd):
                self.assertTrue(_denied(oh._call_run_command(cmd)), cmd)

    def test_never_uses_shell(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            oh._call_run_command("git log -1")
        args, kwargs = run.call_args
        self.assertFalse(kwargs.get("shell"))
        self.assertIsInstance(args[0], list)

    def test_backtick_is_literal_for_echo(self):
        r = oh._call_run_command("echo `whoami`")
        self.assertEqual(r["stdout"].strip(), "`whoami`")

    @unittest.skipUnless(shutil.which("npm"), "npm not installed")
    def test_cmd_target_rejects_cmd_metachars(self):
        # npm is npm.cmd on Windows: cmd.exe would re-parse a quoted '&'
        r = oh._call_run_command('npm run "x&whoami"')
        if os.name == "nt":
            self.assertTrue(_denied(r), r)


class RunCommandLegitTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        os.chdir(PROJECT)

    def tearDown(self):
        os.chdir(self._cwd)

    def test_git_log(self):
        r = oh._call_run_command("git log -1 --format=%H")
        self.assertEqual(r.get("returncode"), 0, r)
        self.assertRegex(r["stdout"].strip(), r"^[0-9a-f]{40}$")

    @unittest.skipUnless(
        sys.platform == "win32", "backslash path separator is Windows-only"
    )
    def test_grep_quoted_alternation_and_backslash_path(self):
        r = oh._call_run_command(r'grep -cE "def |class " tools\validate_report.py')
        self.assertEqual(r.get("returncode"), 0, r)
        self.assertGreater(int(r["stdout"].strip()), 0)

    def test_regex_backslashes_survive(self):
        r = oh._call_run_command(r"grep -c '\bdef\b' tools/validate_report.py")
        self.assertEqual(r.get("returncode"), 0, r)

    def test_wc_and_argv_list(self):
        r = oh._call_run_command(["wc", "-l", "tools/ouros_harness.py"])
        self.assertEqual(r.get("returncode"), 0, r)

    @unittest.skipUnless(shutil.which("tldr"), "tldr not installed")
    def test_tldr_help(self):
        r = oh._call_run_command("tldr --help")
        self.assertNotIn("error", r)
        self.assertTrue(r["stdout"] or r["stderr"])

    @unittest.skipUnless(shutil.which("rg"), "rg not installed")
    def test_rg(self):
        r = oh._call_run_command('rg -c "def " tools/validate_report.py')
        self.assertEqual(r.get("returncode"), 0, r)


class ReadPolicyTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._allow = list(oh.SECURITY_POLICY["read_allow"])
        self.proj = Path(tempfile.mkdtemp(prefix="ouros-policy-")).resolve()
        (self.proj / "sub").mkdir()
        (self.proj / "sub" / "ok.txt").write_text("ok")
        (self.proj / ".env").write_text("API_KEY=secret")
        (self.proj / ".env.local").write_text("API_KEY=secret")
        (self.proj / ".env.example").write_text("API_KEY=")
        (self.proj / "server.pem").write_text("-----BEGIN-----")
        os.chdir(self.proj)

    def tearDown(self):
        os.chdir(self._cwd)
        oh.SECURITY_POLICY["read_allow"][:] = self._allow
        shutil.rmtree(self.proj, ignore_errors=True)

    def test_project_files_readable(self):
        self.assertEqual(oh._call_read_file("sub/ok.txt"), "ok")
        self.assertEqual(oh._call_read_file(".env.example"), "API_KEY=")

    def test_secrets_denied_under_allowed_root(self):
        for name in (".env", ".env.local", "server.pem", "SUB/../.ENV"):
            with self.subTest(name=name):
                self.assertTrue(_denied(oh._call_read_file(name)))

    def test_run_command_cannot_read_secrets_via_arguments(self):
        # Review 2026-10-05: allowed commands read any path they are handed.
        for cmd in (
            ["git", "diff", "--no-index", "NUL", ".env"],
            "git diff --no-index NUL .env",
            "git blame --contents .env x",
            "git show HEAD:.env",
            "grep -r SECRET .env",
            "wc -c .env.local",
            "wc -c server.pem",
            "wc -c .env:stream",
            ["git", "diff", "--no-index", "NUL", str(Path.home() / ".claude.json")],
            f"wc -c {Path.home() / '.ssh' / 'config'}",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(_denied(oh._call_run_command(cmd)), cmd)

    def test_run_command_outside_paths_denied_but_project_ok(self):
        outside = Path(tempfile.mkdtemp(prefix="ouros-out-")).resolve()
        try:
            (outside / "x.txt").write_text("x")
            self.assertTrue(
                _denied(oh._call_run_command(["wc", "-c", str(outside / "x.txt")]))
            )
        finally:
            shutil.rmtree(outside, ignore_errors=True)
        r = oh._call_run_command("wc -c sub/ok.txt")
        self.assertFalse(_denied(r), r)

    def test_recursive_grep_excludes_secrets(self):
        ok, _, argv = oh._check_command_allowed("grep -rn API_KEY .")
        self.assertTrue(ok)
        self.assertIn("--exclude=.env", argv)
        ok, _, argv = oh._check_command_allowed("grep -n API_KEY sub/ok.txt")
        self.assertFalse(any(a.startswith("--exclude") for a in argv))
        ok, _, argv = oh._check_command_allowed("rg API_KEY")
        self.assertTrue(any(a.startswith("--glob=!") for a in argv))

    def test_ads_stream_of_secret_denied(self):
        self.assertTrue(_denied(oh._call_read_file(".env:x")))

    def test_case_variant_of_allowed_path(self):
        if os.name != "nt":
            self.skipTest("case-insensitive paths are a Windows property")
        self.assertEqual(
            oh._call_read_file(str(self.proj / "SUB" / "OK.TXT").upper()), "ok"
        )

    def test_traversal_to_claude_env_denied(self):
        rel = os.path.relpath(Path.home() / ".claude" / ".env", self.proj)
        self.assertTrue(_denied(oh._call_read_file(rel)))
        self.assertTrue(_denied(oh._call_read_file(f"sub/../../{Path(rel).name}")))

    def test_cwd_home_grants_nothing(self):
        os.chdir(Path.home())
        self.assertTrue(
            _denied(oh._call_read_file(str(Path.home() / ".claude" / ".env")))
        )
        self.assertTrue(_denied(oh._call_read_file(".gitconfig")))

    def test_data_root_does_not_expose_secrets(self):
        oh.SECURITY_POLICY["read_allow"].append(str(self.proj))
        os.chdir(PROJECT)
        self.assertTrue(_denied(oh._call_read_file(str(self.proj / ".env"))))
        self.assertEqual(oh._call_read_file(str(self.proj / "sub" / "ok.txt")), "ok")

    def test_credential_dirs_denied(self):
        oh.SECURITY_POLICY["read_allow"].append(str(Path.home() / ".ssh"))
        self.assertTrue(oh._is_secret(Path.home() / ".ssh" / "known_hosts"))
        self.assertTrue(oh._is_secret(Path.home() / ".claude" / ".credentials.json"))

    def test_glob_filters_secrets_and_root(self):
        names = {Path(m).name for m in oh._call_glob_files("**/*", ".")}
        self.assertIn("ok.txt", names)
        self.assertNotIn("server.pem", names)
        # glob skips dotfiles unless the pattern names them explicitly
        dot = {Path(m).name for m in oh._call_glob_files(".env*", ".")}
        self.assertEqual(dot, {".env.example"})
        self.assertTrue(_denied(oh._call_glob_files("*", "..")))
        self.assertEqual(oh._call_glob_files("../../*", "."), [])


class AgentCallTests(unittest.TestCase):
    def _argv(self, **kw):
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "done", "")
            oh._call_agent("task", **kw)
        return run.call_args[0][0]

    def test_default_max_turns(self):
        argv = self._argv()
        self.assertIn("--max-turns", argv)
        self.assertEqual(
            argv[argv.index("--max-turns") + 1],
            str(oh.SECURITY_POLICY["agent_default_max_turns"]),
        )

    def test_explicit_max_turns(self):
        argv = self._argv(max_turns=3)
        self.assertEqual(argv[argv.index("--max-turns") + 1], "3")


class AgentCallCharacterization(unittest.TestCase):
    """VAL-612: exact argv, subprocess kwargs and output shaping of _call_agent,
    pinned on HEAD 2aff883 before the function was split into helpers."""

    def _call(self, *args, stdout="done", stderr="", code=0, side_effect=None, **kw):
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], code, stdout, stderr)
            run.side_effect = side_effect
            out = oh._call_agent(*args, **kw)
        return out, run

    def test_claude_code_defaults(self):
        out, run = self._call("task")
        self.assertEqual(out, "done")
        run.assert_called_once_with(
            [
                "claude",
                "-p",
                "task",
                "--output-format",
                "text",
                "--max-turns",
                str(oh.SECURITY_POLICY["agent_default_max_turns"]),
            ],
            capture_output=True,
            text=True,
            timeout=600,
            cwd=None,
        )

    def test_claude_code_all_options(self):
        _, run = self._call(
            "task",
            model="opus",
            max_turns=7,
            timeout=12,
            cwd="/w",
            isolated=True,
            permission_mode="plan",
        )
        run.assert_called_once_with(
            [
                "claude",
                "-p",
                "task",
                "--output-format",
                "text",
                "--model",
                "opus",
                "--max-turns",
                "7",
                "--worktree",
                "--permission-mode",
                "plan",
            ],
            capture_output=True,
            text=True,
            timeout=12,
            cwd="/w",
        )

    def test_codex_argv_ignores_claude_only_options(self):
        _, run = self._call(
            "task",
            agent="codex",
            model="o3",
            max_turns=3,
            isolated=True,
            permission_mode="plan",
        )
        self.assertEqual(
            run.call_args[0][0], ["codex", "exec", "task", "--json", "-m", "o3"]
        )
        _, run = self._call("task", agent="codex")
        self.assertEqual(run.call_args[0][0], ["codex", "exec", "task", "--json"])

    def test_codex_agent_messages_extracted(self):
        lines = [
            '{"item": {"type": "agent_message", "text": "first"}}',
            "not json",
            '{"item": {"type": "reasoning", "text": "hidden"}}',
            "[1, 2]",
            '{"item": "str-item"}',
            '{"other": 1}',
            '{"item": {"type": "agent_message"}}',
            '{"item": {"type": "agent_message", "text": "last"}}',
        ]
        out, _ = self._call("t", agent="codex", stdout="\n".join(lines) + "\n")
        self.assertEqual(out, "first\n\nlast")

    def test_codex_without_messages_keeps_raw_output(self):
        out, _ = self._call("t", agent="codex", stdout="  plain text \n")
        self.assertEqual(out, "plain text")
        out, _ = self._call("t", agent="codex", stdout="")
        self.assertEqual(out, "")

    def test_stderr_appended_only_on_failure(self):
        out, _ = self._call("t", stdout="partial\n", stderr=" boom \n", code=1)
        self.assertEqual(out, "partial\n[stderr]: boom")
        out, _ = self._call("t", stdout="", stderr="boom", code=2, agent="codex")
        self.assertEqual(out, "\n[stderr]: boom")
        out, _ = self._call("t", stdout="ok", stderr="", code=1)
        self.assertEqual(out, "ok")
        out, _ = self._call("t", stdout="ok", stderr="noise", code=0)
        self.assertEqual(out, "ok")

    def test_unknown_agent_never_spawns(self):
        out, run = self._call("t", agent="gemini")
        self.assertEqual(
            out, {"error": "Unknown agent: gemini. Supported: claude-code, codex"}
        )
        run.assert_not_called()

    def test_timeout_and_missing_binary(self):
        out, _ = self._call(
            "t", timeout=5, side_effect=subprocess.TimeoutExpired(["claude"], 5)
        )
        self.assertEqual(
            out,
            {
                "error": "Agent timed out after 5s. Task may be too complex or agent is stuck."
            },
        )
        out, _ = self._call("t", agent="codex", side_effect=FileNotFoundError())
        self.assertEqual(
            out, {"error": "Agent 'codex' not found on PATH. Is it installed?"}
        )


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """Loopback test server: records each request, then redirects or replies."""

    def _handle(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.seen.append((self.command, self.path, self.headers, body))
        code, location = self.server.route(self.path)
        if location:
            self.send_response(code)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        reply = b'{"choices": [{"message": {"content": "ok"}}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    do_GET = do_POST = do_HEAD = _handle

    def log_message(self, *args):
        """Keep test output quiet."""


def _start_server(route):
    """ThreadingHTTPServer on 127.0.0.1:<free port>; route(path) -> (code, location)."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    server.seen = []
    server.route = route
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class LlmRedirectTests(unittest.TestCase):
    """_post_json never follows a redirect: refused, nothing reaches the target."""

    def setUp(self):
        self.sink = _start_server(lambda path: (200, None))
        self.addCleanup(self.sink.server_close)
        self.addCleanup(self.sink.shutdown)
        sink_url = f"http://127.0.0.1:{self.sink.server_port}/steal"
        self.code = 302

        def route(path):
            if path.startswith("/to-sink"):
                return self.code, sink_url
            if path.startswith("/to-self"):
                return self.code, "/ok"
            return 200, None

        self.origin = _start_server(route)
        self.addCleanup(self.origin.server_close)
        self.addCleanup(self.origin.shutdown)
        self.base = f"http://127.0.0.1:{self.origin.server_port}"
        # Only the origin is an allowed llm_call target; the sink is not.
        allow = frozenset({("127.0.0.1", self.origin.server_port)})
        for patcher in (
            mock.patch.object(oh, "_LLM_LOOPBACK_HTTP", allow),
            mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for var in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
            os.environ.pop(var, None)

    def _post(self, path):
        return oh._post_json(
            self.base + path,
            {"prompt": "secret prompt"},
            {"x-api-key": "sk-secret", "Authorization": "Bearer sk-secret"},
            timeout=5,
        )

    def test_direct_reply_still_parsed(self):
        reply = self._post("/ok")
        self.assertEqual(reply["choices"][0]["message"]["content"], "ok")
        method, _, headers, body = self.origin.seen[0]
        self.assertEqual(method, "POST")
        self.assertEqual(headers.get("x-api-key"), "sk-secret")
        self.assertIn(b"secret prompt", body)

    def test_redirect_to_other_host_refused_and_target_untouched(self):
        for code in (301, 302, 303, 307, 308):
            self.code = code
            with self.subTest(code=code):
                with self.assertRaises(oh.UrlRefused) as cm:
                    self._post("/to-sink")
                self.assertIn("redirect", str(cm.exception))
        self.assertEqual(self.sink.seen, [])

    def test_redirect_to_allowed_url_also_refused(self):
        for code in (301, 302, 303, 307, 308):
            self.code = code
            with self.subTest(code=code), self.assertRaises(oh.UrlRefused):
                self._post("/to-self")
        self.assertEqual([p for _, p, _, _ in self.origin.seen if p == "/ok"], [])

    def test_call_llm_reports_refused_redirect(self):
        with mock.patch.dict(oh.LLM_ENDPOINTS, {"local": self.base + "/to-sink"}):
            out = oh._call_llm("secret prompt", backend="local")
        self.assertIsInstance(out, dict)
        self.assertIn("refused", out["error"])
        self.assertEqual(self.sink.seen, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
