"""Which programs hold the most memory: RAM in use and reserved, grouped by program name.

RAM in use is each process's private working set (pages only it uses, in physical
memory); reserved is its private bytes (committed memory, much of which may sit in the
page file). Summed per program name (``brave``, ``claude``). Windows only, through the
raw performance counters (about 0.25 s); elsewhere or on failure: None.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from . import model

TOP_N = 5
PS_TIMEOUT_S = 20.0
GB = 1024**3
QUERY = (
    "Get-CimInstance -Query 'SELECT Name,WorkingSetPrivate,PrivateBytes"
    " FROM Win32_PerfRawData_PerfProc_Process' |"
    ' ForEach-Object { "$($_.Name)`t$($_.WorkingSetPrivate)`t$($_.PrivateBytes)" }'
)
SKIP = {"_total", "idle"}
_INSTANCE = re.compile(r"#\d+$")


def _powershell(script: str) -> str | None:
    """Run a PowerShell script without a window; its stdout, None on failure."""
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PS_TIMEOUT_S,
            check=False,
            creationflags=model.NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def parse(text: str, n: int = TOP_N) -> list[dict[str, Any]]:
    """``name<TAB>ram bytes<TAB>reserved bytes`` lines -> the top ``n`` programs by RAM.

    Instances ``brave#3`` count under ``brave``; ``_Total`` and ``Idle`` are dropped.
    Each entry: ``name``, ``ram_gb``, ``reserved_gb``, ``count`` (processes).
    """
    sums: dict[str, list[int]] = {}
    for line in text.splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 3 or not all(p.isdigit() for p in parts[1:]):
            continue
        name = _INSTANCE.sub("", parts[0])
        if not name or name.lower() in SKIP:
            continue
        entry = sums.setdefault(name, [0, 0, 0])
        entry[0] += int(parts[1])
        entry[1] += int(parts[2])
        entry[2] += 1
    ranked = sorted(sums.items(), key=lambda kv: (-kv[1][0], kv[0].lower()))
    return [
        {
            "name": name,
            "ram_gb": round(ram / GB, 2),
            "reserved_gb": round(reserved / GB, 2),
            "count": count,
        }
        for name, (ram, reserved, count) in ranked[:n]
    ]


def top_programs(
    n: int = TOP_N, run: Callable[[str], str | None] = _powershell
) -> list[dict[str, Any]] | None:
    """The ``n`` programs using the most RAM; None off Windows or when the query fails."""
    if sys.platform != "win32":
        return None
    out = run(QUERY)
    if out is None:
        return None
    return parse(out, n) or None


def describe(top: list[dict[str, Any]] | None, n: int = 3) -> str:
    """``GGnet 1.25 GB (3.35 reserved), claude x2 0.57 GB (1.35 reserved)``; '' if none."""
    parts = []
    for entry in (top or [])[:n]:
        count = entry.get("count", 1)
        label = f"{entry.get('name')}" + (f" x{count}" if count and count > 1 else "")
        parts.append(
            f"{label} {entry.get('ram_gb', 0):.2f} GB"
            f" ({entry.get('reserved_gb', 0):.2f} reserved)"
        )
    return ", ".join(parts)
