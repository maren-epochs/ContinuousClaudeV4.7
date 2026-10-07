#!/usr/bin/env python3
"""Nia API Client - Complete API coverage for documentation, research, and search.

API Categories:
- Oracle Research: Autonomous AI research agent (Pro only)
- Search: Query repos, docs, web, universal search
- Repositories: Index and search GitHub repos
- Data Sources: Index and search documentation websites
- Research Papers: Index and search arXiv papers
- Context Sharing: Save and retrieve conversation contexts

Usage Examples:
  # Oracle autonomous research (Pro only)
  python tools/nia_docs.py \
    oracle research "best practices for hybrid BM25 + vector search"

  # Universal search across all indexed sources
  python tools/nia_docs.py \
    search universal "authentication middleware patterns"

  # Search in specific package
  python tools/nia_docs.py \
    search package fastapi --query "dependency injection"

  # Web search
  python tools/nia_docs.py \
    search web "sqlite-vss vector search"

  # Deep research (Pro only)
  python tools/nia_docs.py \
    search deep "implementing hybrid retrieval with reranking"

  # List indexed repositories
  python tools/nia_docs.py repos list

  # Index a new repository
  python tools/nia_docs.py repos index owner/repo

  # Search repository code
  python tools/nia_docs.py repos grep owner/repo "pattern"

  # List data sources
  python tools/nia_docs.py sources list

  # Index documentation website
  python tools/nia_docs.py sources index https://docs.example.com

  # List research papers
  python tools/nia_docs.py papers list

  # Index arXiv paper
  python tools/nia_docs.py papers index 2310.06825

  # Save conversation context
  python tools/nia_docs.py context save --title "My Context" --content "..."

  # Search contexts
  python tools/nia_docs.py context search "embeddings"

Requires: NIA_API_KEY environment variable
"""

import argparse
import asyncio
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

from api_common import env_file_value, json_headers, request_json, stream_data_lines

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

# API base URL
NIA_API_URL = os.environ.get("NIA_API_URL", "https://apigcp.trynia.ai")


def load_api_key() -> str:
    """Load API key from environment or .env file."""
    if os.environ.get("NIA_API_KEY"):
        return os.environ["NIA_API_KEY"]

    for env_path in [Path.home() / ".claude" / ".env", Path.cwd() / ".env"]:
        raw = env_file_value(env_path, "NIA_API_KEY")
        if raw is not None:
            os.environ["NIA_API_KEY"] = raw.strip("\"'")
            return os.environ["NIA_API_KEY"]
    return ""


NIA_API_KEY = load_api_key()


def get_headers() -> dict:
    """Get common headers for API requests."""
    return json_headers("Authorization", f"Bearer {NIA_API_KEY}")


# =============================================================================
# ORACLE RESEARCH API
# =============================================================================


async def oracle_research(
    query: str,
    repositories: list[str] | None = None,
    data_sources: list[str] | None = None,
    output_format: str | None = None,
    model: str = "claude-opus-4-5-20251101",
) -> dict:
    """Oracle autonomous research agent (Pro only).

    Args:
        query: Research question to investigate
        repositories: Optional list of repository identifiers
        data_sources: Optional list of documentation source IDs
        output_format: Optional format specification
        model: Model to use (claude-opus-4-5-20251101, claude-sonnet-4-5-20250929, claude-sonnet-4-5-1m)

    Returns:
        Research report with citations, tool calls, iterations, duration
    """
    url = f"{NIA_API_URL}/v2/oracle"
    payload: dict[str, Any] = {"query": query, "model": model}

    if repositories:
        payload["repositories"] = repositories
    if data_sources:
        payload["data_sources"] = data_sources
    if output_format:
        payload["output_format"] = output_format

    # 5 min for deep research
    return await request_json(
        "post", url, headers=get_headers(), json=payload, timeout_s=300
    )


async def oracle_research_stream(
    query: str,
    repositories: list[str] | None = None,
    data_sources: list[str] | None = None,
    model: str = "claude-opus-4-5-20251101",
) -> None:
    """Oracle research with real-time streaming (Pro only). Prints events as they arrive."""
    url = f"{NIA_API_URL}/v2/oracle/stream"
    payload: dict[str, Any] = {"query": query, "model": model}

    if repositories:
        payload["repositories"] = repositories
    if data_sources:
        payload["data_sources"] = data_sources

    await stream_data_lines(
        "post", url, headers=get_headers(), json=payload, timeout_s=300
    )


async def oracle_list_sessions(limit: int = 20, offset: int = 0) -> dict:
    """List Oracle research sessions."""
    url = f"{NIA_API_URL}/v2/oracle/sessions"
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    return await request_json("get", url, headers=get_headers(), params=params)


async def oracle_get_session(session_id: str) -> dict:
    """Get Oracle research session details."""
    url = f"{NIA_API_URL}/v2/oracle/sessions/{session_id}"

    return await request_json("get", url, headers=get_headers())


async def oracle_get_messages(session_id: str) -> dict:
    """Get Oracle session chat messages."""
    url = f"{NIA_API_URL}/v2/oracle/sessions/{session_id}/messages"

    return await request_json("get", url, headers=get_headers())


