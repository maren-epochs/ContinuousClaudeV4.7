"""Table-driven security tests for ouros_harness.py sandbox policy.

Pins the policy tables themselves and checks every entry, so a refactor that
drops, widens or reorders a rule fails here (PREMORTEM tiger 2: this file must
pass before and after any ouros_harness refactor). Complements
tools/test_ouros_policy.py, which covers named bypasses and live execution;
nothing here runs a subprocess.

Covers:
  (a) command_allow: the exact prefix table; every prefix accepted (also via
      .exe/.cmd suffix, upper case and a full path to the executable);
      near-miss prefixes refused
  (b) shell syntax: every operator character and substitution form refused
  (c) command_deny_args: the exact table; each flag refused in both
      `--flag value` and `--flag=value` form on every command that takes it
  (d) read_deny_names: one concrete file per pattern (and its upper-case
      variant) is a secret, denied by read_file and by run_command arguments;
      .env.example/.sample/.template stay readable
  (e) read_deny_dirs: every credential dir under home, plus ~/.claude.json and
      ~/.claude/.credentials*
  (f) outbound HTTP endpoints for llm_call: https + host allowlist; other
      schemes, hosts, userinfo and ports refused

Run: py -3.13 tools/test_ouros_security_table.py
"""

import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT / "tools" / "ouros_harness.py"

_spec = importlib.util.spec_from_file_location("ouros_harness_table", HARNESS)
oh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oh)

POLICY = oh.SECURITY_POLICY

EXPECTED_ALLOW = (
    ("tldr",),
    ("grep",),
    ("rg",),
    ("wc",),
    ("echo",),
    ("git", "log"),
    ("git", "diff"),
    ("git", "show"),
    ("git", "blame"),
    ("cargo", "build"),
    ("cargo", "test"),
    ("cargo", "clippy"),
    ("npm", "test"),
    ("npm", "run"),
    ("python", "-m", "pytest"),
    ("uv", "run", "python"),
)

EXPECTED_DENY_ARGS = (
    "--pre",
    "--output",
    "--ext-diff",
    "--textconv",
    "--open-in-pager",
    "--no-index",
    "--contents",
)

NEAR_MISSES = (
    "git push origin main",
    "git status",
    "git config core.pager x",
    "git -c core.pager=x log",
    "cargo run",
    "cargo install x",
    "npm install x",
    "npm exec x",
    "python script.py",
    "python -c print(1)",
    "python -m pip install x",
    "uv pip install x",
    "uv run pip",
    "claude -p hi",
    "codex exec hi",
    "powershell -c ls",
    "cmd /c dir",
    "bash -c ls",
    "sh -c ls",
    "curl https://example.com",
    "wget https://example.com",
    "rm x",
    "del x",
    "type x",
    "cat x",
    "logit",
    "greppy x",
)

# Unquoted operators split into their own token by shlex and are refused;
# newline/NUL are refused before parsing; command_deny catches the rest.
SHELL_SYNTAX = (
    "git log ; whoami",
    "git log;whoami",
    "git log & whoami",
    "git log && whoami",
    "git log || whoami",
    "git log | whoami",
    "grep x < in.txt",
    "wc -l > out.txt",
    "wc -l >> out.txt",
    "echo $(whoami)",
    "echo (whoami)",
    "git log\nwhoami",
    "git log\rwhoami",
    "git log\x00whoami",
    "echo x | sh",
    "echo x | bash",
    "echo hi; eval x",
    "echo hi; exec x",
)

# Which allowed prefix takes each denied flag in the table.
DENY_ARG_CARRIERS = (
    ("rg", "x", "."),
    ("git", "log"),
    ("git", "diff"),
    ("git", "show"),
    ("git", "blame"),
    ("grep", "x"),
)

