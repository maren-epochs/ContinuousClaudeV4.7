#!/usr/bin/env python3
"""tools.viz.export - write any chart object to png/svg/html, rasterize HTML.

save(fig, path, formats=("png", "svg", "html")) dispatches on the object type:

    matplotlib Figure (or Axes / seaborn grid)  fig.savefig  (png dpi 144, svg)
    plotly Figure      write_html; png/svg via kaleido 1.x, png falls back to
                       render_html when kaleido raises (no Chrome, ...)
    altair Chart       chart.save via vl-convert (png/svg/html, no browser)
    bokeh model        bokeh.embed.file_html (INLINE); png via render_html
    great_tables GT    as_raw_html(make_page=True); png via render_html of the
                       table's container, shrink-wrapped (its padding = the
                       style.gt_style margin; gtsave is never called - it
                       wants its own Chrome)
    holoviews object   holoviews.render (bokeh, then matplotlib) and recurse

and returns {"png": path|None, "svg": path|None, "html": path|None,
"notes": [str]} with absolute paths. Formats that a backend cannot produce
are None with a note saying why.

render_html(html_or_path, png_path, width=1200, height=800, mode="light")
rasterizes an HTML string or file with ONE module-level Playwright chromium
(started lazily, shared across calls, closed at exit). It emulates
prefers-color-scheme=mode, sets <html data-theme=mode>, waits for network idle
and then for the readiness hook READY_HOOK (window.__chartsReady):

    undefined          -> no wait
    a promise/thenable -> wait until it settles (rejection = ExportError)
    any other value    -> wait until it becomes truthy (e.g. false ... true)

Pages that draw asynchronously must assign window.__chartsReady synchronously
in <head> (before load). The browser lives on the thread that started it; call
the browser-backed functions from one thread and not inside a running asyncio
loop (Playwright sync API restriction).

Interpreter exit never blocks on a browser. Importing this module registers
_on_exit with threading's pre-join exit hooks (it runs before non-daemon
threads are joined, i.e. before atexit). It (1) closes the shared browser with
close_browser(), which is time-limited: past CLOSE_TIMEOUT_S the Playwright
driver process tree is killed, which fails the pending call; and (2) releases
kaleido's Chrome stderr readers: choreographer logs Chrome's stderr through
logistro.getPipeLogger, a NON-daemon thread that reads the pipe until EOF. A
Chrome child that survives an unclean kill keeps the write end open, so that
thread - and with it interpreter shutdown - waited forever. On Windows the
blocked read is cancelled (CancelSynchronousIo); a reader still alive after
EXIT_GRACE_S forces exit (os._exit(1), reason on stderr) instead of hanging.

No function prints; nothing is written outside the directory of `path` /
`png_path`. Chart libraries and Playwright are imported lazily.

CLI (for skills calling from Bash):
    py -3.13 tools/viz/export.py render page.html out.png --mode dark --width 1200 --height 800
prints the absolute png path; exit 2 with the reason on stderr on failure.
"""

import argparse
import atexit
import contextlib
import os
import pathlib
import subprocess
import sys
import threading
import time

READY_HOOK = "window.__chartsReady"
FORMATS = ("png", "svg", "html")
MODES = ("light", "dark")
PNG_DPI = 144  # matplotlib png
FIGURE_SCALE = 2  # plotly/altair/bokeh/GT png pixel density
DEFAULT_TIMEOUT_MS = 30000
INSTALL_HINT = (
    "py -3.13 -m pip install playwright && py -3.13 -m playwright install chromium"
)
_EXTENSIONS = (".png", ".svg", ".html", ".htm")
CLOSE_TIMEOUT_S = 10.0  # browser.close + playwright.stop before the driver is killed
EXIT_GRACE_S = 5.0  # browser stderr readers at exit, before forcing exit
_KILL_TIMEOUT_S = 10.0
_EXIT_BACKSTOP_S = CLOSE_TIMEOUT_S + _KILL_TIMEOUT_S + EXIT_GRACE_S + 10.0

_STATE = {"playwright": None, "browser": None}