async def oracle_chat_followup(session_id: str, message: str) -> None:
    """Stream a follow-up chat answer for an Oracle session (SSE)."""
    url = f"{NIA_API_URL}/v2/oracle/sessions/{session_id}/chat"
    payload: dict[str, Any] = {"message": message}

    await stream_data_lines("post", url, headers=get_headers(), json=payload)


async def oracle_list_jobs() -> dict:
    """List Oracle research jobs."""
    url = f"{NIA_API_URL}/v2/oracle/jobs"

    return await request_json("get", url, headers=get_headers())


async def oracle_create_job(
    query: str,
    repositories: list[str] | None = None,
    data_sources: list[str] | None = None,
    model: str = "claude-opus-4-5-20251101",
) -> dict:
    """Create Oracle research job (Pro only). Returns immediately, runs async."""
    url = f"{NIA_API_URL}/v2/oracle/jobs"
    payload: dict[str, Any] = {"query": query, "model": model}

    if repositories:
        payload["repositories"] = repositories
    if data_sources:
        payload["data_sources"] = data_sources

    return await request_json("post", url, headers=get_headers(), json=payload)


async def oracle_get_job(job_id: str) -> dict:
    """Get Oracle job status and result."""
    url = f"{NIA_API_URL}/v2/oracle/jobs/{job_id}"

    return await request_json("get", url, headers=get_headers())


async def oracle_cancel_job(job_id: str) -> dict:
    """Cancel Oracle research job."""
    url = f"{NIA_API_URL}/v2/oracle/jobs/{job_id}"

    return await request_json(
        "delete",
        url,
        headers=get_headers(),
        on_ok={"status": "cancelled", "job_id": job_id},
    )


async def oracle_stream_job_events(job_id: str) -> None:
    """Stream Oracle job events (SSE)."""
    url = f"{NIA_API_URL}/v2/oracle/jobs/{job_id}/events"

    await stream_data_lines("get", url, headers=get_headers())


# =============================================================================
# SEARCH API
# =============================================================================


async def search_query(
    messages: list[dict],
    repositories: list[str] | None = None,
    data_sources: list[str] | None = None,
    search_mode: str = "repositories",
    include_sources: bool = True,
) -> dict:
    """Query indexed repositories and documentation."""
    url = f"{NIA_API_URL}/v2/search/query"
    payload: dict[str, Any] = {
        "messages": messages,
        "search_mode": search_mode,
        "include_sources": include_sources,
    }

    if repositories:
        payload["repositories"] = repositories
    if data_sources:
        payload["data_sources"] = data_sources

    return await request_json("post", url, headers=get_headers(), json=payload)


async def search_web(
    query: str, category: str | None = None, time_range: str | None = None
) -> dict:
    """Web search."""
    url = f"{NIA_API_URL}/v2/search/web"
    payload: dict[str, Any] = {"query": query}

    if category:
        payload["category"] = category
    if time_range:
        payload["time_range"] = time_range

    return await request_json("post", url, headers=get_headers(), json=payload)


async def search_deep(query: str) -> dict:
    """Deep research agent (Pro only)."""
    url = f"{NIA_API_URL}/v2/search/deep"
    payload: dict[str, Any] = {"query": query}

    return await request_json(
        "post", url, headers=get_headers(), json=payload, timeout_s=300
    )


async def search_universal(query: str, limit: int = 10) -> dict:
    """Universal search across all public indexed sources."""
    url = f"{NIA_API_URL}/v2/search/universal"
    payload: dict[str, Any] = {"query": query, "search_mode": "unified", "limit": limit}

    return await request_json("post", url, headers=get_headers(), json=payload)


async def search_package_hybrid(
    package: str, query: str, registry: str = "py_pi", limit: int = 10
) -> dict:
    """Semantic search within a package."""
    url = f"{NIA_API_URL}/v2/package-search/hybrid"
    payload: dict[str, Any] = {
        "registry": registry,
        "package_name": package,
        "semantic_queries": [query],
        "limit": limit,
    }

    return await request_json("post", url, headers=get_headers(), json=payload)


async def search_package_grep(
    package: str, pattern: str, registry: str = "py_pi", limit: int = 10
) -> dict:
    """Regex search within a package."""
    url = f"{NIA_API_URL}/v2/package-search/grep"
    payload: dict[str, Any] = {
        "registry": registry,
        "package_name": package,
        "pattern": pattern,
        "limit": limit,
    }

    return await request_json("post", url, headers=get_headers(), json=payload)


# =============================================================================
# REPOSITORIES API
# =============================================================================


async def repos_list(
    q: str | None = None, status: str | None = None, limit: int = 100, offset: int = 0
) -> dict:
    """List all indexed repositories."""
    url = f"{NIA_API_URL}/v2/repositories"
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if q:
        params["q"] = q
    if status:
        params["status"] = status

    return await request_json("get", url, headers=get_headers(), params=params)


async def repos_index(repo: str, github_token: str | None = None) -> dict:
    """Index a new GitHub repository."""
    url = f"{NIA_API_URL}/v2/repositories"
    payload: dict[str, Any] = {"repository": repo}
    if github_token:
        payload["github_token"] = github_token

    return await request_json("post", url, headers=get_headers(), json=payload)