# One concrete file name per read_deny_names pattern.
SECRET_NAMES = {
    ".env": ".env",
    ".env.*": ".env.local",
    "*.pem": "server.pem",
    "*.key": "tls.key",
    "*.p12": "cert.p12",
    "*.pfx": "cert.pfx",
    "*.keystore": "release.keystore",
    "id_rsa*": "id_rsa",
    "id_dsa*": "id_dsa",
    "id_ecdsa*": "id_ecdsa",
    "id_ed25519*": "id_ed25519",
    ".credentials*": ".credentials.json",
    ".claude.json": ".claude.json",
    ".netrc": ".netrc",
    "_netrc": "_netrc",
    ".git-credentials": ".git-credentials",
    ".npmrc": ".npmrc",
    ".pypirc": ".pypirc",
}
EXTRA_SECRETS = (".env.production", "id_rsa.pub", "id_ed25519_sk", "deploy.KEY")
READABLE_TEMPLATES = (".env.example", ".env.sample", ".env.template")


def _ok(cmd):
    return oh._check_command_allowed(cmd)[0]


def _denied(result):
    return (
        isinstance(result, dict) and "error" in result and "denied" in result["error"]
    )


class CommandAllowTable(unittest.TestCase):
    def test_allow_table_is_pinned(self):
        self.assertEqual(tuple(POLICY["command_allow"]), EXPECTED_ALLOW)

    def test_every_prefix_accepted(self):
        for prefix in EXPECTED_ALLOW:
            for cmd in (list(prefix), " ".join(prefix), " ".join(prefix) + " x"):
                with self.subTest(cmd=cmd):
                    ok, reason, argv = oh._check_command_allowed(cmd)
                    self.assertTrue(ok, f"{cmd!r}: {reason}")
                    self.assertEqual(argv[0], prefix[0])

    def test_executable_spellings_normalised(self):
        for prefix in EXPECTED_ALLOW:
            exe, rest = prefix[0], list(prefix[1:])
            for spelled in (exe + ".exe", exe + ".cmd", exe.upper(), "C:/bin/" + exe):
                cmd = [spelled] + rest
                with self.subTest(cmd=cmd):
                    ok, reason, argv = oh._check_command_allowed(cmd)
                    self.assertTrue(ok, f"{cmd!r}: {reason}")
                    self.assertEqual(argv[0], exe)

    def test_near_misses_refused(self):
        for cmd in NEAR_MISSES:
            with self.subTest(cmd=cmd):
                self.assertFalse(_ok(cmd), cmd)


class ShellSyntaxTable(unittest.TestCase):
    def test_operators_and_substitution_refused(self):
        for cmd in SHELL_SYNTAX:
            with self.subTest(cmd=cmd):
                self.assertFalse(_ok(cmd), repr(cmd))

    def test_every_operator_char_refused_unquoted(self):
        for ch in sorted(oh._SHELL_OPERATOR_CHARS):
            for cmd in (f"echo a {ch} b", f"echo a{ch}b"):
                with self.subTest(cmd=cmd):
                    self.assertFalse(_ok(cmd), cmd)

    def test_quoted_operators_are_literal_arguments(self):
        ok, _, argv = oh._check_command_allowed('echo "a | b" "c;d"')
        self.assertTrue(ok)
        self.assertEqual(argv, ["echo", "a | b", "c;d"])

    def test_backtick_never_substituted(self):
        ok, _, argv = oh._check_command_allowed("echo `whoami`")
        self.assertTrue(ok)
        self.assertEqual(argv, ["echo", "`whoami`"])


