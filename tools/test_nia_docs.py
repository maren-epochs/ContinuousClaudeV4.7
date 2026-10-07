"""Characterization tests for tools/nia_docs.py (VAL-612).

Pins the CLI dispatch of main() (which API function each subcommand calls,
with which arguments, and the exact stdout) and the output of the
format_* helpers, so the complexity refactor of main / format_search_result
is behavior-preserving. Expected values were captured on the pre-refactor
code (HEAD 2aff883).

No network: API functions are replaced by recording fakes for the CLI tests,
and a fake `aiohttp` module (the API functions import it locally) stands in
for the HTTP layer in the request tests.

Run: py -3.13 tools/test_nia_docs.py
"""

import asyncio
import concurrent.futures
import contextlib
import importlib.util
import io
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parent.parent
NIA = PROJECT / "tools" / "nia_docs.py"


def _load():
    # NIA_API_KEY is read at import; set it so no ~/.claude/.env or ./.env is read.
    with mock.patch.dict(os.environ, {"NIA_API_KEY": "test-key"}):
        spec = importlib.util.spec_from_file_location("nia_docs_under_test", NIA)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


nia = _load()

LIST_RESULT = {
    "repositories": [{"display_name": "repo-a", "status": "ready"}, {"id": "r2"}],
    "total": 7,
}
SEARCH_RESULT = {
    "results": [
        {"title": "hit", "score": 0.5, "source": {"url": "https://x"}, "snippet": "s"}
    ]
}
JSON_RESULT = {"id": "r1", "n": 1}
ORACLE_RESULT = {
    "final_report": "report body",
    "citations": [{"tool": "grep", "summary": "found it"}],
    "duration_ms": 5,
    "iterations": 2,
}

# API function -> canned return value
RETURNS = {
    "oracle_research": ORACLE_RESULT,
    "oracle_research_stream": None,
    "oracle_list_sessions": LIST_RESULT,
    "oracle_get_session": JSON_RESULT,
    "oracle_get_messages": JSON_RESULT,
    "oracle_chat_followup": None,
    "oracle_list_jobs": LIST_RESULT,
    "oracle_create_job": JSON_RESULT,
    "oracle_get_job": JSON_RESULT,
    "oracle_cancel_job": {"status": "cancelled", "job_id": "j1"},
    "oracle_stream_job_events": None,
    "search_query": SEARCH_RESULT,
    "search_web": SEARCH_RESULT,
    "search_deep": {"content": "deep answer", "sources": ["s1"]},
    "search_universal": SEARCH_RESULT,
    "search_package_hybrid": SEARCH_RESULT,
    "search_package_grep": {"matches": [{"path": "a.py", "line": "x = 1"}]},
    "repos_list": LIST_RESULT,
    "repos_index": JSON_RESULT,
    "repos_status": JSON_RESULT,
    "repos_tree": JSON_RESULT,
    "repos_content": {"content": "file body"},
    "repos_grep": SEARCH_RESULT,
    "repos_delete": {"status": "deleted", "repository_id": "r1"},
    "sources_list": LIST_RESULT,
    "sources_index": JSON_RESULT,
    "sources_get": JSON_RESULT,
    "sources_tree": JSON_RESULT,
    "sources_content": {"path": "p", "no_content": True},
    "sources_grep": SEARCH_RESULT,
    "sources_delete": {"status": "deleted", "source_id": "s1"},
    "papers_list": LIST_RESULT,
    "papers_index": JSON_RESULT,
    "context_list": LIST_RESULT,
    "context_save": JSON_RESULT,
    "context_search_text": SEARCH_RESULT,
    "context_search_semantic": SEARCH_RESULT,
    "context_get": JSON_RESULT,
    "context_delete": {"status": "deleted", "context_id": "c1"},
}
STREAMERS = {
    "oracle_research_stream",
    "oracle_chat_followup",
    "oracle_stream_job_events",
}