async def repos_status(repository_id: str) -> dict:
    """Get repository indexing status."""
    url = f"{NIA_API_URL}/v2/repositories/{repository_id}"

    return await request_json("get", url, headers=get_headers())


async def repos_delete(repository_id: str) -> dict:
    """Delete a repository."""
    url = f"{NIA_API_URL}/v2/repositories/{repository_id}"

    return await request_json(
        "delete",
        url,
        headers=get_headers(),
        on_ok={"status": "deleted", "repository_id": repository_id},
    )


async def repos_rename(repository_id: str, display_name: str) -> dict:
    """Rename a repository."""
    url = f"{NIA_API_URL}/v2/repositories/{repository_id}/rename"
    payload: dict[str, Any] = {"display_name": display_name}

    return await request_json("patch", url, headers=get_headers(), json=payload)


async def repos_tree(repository_id: str) -> dict:
    """Get repository tree structure."""
    url = f"{NIA_API_URL}/v2/repositories/{repository_id}/tree"

    return await request_json("get", url, headers=get_headers())


async def repos_content(repository_id: str, path: str) -> dict:
    """Get repository file content."""
    url = f"{NIA_API_URL}/v2/repositories/{repository_id}/content"
    payload: dict[str, Any] = {"path": path}

    return await request_json("post", url, headers=get_headers(), json=payload)


async def repos_grep(
    repository_id: str, pattern: str, context_lines: int = 3, exhaustive: bool = False
) -> dict:
    """Search repository code with regex."""
    url = f"{NIA_API_URL}/v2/repositories/{repository_id}/grep"
    payload: dict[str, Any] = {
        "pattern": pattern,
        "context_lines": context_lines,
        "exhaustive": exhaustive,
    }

    return await request_json("post", url, headers=get_headers(), json=payload)


# =============================================================================
# DATA SOURCES API
# =============================================================================