class DenyArgsTable(unittest.TestCase):
    def test_deny_args_table_is_pinned(self):
        self.assertEqual(tuple(POLICY["command_deny_args"]), EXPECTED_DENY_ARGS)

    def test_each_flag_refused_in_both_forms(self):
        for flag in EXPECTED_DENY_ARGS:
            for carrier in DENY_ARG_CARRIERS:
                for form in ([flag, "v"], [flag + "=v"]):
                    cmd = list(carrier) + form
                    with self.subTest(cmd=cmd):
                        ok, reason, _ = oh._check_command_allowed(cmd)
                        self.assertFalse(ok, cmd)
                        self.assertIn("not allowed", reason)

    def test_named_exec_and_write_flags(self):
        for cmd in (
            "rg --pre cmd x .",
            "rg --pre=cmd x .",
            "git diff --output=out.txt",
            "git diff --output out.txt",
            "git log --ext-diff",
            "git show --textconv HEAD",
            "git diff --textconv",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(_ok(cmd), cmd)


class CredentialTable(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self.proj = Path(tempfile.mkdtemp(prefix="ouros-table-")).resolve()
        os.chdir(self.proj)

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self.proj, ignore_errors=True)

    def test_every_deny_pattern_has_a_case(self):
        self.assertEqual(set(SECRET_NAMES), set(POLICY["read_deny_names"]))

    def _secret_names(self):
        names = list(SECRET_NAMES.values()) + list(EXTRA_SECRETS)
        return names + [n.upper() for n in names]

    def test_secret_files_denied(self):
        for name in self._secret_names():
            (self.proj / "f").mkdir(exist_ok=True)
            path = self.proj / "f" / name
            path.write_text("secret", encoding="utf-8")
            with self.subTest(name=name):
                self.assertTrue(oh._is_secret(path), name)
                self.assertTrue(_denied(oh._call_read_file(f"f/{name}")), name)
                self.assertTrue(_denied(oh._call_read_file(f"f/../f/{name}")), name)
                self.assertFalse(_ok(["wc", "-c", f"f/{name}"]), name)
                self.assertFalse(_ok(["git", "show", f"HEAD:f/{name}"]), name)
            path.unlink()

    def test_templates_readable(self):
        for name in READABLE_TEMPLATES:
            (self.proj / name).write_text("KEY=", encoding="utf-8")
            with self.subTest(name=name):
                self.assertFalse(oh._is_secret(self.proj / name))
                self.assertEqual(oh._call_read_file(name), "KEY=")

    def test_home_credential_locations(self):
        home = Path.home()
        cases = [home / d / "config" for d in POLICY["read_deny_dirs"]]
        cases += [home / d for d in POLICY["read_deny_dirs"]]
        cases += [
            home / ".ssh" / "id_ed25519",
            home / ".ssh" / "known_hosts",
            home / ".aws" / "credentials",
            home / ".claude.json",
            home / ".claude" / ".credentials.json",
            home / ".claude" / ".credentials",
            home / ".claude" / ".env",
        ]
        for path in cases:
            with self.subTest(path=str(path)):
                self.assertTrue(oh._is_secret(path), path)
                self.assertTrue(_denied(oh._call_read_file(str(path))), path)
                self.assertFalse(_ok(["wc", "-c", str(path)]), path)


REFUSED_URLS = (
    "",
    "api.anthropic.com/v1/messages",
    "http://api.anthropic.com/v1/messages",
    "ftp://api.openai.com/v1/chat/completions",
    "file:///etc/passwd",
    "gopher://openrouter.ai/",
    "https://evil.example/v1/messages",
    "https://api.anthropic.com.evil.example/v1/messages",
    "https://evilapi.openai.com/v1/chat/completions",
    "https://user@api.anthropic.com/v1/messages",
    "https://user:pw@api.openai.com/v1/chat/completions",
    "https://api.anthropic.com:8443/v1/messages",
    "https://api.anthropic.com:abc/v1/messages",
    "https://localhost:1234/v1/chat/completions",
    "http://localhost:8080/v1/chat/completions",
    "http://localhost/v1/chat/completions",
    "http://169.254.169.254/latest/meta-data/",
    "http://0.0.0.0:1234/v1/chat/completions",
    "http://localhost.evil.example:1234/v1/chat/completions",
)


def _fake_urlopen(payload):
    from unittest import mock

    cm = mock.MagicMock()
    cm.__enter__.return_value.read.return_value = payload
    return mock.MagicMock(return_value=cm)


class LlmEndpointTable(unittest.TestCase):
    """llm_call only POSTs to the documented provider endpoints."""

    REPLIES = {
        "local": b'{"choices": [{"message": {"content": "ok-local"}}]}',
        "anthropic": b'{"content": [{"text": "ok-anthropic"}]}',
        "openai": b'{"choices": [{"message": {"content": "ok-openai"}}]}',
        "openrouter": b'{"choices": [{"message": {"content": "ok-openrouter"}}]}',
    }

    def setUp(self):
        from unittest import mock

        self._endpoints = dict(oh.LLM_ENDPOINTS)
        keys = {
            "ANTHROPIC_API_KEY": "k",
            "OPENAI_API_KEY": "k",
            "OPENROUTER_API_KEY": "k",
        }
        patcher = mock.patch.dict(os.environ, keys)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        oh.LLM_ENDPOINTS.clear()
        oh.LLM_ENDPOINTS.update(self._endpoints)

    def test_endpoint_table_is_pinned(self):
        self.assertEqual(
            oh.LLM_ENDPOINTS,
            {
                "local": "http://localhost:1234/v1/chat/completions",
                "anthropic": "https://api.anthropic.com/v1/messages",
                "openai": "https://api.openai.com/v1/chat/completions",
                "openrouter": "https://openrouter.ai/api/v1/chat/completions",
            },
        )

    def test_documented_endpoints_allowed(self):
        urls = list(oh.LLM_ENDPOINTS.values()) + [
            "https://API.ANTHROPIC.COM/v1/messages",
            "https://api.openai.com:443/v1/chat/completions",
            "http://127.0.0.1:1234/v1/chat/completions",
        ]
        for url in urls:
            with self.subTest(url=url):
                ok, reason = oh._check_url_allowed(url)
                self.assertTrue(ok, f"{url}: {reason}")

    def test_other_schemes_hosts_userinfo_ports_refused(self):
        for url in REFUSED_URLS:
            with self.subTest(url=url):
                ok, reason = oh._check_url_allowed(url)
                self.assertFalse(ok, url)
                self.assertTrue(reason)

    def test_post_json_refuses_before_any_request(self):
        from unittest import mock

        with mock.patch("urllib.request.urlopen") as urlopen:
            for url in REFUSED_URLS:
                with self.subTest(url=url), self.assertRaises(oh.UrlRefused):
                    oh._post_json(url, {"x": 1}, {}, timeout=1)
        urlopen.assert_not_called()

    def test_each_backend_posts_to_its_endpoint(self):
        from unittest import mock

        for backend, reply in self.REPLIES.items():
            fake = _fake_urlopen(reply)
            with (
                self.subTest(backend=backend),
                mock.patch("urllib.request.urlopen", fake),
            ):
                self.assertEqual(oh._call_llm("hi", backend=backend), f"ok-{backend}")
                req = fake.call_args[0][0]
                self.assertEqual(req.full_url, oh.LLM_ENDPOINTS[backend])
                self.assertEqual(req.get_method(), "POST")

    def test_tampered_endpoint_refused(self):
        from unittest import mock

        crafted = {
            "anthropic": "https://evil.example/v1/messages",
            "openai": "http://api.openai.com/v1/chat/completions",
            "openrouter": "file:///etc/passwd",
            "local": "http://169.254.169.254:1234/latest",
        }
        for backend, url in crafted.items():
            oh.LLM_ENDPOINTS[backend] = url
            with (
                self.subTest(backend=backend),
                mock.patch("urllib.request.urlopen") as urlopen,
            ):
                r = oh._call_llm("hi", backend=backend)
                urlopen.assert_not_called()
                self.assertIsInstance(r, dict)
                self.assertIn("refused", r["error"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