# (case id, argv after the script name)
CASES = [
    ("oracle-research", ["oracle", "research", "q1", "--repos", "r1", "r2"]),
    (
        "oracle-research-stream",
        ["oracle", "research", "q1", "--stream", "--sources", "s1"],
    ),
    (
        "oracle-research-model",
        ["oracle", "research", "q1", "--model", "claude-sonnet-4-5-1m"],
    ),
    ("oracle-sessions", ["oracle", "sessions", "--limit", "3"]),
    ("oracle-session", ["oracle", "session", "sid"]),
    ("oracle-session-messages", ["oracle", "session", "sid", "--messages"]),
    ("oracle-chat", ["oracle", "chat", "sid", "more please"]),
    ("oracle-jobs", ["oracle", "jobs"]),
    ("oracle-job", ["oracle", "job", "j1"]),
    ("oracle-job-cancel", ["oracle", "job", "j1", "--cancel"]),
    ("oracle-job-stream", ["oracle", "job", "j1", "--stream"]),
    ("oracle-job-cancel-and-stream", ["oracle", "job", "j1", "--cancel", "--stream"]),
    ("oracle-create-job", ["oracle", "create-job", "q2", "--repos", "r1"]),
    ("oracle-no-action", ["oracle"]),
    ("search-universal", ["search", "universal", "auth", "--limit", "4"]),
    ("search-web", ["search", "web", "vss", "--category", "news", "--time", "week"]),
    ("search-deep", ["search", "deep", "hybrid"]),
    ("search-package", ["search", "package", "fastapi", "--query", "di"]),
    ("search-package-noquery", ["search", "package", "fastapi", "--registry", "npm"]),
    (
        "search-package-grep",
        ["search", "package", "fastapi", "--grep", "Depends", "--limit", "2"],
    ),
    ("search-query", ["search", "query", "how", "--repos", "r1", "--sources", "s1"]),
    ("search-no-action", ["search"]),
    (
        "repos-list",
        ["repos", "list", "--filter", "ab", "--status", "ready", "--limit", "5"],
    ),
    ("repos-index", ["repos", "index", "o/r", "--token", "tok"]),
    ("repos-status", ["repos", "status", "r1"]),
    ("repos-tree", ["repos", "tree", "r1"]),
    ("repos-content", ["repos", "content", "r1", "src/a.txt"]),
    ("repos-grep", ["repos", "grep", "r1", "pat", "--context", "5"]),
    ("repos-delete", ["repos", "delete", "r1"]),
    ("sources-list", ["sources", "list", "--filter", "doc", "--limit", "9"]),
    ("sources-index", ["sources", "index", "https://docs.x", "--name", "X"]),
    ("sources-get", ["sources", "get", "s1"]),
    ("sources-tree", ["sources", "tree", "s1"]),
    ("sources-content", ["sources", "content", "s1", "/page"]),
    ("sources-grep", ["sources", "grep", "s1", "pat"]),
    ("sources-delete", ["sources", "delete", "s1"]),
    ("papers-list", ["papers", "list", "--status", "ok", "--limit", "2"]),
    ("papers-index", ["papers", "index", "2310.06825"]),
    ("context-list", ["context", "list", "--tags", "a,b", "--limit", "4"]),
    (
        "context-save",
        [
            "context",
            "save",
            "--title",
            "T",
            "--content",
            "C",
            "--summary",
            "S",
            "--tags",
            "x",
            "y",
        ],
    ),
    ("context-search", ["context", "search", "emb"]),
    ("context-search-semantic", ["context", "search", "emb", "--semantic"]),
    ("context-get", ["context", "get", "c1"]),
    ("context-delete", ["context", "delete", "c1"]),
    ("script-arg-filtered", ["tools/nia_docs.py", "repos", "status", "r1"]),
]