async def sources_list(
    q: str | None = None,
    status: str | None = None,
    source_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """List all data sources."""
    url = f"{NIA_API_URL}/v2/data-sources"
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if q:
        params["q"] = q
    if status:
        params["status"] = status
    if source_type:
        params["source_type"] = source_type

    return await request_json("get", url, headers=get_headers(), params=params)


async def sources_index(url_to_index: str, display_name: str | None = None) -> dict:
    """Index a new data source (documentation website)."""
    url = f"{NIA_API_URL}/v2/data-sources"
    payload: dict[str, Any] = {"url": url_to_index}
    if display_name:
        payload["display_name"] = display_name

    return await request_json("post", url, headers=get_headers(), json=payload)


async def sources_get(source_id: str) -> dict:
    """Get data source details."""
    url = f"{NIA_API_URL}/v2/data-sources/{source_id}"

    return await request_json("get", url, headers=get_headers())


async def sources_delete(source_id: str) -> dict:
    """Delete a data source."""
    url = f"{NIA_API_URL}/v2/data-sources/{source_id}"

    return await request_json(
        "delete",
        url,
        headers=get_headers(),
        on_ok={"status": "deleted", "source_id": source_id},
    )


async def sources_content(source_id: str, path: str) -> dict:
    """Get data source page content."""
    url = f"{NIA_API_URL}/v2/data-sources/{source_id}/content"
    payload: dict[str, Any] = {"path": path}

    return await request_json("post", url, headers=get_headers(), json=payload)


async def sources_tree(source_id: str) -> dict:
    """Get documentation tree structure."""
    url = f"{NIA_API_URL}/v2/data-sources/{source_id}/tree"

    return await request_json("get", url, headers=get_headers())


async def sources_ls(source_id: str, path: str = "/") -> dict:
    """List documentation directory contents."""
    url = f"{NIA_API_URL}/v2/data-sources/{source_id}/ls"
    params: dict[str, Any] = {"path": path}

    return await request_json("get", url, headers=get_headers(), params=params)


async def sources_read(source_id: str, path: str) -> dict:
    """Read documentation page content."""
    url = f"{NIA_API_URL}/v2/data-sources/{source_id}/read"
    params: dict[str, Any] = {"path": path}

    return await request_json("get", url, headers=get_headers(), params=params)


async def sources_grep(source_id: str, pattern: str, context_lines: int = 3) -> dict:
    """Search documentation with regex."""
    url = f"{NIA_API_URL}/v2/data-sources/{source_id}/grep"
    payload: dict[str, Any] = {"pattern": pattern, "context_lines": context_lines}

    return await request_json("post", url, headers=get_headers(), json=payload)


async def sources_rename(source_id: str, display_name: str) -> dict:
    """Rename a data source."""
    url = f"{NIA_API_URL}/v2/data-sources/rename"
    payload: dict[str, Any] = {"source_id": source_id, "display_name": display_name}

    return await request_json("patch", url, headers=get_headers(), json=payload)


# =============================================================================
# RESEARCH PAPERS API
# =============================================================================


async def papers_list(
    limit: int = 50, offset: int = 0, status: str | None = None
) -> dict:
    """List indexed research papers."""
    url = f"{NIA_API_URL}/v2/research-papers"
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status

    return await request_json("get", url, headers=get_headers(), params=params)


async def papers_index(arxiv_id: str) -> dict:
    """Index an arXiv research paper."""
    url = f"{NIA_API_URL}/v2/research-papers"
    payload: dict[str, Any] = {"arxiv_id": arxiv_id}

    return await request_json("post", url, headers=get_headers(), json=payload)


# =============================================================================
# CONTEXT SHARING API
# =============================================================================


async def context_list(
    limit: int = 20,
    offset: int = 0,
    tags: str | None = None,
    agent_source: str | None = None,
) -> dict:
    """List conversation contexts."""
    url = f"{NIA_API_URL}/v2/contexts"
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if tags:
        params["tags"] = tags
    if agent_source:
        params["agent_source"] = agent_source

    return await request_json("get", url, headers=get_headers(), params=params)


async def context_save(
    title: str,
    content: str,
    summary: str | None = None,
    tags: list[str] | None = None,
    metadata: dict | None = None,
) -> dict:
    """Save conversation context."""
    url = f"{NIA_API_URL}/v2/contexts"
    payload: dict[str, Any] = {"title": title, "content": content}
    if summary:
        payload["summary"] = summary
    if tags:
        payload["tags"] = tags
    if metadata:
        payload["metadata"] = metadata

    return await request_json("post", url, headers=get_headers(), json=payload)


async def context_search_text(query: str) -> dict:
    """Text search contexts."""
    url = f"{NIA_API_URL}/v2/contexts/search"
    params: dict[str, Any] = {"query": query}

    return await request_json("get", url, headers=get_headers(), params=params)


async def context_search_semantic(query: str) -> dict:
    """Semantic search contexts using embeddings."""
    url = f"{NIA_API_URL}/v2/contexts/semantic-search"
    params: dict[str, Any] = {"query": query}

    return await request_json("get", url, headers=get_headers(), params=params)


async def context_get(context_id: str) -> dict:
    """Get conversation context details."""
    url = f"{NIA_API_URL}/v2/contexts/{context_id}"

    return await request_json("get", url, headers=get_headers())


async def context_update(context_id: str, updates: dict) -> dict:
    """Update conversation context."""
    url = f"{NIA_API_URL}/v2/contexts/{context_id}"

    return await request_json("put", url, headers=get_headers(), json=updates)


async def context_delete(context_id: str) -> dict:
    """Delete conversation context."""
    url = f"{NIA_API_URL}/v2/contexts/{context_id}"

    return await request_json(
        "delete",
        url,
        headers=get_headers(),
        on_ok={"status": "deleted", "context_id": context_id},
    )


# =============================================================================
# OUTPUT FORMATTING
# =============================================================================


def format_oracle_result(result: dict) -> str:
    """Format Oracle research result."""
    if "error" in result:
        return f"Error: {result['error']}"

    output = ["# Oracle Research Result\n"]

    if result.get("final_report"):
        output.append(result["final_report"])

    if result.get("citations"):
        output.append("\n## Citations")
        for i, cite in enumerate(result["citations"], 1):
            output.append(
                f"{i}. [{cite.get('tool', 'unknown')}] {cite.get('summary', '')[:200]}"
            )

    if result.get("duration_ms"):
        output.append(
            f"\n---\nDuration: {result['duration_ms']}ms | Iterations: {result.get('iterations', 'N/A')}"
        )

    return "\n".join(output)


def _first(*values: Any) -> Any:
    """First truthy value, else "" (same result as an `a or b or ... or ""` chain)."""
    return next((v for v in values if v), "")


def _dict_field(item: dict, key: str) -> dict:
    """item[key] when it is a dict, else an empty dict."""
    value = item.get(key)
    return value if isinstance(value, dict) else {}


def _content_lines(result: dict) -> list[str]:
    """Answer text (first 2000 chars) plus up to five sources."""
    lines = [result["content"][:2000]]
    sources = result.get("sources")
    if sources:
        lines.append("\n## Sources")
        for src in sources[:5]:
            label = (
                src.get("title", src.get("path", "unknown"))
                if isinstance(src, dict)
                else src
            )
            lines.append(f"- {label}")
    return lines


def _result_title(item: dict, text: str, i: int) -> str:
    """Title: top-level -> inner -> source nested -> metadata path -> markdown heading."""
    inner = _dict_field(item, "result")
    source = _dict_field(item, "source")
    meta = _dict_field(item, "metadata")
    title = _first(
        item.get("title"),
        item.get("path"),
        item.get("name"),
        inner.get("file_path"),
        source.get("document_name"),
        source.get("display_name"),
        meta.get("document_key"),
    )
    if not title and text.startswith("# "):
        title = text.split("\n", 1)[0].lstrip("# ").strip()
    title = title or f"Result {i}"
    # Line info from grep/inner results
    line_num = inner.get("start_line")
    return f"{title}:{line_num}" if line_num else title


def _result_item_lines(i: int, item: Any) -> list[str]:
    """Lines for one entry of a "results" list."""
    if not isinstance(item, dict):
        return [f"\n{i}. {str(item)[:300]}"]
    # Grep results nest data under "result" key — unwrap it
    inner = _dict_field(item, "result")
    source = _dict_field(item, "source")
    # Content: check all levels (top, inner, document)
    text = _first(
        item.get("snippet"),
        item.get("content"),
        inner.get("content"),
        item.get("document"),
        item.get("description"),
    )
    title = _result_title(item, text, i)
    score = item.get("score")
    score_str = f" (score: {score:.3f})" if isinstance(score, (int, float)) else ""
    lines = [f"\n{i}. **{title}**{score_str}"]
    url = _first(source.get("url"), source.get("file_path"))
    if url:
        lines.append(f"   {url}")
    if text:
        lines.append(f"   {text[:800]}")
    return lines


def _match_lines(matches: list) -> list[str]:
    """Lines for up to ten grep "matches"."""
    lines = []
    for i, match in enumerate(matches[:10], 1):
        path = match.get("path", match.get("file", "unknown"))
        line = match.get("line", match.get("content", ""))
        lines.append(f"\n{i}. `{path}`")
        if line:
            lines.append(f"   {line[:200]}")
    return lines


def format_search_result(result: dict, search_type: str) -> str:
    """Format search results."""
    if "error" in result:
        return f"Error: {result['error']}"

    output = [f"# {search_type} Results\n"]
    if "content" in result:
        output += _content_lines(result)
    elif "results" in result:
        for i, item in enumerate(result["results"][:10], 1):
            output += _result_item_lines(i, item)
    elif "matches" in result:
        output += _match_lines(result["matches"])
    else:
        output.append(json.dumps(result, indent=2, default=str)[:2000])
    return "\n".join(output)


def format_list_result(result, item_type: str) -> str:
    """Format list results. Handles both dict wrapper and plain list responses."""
    if isinstance(result, dict) and "error" in result:
        return f"Error: {result['error']}"

    items = _list_items(result)
    output = [f"# {item_type}\n"]
    if not items:
        output.append("No items found.")
    output += [_list_item_line(i, item) for i, item in enumerate(items[:20], 1)]

    total = result.get("total", len(items)) if isinstance(result, dict) else len(items)
    output.append(f"\n---\nTotal: {total}")

    return "\n".join(output)


# Keys a list endpoint may nest its items under, and an item's name fields,
# each in priority order (first truthy wins).
_LIST_KEYS = ("repositories", "data_sources", "papers", "contexts", "sessions", "jobs")
_NAME_KEYS = ("display_name", "repository", "title", "url", "repository_id", "id")


def _list_items(result) -> list:
    """The item list: a plain-list response itself, else the first non-empty nested list."""
    if isinstance(result, list):
        return result
    return _first(*(result.get(k) for k in _LIST_KEYS)) or []


def _list_item_line(i: int, item: Any) -> str:
    """One numbered line: dict items as 'name (status)', others via str()."""
    if not isinstance(item, dict):
        return f"{i}. {item}"
    name = _first(*(item.get(k) for k in _NAME_KEYS)) or f"Item {i}"
    status = item.get("status", "")
    return f"{i}. {name} {f'({status})' if status else ''}"


# =============================================================================
# CLI INTERFACE
# =============================================================================


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        description="Nia API Client - Complete API coverage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Command category")
    _add_oracle_commands(subparsers)
    _add_search_commands(subparsers)
    _add_repos_commands(subparsers)
    _add_sources_commands(subparsers)
    _add_papers_commands(subparsers)
    _add_context_commands(subparsers)
    return parser


