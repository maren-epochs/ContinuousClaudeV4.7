#!/usr/bin/env python3
"""Exa AI Search - Semantic web search with clean text extraction.

Exa uses neural search (embeddings) rather than keyword matching,
returning high-quality results with clean extracted text content.

Default: highlights + summary (token-efficient, ~300-500 chars/result).
Use --text for full page text, --no-contents for metadata only.

Methods:
- search: Find URLs + highlights + summary (default)
- findSimilar: Find pages similar to a given URL
- getContents: Extract clean text from specific URLs

Usage:
  # Search with highlights + summary (default, token-efficient)
  python tools/exa_search.py \
    --search "best practices for LLM context engineering"

  # Search with full text (when you need complete content)
  python tools/exa_search.py \
    --search "rust async patterns" --text

  # Search metadata only (fastest, no content extraction)
  python tools/exa_search.py \
    --search "rust async patterns" --no-contents

  # Find pages similar to a URL
  python tools/exa_search.py \
    --similar "https://docs.anthropic.com/en/docs/build-with-claude/tool-use"

  # Extract clean text from specific URLs
  python tools/exa_search.py \
    --extract "https://exa.ai/docs/sdks/typescript-sdk-specification"

  # Filter by domain, category, or date
  python tools/exa_search.py \
    --search "transformer architecture" --category "research paper" --num 5

  # Deep reasoning search (slower, higher quality)
  python tools/exa_search.py \
    --search "novel approaches to KV cache compression" --type deep-reasoning

Requires: EXA_API_KEY in environment or ~/.claude/.env
"""

import argparse
import asyncio
import io
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from api_common import env_file_value, json_headers, request_json

# Search results routinely contain emoji and zero-width characters. A Windows
# console defaults to cp1252, so printing them raises UnicodeEncodeError and
# kills the run after the request has already been made. Force UTF-8 and never
# fail on a character we cannot represent.
for _stream in (sys.stdout, sys.stderr):
    try:
        if isinstance(_stream, io.TextIOWrapper):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

EXA_API_URL = "https://api.exa.ai"


def load_api_key() -> str:
    """Load API key from environment or ~/.claude/.env."""
    keys = map(_env_file_key, (Path.home() / ".claude" / ".env", Path.cwd() / ".env"))
    return os.environ.get("EXA_API_KEY", "") or next(filter(None, keys), "")


def _env_file_key(env_path: Path) -> str:
    """EXA_API_KEY from one .env file with whitespace and quotes stripped, else ""."""
    raw = env_file_value(env_path, "EXA_API_KEY") or ""
    return raw.strip().strip('"').strip("'")


def get_headers(api_key: str) -> dict:
    """HTTP headers for Exa API requests (API key + JSON content type)."""
    return json_headers("x-api-key", api_key)


