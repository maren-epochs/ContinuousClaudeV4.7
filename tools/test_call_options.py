"""Call-form tests for the option-object signatures (VAL-701).

Characterization (classes *CallForms): every call form found in harness/skills,
CLAUDE.md, README, demo.py, tools/ and tests/ - positional and keyword - for
exa_search.exa_search and the sandbox externals exa_search / llm_call /
agent_call (_call_exa_search[_sync], _call_llm, _call_agent). Expected values
were captured on HEAD 161e58b, before the option objects existed.

Option objects (classes *Options): each call site builds a frozen dataclass from
its keywords, keeps the first argument positional and its signature <= 5
parameters (tldr long_param_list), and names an unknown keyword in a TypeError.

Run: py -3.13 tools/test_call_options.py
"""

import dataclasses
import inspect
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from _testing import FakeResponse, fake_aiohttp, load_module, run_async

PROJECT = Path(__file__).resolve().parent.parent
exa = load_module("exa_search_call_forms", PROJECT / "tools" / "exa_search.py")
oh = load_module("ouros_harness_call_forms", PROJECT / "tools" / "ouros_harness.py")

ALL_OPTIONS_PAYLOAD = [
    ("query", "q"),
    ("numResults", 3),
    ("type", "deep"),
    ("contents", {"text": {"maxCharacters": 10}}),
    ("category", "news"),
    ("includeDomains", ["a.com"]),
    ("startPublishedDate", "2025-01-01"),
    ("endPublishedDate", "2026-01-01"),
]
DEFAULT_PAYLOAD = {
    "query": "q",
    "numResults": 5,
    "type": "auto",
    "contents": {
        "text": False,
        "highlights": {"maxCharacters": 2000, "query": "q"},
        "summary": {"query": "q"},
    },
}


def _payload(*args, **kwargs):
    """The JSON payload exa_search POSTs for this call (fake aiohttp, fake key)."""
    log = []
    resp = FakeResponse(200, {"results": []})
    with (
        mock.patch.dict(sys.modules, {"aiohttp": fake_aiohttp(log, resp)}),
        mock.patch.object(exa, "load_api_key", return_value="k1"),
    ):
        result = run_async(exa.exa_search, *args, **kwargs)
    assert result == {"results": []}, result
    return log[0][2]["json"]


