"""Answer a session's open question box by typing into its "Type something" option.

Windows only. The session's Windows Terminal tab is found by its title (``fleet.py open``
names tabs after the project folder) through UI Automation; the terminal text is read back
through its TextPattern. Every key goes through WScript.Shell SendKeys after checking the
tab's window is in the foreground, and after every step the screen must show what the step
should produce, else nothing more is typed. The previously selected tab and foreground
window are restored afterwards. Used by ``/fleet handoff-all`` for sessions stopped on a box.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

POINTER = "❯"  # ❯ marks the highlighted option in Claude Code's question box
TYPE_OPTION = "Type something."
BOX_FOOTER = "Enter to select"
TIMEOUT_S = 60
_OPTION = re.compile(r"^\s*(❯\s*)?(\d+)\.\s+(.*?)\s*$")
_SENDKEYS_SPECIAL = re.compile(r"([+^%~(){}\[\]])")

HANDOFF_ANSWER = (
    "Write a handoff now with /create-handoff (requested via /fleet handoff-all). "
    "Put this question and every other open question in the handoff as A-D options; "
    "do not ask them now. Then carry on."
)


class BoxError(Exception):
    """The box cannot be answered safely; nothing (more) was typed."""


@dataclass
class Plan:
    """Highlighted option number and the number of "Type something."."""

    current: int
    target: int

    @property
    def keys(self) -> str:
        """Arrow keys that move the pointer onto "Type something."."""
        step = "{DOWN}" if self.target >= self.current else "{UP}"
        return step * abs(self.target - self.current)


def plan(screen: str) -> Plan:
    """Where the pointer is and which number "Type something." has, from the screen text."""
    lines = [ln for ln in screen.splitlines() if ln.strip()]
    if not any(BOX_FOOTER in ln for ln in lines[-4:]):
        raise BoxError("no open question box on screen")
    current = target = None
    for ln in lines:
        m = _OPTION.match(ln)
        if not m:
            continue
        number = int(m.group(2))
        if m.group(1):
            current = number
        if m.group(3) == TYPE_OPTION:
            target = number
    if current is None or target is None:
        raise BoxError("question box has no highlighted option or no 'Type something.'")
    return Plan(current, target)


def sendkeys_literal(text: str) -> str:
    """text escaped for SendKeys (braces around + ^ % ~ ( ) { } [ ]); one line only."""
    if "\n" in text or "\r" in text:
        raise BoxError("answer must be one line")
    return _SENDKEYS_SPECIAL.sub(r"{\1}", text)


_PS = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type @'
using System; using System.Runtime.InteropServices;
public class FleetFG {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
}
'@
$A = [System.Windows.Automation.AutomationElement]
$S = [System.Windows.Automation.TreeScope]
$req = $env:FLEET_BOX_REQ | ConvertFrom-Json
# base64 of UTF-8 JSON: the console code page would mangle the box's non-ASCII glyphs
function Out($o) { 'FLEETBOX ' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($o | ConvertTo-Json -Compress -Depth 4))); exit 0 }
function Fail($m) { Out @{ ok = $false; error = $m } }
$cond = New-Object System.Windows.Automation.AndCondition(
  (New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, [System.Windows.Automation.ControlType]::TabItem)),
  (New-Object System.Windows.Automation.PropertyCondition($A::NameProperty, $req.tab)))
$tabs = $A::RootElement.FindAll($S::Descendants, $cond)
if ($tabs.Count -ne 1) { Fail "expected one Windows Terminal tab named '$($req.tab)', found $($tabs.Count)" }
$tab = $tabs[0]
$win = $tab
while ($win -and $win.Current.ClassName -ne 'CASCADIA_HOSTING_WINDOW_CLASS') {
  $win = [System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($win)
}
if (-not $win) { Fail 'tab has no Windows Terminal window' }
$hwnd = [IntPtr]$win.Current.NativeWindowHandle
$prevFg = [FleetFG]::GetForegroundWindow()
$prevTab = $null
foreach ($t in $win.FindAll($S::Descendants, (New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, [System.Windows.Automation.ControlType]::TabItem)))) {
  if ($t.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Current.IsSelected) { $prevTab = $t }
}
function Screen {
  $term = $win.FindFirst($S::Descendants, (New-Object System.Windows.Automation.PropertyCondition($A::ClassNameProperty, 'TermControl')))
  $term.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern).DocumentRange.GetText(-1)
}
function Restore {
  if ($prevTab -and -not [System.Windows.Automation.Automation]::Compare($prevTab, $tab)) {
    try { $prevTab.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select() } catch {}
  }
  if ($prevFg -ne $hwnd) { [FleetFG]::SetForegroundWindow($prevFg) | Out-Null }
}
$tab.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
Start-Sleep -Milliseconds 500
if ($req.mode -eq 'read') { $text = Screen; Restore; Out @{ ok = $true; screen = $text } }
$sh = New-Object -ComObject WScript.Shell
$done = 0
foreach ($step in $req.steps) {
  [FleetFG]::SetForegroundWindow($hwnd) | Out-Null
  Start-Sleep -Milliseconds 300
  if ([FleetFG]::GetForegroundWindow() -ne $hwnd) { Restore; Out @{ ok = $false; error = 'could not bring the tab window to the front'; done = $done } }
  $sh.SendKeys($step.keys)
  Start-Sleep -Milliseconds ([int]$step.wait_ms)
  $text = Screen
  if ($step.expect -and -not ($text -split "`n" | Where-Object { $_.Contains($step.expect) })) {
    Restore; Out @{ ok = $false; error = "screen did not show '$($step.expect)'"; done = $done; screen = $text }
  }
  $done++
}
Restore
Out @{ ok = $true; done = $done; screen = $text }
"""