def _add_oracle_commands(subparsers) -> None:
    """Oracle research subcommands: research, sessions, session, chat, jobs, job, create-job."""
    oracle_parser = subparsers.add_parser("oracle", help="Oracle research (Pro only)")
    oracle_sub = oracle_parser.add_subparsers(dest="action")

    oracle_research_p = oracle_sub.add_parser(
        "research", help="Run autonomous research"
    )
    oracle_research_p.add_argument("query", help="Research question")
    oracle_research_p.add_argument("--repos", nargs="*", help="Repository IDs")
    oracle_research_p.add_argument("--sources", nargs="*", help="Data source IDs")
    oracle_research_p.add_argument(
        "--model",
        default="claude-opus-4-5-20251101",
        choices=[
            "claude-opus-4-5-20251101",
            "claude-sonnet-4-5-20250929",
            "claude-sonnet-4-5-1m",
        ],
    )
    oracle_research_p.add_argument(
        "--stream", action="store_true", help="Stream results"
    )

    oracle_sessions_p = oracle_sub.add_parser("sessions", help="List research sessions")
    oracle_sessions_p.add_argument("--limit", type=int, default=20)

    oracle_session_p = oracle_sub.add_parser("session", help="Get session details")
    oracle_session_p.add_argument("session_id", help="Session ID")
    oracle_session_p.add_argument(
        "--messages", action="store_true", help="Get chat messages"
    )

    oracle_chat_p = oracle_sub.add_parser("chat", help="Follow-up chat in session")
    oracle_chat_p.add_argument("session_id", help="Session ID")
    oracle_chat_p.add_argument("message", help="Follow-up message")

    oracle_sub.add_parser("jobs", help="List research jobs")

    oracle_job_p = oracle_sub.add_parser("job", help="Get/manage job")
    oracle_job_p.add_argument("job_id", help="Job ID")
    oracle_job_p.add_argument("--cancel", action="store_true", help="Cancel job")
    oracle_job_p.add_argument("--stream", action="store_true", help="Stream events")

    oracle_create_job_p = oracle_sub.add_parser("create-job", help="Create async job")
    oracle_create_job_p.add_argument("query", help="Research question")
    oracle_create_job_p.add_argument("--repos", nargs="*", help="Repository IDs")
    oracle_create_job_p.add_argument("--model", default="claude-opus-4-5-20251101")