class ExaSearchCallForms(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(_payload("q"), DEFAULT_PAYLOAD)

    def test_query_keyword(self):
        self.assertEqual(_payload(query="q"), DEFAULT_PAYLOAD)

    def test_oracle_form(self):  # harness/agents/oracle.md, CLAUDE.md
        self.assertEqual(_payload("q", num_results=5), DEFAULT_PAYLOAD)

    def test_all_positional(self):
        payload = _payload(
            "q", 3, "deep", "news", ["a.com"], 10, 20, True, False, False,
            "2025-01-01", "2026-01-01",
        )  # fmt: skip
        self.assertEqual(list(payload.items()), ALL_OPTIONS_PAYLOAD)

    def test_mixed_positional_and_keyword(self):
        payload = _payload(
            "q", 3, "deep", category="news", domains=["a.com"], max_chars=10,
            highlight_chars=20, with_text=True, highlights=False, summary=False,
            start_date="2025-01-01", end_date="2026-01-01",
        )  # fmt: skip
        self.assertEqual(list(payload.items()), ALL_OPTIONS_PAYLOAD)

    def test_research_skill_form(self):  # harness/skills/research/SKILL.md
        payload = _payload("q", num_results=5, with_text=True)
        self.assertEqual(payload["contents"]["text"], {"maxCharacters": 1500})

    def test_unknown_keyword_names_it(self):
        with self.assertRaises(TypeError) as ctx:
            _payload("q", bogus_opt=1)
        self.assertIn("bogus_opt", str(ctx.exception))

    def test_too_many_positionals(self):
        with self.assertRaises(TypeError):
            _payload(
                "q", 3, "deep", None, None, 10, 20, True, False, False, "a", "b", 1
            )

    def test_positional_and_keyword_twice(self):
        with self.assertRaises(TypeError) as ctx:
            _payload("q", 3, num_results=4)
        self.assertIn("num_results", str(ctx.exception))


class _FakeExaModule:
    """sys.modules["exa_search"] stand-in recording the bridge's exa_search kwargs."""

    def __init__(self, key="k"):
        self.calls = []
        self.module = types.ModuleType("exa_search")
        self.module.load_api_key = lambda: key  # type: ignore[attr-defined]
        self.module.exa_search = self._search  # type: ignore[attr-defined]

    async def _search(self, **kwargs):
        self.calls.append(kwargs)
        return {"results": ["r"]}


def _bridge(*args, key="k", **kwargs):
    """(result, recorded exa_search kwargs) for one _call_exa_search_sync call."""
    fake = _FakeExaModule(key)
    with mock.patch.dict(sys.modules, {"exa_search": fake.module}):
        result = run_async(oh._call_exa_search, *args, **kwargs)
    return result, fake.calls


class ExaBridgeCallForms(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(
            _bridge("q"),
            (
                {"results": ["r"]},
                [{"query": "q", "num_results": 5, "with_text": False}],
            ),
        )

    def test_query_keyword(self):
        _, calls = _bridge(query="q", num_results=2)
        self.assertEqual(calls, [{"query": "q", "num_results": 2, "with_text": False}])

    def test_all_positional(self):
        _, calls = _bridge("q", 3, "news", ["a.com"], True, "2025-01-01")
        self.assertEqual(
            calls,
            [
                {
                    "query": "q",
                    "num_results": 3,
                    "with_text": True,
                    "category": "news",
                    "domains": ["a.com"],
                    "start_date": "2025-01-01",
                }
            ],
        )

    def test_research_skill_form(self):
        _, calls = _bridge("q", num_results=5, with_text=True)
        self.assertEqual(calls, [{"query": "q", "num_results": 5, "with_text": True}])

    def test_domains_must_be_list_and_falsy_filters_dropped(self):
        _, calls = _bridge("q", category="", domains="a.com", start_date="")
        self.assertEqual(calls, [{"query": "q", "num_results": 5, "with_text": False}])

    def test_missing_key(self):
        self.assertEqual(_bridge("q", key=""), ({"error": "EXA_API_KEY not found"}, []))

    def test_research_package_internal_form(self):
        fake = _FakeExaModule()
        with mock.patch.dict(sys.modules, {"exa_search": fake.module}):
            out = run_async(lambda: _sync_in_thread("q", num_results=4))
        self.assertEqual(out, {"results": ["r"]})
        self.assertEqual(
            fake.calls, [{"query": "q", "num_results": 4, "with_text": False}]
        )

    def test_unknown_keyword_names_it(self):
        with self.assertRaises(TypeError) as ctx:
            _bridge("q", search_type="deep")
        self.assertIn("search_type", str(ctx.exception))

    def test_sandbox_dispatch_reports_unknown_keyword(self):
        fake = _FakeExaModule()
        progress = {
            "function_name": "exa_search",
            "args": ["q"],
            "kwargs": {"bogus": 1},
        }
        with mock.patch.dict(sys.modules, {"exa_search": fake.module}):
            out = run_async(lambda: _dispatch_in_thread(progress))
        self.assertIn("exa_search failed", out["error"])
        self.assertIn("bogus", out["error"])


async def _sync_in_thread(*args, **kwargs):
    """_call_exa_search_sync runs its own loop, so call it from a loop-free thread."""
    import asyncio

    return await asyncio.to_thread(oh._call_exa_search_sync, *args, **kwargs)


async def _dispatch_in_thread(progress):
    """The harness dispatcher for one external call, off the running loop."""
    import asyncio

    return await asyncio.to_thread(oh._dispatch_external, progress)


class LlmCallForms(unittest.TestCase):
    KEYS = {"ANTHROPIC_API_KEY": "ka", "OPENROUTER_API_KEY": "kr"}
    ANTHROPIC = {"content": [{"text": "A"}]}
    CHAT = {"choices": [{"message": {"content": "C"}}]}

    def _run(self, reply, *args, **kwargs):
        with (
            mock.patch.dict(os.environ, self.KEYS),
            mock.patch.object(oh, "_post_json", return_value=reply) as post,
        ):
            return oh._call_llm(*args, **kwargs), post.call_args.args

    def test_defaults(self):
        out, (url, body, headers, timeout) = self._run(self.ANTHROPIC, "p")
        self.assertEqual(out, "A")
        self.assertEqual(url, oh.LLM_ENDPOINTS["anthropic"])
        self.assertEqual(
            body,
            {
                "model": "claude-haiku-4-5-20251001",
                "messages": [{"role": "user", "content": "p"}],
                "max_tokens": 1000,
            },
        )
        self.assertEqual(headers["x-api-key"], "ka")
        self.assertEqual(timeout, 120)

    def test_all_positional(self):
        out, (url, body, _, timeout) = self._run(
            self.CHAT, "p", "m", 50, "S", 0.5, "openrouter"
        )
        self.assertEqual(out, "C")
        self.assertEqual(url, oh.LLM_ENDPOINTS["openrouter"])
        self.assertEqual(
            body,
            {
                "model": "m",
                "messages": [{"role": "user", "content": "p"}],
                "max_tokens": 50,
                "temperature": 0.5,
            },
        )
        self.assertEqual(timeout, 120)

    def test_research_skill_form(self):
        out, (_, body, _, _) = self._run(
            self.ANTHROPIC,
            "p",
            model="claude-sonnet-4-6",
            backend="anthropic",
            max_tokens=2000,
        )
        self.assertEqual(out, "A")
        self.assertEqual(
            (body["model"], body["max_tokens"]), ("claude-sonnet-4-6", 2000)
        )

    def test_prompt_keyword_and_system_temperature(self):
        _, (_, body, _, _) = self._run(
            self.ANTHROPIC, prompt="p", system="S", temperature=0.2
        )
        self.assertEqual((body["system"], body["temperature"]), ("S", 0.2))

    def test_unknown_backend_is_error_dict(self):
        out = oh._call_llm("p", backend="x")
        self.assertIn("Unknown backend: x", out["error"])

    def test_unknown_keyword_names_it(self):
        with self.assertRaises(TypeError) as ctx:
            oh._call_llm("p", top_p=0.9)
        self.assertIn("top_p", str(ctx.exception))

    def test_too_many_positionals(self):
        with self.assertRaises(TypeError):
            oh._call_llm("p", "m", 1, None, 0.0, "anthropic", "extra")


class AgentCallForms(unittest.TestCase):
    def _run(self, *args, **kwargs):
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "done", "")
            out = oh._call_agent(*args, **kwargs)
        return out, run.call_args

    def test_defaults(self):
        out, call = self._run("t")
        self.assertEqual(out, "done")
        self.assertEqual(
            call.args[0],
            ["claude", "-p", "t", "--output-format", "text", "--max-turns", "25"],
        )
        self.assertEqual(
            call.kwargs,
            {"capture_output": True, "text": True, "timeout": 600, "cwd": None},
        )

    def test_all_positional(self):
        _, call = self._run("t", "claude-code", "opus", 3, 9, "/w", True, "plan")
        self.assertEqual(
            call.args[0],
            [
                "claude", "-p", "t", "--output-format", "text", "--model", "opus",
                "--max-turns", "3", "--worktree", "--permission-mode", "plan",
            ],
        )  # fmt: skip
        self.assertEqual((call.kwargs["timeout"], call.kwargs["cwd"]), (9, "/w"))

    def test_research_skill_form(self):
        _, call = self._run("t", agent="claude-code", model="sonnet", max_turns=5)
        self.assertEqual(
            call.args[0],
            [
                "claude", "-p", "t", "--output-format", "text", "--model", "sonnet",
                "--max-turns", "5",
            ],
        )  # fmt: skip

    def test_prompt_keyword_codex(self):
        _, call = self._run(prompt="t", agent="codex", model="o3", timeout=5)
        self.assertEqual(call.args[0], ["codex", "exec", "t", "--json", "-m", "o3"])
        self.assertEqual(call.kwargs["timeout"], 5)

    def test_unknown_agent_is_error_dict(self):
        out, call = self._run("t", agent="x")
        self.assertIn("Unknown agent: x", out["error"])
        self.assertIsNone(call)

    def test_unknown_keyword_names_it(self):
        with self.assertRaises(TypeError) as ctx:
            oh._call_agent("t", max_turn=3)
        self.assertIn("max_turn", str(ctx.exception))


def _fields(cls):
    """{field name: default} of a dataclass, in declaration order."""
    return {f.name: f.default for f in dataclasses.fields(cls)}


class OptionObjects(unittest.TestCase):
    CASES = (
        (
            "exa_search.exa_search",
            lambda: exa.exa_search,
            lambda: exa.ExaSearchOptions,
            {
                "num_results": 5,
                "search_type": "auto",
                "category": None,
                "domains": None,
                "max_chars": 1500,
                "highlight_chars": 2000,
                "with_text": False,
                "highlights": True,
                "summary": True,
                "start_date": None,
                "end_date": None,
            },
        ),
        (
            "_call_exa_search",
            lambda: oh._call_exa_search,
            lambda: oh.ExaCallOptions,
            {
                "num_results": 5,
                "category": None,
                "domains": None,
                "with_text": False,
                "start_date": None,
            },
        ),
        (
            "_call_llm",
            lambda: oh._call_llm,
            lambda: oh.LlmCallOptions,
            {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1000,
                "system": None,
                "temperature": 0.0,
                "backend": "anthropic",
            },
        ),
        (
            "_call_agent",
            lambda: oh._call_agent,
            lambda: oh.AgentCallOptions,
            {
                "agent": "claude-code",
                "model": None,
                "max_turns": None,
                "timeout": 600,
                "cwd": None,
                "isolated": False,
                "permission_mode": "default",
            },
        ),
    )

    def test_frozen_dataclass_with_documented_defaults(self):
        for name, _, cls, defaults in self.CASES:
            with self.subTest(name):
                opts_cls = cls()
                self.assertTrue(dataclasses.is_dataclass(opts_cls))
                self.assertEqual(_fields(opts_cls), defaults)
                opts = opts_cls()
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    opts.__setattr__(next(iter(defaults)), 1)

    def test_short_signature_first_argument_positional(self):
        for name, fn, _, _ in self.CASES:
            with self.subTest(name):
                params = list(inspect.signature(fn()).parameters.values())
                self.assertLessEqual(len(params), 5)
                self.assertIs(params[0].kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)


if __name__ == "__main__":
    unittest.main()