def parse_args():
    """Parse the exa_search command line (search/find-similar/contents modes and filters)."""
    parser = argparse.ArgumentParser(
        description="Semantic web search via Exa AI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Search types:
  auto             Let Exa choose (default)
  instant          Fastest, simple factual queries
  fast             Quick keyword-like search
  deep             High quality neural search
  deep-reasoning   Highest quality, slower

Categories (optional filter):
  "research paper", "company", "news", "github", "tweet",
  "personal site", "pdf", "linkedin profile"

Examples:
  %(prog)s --search "how to implement RAG with PostgreSQL"
  %(prog)s --search "CUDA kernel optimization" --category "research paper"
  %(prog)s --similar "https://arxiv.org/abs/2301.00001" --num 5
  %(prog)s --extract "https://docs.example.com/api"
        """,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--search", metavar="QUERY", help="Search with text content extraction"
    )
    group.add_argument("--similar", metavar="URL", help="Find pages similar to URL")
    group.add_argument(
        "--extract", metavar="URLS", nargs="+", help="Extract clean text from URLs"
    )

    parser.add_argument(
        "--num", type=int, default=5, help="Number of results (default: 5)"
    )
    parser.add_argument(
        "--type",
        choices=["auto", "instant", "fast", "deep", "deep-reasoning"],
        default="auto",
        help="Search type (default: auto)",
    )
    parser.add_argument(
        "--category", help="Filter by category (e.g. 'research paper', 'github')"
    )
    parser.add_argument("--domains", nargs="+", help="Limit to specific domains")
    parser.add_argument(
        "--max-chars",
        type=int,
        default=1500,
        help="Max chars per result text (default: 1500)",
    )
    parser.add_argument(
        "--highlight-chars",
        type=int,
        default=2000,
        help="Max chars for highlights (default: 2000)",
    )
    parser.add_argument(
        "--no-contents",
        action="store_true",
        help="Skip content extraction (metadata only)",
    )
    parser.add_argument(
        "--text", action="store_true", help="Include full page text (off by default)"
    )
    parser.add_argument(
        "--no-highlights", action="store_true", help="Disable highlighted snippets"
    )
    parser.add_argument(
        "--no-summary", action="store_true", help="Disable AI-generated summary"
    )
    parser.add_argument("--start-date", help="Filter: published after (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="Filter: published before (YYYY-MM-DD)")

    args_to_parse = [arg for arg in sys.argv[1:] if not arg.endswith(".py")]
    return parser.parse_args(args_to_parse)


@dataclass(frozen=True)
class ExaSearchOptions:
    """exa_search() keywords after the query, in positional order, with their defaults."""

    num_results: int = 5
    search_type: str = "auto"
    category: str | None = None
    domains: list | None = None
    max_chars: int = 1500
    highlight_chars: int = 2000
    with_text: bool = False
    highlights: bool = True
    summary: bool = True
    start_date: str | None = None
    end_date: str | None = None


async def exa_search(query: str, *args: Any, **kwargs: Any) -> dict:
    """Search via Exa API, optionally with content extraction.

    Positional/keyword arguments after `query` are ExaSearchOptions fields (same
    names, order and defaults); an unknown keyword raises TypeError naming it.
    """
    o = ExaSearchOptions(*args, **kwargs)
    import aiohttp  # noqa: F401 - fail on missing aiohttp before the key check

    api_key = load_api_key()
    if not api_key:
        return {"error": "EXA_API_KEY not found in environment or ~/.claude/.env"}

    payload: dict[str, Any] = {
        "query": query,
        "numResults": o.num_results,
        "type": o.search_type,
    }

    contents = _search_contents(
        query,
        text_chars=o.max_chars if o.with_text else None,
        highlight_chars=o.highlight_chars if o.highlights else None,
        summary=o.summary,
    )
    if contents:
        payload["contents"] = contents

    filters = (
        ("category", o.category),
        ("includeDomains", o.domains),
        ("startPublishedDate", o.start_date),
        ("endPublishedDate", o.end_date),
    )
    payload.update((k, v) for k, v in filters if v)

    return await _post("/search", api_key, payload, 60)


def _search_contents(
    query: str, text_chars: int | None, highlight_chars: int | None, summary: bool
) -> dict[str, Any]:
    """The /search "contents" spec; {} when text, highlights and summary are all off."""
    if text_chars is None and highlight_chars is None and not summary:
        return {}
    contents: dict[str, Any] = {
        "text": False if text_chars is None else {"maxCharacters": text_chars}
    }
    if highlight_chars is not None:
        contents["highlights"] = {"maxCharacters": highlight_chars, "query": query}
    if summary:
        contents["summary"] = {"query": query}
    return contents


async def _post(endpoint: str, api_key: str, payload: dict, timeout_s: int) -> dict:
    """POST payload to EXA_API_URL + endpoint; the JSON reply, or an error dict on non-200."""
    url, h = f"{EXA_API_URL}{endpoint}", get_headers(api_key)
    return await request_json("post", url, headers=h, json=payload, timeout_s=timeout_s)


async def exa_find_similar(
    url: str,
    num_results: int = 5,
    highlight_chars: int = 2000,
) -> dict:
    """Find pages similar to a URL."""
    import aiohttp  # noqa: F401 - fail on missing aiohttp before the key check

    api_key = load_api_key()
    if not api_key:
        return {"error": "EXA_API_KEY not found in environment or ~/.claude/.env"}

    payload: dict[str, Any] = {
        "url": url,
        "numResults": num_results,
        "contents": {
            "text": False,
            "highlights": {"maxCharacters": highlight_chars},
            "summary": {},
        },
    }

    return await _post("/findSimilar", api_key, payload, 30)


async def exa_get_contents(
    urls: list[str],
    max_chars: int = 8000,
) -> dict:
    """Extract clean text from specific URLs."""
    import aiohttp  # noqa: F401 - fail on missing aiohttp before the key check

    api_key = load_api_key()
    if not api_key:
        return {"error": "EXA_API_KEY not found in environment or ~/.claude/.env"}

    payload: dict[str, Any] = {
        "ids": urls,
        "text": {"maxCharacters": max_chars},
        "summary": {},
    }

    return await _post("/contents", api_key, payload, 30)


def print_results(results: list, show_text: bool = True, max_display: int = 800):
    """Format and print search results."""
    for i, r in enumerate(results, 1):
        for line in _result_head(i, r):
            print(line)
        # Full text
        if show_text and r.get("text"):
            print(f"\n{_clip(r['text'].strip(), max_display)}\n")
        print()


def _result_head(i: int, r: dict) -> list[str]:
    """Title, URL, optional author/date/score, summary and up to three highlights."""
    lines = [f"**{i}. {r.get('title', 'No title')}**", f"   {r.get('url', '')}"]
    author = r.get("author", "")
    if author:
        lines.append(f"   Author: {author}")
    date = r.get("publishedDate", "")
    if date:
        lines.append(f"   Published: {date[:10]}")
    score = r.get("score")
    if score is not None:
        lines.append(f"   Score: {score:.3f}")
    # Summary (compact, high signal)
    if r.get("summary"):
        lines.append(f"   Summary: {r['summary'][:300]}")
    # Highlights (key passages)
    lines += [f"   > {h[:200]}" for h in (r.get("highlights") or [])[:3]]
    return lines


def _clip(text: str, max_display: int) -> str:
    """text cut to max_display chars plus a truncation marker; 0 means no limit."""
    if max_display and len(text) > max_display:
        return text[:max_display] + "\n   [... truncated]"
    return text


async def main():
    """CLI entry: run the selected Exa request and print text or JSON results."""
    args = parse_args()

    if args.search:
        await _run_search(args)
    elif args.similar:
        await _run_similar(args)
    elif args.extract:
        await _run_extract(args)


def _exit_on_error(result: dict) -> list:
    """Print the error and exit 1 if result has one; else its "results" list."""
    if "error" in result:
        print(f"\nError: {result['error']}")
        sys.exit(1)
    return result.get("results", [])


def _content_flags(args) -> tuple[bool, bool, bool]:
    """(with_text, highlights, summary) for --search; --no-contents turns all off."""
    if args.no_contents:
        return False, False, False
    return args.text, not args.no_highlights, not args.no_summary


def _search_header(args, flags: tuple[bool, bool, bool]) -> list[str]:
    """The --search banner: query, active content modes, non-default filters."""
    with_text, highlights, summary = flags
    modes = (("highlights", highlights), ("summary", summary), ("text", with_text))
    mode = " + ".join(name for name, on in modes if on) or "metadata only"
    lines = [f"Exa search: {args.search}  [{mode}]"]
    if args.type != "auto":
        lines.append(f"  Type: {args.type}")
    if args.category:
        lines.append(f"  Category: {args.category}")
    if args.domains:
        lines.append(f"  Domains: {', '.join(args.domains)}")
    return lines


async def _run_search(args) -> None:
    """--search: print the banner, run exa_search, print the results."""
    with_text, highlights, summary = flags = _content_flags(args)
    print("\n".join(_search_header(args, flags)))
    result = await exa_search(
        args.search,
        num_results=args.num,
        search_type=args.type,
        category=args.category,
        domains=args.domains,
        max_chars=args.max_chars,
        highlight_chars=args.highlight_chars,
        with_text=with_text,
        highlights=highlights,
        summary=summary,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    results = _exit_on_error(result)
    print(f"\nFound {len(results)} results\n")
    print_results(results, show_text=with_text)


async def _run_similar(args) -> None:
    """--similar: run exa_find_similar and print the pages."""
    url = args.similar
    print(f"Finding similar to: {url}")
    result = await exa_find_similar(
        url, num_results=args.num, highlight_chars=args.highlight_chars
    )
    results = _exit_on_error(result)
    print(f"\nFound {len(results)} similar pages\n")
    print_results(results)


async def _run_extract(args) -> None:
    """--extract: run exa_get_contents and print each page's text."""
    urls = args.extract
    print(f"Extracting content from {len(urls)} URL(s)")
    result = await exa_get_contents(urls, max_chars=args.max_chars)
    results = _exit_on_error(result)
    print(f"\nExtracted {len(results)} page(s)\n")
    print_results(results, max_display=args.max_chars)


if __name__ == "__main__":
    asyncio.run(main())