def _run(req: dict[str, object]) -> dict[str, object]:
    if sys.platform != "win32":
        raise BoxError("answer-box needs Windows Terminal (Windows only)")
    env = {**os.environ, "FLEET_BOX_REQ": json.dumps(req)}
    try:
        done = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                base64.b64encode(_PS.encode("utf-16-le")).decode("ascii"),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_S,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BoxError(f"powershell failed: {exc}") from exc
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("FLEETBOX ")]
    if not lines:
        tail = (done.stderr or done.stdout).strip().splitlines()[-1:]
        raise BoxError(f"no result from powershell: {' '.join(tail)}")
    try:
        out = json.loads(base64.b64decode(lines[-1].split(" ", 1)[1]).decode("utf-8"))
    except ValueError as exc:
        raise BoxError(f"unreadable result from powershell: {exc}") from exc
    if not isinstance(out, dict):
        raise BoxError("unreadable result from powershell")
    return out


def _tail(screen: object, n: int = 8) -> str:
    lines = [ln.rstrip() for ln in str(screen or "").splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def answer(tab: str, text: str, dry_run: bool = False) -> str:
    """Type text into the 'Type something.' option of the box open in tab and submit it."""
    keys_text = sendkeys_literal(text)
    read = _run({"mode": "read", "tab": tab})
    if not read.get("ok"):
        raise BoxError(str(read.get("error")))
    p = plan(str(read.get("screen", "")))
    if dry_run:
        return (
            f"{tab}: box open, pointer on {p.current}, 'Type something.' is {p.target}; "
            f"would type: {text}"
        )
    steps = [
        {
            "keys": p.keys,
            "expect": f"{POINTER} {p.target}. {TYPE_OPTION}",
            "wait_ms": 300,
        },
        {
            "keys": keys_text,
            "expect": f"{POINTER} {p.target}. {text[:40]}",
            "wait_ms": 400,
        },
        {"keys": "{ENTER}", "expect": None, "wait_ms": 2000},
    ]
    res = _run({"mode": "answer", "tab": tab, "steps": steps})
    if not res.get("ok"):
        raise BoxError(
            f"{res.get('error')} after {res.get('done', 0)} of 3 steps\n{_tail(res.get('screen'))}"
        )
    return f"{tab}: answered '{text}'\n{_tail(res.get('screen'))}"