def run_async(fn, *args):
    """asyncio.run(fn(*args)) on a worker thread.

    A sync Playwright session started earlier in the same pytest process (the
    viz suites) leaves an event loop running on the main thread, where
    asyncio.run() refuses to start; a fresh thread has no running loop.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, fn(*args)).result()


def _fake(name, calls, ret):
    async def fake(*args, **kwargs):
        calls.append((name, args, kwargs))
        if name in STREAMERS:
            print(f"<{name} streamed>")
        return ret

    return fake


def run_cli(argv, returns=None, key="test-key"):
    """Run nia.main() with argv; returns (stdout, stderr, calls)."""
    calls = []
    table = dict(RETURNS, **(returns or {}))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.ExitStack() as stack:
        for name, ret in table.items():
            fake = ret if callable(ret) else _fake(name, calls, ret)
            stack.enter_context(mock.patch.object(nia, name, fake))
        stack.enter_context(mock.patch.object(nia, "NIA_API_KEY", key))
        stack.enter_context(mock.patch.object(sys, "argv", ["nia_docs.py", *argv]))
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        run_async(nia.main)
    return out.getvalue(), err.getvalue(), calls


# Captured on HEAD 2aff883 (pre-refactor): case id -> (calls, stdout)
EXPECTED = {
    "oracle-research": (
        [
            (
                "oracle_research",
                ("q1", ["r1", "r2"], None),
                {"model": "claude-opus-4-5-20251101"},
            )
        ],
        "Running Oracle research: q1\n# Oracle Research Result\n\nreport body\n\n## Citations\n1. [grep] found it\n\n---\nDuration: 5ms | Iterations: 2\n",
    ),
    "oracle-research-stream": (
        [
            (
                "oracle_research_stream",
                ("q1", None, ["s1"], "claude-opus-4-5-20251101"),
                {},
            )
        ],
        "Running Oracle research: q1\n<oracle_research_stream streamed>\n",
    ),
    "oracle-research-model": (
        [("oracle_research", ("q1", None, None), {"model": "claude-sonnet-4-5-1m"})],
        "Running Oracle research: q1\n# Oracle Research Result\n\nreport body\n\n## Citations\n1. [grep] found it\n\n---\nDuration: 5ms | Iterations: 2\n",
    ),
    "oracle-sessions": (
        [("oracle_list_sessions", (3,), {})],
        "# Oracle Sessions\n\n1. repo-a (ready)\n2. r2 \n\n---\nTotal: 7\n",
    ),
    "oracle-session": (
        [("oracle_get_session", ("sid",), {})],
        '{\n  "id": "r1",\n  "n": 1\n}\n',
    ),
    "oracle-session-messages": (
        [("oracle_get_messages", ("sid",), {})],
        '{\n  "id": "r1",\n  "n": 1\n}\n',
    ),
    "oracle-chat": (
        [("oracle_chat_followup", ("sid", "more please"), {})],
        "<oracle_chat_followup streamed>\n",
    ),
    "oracle-jobs": (
        [("oracle_list_jobs", (), {})],
        "# Oracle Jobs\n\n1. repo-a (ready)\n2. r2 \n\n---\nTotal: 7\n",
    ),
    "oracle-job": (
        [("oracle_get_job", ("j1",), {})],
        '{\n  "id": "r1",\n  "n": 1\n}\n',
    ),
    "oracle-job-cancel": (
        [("oracle_cancel_job", ("j1",), {})],
        '{\n  "status": "cancelled",\n  "job_id": "j1"\n}\n',
    ),
    "oracle-job-stream": (
        [("oracle_stream_job_events", ("j1",), {})],
        "<oracle_stream_job_events streamed>\n",
    ),
    "oracle-job-cancel-and-stream": (
        [("oracle_cancel_job", ("j1",), {})],
        '{\n  "status": "cancelled",\n  "job_id": "j1"\n}\n',
    ),
    "oracle-create-job": (
        [("oracle_create_job", ("q2", ["r1"]), {"model": "claude-opus-4-5-20251101"})],
        '{\n  "id": "r1",\n  "n": 1\n}\n',
    ),
    "oracle-no-action": ([], ""),
    "search-universal": (
        [("search_universal", ("auth", 4), {})],
        "Universal search: auth\n# Universal Search Results\n\n\n1. **hit** (score: 0.500)\n   https://x\n   s\n",
    ),
    "search-web": (
        [("search_web", ("vss", "news", "week"), {})],
        "Web search: vss\n# Web Search Results\n\n\n1. **hit** (score: 0.500)\n   https://x\n   s\n",
    ),
    "search-deep": (
        [("search_deep", ("hybrid",), {})],
        "Deep research: hybrid\n# Deep Research Results\n\ndeep answer\n\n## Sources\n- s1\n",
    ),
    "search-package": (
        [("search_package_hybrid", ("fastapi", "di", "py_pi", 10), {})],
        "Package search: fastapi / di\n# Package Search Results\n\n\n1. **hit** (score: 0.500)\n   https://x\n   s\n",
    ),
    "search-package-noquery": (
        [("search_package_hybrid", ("fastapi", "", "npm", 10), {})],
        "Package search: fastapi / None\n# Package Search Results\n\n\n1. **hit** (score: 0.500)\n   https://x\n   s\n",
    ),
    "search-package-grep": (
        [("search_package_grep", ("fastapi", "Depends", "py_pi", 2), {})],
        "Package grep: fastapi / Depends\n# Package Search Results\n\n\n1. `a.py`\n   x = 1\n",
    ),
    "search-query": (
        [("search_query", ([{"role": "user", "content": "how"}], ["r1"], ["s1"]), {})],
        "# Query Results\n\n\n1. **hit** (score: 0.500)\n   https://x\n   s\n",
    ),
    "search-no-action": ([], ""),
    "repos-list": (
        [("repos_list", ("ab", "ready", 5), {})],
        "# Repositories\n\n1. repo-a (ready)\n2. r2 \n\n---\nTotal: 7\n",
    ),
    "repos-index": (
        [("repos_index", ("o/r", "tok"), {})],
        'Indexing repository: o/r\n{\n  "id": "r1",\n  "n": 1\n}\n',
    ),
    "repos-status": (
        [("repos_status", ("r1",), {})],
        '{\n  "id": "r1",\n  "n": 1\n}\n',
    ),
    "repos-tree": ([("repos_tree", ("r1",), {})], '{\n  "id": "r1",\n  "n": 1\n}\n'),
    "repos-content": ([("repos_content", ("r1", "src/a.txt"), {})], "file body\n"),
    "repos-grep": (
        [("repos_grep", ("r1", "pat", 5), {})],
        "# Repository Grep Results\n\n\n1. **hit** (score: 0.500)\n   https://x\n   s\n",
    ),
    "repos-delete": (
        [("repos_delete", ("r1",), {})],
        '{\n  "status": "deleted",\n  "repository_id": "r1"\n}\n',
    ),
    "sources-list": (
        [("sources_list", ("doc", None), {"limit": 9})],
        "# Data Sources\n\n1. repo-a (ready)\n2. r2 \n\n---\nTotal: 7\n",
    ),
    "sources-index": (
        [("sources_index", ("https://docs.x", "X"), {})],
        'Indexing: https://docs.x\n{\n  "id": "r1",\n  "n": 1\n}\n',
    ),
    "sources-get": ([("sources_get", ("s1",), {})], '{\n  "id": "r1",\n  "n": 1\n}\n'),
    "sources-tree": (
        [("sources_tree", ("s1",), {})],
        '{\n  "id": "r1",\n  "n": 1\n}\n',
    ),
    "sources-content": (
        [("sources_content", ("s1", "/page"), {})],
        '{\n  "path": "p",\n  "no_content": true\n}\n',
    ),
    "sources-grep": (
        [("sources_grep", ("s1", "pat"), {})],
        "# Source Grep Results\n\n\n1. **hit** (score: 0.500)\n   https://x\n   s\n",
    ),
    "sources-delete": (
        [("sources_delete", ("s1",), {})],
        '{\n  "status": "deleted",\n  "source_id": "s1"\n}\n',
    ),
    "papers-list": (
        [("papers_list", (2,), {"status": "ok"})],
        "# Research Papers\n\n1. repo-a (ready)\n2. r2 \n\n---\nTotal: 7\n",
    ),
    "papers-index": (
        [("papers_index", ("2310.06825",), {})],
        'Indexing arXiv paper: 2310.06825\n{\n  "id": "r1",\n  "n": 1\n}\n',
    ),
    "context-list": (
        [("context_list", (4,), {"tags": "a,b"})],
        "# Contexts\n\n1. repo-a (ready)\n2. r2 \n\n---\nTotal: 7\n",
    ),
    "context-save": (
        [("context_save", ("T", "C", "S", ["x", "y"]), {})],
        '{\n  "id": "r1",\n  "n": 1\n}\n',
    ),
    "context-search": (
        [("context_search_text", ("emb",), {})],
        "# Context Search Results\n\n\n1. **hit** (score: 0.500)\n   https://x\n   s\n",
    ),
    "context-search-semantic": (
        [("context_search_semantic", ("emb",), {})],
        "# Context Search Results\n\n\n1. **hit** (score: 0.500)\n   https://x\n   s\n",
    ),
    "context-get": ([("context_get", ("c1",), {})], '{\n  "id": "r1",\n  "n": 1\n}\n'),
    "context-delete": (
        [("context_delete", ("c1",), {})],
        '{\n  "status": "deleted",\n  "context_id": "c1"\n}\n',
    ),
    "script-arg-filtered": (
        [("repos_status", ("r1",), {})],
        '{\n  "id": "r1",\n  "n": 1\n}\n',
    ),
}


class CliDispatch(unittest.TestCase):
    def test_every_subcommand_pinned(self):
        self.assertEqual(sorted(EXPECTED), sorted(c for c, _ in CASES))
        for case, argv in CASES:
            with self.subTest(case=case):
                out, _, calls = run_cli(argv)
                want_calls, want_out = EXPECTED[case]
                self.assertEqual(calls, want_calls)
                self.assertEqual(out, want_out)

    def test_no_args_prints_help(self):
        out, _, calls = run_cli([])
        self.assertTrue(out.startswith("usage: "), out[:80])
        self.assertIn("{oracle,search,repos,sources,papers,context}", out)
        self.assertEqual(calls, [])

    def test_only_script_args_prints_help(self):
        out, _, calls = run_cli(["nia_docs.py"])
        self.assertTrue(out.startswith("usage: "))
        self.assertEqual(calls, [])

    def test_missing_key_stops_before_any_call(self):
        out, _, calls = run_cli(["repos", "list"], key="")
        self.assertEqual(
            out, "Error: NIA_API_KEY not found. Set in environment or ~/.claude/.env\n"
        )
        self.assertEqual(calls, [])

    def test_import_error_message(self):
        async def no_aiohttp(*a, **kw):
            raise ImportError("No module named 'aiohttp'")

        out, _, _ = run_cli(["repos", "list"], {"repos_list": no_aiohttp})
        self.assertEqual(
            out, "Error: aiohttp not installed. Run: pip install aiohttp\n"
        )

    def test_real_import_error_through_api_function(self):
        with mock.patch.dict(sys.modules, {"aiohttp": None}):
            out, _, _ = run_cli(
                ["repos", "status", "r1"], {"repos_status": nia.repos_status}
            )
        self.assertEqual(
            out, "Error: aiohttp not installed. Run: pip install aiohttp\n"
        )

    def test_other_exception_prints_error_and_traceback(self):
        async def boom(*a, **kw):
            raise RuntimeError("boom")

        out, err, _ = run_cli(["sources", "get", "s1"], {"sources_get": boom})
        self.assertEqual(out, "Error: boom\n")
        self.assertIn("Traceback (most recent call last)", err)
        self.assertIn("RuntimeError: boom", err)

    def test_result_without_content_is_json_dumped(self):
        out, _, _ = run_cli(
            ["repos", "content", "r1", "p"], {"repos_content": {"a": 1}}
        )
        self.assertEqual(out, '{\n  "a": 1\n}\n')

    def test_parse_error_exits_2(self):
        with self.assertRaises(SystemExit) as cm:
            run_cli(["repos", "grep", "r1"])
        self.assertEqual(cm.exception.code, 2)

    def test_any_arg_ending_in_py_is_dropped(self):
        # Quirk kept as-is: the harness filter drops every *.py token, so a
        # positional path ending in .py goes missing and argparse exits 2.
        with self.assertRaises(SystemExit) as cm:
            run_cli(["repos", "content", "r1", "src/a.py"])
        self.assertEqual(cm.exception.code, 2)


# (case id, result, search_type)
SEARCH_CASES = [
    ("error", {"error": "API error 500: x"}, "Web Search"),
    ("content-only", {"content": "answer"}, "Deep Research"),
    ("content-truncated", {"content": "a" * 2100}, "Deep Research"),
    (
        "content-sources",
        {
            "content": "answer",
            "sources": [
                {"title": "T1", "path": "p1"},
                {"path": "p2"},
                {},
                "plain-source",
                {"title": "T5"},
                {"title": "T6-dropped"},
            ],
        },
        "Query",
    ),
    ("content-empty-sources", {"content": "answer", "sources": []}, "Query"),
    (
        "results-shapes",
        {
            "results": [
                {"title": "Top", "score": 0.12345, "snippet": "snip", "content": "c"},
                {"path": "a/b.py", "content": "body", "score": 3},
                {"name": "nm", "description": "desc", "score": "high"},
                {
                    "result": {
                        "file_path": "src/x.py",
                        "start_line": 12,
                        "content": "inner",
                    },
                    "source": {"url": "https://u", "file_path": "fp"},
                },
                {
                    "source": {"document_name": "Doc", "file_path": "f.md"},
                    "document": "d",
                },
                {"source": {"display_name": "Disp"}},
                {"metadata": {"document_key": "key.md"}},
                {"content": "# Heading line\nrest of doc"},
                {"content": "no heading"},
                "plain string item",
                {"result": "not-a-dict", "source": ["x"], "metadata": 5, "score": True},
                {"title": "eleventh-dropped"},
            ]
        },
        "Universal Search",
    ),
    (
        "results-long-text",
        {"results": [{"title": "L", "snippet": "z" * 900}, "y" * 400]},
        "Web Search",
    ),
    ("results-empty", {"results": []}, "Web Search"),
    (
        "matches",
        {
            "matches": [
                {"path": "a.py", "line": "x = 1"},
                {"file": "b.py", "content": "y = 2"},
                {},
                {"path": "c.py", "line": "q" * 250},
            ]
            + [{"path": f"m{i}.py"} for i in range(10)]
        },
        "Package Search",
    ),
    (
        "fallback-json",
        {"something": {"nested": [1, 2]}, "when": object},
        "Context Search",
    ),
    ("fallback-json-long", {"blob": "b" * 2500}, "Context Search"),
]

LIST_CASES = [
    ("error", {"error": "nope"}, "Repositories"),
    (
        "plain-list",
        [{"title": "t1"}, "raw", {"url": "u", "status": "s"}],
        "Data Sources",
    ),
    ("dict-repos", LIST_RESULT, "Repositories"),
    (
        "dict-keys",
        {
            "data_sources": [
                {"repository": "o/r"},
                {"repository_id": "rid"},
                {},
                {"display_name": "D", "status": ""},
            ]
        },
        "Data Sources",
    ),
    ("papers", {"papers": [{"id": "p1"}]}, "Research Papers"),
    ("contexts", {"contexts": [{"title": "c"}], "total": 99}, "Contexts"),
    ("sessions", {"sessions": [{"id": "s"}]}, "Oracle Sessions"),
    ("jobs", {"jobs": [{"id": "j", "status": "running"}]}, "Oracle Jobs"),
    ("empty-dict", {}, "Repositories"),
    ("empty-list", [], "Repositories"),
    ("over-20", [f"i{i}" for i in range(25)], "Repositories"),
]

ORACLE_CASES = [
    ("error", {"error": "x"}),
    ("full", ORACLE_RESULT),
    ("empty", {}),
    (
        "citation-defaults",
        {"citations": [{}, {"summary": "s" * 300}], "duration_ms": 1},
    ),
]

# Captured on HEAD 2aff883 (pre-refactor): case id -> formatted string
EXPECTED_SEARCH = {
    "error": "Error: API error 500: x",
    "content-only": "# Deep Research Results\n\nanswer",
    "content-truncated": "# Deep Research Results\n\naaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "content-sources": "# Query Results\n\nanswer\n\n## Sources\n- T1\n- p2\n- unknown\n- plain-source\n- T5",
    "content-empty-sources": "# Query Results\n\nanswer",
    "results-shapes": "# Universal Search Results\n\n\n1. **Top** (score: 0.123)\n   snip\n\n2. **a/b.py** (score: 3.000)\n   body\n\n3. **nm**\n   desc\n\n4. **src/x.py:12**\n   https://u\n   inner\n\n5. **Doc**\n   f.md\n   d\n\n6. **Disp**\n\n7. **key.md**\n\n8. **Heading line**\n   # Heading line\nrest of doc\n\n9. **Result 9**\n   no heading\n\n10. plain string item",
    "results-long-text": "# Web Search Results\n\n\n1. **L**\n   zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz\n\n2. yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy",
    "results-empty": "# Web Search Results\n",
    "matches": "# Package Search Results\n\n\n1. `a.py`\n   x = 1\n\n2. `b.py`\n   y = 2\n\n3. `unknown`\n\n4. `c.py`\n   qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq\n\n5. `m0.py`\n\n6. `m1.py`\n\n7. `m2.py`\n\n8. `m3.py`\n\n9. `m4.py`\n\n10. `m5.py`",
    "fallback-json": '# Context Search Results\n\n{\n  "something": {\n    "nested": [\n      1,\n      2\n    ]\n  },\n  "when": "<class \'object\'>"\n}',
    "fallback-json-long": '# Context Search Results\n\n{\n  "blob": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
}
EXPECTED_LIST = {
    "error": "Error: nope",
    "plain-list": "# Data Sources\n\n1. t1 \n2. raw\n3. u (s)\n\n---\nTotal: 3",
    "dict-repos": "# Repositories\n\n1. repo-a (ready)\n2. r2 \n\n---\nTotal: 7",
    "dict-keys": "# Data Sources\n\n1. o/r \n2. rid \n3. Item 3 \n4. D \n\n---\nTotal: 4",
    "papers": "# Research Papers\n\n1. p1 \n\n---\nTotal: 1",
    "contexts": "# Contexts\n\n1. c \n\n---\nTotal: 99",
    "sessions": "# Oracle Sessions\n\n1. s \n\n---\nTotal: 1",
    "jobs": "# Oracle Jobs\n\n1. j (running)\n\n---\nTotal: 1",
    "empty-dict": "# Repositories\n\nNo items found.\n\n---\nTotal: 0",
    "empty-list": "# Repositories\n\nNo items found.\n\n---\nTotal: 0",
    "over-20": "# Repositories\n\n1. i0\n2. i1\n3. i2\n4. i3\n5. i4\n6. i5\n7. i6\n8. i7\n9. i8\n10. i9\n11. i10\n12. i11\n13. i12\n14. i13\n15. i14\n16. i15\n17. i16\n18. i17\n19. i18\n20. i19\n\n---\nTotal: 25",
}
EXPECTED_ORACLE = {
    "error": "Error: x",
    "full": "# Oracle Research Result\n\nreport body\n\n## Citations\n1. [grep] found it\n\n---\nDuration: 5ms | Iterations: 2",
    "empty": "# Oracle Research Result\n",
    "citation-defaults": "# Oracle Research Result\n\n\n## Citations\n1. [unknown] \n2. [unknown] ssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssssss\n\n---\nDuration: 1ms | Iterations: N/A",
}


class Formatters(unittest.TestCase):
    def test_format_search_result_pinned(self):
        self.assertEqual(sorted(EXPECTED_SEARCH), sorted(c for c, _, _ in SEARCH_CASES))
        for case, result, kind in SEARCH_CASES:
            with self.subTest(case=case):
                self.assertEqual(
                    nia.format_search_result(result, kind), EXPECTED_SEARCH[case]
                )

    def test_format_list_result_pinned(self):
        self.assertEqual(sorted(EXPECTED_LIST), sorted(c for c, _, _ in LIST_CASES))
        for case, result, kind in LIST_CASES:
            with self.subTest(case=case):
                self.assertEqual(
                    nia.format_list_result(result, kind), EXPECTED_LIST[case]
                )

    def test_format_oracle_result_pinned(self):
        self.assertEqual(sorted(EXPECTED_ORACLE), sorted(c for c, _ in ORACLE_CASES))
        for case, result in ORACLE_CASES:
            with self.subTest(case=case):
                self.assertEqual(
                    nia.format_oracle_result(result), EXPECTED_ORACLE[case]
                )


class _FakeResponse:
    def __init__(self, status, body, lines=()):
        self.status = status
        self._body = body
        self.content = self._iter(lines)

    async def _iter(self, lines):
        for line in lines:
            yield line

    async def text(self):
        return str(self._body)

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, log, response):
        self._log = log
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _request(self, method, url, **kwargs):
        self._log.append((method, url, kwargs))
        return self._response

    def get(self, url, **kw):
        return self._request("GET", url, **kw)

    def post(self, url, **kw):
        return self._request("POST", url, **kw)

    def delete(self, url, **kw):
        return self._request("DELETE", url, **kw)


def fake_aiohttp(log, response):
    mod = types.ModuleType("aiohttp")
    mod.ClientSession = lambda: _FakeSession(log, response)
    mod.ClientTimeout = lambda total: ("timeout", total)
    return mod


class HttpLayer(unittest.TestCase):
    """The request builders against a fake aiohttp: URL, method, payload, errors."""

    def call(self, coro_fn, *args, status=200, body=None, lines=()):
        log = []
        resp = _FakeResponse(status, body if body is not None else {"ok": True}, lines)
        out = io.StringIO()
        fake = {"aiohttp": fake_aiohttp(log, resp)}
        with mock.patch.dict(sys.modules, fake), contextlib.redirect_stdout(out):
            result = run_async(coro_fn, *args)
        return result, log, out.getvalue()

    def test_post_with_payload(self):
        result, log, _ = self.call(nia.search_universal, "auth", 4)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            log,
            [
                (
                    "POST",
                    f"{nia.NIA_API_URL}/v2/search/universal",
                    {
                        "headers": {
                            "Authorization": "Bearer test-key",
                            "Content-Type": "application/json",
                        },
                        "json": {"query": "auth", "search_mode": "unified", "limit": 4},
                    },
                )
            ],
        )

    def test_get_with_params_and_optional_filters(self):
        _, log, _ = self.call(nia.repos_list, "ab", None, 5)
        self.assertEqual(log[0][0], "GET")
        self.assertEqual(log[0][2]["params"], {"limit": 5, "offset": 0, "q": "ab"})

    def test_timeout_for_long_requests(self):
        _, log, _ = self.call(nia.search_deep, "q")
        self.assertEqual(log[0][2]["timeout"], ("timeout", 300))

    def test_error_status_returns_error_dict(self):
        result, _, _ = self.call(nia.repos_status, "r1", status=404, body="missing")
        self.assertEqual(result, {"error": "API error 404: missing"})

    def test_delete_returns_synthesized_status(self):
        result, log, _ = self.call(nia.sources_delete, "s1")
        self.assertEqual(result, {"status": "deleted", "source_id": "s1"})
        self.assertEqual(
            log[0][:2], ("DELETE", f"{nia.NIA_API_URL}/v2/data-sources/s1")
        )

    def test_stream_prints_data_lines_only(self):
        lines = [b"data: one\n", b"event: x\n", b"\n", b"data:two\n"]
        result, _, out = self.call(nia.oracle_stream_job_events, "j1", lines=lines)
        self.assertIsNone(result)
        self.assertEqual(out, "one\ntwo\n")

    def test_stream_error_status_prints(self):
        _, _, out = self.call(
            nia.oracle_chat_followup, "s", "m", status=500, body="bad"
        )
        self.assertEqual(out, "Error: 500 - bad\n")


if __name__ == "__main__":
    unittest.main()