def _add_search_commands(subparsers) -> None:
    """Search subcommands: universal, web, deep, package, query."""
    search_parser = subparsers.add_parser("search", help="Search operations")
    search_sub = search_parser.add_subparsers(dest="action")

    search_universal_p = search_sub.add_parser("universal", help="Universal search")
    search_universal_p.add_argument("query", help="Search query")
    search_universal_p.add_argument("--limit", type=int, default=10)

    search_web_p = search_sub.add_parser("web", help="Web search")
    search_web_p.add_argument("query", help="Search query")
    search_web_p.add_argument("--category", help="Category filter")
    search_web_p.add_argument("--time", help="Time range")

    search_deep_p = search_sub.add_parser("deep", help="Deep research (Pro)")
    search_deep_p.add_argument("query", help="Research query")

    search_package_p = search_sub.add_parser("package", help="Search in package")
    search_package_p.add_argument("package", help="Package name")
    search_package_p.add_argument("--query", help="Semantic search query")
    search_package_p.add_argument("--grep", help="Regex pattern")
    search_package_p.add_argument(
        "--registry", default="py_pi", choices=["npm", "py_pi", "crates", "go_modules"]
    )
    search_package_p.add_argument("--limit", type=int, default=10)

    search_query_p = search_sub.add_parser("query", help="Query repos/docs")
    search_query_p.add_argument("query", help="Query text")
    search_query_p.add_argument("--repos", nargs="*", help="Repository IDs")
    search_query_p.add_argument("--sources", nargs="*", help="Data source IDs")


def _add_repos_commands(subparsers) -> None:
    """Repository subcommands: list, index, status, tree, content, grep, delete."""
    repos_parser = subparsers.add_parser("repos", help="Repository operations")
    repos_sub = repos_parser.add_subparsers(dest="action")

    repos_list_p = repos_sub.add_parser("list", help="List repositories")
    repos_list_p.add_argument("--filter", help="Filter substring")
    repos_list_p.add_argument("--status", help="Status filter")
    repos_list_p.add_argument("--limit", type=int, default=100)

    repos_index_p = repos_sub.add_parser("index", help="Index repository")
    repos_index_p.add_argument("repo", help="owner/repo")
    repos_index_p.add_argument("--token", help="GitHub token for private repos")

    repos_status_p = repos_sub.add_parser("status", help="Get status")
    repos_status_p.add_argument("repo_id", help="Repository ID")

    repos_tree_p = repos_sub.add_parser("tree", help="Get tree structure")
    repos_tree_p.add_argument("repo_id", help="Repository ID")

    repos_content_p = repos_sub.add_parser("content", help="Get file content")
    repos_content_p.add_argument("repo_id", help="Repository ID")
    repos_content_p.add_argument("path", help="File path")

    repos_grep_p = repos_sub.add_parser("grep", help="Search with regex")
    repos_grep_p.add_argument("repo_id", help="Repository ID")
    repos_grep_p.add_argument("pattern", help="Regex pattern")
    repos_grep_p.add_argument("--context", type=int, default=3)

    repos_delete_p = repos_sub.add_parser("delete", help="Delete repository")
    repos_delete_p.add_argument("repo_id", help="Repository ID")


def _add_sources_commands(subparsers) -> None:
    """Data source subcommands: list, index, get, tree, content, grep, delete."""
    sources_parser = subparsers.add_parser("sources", help="Data source operations")
    sources_sub = sources_parser.add_subparsers(dest="action")

    sources_list_p = sources_sub.add_parser("list", help="List sources")
    sources_list_p.add_argument("--filter", help="Filter substring")
    sources_list_p.add_argument("--status", help="Status filter")
    sources_list_p.add_argument("--limit", type=int, default=100)

    sources_index_p = sources_sub.add_parser("index", help="Index documentation")
    sources_index_p.add_argument("url", help="Documentation URL")
    sources_index_p.add_argument("--name", help="Display name")

    sources_get_p = sources_sub.add_parser("get", help="Get source details")
    sources_get_p.add_argument("source_id", help="Source ID")

    sources_tree_p = sources_sub.add_parser("tree", help="Get tree structure")
    sources_tree_p.add_argument("source_id", help="Source ID")

    sources_content_p = sources_sub.add_parser("content", help="Get page content")
    sources_content_p.add_argument("source_id", help="Source ID")
    sources_content_p.add_argument("path", help="Page path")

    sources_grep_p = sources_sub.add_parser("grep", help="Search with regex")
    sources_grep_p.add_argument("source_id", help="Source ID")
    sources_grep_p.add_argument("pattern", help="Regex pattern")

    sources_delete_p = sources_sub.add_parser("delete", help="Delete source")
    sources_delete_p.add_argument("source_id", help="Source ID")


