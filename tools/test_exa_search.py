"""Characterization tests for tools/exa_search.py (VAL-613).

Pins load_api_key lookup order, the request builders (URL, payload, timeout,
error dict) against a fake `aiohttp`, the exact print_results output, and the
CLI dispatch of main() (which request function, with which arguments, stdout
and exit code), so the complexity refactor is behavior-preserving. Expected
values were captured on the pre-refactor code (HEAD 34862e9).

No network: a fake `aiohttp` module (the request functions import it locally)
stands in for the HTTP layer; main() tests replace the request functions.

Run: py -3.13 tools/test_exa_search.py
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _testing import FakeResponse, fake_aiohttp, load_module, run_async

PROJECT = Path(__file__).resolve().parent.parent
exa = load_module("exa_search_under_test", PROJECT / "tools" / "exa_search.py")


HEADERS = {"x-api-key": "k1", "Content-Type": "application/json"}


class LoadApiKey(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.cwd = root / "cwd"
        (self.home / ".claude").mkdir(parents=True)
        self.cwd.mkdir()

    def lookup(self, env=None):
        env = {"EXA_API_KEY": ""} if env is None else env
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(Path, "home", return_value=self.home),
            mock.patch.object(Path, "cwd", return_value=self.cwd),
        ):
            return exa.load_api_key()

    def test_environment_wins(self):
        (self.home / ".claude" / ".env").write_text("EXA_API_KEY=fromfile\n")
        self.assertEqual(self.lookup({"EXA_API_KEY": "fromenv"}), "fromenv")

    def test_home_env_file_quotes_stripped(self):
        (self.home / ".claude" / ".env").write_text(
            "OTHER=1\n  EXA_API_KEY = \"'abc'\"  \nEXA_API_KEY=second\n"
        )
        # startswith("EXA_API_KEY=") fails on the spaced line; the next one matches
        self.assertEqual(self.lookup(), "second")
        (self.home / ".claude" / ".env").write_text("EXA_API_KEY=\"'q1'\"\n")
        self.assertEqual(self.lookup(), "q1")

    def test_home_before_cwd_and_cwd_fallback(self):
        (self.cwd / ".env").write_text("EXA_API_KEY=cwdkey\n")
        self.assertEqual(self.lookup(), "cwdkey")
        (self.home / ".claude" / ".env").write_text("EXA_API_KEY=homekey\n")
        self.assertEqual(self.lookup(), "homekey")

    def test_empty_home_value_falls_through_to_cwd(self):
        (self.home / ".claude" / ".env").write_text("EXA_API_KEY=\n")
        (self.cwd / ".env").write_text("EXA_API_KEY='c2'\n")
        self.assertEqual(self.lookup(), "c2")

    def test_missing_everywhere_is_empty(self):
        self.assertEqual(self.lookup(), "")

    def test_file_value_not_exported(self):
        (self.cwd / ".env").write_text("EXA_API_KEY=x\n")
        with (
            mock.patch.dict(os.environ, {"EXA_API_KEY": ""}),
            mock.patch.object(Path, "home", return_value=self.home),
            mock.patch.object(Path, "cwd", return_value=self.cwd),
        ):
            self.assertEqual(exa.load_api_key(), "x")
            self.assertEqual(os.environ["EXA_API_KEY"], "")


class Requests(unittest.TestCase):
    def call(self, fn, *args, status=200, body=None, key="k1", **kwargs):
        log = []
        resp = FakeResponse(status, {"results": []} if body is None else body)
        with (
            mock.patch.dict(sys.modules, {"aiohttp": fake_aiohttp(log, resp)}),
            mock.patch.object(exa, "load_api_key", return_value=key),
        ):
            result = run_async(fn, *args, **kwargs)
        return result, log

    def test_headers(self):
        self.assertEqual(exa.get_headers("k1"), HEADERS)

    def test_search_defaults(self):
        result, log = self.call(exa.exa_search, "q")
        self.assertEqual(result, {"results": []})
        self.assertEqual(
            log,
            [
                (
                    "POST",
                    "https://api.exa.ai/search",
                    {
                        "headers": HEADERS,
                        "json": {
                            "query": "q",
                            "numResults": 5,
                            "type": "auto",
                            "contents": {
                                "text": False,
                                "highlights": {"maxCharacters": 2000, "query": "q"},
                                "summary": {"query": "q"},
                            },
                        },
                        "timeout": ("timeout", 60),
                    },
                )
            ],
        )

    def test_search_all_options(self):
        _, log = self.call(
            exa.exa_search,
            "q",
            num_results=3,
            search_type="deep",
            category="news",
            domains=["a.com"],
            max_chars=10,
            highlight_chars=20,
            with_text=True,
            highlights=False,
            summary=False,
            start_date="2025-01-01",
            end_date="2026-01-01",
        )
        payload = log[0][2]["json"]
        self.assertEqual(
            list(payload.items()),
            [
                ("query", "q"),
                ("numResults", 3),
                ("type", "deep"),
                ("contents", {"text": {"maxCharacters": 10}}),
                ("category", "news"),
                ("includeDomains", ["a.com"]),
                ("startPublishedDate", "2025-01-01"),
                ("endPublishedDate", "2026-01-01"),
            ],
        )

    def test_search_no_contents_and_falsy_filters(self):
        _, log = self.call(
            exa.exa_search,
            "q",
            with_text=False,
            highlights=False,
            summary=False,
            category="",
            domains=[],
            start_date="",
            end_date=None,
        )
        self.assertEqual(
            log[0][2]["json"], {"query": "q", "numResults": 5, "type": "auto"}
        )

    def test_search_summary_only(self):
        _, log = self.call(exa.exa_search, "q", highlights=False)
        self.assertEqual(
            log[0][2]["json"]["contents"], {"text": False, "summary": {"query": "q"}}
        )

    def test_error_status_and_missing_key(self):
        for fn, args in (
            (exa.exa_search, ("q",)),
            (exa.exa_find_similar, ("https://u",)),
            (exa.exa_get_contents, (["https://u"],)),
        ):
            with self.subTest(fn=fn.__name__):
                result, _ = self.call(fn, *args, status=429, body="slow down")
                self.assertEqual(result, {"error": "API error 429: slow down"})
                result, log = self.call(fn, *args, key="")
                self.assertEqual(
                    result,
                    {"error": "EXA_API_KEY not found in environment or ~/.claude/.env"},
                )
                self.assertEqual(log, [])

    def test_find_similar(self):
        result, log = self.call(
            exa.exa_find_similar, "https://u", num_results=2, highlight_chars=9
        )
        self.assertEqual(result, {"results": []})
        self.assertEqual(
            log,
            [
                (
                    "POST",
                    "https://api.exa.ai/findSimilar",
                    {
                        "headers": HEADERS,
                        "json": {
                            "url": "https://u",
                            "numResults": 2,
                            "contents": {
                                "text": False,
                                "highlights": {"maxCharacters": 9},
                                "summary": {},
                            },
                        },
                        "timeout": ("timeout", 30),
                    },
                )
            ],
        )

    def test_get_contents(self):
        _, log = self.call(exa.exa_get_contents, ["https://a", "https://b"])
        self.assertEqual(
            log,
            [
                (
                    "POST",
                    "https://api.exa.ai/contents",
                    {
                        "headers": HEADERS,
                        "json": {
                            "ids": ["https://a", "https://b"],
                            "text": {"maxCharacters": 8000},
                            "summary": {},
                        },
                        "timeout": ("timeout", 30),
                    },
                )
            ],
        )


FULL = {
    "title": "T",
    "url": "https://t",
    "publishedDate": "2026-01-02T03:04:05Z",
    "score": 0.12345,
    "author": "A",
    "summary": "S" * 310,
    "highlights": ["h1", "H" * 210, "h3", "h4"],
    "text": "  body text  ",
}


def printed(results, **kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exa.print_results(results, **kw)
    return out.getvalue()


class PrintResults(unittest.TestCase):
    def test_full_record(self):
        self.assertEqual(
            printed([FULL]),
            "**1. T**\n   https://t\n   Author: A\n   Published: 2026-01-02\n"
            "   Score: 0.123\n   Summary: " + "S" * 300 + "\n   > h1\n"
            "   > " + "H" * 200 + "\n   > h3\n\nbody text\n\n\n",
        )

    def test_minimal_record_and_numbering(self):
        self.assertEqual(
            printed([{}, {"score": 0, "highlights": [], "summary": ""}]),
            "**1. No title**\n   \n\n**2. No title**\n   \n   Score: 0.000\n\n",
        )

    def test_text_hidden_and_truncation(self):
        rec = {"title": "T", "url": "u", "text": "abcdefghij"}
        self.assertEqual(printed([rec], show_text=False), "**1. T**\n   u\n\n")
        self.assertEqual(
            printed([rec], max_display=4),
            "**1. T**\n   u\n\nabcd\n   [... truncated]\n\n\n",
        )
        self.assertEqual(
            printed([rec], max_display=0), "**1. T**\n   u\n\nabcdefghij\n\n\n"
        )
        self.assertEqual(
            printed([rec], max_display=10), "**1. T**\n   u\n\nabcdefghij\n\n\n"
        )


def run_main(argv, ret=None):
    """Run main() with fake request functions; return (stdout, exit code, calls)."""
    calls = []
    ret = (
        {"results": [{"title": "R", "url": "https://r", "text": "x" * 5}]}
        if (ret is None)
        else ret
    )

    def fake(name):
        async def f(*args, **kwargs):
            calls.append((name, args, kwargs))
            return ret

        return f

    out = io.StringIO()
    code = 0
    with (
        mock.patch.object(sys, "argv", ["exa_search.py", *argv]),
        mock.patch.object(exa, "exa_search", fake("exa_search")),
        mock.patch.object(exa, "exa_find_similar", fake("exa_find_similar")),
        mock.patch.object(exa, "exa_get_contents", fake("exa_get_contents")),
        contextlib.redirect_stdout(out),
    ):
        try:
            run_async(exa.main)
        except SystemExit as e:
            code = e.code
    return out.getvalue(), code, calls


SEARCH_KW = {
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
}


class Cli(unittest.TestCase):
    def test_search_default(self):
        out, code, calls = run_main(["--search", "q"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("exa_search", ("q",), SEARCH_KW)])
        self.assertEqual(
            out,
            "Exa search: q  [highlights + summary]\n\nFound 1 results\n\n"
            "**1. R**\n   https://r\n\n",
        )

    def test_search_options_and_text(self):
        out, _, calls = run_main(
            [
                "--search",
                "q",
                "--text",
                "--no-summary",
                "--type",
                "deep",
                "--category",
                "news",
                "--domains",
                "a.com",
                "b.com",
                "--num",
                "2",
                "--max-chars",
                "3",
                "--highlight-chars",
                "4",
                "--start-date",
                "s",
                "--end-date",
                "e",
                "tools/whatever.py",
            ]
        )
        kw = dict(
            SEARCH_KW,
            num_results=2,
            search_type="deep",
            category="news",
            domains=["a.com", "b.com"],
            max_chars=3,
            highlight_chars=4,
            with_text=True,
            summary=False,
            start_date="s",
            end_date="e",
        )
        self.assertEqual(calls, [("exa_search", ("q",), kw)])
        self.assertEqual(
            out,
            "Exa search: q  [highlights + text]\n  Type: deep\n  Category: news\n"
            "  Domains: a.com, b.com\n\nFound 1 results\n\n"
            "**1. R**\n   https://r\n\nxxxxx\n\n\n",
        )

    def test_search_no_contents_overrides_flags(self):
        out, _, calls = run_main(["--search", "q", "--no-contents", "--text"])
        kw = dict(SEARCH_KW, highlights=False, summary=False)
        self.assertEqual(calls, [("exa_search", ("q",), kw)])
        self.assertTrue(out.startswith("Exa search: q  [metadata only]\n"))

    def test_search_highlights_off(self):
        out, _, _ = run_main(["--search", "q", "--no-highlights"])
        self.assertTrue(out.startswith("Exa search: q  [summary]\n"))

    def test_similar(self):
        out, code, calls = run_main(["--similar", "https://u", "--num", "3"])
        self.assertEqual(code, 0)
        self.assertEqual(
            calls,
            [
                (
                    "exa_find_similar",
                    ("https://u",),
                    {"num_results": 3, "highlight_chars": 2000},
                )
            ],
        )
        self.assertEqual(
            out,
            "Finding similar to: https://u\n\nFound 1 similar pages\n\n"
            "**1. R**\n   https://r\n\nxxxxx\n\n\n",
        )

    def test_extract(self):
        out, _, calls = run_main(
            ["--extract", "https://a", "https://b", "--max-chars", "3"]
        )
        self.assertEqual(
            calls,
            [("exa_get_contents", (["https://a", "https://b"],), {"max_chars": 3})],
        )
        self.assertEqual(
            out,
            "Extracting content from 2 URL(s)\n\nExtracted 1 page(s)\n\n"
            "**1. R**\n   https://r\n\nxxx\n   [... truncated]\n\n\n",
        )

    def test_error_exits_1(self):
        for argv, head in (
            (["--search", "q"], "Exa search: q  [highlights + summary]\n"),
            (["--similar", "u"], "Finding similar to: u\n"),
            (["--extract", "u"], "Extracting content from 1 URL(s)\n"),
        ):
            with self.subTest(argv=argv):
                out, code, _ = run_main(argv, ret={"error": "boom"})
                self.assertEqual(code, 1)
                self.assertEqual(out, head + "\nError: boom\n")

    def test_missing_results_key(self):
        out, _, _ = run_main(["--similar", "u"], ret={})
        self.assertEqual(out, "Finding similar to: u\n\nFound 0 similar pages\n\n")

    def test_mode_required(self):
        with (
            mock.patch.object(sys, "argv", ["exa_search.py"]),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as cm,
        ):
            exa.parse_args()
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
