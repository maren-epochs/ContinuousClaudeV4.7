"""Shared helpers for the tools/ unittest suites (not collected: no test_ prefix).

load_module imports a tool by file path under a private name; run_async runs a
coroutine function off the main thread; fake_aiohttp builds a stand-in
`aiohttp` module that records every request instead of touching the network.
"""

import asyncio
import concurrent.futures
import importlib.util
import types
from pathlib import Path


def load_module(name: str, path: Path) -> types.ModuleType:
    """Execute the file at path as a fresh module called name and return it."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_async(fn, *args, **kwargs):
    """asyncio.run(fn(*args, **kwargs)) on a worker thread.

    A sync Playwright session started earlier in the same pytest process (the
    viz suites) leaves an event loop running on the main thread, where
    asyncio.run() refuses to start; a fresh thread has no running loop.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, fn(*args, **kwargs)).result()


class FakeResponse:
    """Async-context response with a status, a text/JSON body and SSE lines."""

    def __init__(self, status, body, lines=()):
        self.status = status
        self._body = body
        self.content = self._iter(lines)

    async def _iter(self, lines):
        """Yield the canned byte lines (resp.content stand-in)."""
        for line in lines:
            yield line

    async def text(self):
        """The body as text."""
        return str(self._body)

    async def json(self):
        """The body unchanged."""
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """ClientSession stand-in: logs (METHOD, url, kwargs) and returns one response."""

    def __init__(self, log, response):
        self._log = log
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _request(self, method, url, **kwargs):
        """Record the call and hand back the canned response."""
        self._log.append((method, url, kwargs))
        return self._response

    def get(self, url, **kw):
        """Recorded GET."""
        return self._request("GET", url, **kw)

    def post(self, url, **kw):
        """Recorded POST."""
        return self._request("POST", url, **kw)

    def delete(self, url, **kw):
        """Recorded DELETE."""
        return self._request("DELETE", url, **kw)

    def patch(self, url, **kw):
        """Recorded PATCH."""
        return self._request("PATCH", url, **kw)


def fake_aiohttp(log, response):
    """A module standing in for aiohttp; ClientTimeout(total=n) is ("timeout", n)."""
    mod = types.ModuleType("aiohttp")
    mod.ClientSession = lambda: FakeSession(log, response)  # type: ignore[attr-defined]
    mod.ClientTimeout = lambda total: ("timeout", total)  # type: ignore[attr-defined]
    return mod