# Polled by page.wait_for_function. A thenable is watched once and reported
# through a private global so a rejection can be surfaced.
_READY_JS = """() => {
  const r = window.__chartsReady;
  if (r && typeof r.then === 'function') {
    if (!window.__vizExportWatch) {
      const w = window.__vizExportWatch = {done: false, error: null};
      Promise.resolve(r).then(() => { w.done = true; },
                              (e) => { w.error = String(e); w.done = true; });
    }
    return window.__vizExportWatch.done;
  }
  return r === undefined || Boolean(r);
}"""
_READY_ERROR_JS = (
    "() => (window.__vizExportWatch && window.__vizExportWatch.error) || null"
)
_SETTLE_JS = """() => document.fonts.ready.then(() => new Promise(
  (resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))"""
_THEME_JS = "(mode) => { document.documentElement.dataset.theme = mode; }"
_RESET_CSS = "<style>html,body{margin:0;padding:0}</style>"
# great_tables PNG: screenshot the table's container (its padding is the margin),
# shrink-wrapped to the table and never scrolling. The page background takes the
# container's (else the table's) background so the sub-pixel sliver a fractional
# container width leaves at the screenshot edge is not the white/dark canvas.
_GT_CONTAINER = "div:has(> table.gt_table)"
_GT_PNG_CSS = (
    "<style>"
    + _GT_CONTAINER
    + "{width:max-content !important;overflow:visible !important}</style>"
    "<script>addEventListener('DOMContentLoaded', () => {"
    " const box = document.querySelector('" + _GT_CONTAINER + "');"
    " if (!box) return;"
    " const clear = (c) => !c || c === 'transparent' || c === 'rgba(0, 0, 0, 0)';"
    " let bg = getComputedStyle(box).backgroundColor;"
    " if (clear(bg)) bg = getComputedStyle(box.querySelector('table')).backgroundColor;"
    " if (!clear(bg)) document.documentElement.style.background = bg;"
    "});</script>"
)


class ExportError(RuntimeError):
    """Export could not be produced (missing browser, page never ready, ...)."""


def _short(exc):
    text = str(exc).strip().splitlines()
    text = text[0] if text else type(exc).__name__
    return text.encode("ascii", "replace").decode("ascii")[:200]


# ---------------------------------------------------------------- browser


def get_browser():
    """Return the shared Playwright chromium, launching it on first use."""
    browser = _STATE["browser"]
    if browser is not None and browser.is_connected():
        _mark_loop(_STATE["playwright"])
        return browser
    close_browser()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ExportError(
            f"playwright is not installed ({_short(exc)}); install: {INSTALL_HINT}"
        ) from exc
    pw = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as exc:
        if pw is not None:
            with contextlib.suppress(Exception):
                pw.stop()
        raise ExportError(
            f"cannot launch Playwright chromium ({_short(exc)}); install: "
            f"{INSTALL_HINT}"
        ) from exc
    _STATE["playwright"], _STATE["browser"] = pw, browser
    return browser


def close_browser(timeout=CLOSE_TIMEOUT_S):
    """Close the shared browser (idempotent; the exit hook calls it too).

    Returns True when browser.close + playwright.stop finished within `timeout`
    seconds, False when a daemon watchdog had to kill the Playwright driver
    process tree (which fails the pending call so this returns). Call it from
    the thread that started the browser.
    """
    browser, pw = _STATE["browser"], _STATE["playwright"]
    _STATE["browser"] = _STATE["playwright"] = None
    if browser is None and pw is None:
        return True
    pid = _driver_pid(pw)
    finished = threading.Event()
    killed = []

    def watchdog():
        if not finished.wait(timeout) and pid is not None:
            killed.append(pid)
            _kill_tree(pid)

    guard = threading.Thread(
        target=watchdog, name="viz-export-close-watchdog", daemon=True
    )
    guard.start()
    _mark_loop(pw)
    try:
        if browser is not None:
            with contextlib.suppress(Exception):
                browser.close()
        if pw is not None:
            with contextlib.suppress(Exception):
                pw.stop()
    finally:
        finished.set()
        guard.join(_KILL_TIMEOUT_S + 1)
        _release_loop_mark(pw)  # the loop is closed now
    return not killed


def _pw_loop(pw):
    loop = getattr(pw, "_loop", None)  # SyncBase._loop: the dispatcher's loop
    return loop if hasattr(loop, "is_closed") else None


