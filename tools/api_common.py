"""Shared aiohttp request layer for the nia_docs and exa_search CLIs.

aiohttp is imported inside each call (not at module import), so the CLIs load
and print their own "aiohttp not installed" guidance when it is missing, and
tests can swap in a fake `aiohttp` through sys.modules.
"""

from pathlib import Path
from typing import Any


def env_file_value(env_path: Path, name: str) -> str | None:
    """Text after `name=` on the first such line (line stripped); None if absent."""
    if not env_path.exists():
        return None
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1]
    return None


def json_headers(auth_name: str, auth_value: str) -> dict:
    """Request headers: the API's auth header, then a JSON content type."""
    return {auth_name: auth_value, "Content-Type": "application/json"}


def _timeout_kwargs(aiohttp: Any, timeout_s: int | None, kwargs: dict) -> dict:
    """kwargs plus timeout=ClientTimeout(total=timeout_s) when a timeout is given."""
    if timeout_s is not None:
        kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout_s)
    return kwargs


async def request_json(
    method: str,
    url: str,
    *,
    timeout_s: int | None = None,
    on_ok: Any = None,
    **kwargs: Any,
) -> Any:
    """One request in a fresh ClientSession via session.<method>(url, **kwargs).

    Non-200 -> {"error": "API error <status>: <body text>"}; 200 -> on_ok when
    given (endpoints whose reply body is not used), else the JSON body.
    """
    import aiohttp

    kwargs = _timeout_kwargs(aiohttp, timeout_s, kwargs)
    async with (
        aiohttp.ClientSession() as session,
        getattr(session, method)(url, **kwargs) as resp,
    ):
        if resp.status != 200:
            return {"error": f"API error {resp.status}: {await resp.text()}"}
        return await resp.json() if on_ok is None else on_ok


async def stream_data_lines(
    method: str, url: str, *, timeout_s: int | None = None, **kwargs: Any
) -> None:
    """Print the payload of each SSE `data:` line; non-200 prints 'Error: <status> - <body>'."""
    import aiohttp

    kwargs = _timeout_kwargs(aiohttp, timeout_s, kwargs)
    async with (
        aiohttp.ClientSession() as session,
        getattr(session, method)(url, **kwargs) as resp,
    ):
        if resp.status != 200:
            print(f"Error: {resp.status} - {await resp.text()}")
            return
        async for line in resp.content:
            text = line.decode("utf-8").strip()
            if text.startswith("data:"):
                print(text[5:].strip())
