"""Screenshots of every screen, one PNG each, for Claude to Read.

Seeing the user's screen is a task Claude does itself, never one to hand over
(2026-11-19). Windows only: a short PowerShell script marks the process DPI aware
(so the captures are full resolution), copies each screen's bounds with
Graphics.CopyFromScreen and prints one ``path|device|x,y,w,h|primary`` line per screen.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_PS = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type 'using System.Runtime.InteropServices; public static class FleetDpi { [DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); }'
[FleetDpi]::SetProcessDPIAware() | Out-Null
$out = $env:FLEET_SHOT_DIR
$i = 0
foreach ($sc in [System.Windows.Forms.Screen]::AllScreens) {
  $b = $sc.Bounds
  $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
  $p = Join-Path $out ("screen{0}.png" -f $i)
  $bmp.Save($p, [System.Drawing.Imaging.ImageFormat]::Png)
  $g.Dispose(); $bmp.Dispose()
  "{0}|{1}|{2},{3},{4},{5}|{6}" -f $p, $sc.DeviceName, $b.X, $b.Y, $b.Width, $b.Height, $sc.Primary
  $i++
}
"""


class ScreenshotError(Exception):
    """Capture failed (not Windows, PowerShell missing or the script errored)."""


@dataclass(frozen=True)
class Shot:
    """One captured screen: PNG path, device name, bounds, primary or not."""

    path: str
    device: str
    bounds: tuple[int, int, int, int]
    primary: bool


def default_dir(now: float | None = None) -> Path:
    """Temp folder fleet-screens/<timestamp> for one capture."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return Path(tempfile.gettempdir()) / "fleet-screens" / stamp


def parse(stdout: str) -> list[Shot]:
    """Shots from the script's output lines; other lines are skipped."""
    shots = []
    for line in stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 4:
            continue
        try:
            x, y, w, h = (int(v) for v in parts[2].split(","))
        except ValueError:
            continue
        shots.append(Shot(parts[0], parts[1], (x, y, w, h), parts[3].lower() == "true"))
    return shots


def capture(out_dir: Path | None = None) -> list[Shot]:
    """Capture every screen into out_dir (default a new temp folder)."""
    if os.name != "nt":
        raise ScreenshotError("screenshots need Windows")
    target = out_dir or default_dir()
    target.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "FLEET_SHOT_DIR": str(target)}
    try:
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScreenshotError(f"powershell failed: {exc}") from exc
    shots = parse(done.stdout)
    if done.returncode != 0 or not shots:
        tail = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["no output"]
        raise ScreenshotError(f"capture failed: {tail[0]}")
    return shots


def report(shots: list[Shot]) -> str:
    """One line per PNG, for the command output."""
    lines = [f"{len(shots)} screen(s) captured (Read each PNG to see it):"]
    for s in shots:
        x, y, w, h = s.bounds
        tag = " primary" if s.primary else ""
        lines.append(f"  {s.path}  {s.device} {w}x{h} at {x},{y}{tag}")
    return "\n".join(lines)