def _add_papers_commands(subparsers) -> None:
    """Research paper subcommands: list, index."""
    papers_parser = subparsers.add_parser("papers", help="Research papers")
    papers_sub = papers_parser.add_subparsers(dest="action")

    papers_list_p = papers_sub.add_parser("list", help="List papers")
    papers_list_p.add_argument("--status", help="Status filter")
    papers_list_p.add_argument("--limit", type=int, default=50)

    papers_index_p = papers_sub.add_parser("index", help="Index arXiv paper")
    papers_index_p.add_argument("arxiv_id", help="arXiv ID (e.g., 2310.06825)")


def _add_context_commands(subparsers) -> None:
    """Context sharing subcommands: list, save, search, get, delete."""
    context_parser = subparsers.add_parser("context", help="Context sharing")
    context_sub = context_parser.add_subparsers(dest="action")

    context_list_p = context_sub.add_parser("list", help="List contexts")
    context_list_p.add_argument("--tags", help="Filter by tags")
    context_list_p.add_argument("--limit", type=int, default=20)

    context_save_p = context_sub.add_parser("save", help="Save context")
    context_save_p.add_argument("--title", required=True, help="Context title")
    context_save_p.add_argument("--content", required=True, help="Context content")
    context_save_p.add_argument("--summary", help="Optional summary")
    context_save_p.add_argument("--tags", nargs="*", help="Tags")

    context_search_p = context_sub.add_parser("search", help="Search contexts")
    context_search_p.add_argument("query", help="Search query")
    context_search_p.add_argument(
        "--semantic", action="store_true", help="Use semantic search"
    )

    context_get_p = context_sub.add_parser("get", help="Get context")
    context_get_p.add_argument("context_id", help="Context ID")

    context_delete_p = context_sub.add_parser("delete", help="Delete context")
    context_delete_p.add_argument("context_id", help="Context ID")


def _json(result: Any) -> str:
    """Pretty JSON; non-JSON values via str()."""
    return json.dumps(result, indent=2, default=str)


def _json_strict(result: Any) -> str:
    """Pretty JSON without a default (the delete commands' output)."""
    return json.dumps(result, indent=2)


def _content_or_json(result: dict) -> str:
    """A content endpoint's "content", else the whole result as JSON."""
    return result.get("content", json.dumps(result, indent=2))


def _listing(title: str):
    """Formatter: format_list_result under `title`."""
    return lambda result: format_list_result(result, title)


def _searching(title: str):
    """Formatter: format_search_result under `title`."""
    return lambda result: format_search_result(result, title)


def _call(*args: Any, **kwargs: Any) -> tuple[tuple, dict]:
    """Positional and keyword arguments for one API call."""
    return args, kwargs


def _route(func: str, call, fmt, banner=None):
    """Handler for a plain subcommand: optional banner line, one API call, print fmt(result).

    `func` is looked up in the module at call time, so the dispatch table always
    reaches the current module-level API function.
    """

    async def handler(args: argparse.Namespace) -> None:
        if banner:
            print(banner(args))
        pos, kw = call(args)
        print(fmt(await globals()[func](*pos, **kw)))

    return handler


async def _oracle_research(args: argparse.Namespace) -> None:
    """oracle research [--stream]."""
    print(f"Running Oracle research: {args.query}")
    if args.stream:
        await oracle_research_stream(args.query, args.repos, args.sources, args.model)
        return
    result = await oracle_research(
        args.query, args.repos, args.sources, model=args.model
    )
    print(format_oracle_result(result))


async def _oracle_session(args: argparse.Namespace) -> None:
    """oracle session <id> [--messages]."""
    if args.messages:
        result = await oracle_get_messages(args.session_id)
    else:
        result = await oracle_get_session(args.session_id)
    print(_json(result))


async def _oracle_chat(args: argparse.Namespace) -> None:
    """oracle chat <id> <message> (streams its own output)."""
    await oracle_chat_followup(args.session_id, args.message)


async def _oracle_job(args: argparse.Namespace) -> None:
    """oracle job <id> [--cancel | --stream]; --cancel wins over --stream."""
    if args.cancel:
        result = await oracle_cancel_job(args.job_id)
    elif args.stream:
        await oracle_stream_job_events(args.job_id)
        return
    else:
        result = await oracle_get_job(args.job_id)
    print(_json(result))


async def _search_package(args: argparse.Namespace) -> None:
    """search package <pkg> [--grep PATTERN | --query TEXT]."""
    if args.grep:
        print(f"Package grep: {args.package} / {args.grep}")
        result = await search_package_grep(
            args.package, args.grep, args.registry, args.limit
        )
    else:
        print(f"Package search: {args.package} / {args.query}")
        result = await search_package_hybrid(
            args.package, args.query or "", args.registry, args.limit
        )
    print(format_search_result(result, "Package Search"))


async def _context_search(args: argparse.Namespace) -> None:
    """context search <query> [--semantic]."""
    if args.semantic:
        result = await context_search_semantic(args.query)
    else:
        result = await context_search_text(args.query)
    print(format_search_result(result, "Context Search"))


