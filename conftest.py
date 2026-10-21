"""Repo-wide pytest hooks: isolate tests from tools/viz/export's shared browser.

The viz test modules launch export's shared Playwright chromium at import
(collection) time. Playwright's sync API leaves the main thread marked as
running its event loop, so asyncio.run() in any later, unrelated test raises
"cannot be called from a running event loop". Each test therefore starts with
that mark cleared; export.get_browser()/render_html() set it again for the
tests that use the browser. The session closes the browser while pytest still
runs (export's exit hook is the backstop).
"""

import sys

import pytest


def _export():
    return sys.modules.get("tools.viz.export")


@pytest.fixture(autouse=True)
def _clear_playwright_loop_mark():
    export = _export()
    if export is not None:
        export._release_loop_mark()
    yield


def pytest_sessionfinish(session, exitstatus):
    export = _export()
    if export is not None:
        export.close_browser()