def _mark_loop(pw):
    """Re-mark this thread as running Playwright's loop when the mark was cleared.

    Playwright's sync API sets that mark (asyncio's running loop) after every
    call and needs it for the next one; _release_loop_mark clears it.
    """
    import asyncio

    loop = _pw_loop(pw)
    if (
        loop is not None
        and not loop.is_closed()
        and asyncio.events._get_running_loop() is None
    ):
        asyncio.events._set_running_loop(loop)


def _release_loop_mark(pw=None):
    """Clear this thread's running-loop mark if it is (shared) Playwright's.

    While it is set, asyncio.run() in this thread raises "cannot be called from
    a running event loop". The next get_browser()/render_html() re-marks. Returns
    True when the thread has no running-loop mark afterwards.
    """
    import asyncio

    current = asyncio.events._get_running_loop()
    if current is None:
        return True
    ours = _pw_loop(pw if pw is not None else _STATE["playwright"])
    if current is ours or current.is_closed():
        asyncio.events._set_running_loop(None)
        return True
    return False


def _driver_pid(pw):
    """pid of the Playwright node driver (private attribute chain), or None."""
    obj = pw
    for name in ("_impl_obj", "_connection", "_transport", "_proc"):
        obj = getattr(obj, name, None)
    pid = getattr(obj, "pid", None)
    return pid if isinstance(pid, int) else None