# (command, action) -> async handler(args). A known command with a missing or
# unknown action prints nothing; an unknown command prints the help.
HANDLERS = {
    ("oracle", "research"): _oracle_research,
    ("oracle", "sessions"): _route(
        "oracle_list_sessions", lambda a: _call(a.limit), _listing("Oracle Sessions")
    ),
    ("oracle", "session"): _oracle_session,
    ("oracle", "chat"): _oracle_chat,
    ("oracle", "jobs"): _route(
        "oracle_list_jobs", lambda a: _call(), _listing("Oracle Jobs")
    ),
    ("oracle", "job"): _oracle_job,
    ("oracle", "create-job"): _route(
        "oracle_create_job",
        lambda a: _call(a.query, a.repos, model=a.model),
        _json,
    ),
    ("search", "universal"): _route(
        "search_universal",
        lambda a: _call(a.query, a.limit),
        _searching("Universal Search"),
        lambda a: f"Universal search: {a.query}",
    ),
    ("search", "web"): _route(
        "search_web",
        lambda a: _call(a.query, a.category, a.time),
        _searching("Web Search"),
        lambda a: f"Web search: {a.query}",
    ),
    ("search", "deep"): _route(
        "search_deep",
        lambda a: _call(a.query),
        _searching("Deep Research"),
        lambda a: f"Deep research: {a.query}",
    ),
    ("search", "package"): _search_package,
    ("search", "query"): _route(
        "search_query",
        lambda a: _call([{"role": "user", "content": a.query}], a.repos, a.sources),
        _searching("Query"),
    ),
    ("repos", "list"): _route(
        "repos_list",
        lambda a: _call(a.filter, a.status, a.limit),
        _listing("Repositories"),
    ),
    ("repos", "index"): _route(
        "repos_index",
        lambda a: _call(a.repo, a.token),
        _json,
        lambda a: f"Indexing repository: {a.repo}",
    ),
    ("repos", "status"): _route("repos_status", lambda a: _call(a.repo_id), _json),
    ("repos", "tree"): _route("repos_tree", lambda a: _call(a.repo_id), _json),
    ("repos", "content"): _route(
        "repos_content", lambda a: _call(a.repo_id, a.path), _content_or_json
    ),
    ("repos", "grep"): _route(
        "repos_grep",
        lambda a: _call(a.repo_id, a.pattern, a.context),
        _searching("Repository Grep"),
    ),
    ("repos", "delete"): _route(
        "repos_delete", lambda a: _call(a.repo_id), _json_strict
    ),
    ("sources", "list"): _route(
        "sources_list",
        lambda a: _call(a.filter, a.status, limit=a.limit),
        _listing("Data Sources"),
    ),
    ("sources", "index"): _route(
        "sources_index",
        lambda a: _call(a.url, a.name),
        _json,
        lambda a: f"Indexing: {a.url}",
    ),
    ("sources", "get"): _route("sources_get", lambda a: _call(a.source_id), _json),
    ("sources", "tree"): _route("sources_tree", lambda a: _call(a.source_id), _json),
    ("sources", "content"): _route(
        "sources_content", lambda a: _call(a.source_id, a.path), _content_or_json
    ),
    ("sources", "grep"): _route(
        "sources_grep",
        lambda a: _call(a.source_id, a.pattern),
        _searching("Source Grep"),
    ),
    ("sources", "delete"): _route(
        "sources_delete", lambda a: _call(a.source_id), _json_strict
    ),
    ("papers", "list"): _route(
        "papers_list",
        lambda a: _call(a.limit, status=a.status),
        _listing("Research Papers"),
    ),
    ("papers", "index"): _route(
        "papers_index",
        lambda a: _call(a.arxiv_id),
        _json,
        lambda a: f"Indexing arXiv paper: {a.arxiv_id}",
    ),
    ("context", "list"): _route(
        "context_list",
        lambda a: _call(a.limit, tags=a.tags),
        _listing("Contexts"),
    ),
    ("context", "save"): _route(
        "context_save",
        lambda a: _call(a.title, a.content, a.summary, a.tags),
        _json,
    ),
    ("context", "search"): _context_search,
    ("context", "get"): _route("context_get", lambda a: _call(a.context_id), _json),
    ("context", "delete"): _route(
        "context_delete", lambda a: _call(a.context_id), _json_strict
    ),
}
COMMANDS = frozenset(command for command, _ in HANDLERS)


async def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Run the handler for (command, action)."""
    if args.command not in COMMANDS:
        parser.print_help()
        return
    handler = HANDLERS.get((args.command, args.action))
    if handler is not None:
        await handler(args)


async def main():
    """CLI entry point: parse, check the key, dispatch; errors are printed, not raised."""
    parser = build_parser()

    # Handle args that might come from runtime harness
    args_to_parse = [arg for arg in sys.argv[1:] if not arg.endswith(".py")]
    if not args_to_parse:
        parser.print_help()
        return

    args = parser.parse_args(args_to_parse)

    if not NIA_API_KEY:
        print("Error: NIA_API_KEY not found. Set in environment or ~/.claude/.env")
        return

    try:
        await _dispatch(args, parser)
    except ImportError:
        print("Error: aiohttp not installed. Run: pip install aiohttp")
    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