def _kill_tree(pid):
    """Kill a process and its children (best effort, bounded)."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_KILL_TIMEOUT_S,
                check=False,
            )
        else:
            import signal

            os.kill(pid, signal.SIGKILL)


# ---------------------------------------------------------------- exit hook


def _browser_pipe_readers():
    """Live non-daemon logistro pipe readers (choreographer's Chrome stderr)."""
    current = threading.current_thread()
    readers = []
    for thread in threading.enumerate():
        target = getattr(thread, "_target", None)  # deleted once run() returns
        module = str(getattr(target, "__module__", None) or "")
        if (
            thread is not current
            and not thread.daemon
            and thread.is_alive()
            and module.split(".")[0] == "logistro"
        ):
            readers.append(thread)
    return readers


def _cancel_blocking_read(thread):
    """Windows: abort the synchronous read `thread` is blocked in. True if sent."""
    if sys.platform != "win32" or thread.native_id is None:
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    thread_terminate = 0x0001  # access right CancelSynchronousIo requires
    handle = kernel32.OpenThread(thread_terminate, False, thread.native_id)
    if not handle:
        return False
    try:
        return bool(kernel32.CancelSynchronousIo(handle))
    finally:
        kernel32.CloseHandle(handle)


def _release_pipe_readers(grace=EXIT_GRACE_S):
    """Unblock browser stderr readers; return the ones still alive after `grace`.

    A reader whose read is cancelled sees an OSError, flushes its buffered
    lines and returns (logistro treats any read error as end of stream).
    """
    deadline = time.monotonic() + grace
    readers = _browser_pipe_readers()
    while readers and time.monotonic() < deadline:
        for thread in readers:
            _cancel_blocking_read(thread)  # retried: the thread may be mid-log
            thread.join(0.05)
        readers = [t for t in readers if t.is_alive()]
    return readers


def _force_exit(reason):
    with contextlib.suppress(Exception):
        sys.stderr.write(f"export.py: {reason}; forcing exit\n")
        sys.stderr.flush()
    os._exit(1)


def _on_exit():
    """Pre-join exit hook: close the browser, release blocked stderr readers.

    A daemon backstop timer forces exit if this cleanup itself hangs.
    """
    backstop = threading.Timer(
        _EXIT_BACKSTOP_S,
        _force_exit,
        args=(f"exit cleanup did not finish within {_EXIT_BACKSTOP_S:g}s",),
    )
    backstop.daemon = True
    backstop.start()
    try:
        close_browser()
        stuck = _release_pipe_readers(EXIT_GRACE_S)
    except Exception:  # noqa: BLE001 - never raise into interpreter shutdown
        stuck = []
    finally:
        backstop.cancel()
    if stuck:
        _force_exit(
            f"{len(stuck)} browser stderr reader thread(s) still blocked after "
            f"{EXIT_GRACE_S:g}s (a browser child process holds the pipe open)"
        )


def _register_exit_hook():
    """Run _on_exit before non-daemon threads are joined (atexit runs after)."""
    register = getattr(threading, "_register_atexit", None)
    if register is None:
        atexit.register(_on_exit)
        return
    with contextlib.suppress(RuntimeError):  # already shutting down
        register(_on_exit)


_register_exit_hook()


# ---------------------------------------------------------------- render_html


def _html_source(html_or_path):
    if isinstance(html_or_path, bytes):
        html_or_path = html_or_path.decode("utf-8")
    if isinstance(html_or_path, os.PathLike) or (
        isinstance(html_or_path, str) and "<" not in html_or_path
    ):
        path = os.path.abspath(os.fspath(html_or_path))
        if not os.path.isfile(path):
            raise FileNotFoundError(f"no such HTML file: {path}")
        return "url", pathlib.Path(path).as_uri()
    if isinstance(html_or_path, str):
        return "html", html_or_path
    raise TypeError(
        f"html_or_path must be an HTML string or a file path, "
        f"not {type(html_or_path).__name__}"
    )


def render_html(
    html_or_path,
    png_path,
    width=1200,
    height=800,
    mode="light",
    *,
    scale=1,
    timeout_ms=DEFAULT_TIMEOUT_MS,
    selector=None,
):
    """Rasterize HTML (string or file path) to png_path; return its absolute path.

    width/height set the viewport (CSS px); the screenshot is full_page, or the
    first element matching `selector` when given. scale = device pixel ratio.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    png_path = os.path.abspath(os.fspath(png_path))
    kind, source = _html_source(html_or_path)
    browser = get_browser()
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    context = browser.new_context(
        viewport={"width": int(width), "height": int(height)},
        device_scale_factor=scale,
        color_scheme=mode,
    )
    try:
        page = context.new_page()
        page.emulate_media(color_scheme=mode)
        try:
            if kind == "url":
                page.goto(source, wait_until="networkidle", timeout=timeout_ms)
            else:
                page.set_content(source, wait_until="networkidle", timeout=timeout_ms)
        except PlaywrightTimeout as exc:
            raise ExportError(
                f"page did not reach network idle within {timeout_ms} ms"
            ) from exc
        page.evaluate(_THEME_JS, mode)
        try:
            page.wait_for_function(_READY_JS, timeout=timeout_ms)
        except PlaywrightTimeout as exc:
            raise ExportError(
                f"{READY_HOOK} did not become ready within {timeout_ms} ms"
            ) from exc
        failure = page.evaluate(_READY_ERROR_JS)
        if failure:
            raise ExportError(f"{READY_HOOK} rejected: {failure}")
        page.evaluate(_SETTLE_JS)
        os.makedirs(os.path.dirname(png_path), exist_ok=True)
        if selector:
            page.locator(selector).first.screenshot(path=png_path, timeout=timeout_ms)
        else:
            page.screenshot(path=png_path, full_page=True, timeout=timeout_ms)
    except PlaywrightError as exc:
        raise ExportError(f"render_html failed: {_short(exc)}") from exc
    finally:
        with contextlib.suppress(Exception):
            context.close()
    return png_path


html_to_png = render_html


# ---------------------------------------------------------------- save


def _formats(formats):
    if isinstance(formats, str):
        formats = (formats,)
    fmts = tuple(dict.fromkeys(f.lower().lstrip(".") for f in formats))
    bad = [f for f in fmts if f not in FORMATS]
    if bad or not fmts:
        raise ValueError(
            f"formats must be a non-empty subset of {FORMATS}, got {formats!r}"
        )
    return fmts


def _stem(path):
    path = os.path.abspath(os.fspath(path))
    root, ext = os.path.splitext(path)
    if ext.lower() in _EXTENSIONS:
        path = root
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _roots(obj):
    return {cls.__module__.split(".")[0] for cls in type(obj).__mro__}


def _classify(obj):
    """Return (kind, object-to-export). Only libraries already imported are checked."""
    roots = _roots(obj)
    if roots & {"matplotlib", "seaborn"}:
        from matplotlib.figure import Figure

        if isinstance(obj, Figure):
            return "matplotlib", obj
        fig = getattr(obj, "figure", None)
        if isinstance(fig, Figure):
            return "matplotlib", fig
    if "plotly" in roots:
        from plotly.basedatatypes import BaseFigure

        if isinstance(obj, BaseFigure):
            return "plotly", obj
    if "altair" in roots:
        import altair

        if isinstance(obj, altair.TopLevelMixin):
            return "altair", obj
    if "bokeh" in roots:
        from bokeh.document import Document
        from bokeh.model import Model

        if isinstance(obj, (Model, Document)):
            return "bokeh", obj
    if "great_tables" in roots:
        from great_tables import GT

        if isinstance(obj, GT):
            return "great_tables", obj
    if "holoviews" in roots:
        from holoviews.core.dimension import Dimensioned

        if isinstance(obj, Dimensioned):
            return "holoviews", obj
    raise TypeError(
        f"cannot export {type(obj).__module__}.{type(obj).__name__}; supported: "
        "matplotlib Figure/Axes, plotly Figure, altair Chart, bokeh model, "
        "great_tables GT, holoviews object"
    )


def _wrap_fragment(fragment):
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        + _RESET_CSS
        + "</head><body>"
        + fragment
        + "</body></html>"
    )


def _write_text(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _save_matplotlib(fig, stem, fmts, result, opts):
    for fmt in fmts:
        if fmt == "html":
            result["notes"].append("matplotlib: no html export; html skipped")
            continue
        path = f"{stem}.{fmt}"
        kwargs = {"dpi": PNG_DPI} if fmt == "png" else {}
        fig.savefig(path, format=fmt, **kwargs)
        result[fmt] = path


def _save_plotly(fig, stem, fmts, result, opts):
    if "html" in fmts:
        path = f"{stem}.html"
        fig.write_html(path, include_plotlyjs=True, full_html=True)
        result["html"] = path
    for fmt in ("png", "svg"):
        if fmt not in fmts:
            continue
        path = f"{stem}.{fmt}"
        try:
            fig.write_image(path, format=fmt, scale=opts["scale"])
            result[fmt] = path
            result["notes"].append(f"plotly {fmt} via kaleido")
            continue
        except Exception as exc:  # noqa: BLE001 - kaleido: no Chrome, crash, timeout
            reason = _short(exc)
            with contextlib.suppress(OSError):
                os.remove(path)
        if fmt == "svg":
            result["notes"].append(
                f"plotly svg skipped: kaleido failed ({reason}); "
                "no browser fallback for svg"
            )
            continue
        width = fig.layout.width or 700
        height = fig.layout.height or 500
        fragment = fig.to_html(
            include_plotlyjs=True,
            full_html=False,
            default_width=f"{width}px",
            default_height=f"{height}px",
        )
        result["png"] = render_html(
            _wrap_fragment(fragment),
            path,
            width=width,
            height=height,
            mode=opts["mode"],
            scale=opts["scale"],
            timeout_ms=opts["timeout_ms"],
        )
        result["notes"].append(
            f"plotly png via render_html fallback (kaleido failed: {reason})"
        )


def _save_altair(chart, stem, fmts, result, opts):
    for fmt in fmts:
        path = f"{stem}.{fmt}"
        if fmt == "png":
            chart.save(path, format="png", scale_factor=opts["scale"])
        elif fmt == "svg":
            chart.save(path, format="svg")
        else:
            chart.save(path, format="html", inline=True)
        result[fmt] = path
        result["notes"].append(f"altair {fmt} via vl-convert")


def _save_bokeh(model, stem, fmts, result, opts):
    from bokeh.embed import file_html
    from bokeh.resources import INLINE

    kwargs = {"theme": opts["bokeh_theme"]} if opts["bokeh_theme"] is not None else {}
    html = file_html(model, INLINE, title=os.path.basename(stem), **kwargs)
    if "html" in fmts:
        result["html"] = f"{stem}.html"
        _write_text(result["html"], html)
    if "png" in fmts:
        width = getattr(model, "width", None) or 800
        height = getattr(model, "height", None) or 600
        page = html.replace("<head>", "<head>" + _RESET_CSS, 1)
        result["png"] = render_html(
            page,
            f"{stem}.png",
            width=width,
            height=height,
            mode=opts["mode"],
            scale=opts["scale"],
            timeout_ms=opts["timeout_ms"],
        )
        result["notes"].append("bokeh png via render_html")
    if "svg" in fmts:
        result["notes"].append(
            "bokeh svg skipped: needs output_backend='svg' + "
            "selenium export_svgs; not supported"
        )


def _gt_png_page(html):
    """GT page for rasterizing: the container shrink-wraps the table so its padding
    (style.gt_style's margin) frames the screenshot instead of the viewport width."""
    if "</head>" in html:
        return html.replace("</head>", _GT_PNG_CSS + "</head>", 1)
    return _GT_PNG_CSS + html


def _save_great_tables(table, stem, fmts, result, opts):
    html = table.as_raw_html(make_page=True)
    if "html" in fmts:
        result["html"] = f"{stem}.html"
        _write_text(result["html"], html)
    if "png" in fmts:
        result["png"] = render_html(
            _gt_png_page(html),
            f"{stem}.png",
            width=800,
            height=600,
            mode=opts["mode"],
            scale=opts["scale"],
            timeout_ms=opts["timeout_ms"],
            selector=_GT_CONTAINER,
        )
        result["notes"].append("great_tables png via render_html (gtsave not used)")
    if "svg" in fmts:
        result["notes"].append("great_tables svg skipped: no svg export")


def _save_holoviews(obj, stem, fmts, result, opts):
    import holoviews

    errors = []
    for backend in ("bokeh", "matplotlib"):
        try:
            rendered = holoviews.render(obj, backend=backend)
        except Exception as exc:  # noqa: BLE001 - backend import failures vary
            errors.append(f"{backend}: {_short(exc)}")
            continue
        kind, target = _classify(rendered)
        result["notes"].append(f"holoviews rendered via {backend} backend to {kind}")
        _HANDLERS[kind](target, stem, fmts, result, opts)
        return
    raise ExportError(
        "holoviews could not render with any plotting backend ("
        + "; ".join(errors)
        + ")"
    )


_HANDLERS = {
    "matplotlib": _save_matplotlib,
    "plotly": _save_plotly,
    "altair": _save_altair,
    "bokeh": _save_bokeh,
    "great_tables": _save_great_tables,
    "holoviews": _save_holoviews,
}


def save(
    fig,
    path,
    formats=FORMATS,
    *,
    mode="light",
    scale=FIGURE_SCALE,
    bokeh_theme=None,
    timeout_ms=DEFAULT_TIMEOUT_MS,
):
    """Export a chart object; return {"png", "svg", "html": abs path|None, "notes": [str]}.

    `path` is a stem or a file name whose .png/.svg/.html extension is dropped;
    outputs are <stem>.<fmt> in that directory. `mode` is the color scheme for
    browser-rasterized PNGs; `bokeh_theme` is passed to bokeh's file_html.
    """
    fmts = _formats(formats)
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    kind, target = _classify(fig)
    stem = _stem(path)
    result = {"png": None, "svg": None, "html": None, "notes": []}
    opts = {
        "mode": mode,
        "scale": scale,
        "bokeh_theme": bokeh_theme,
        "timeout_ms": timeout_ms,
    }
    _HANDLERS[kind](target, stem, fmts, result, opts)
    return result


# ---------------------------------------------------------------- CLI


def main(argv=None):
    with contextlib.suppress(AttributeError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        prog="export.py",
        description="Rasterize HTML with the shared Playwright chromium.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    render = sub.add_parser("render", help="render an HTML file to PNG")
    render.add_argument("html", help="HTML file path")
    render.add_argument("png", help="output PNG path")
    render.add_argument("--mode", choices=MODES, default="light")
    render.add_argument("--width", type=int, default=1200)
    render.add_argument("--height", type=int, default=800)
    render.add_argument("--scale", type=float, default=1)
    render.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    render.add_argument("--selector", default=None, help="screenshot this element only")
    args = parser.parse_args(argv)
    try:
        path = render_html(
            pathlib.Path(args.html),
            args.png,
            width=args.width,
            height=args.height,
            mode=args.mode,
            scale=args.scale,
            timeout_ms=args.timeout_ms,
            selector=args.selector,
        )
    except (ExportError, OSError, ValueError) as exc:
        sys.stderr.write(f"export.py: {_short(exc)}\n")
        return 2
    finally:
        close_browser()
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
