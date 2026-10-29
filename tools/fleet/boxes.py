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
SUBMIT_PROMPT = "Ready to submit your answers?"
SUBMIT_OPTION = "Submit answers"
MAX_QUESTIONS = 4  # AskUserQuestion takes at most 4 questions per box
TIMEOUT_S = 60
_OPTION = re.compile(r"^\s*(❯\s*)?(\d+)\.\s+(.*?)\s*$")
_SENDKEYS_SPECIAL = re.compile(r"([+^%~(){}\[\]])")

HANDOFF_ANSWER = (
    "Write a handoff now with /create-handoff (requested via /fleet handoff-all). "
    "Put this question and every other open question in the handoff as A-D options; "
    "do not ask them now. Then carry on."
)

AWAY_ANSWER = (
    "The user is away (fleet away mode is on). Do not wait for an answer: ask this and any "
    "other question again with AskUserQuestion (it now goes to the fleet queue) or list them "
    "as A-D options in your handoff, then carry on with work that needs no decision."
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
    # Only the open box: from its last "☐" header (or the last 40 lines) to the footer, so a
    # numbered list in the scrollback cannot steer the keys.
    start = max((i for i, ln in enumerate(lines) if "☐" in ln), default=len(lines) - 40)
    current = target = None
    for ln in lines[max(start, 0) :]:
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
if ($tabs.Count -eq 0) { Fail "no Windows Terminal tab named '$($req.tab)'" }
if ($tabs.Count -gt 1) { Fail "$($tabs.Count) Windows Terminal tabs are named '$($req.tab)'; close or rename the extra one (right-click the tab, Rename Tab) and run again" }
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
  # Selecting a tab through UI Automation does not move keyboard focus into its terminal,
  # so keys went to the previously focused tab; focus the terminal control first.
  try { $win.FindFirst($S::Descendants, (New-Object System.Windows.Automation.PropertyCondition($A::ClassNameProperty, 'TermControl'))).SetFocus() } catch {}
  Start-Sleep -Milliseconds 150
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


def submit_ready(screen: str) -> bool:
    """True when a multi-question box shows its review screen with "Submit answers" highlighted."""
    lines = [ln.strip() for ln in screen.splitlines() if ln.strip()]
    tail = lines[-6:]
    return any(SUBMIT_PROMPT in ln for ln in tail) and any(
        ln.startswith(POINTER) and SUBMIT_OPTION in ln for ln in tail
    )


def answer(tab: str, text: str, dry_run: bool = False) -> str:
    """Type text into the 'Type something.' option of the box open in tab and submit it.

    A box holding several questions shows them one after another and ends on a review
    screen; each question gets the same text and the review screen is submitted.
    """
    keys_text = sendkeys_literal(text)
    read = _run({"mode": "read", "tab": tab})
    if not read.get("ok"):
        raise BoxError(str(read.get("error")))
    screen = str(read.get("screen", ""))
    if submit_ready(screen):
        if dry_run:
            return (
                f"{tab}: answered box waiting on its submit screen; would press Enter"
            )
        return f"{tab}: submitted\n{_tail(_submit(tab))}"
    p = plan(screen)
    if dry_run:
        return (
            f"{tab}: box open, pointer on {p.current}, 'Type something.' is {p.target}; "
            f"would type: {text}"
        )
    screen = _answer_one(tab, p, text, keys_text)
    for _ in range(MAX_QUESTIONS):
        if submit_ready(screen):
            screen = _submit(tab)
            break
        try:
            p = plan(screen)
        except BoxError:
            break
        screen = _answer_one(tab, p, text, keys_text)
    return f"{tab}: answered '{text}'\n{_tail(screen)}"


def _submit(tab: str) -> str:
    """Press Enter on the review screen ("Submit answers" highlighted); returns the screen."""
    step = {"keys": "{ENTER}", "expect": None, "wait_ms": 2000}
    res = _run({"mode": "answer", "tab": tab, "steps": [step]})
    if not res.get("ok"):
        raise BoxError(
            f"{res.get('error')} on the submit screen\n{_tail(res.get('screen'))}"
        )
    return str(res.get("screen", ""))


def _answer_one(tab: str, p: Plan, text: str, keys_text: str) -> str:
    """Move to "Type something.", type text and press Enter; returns the screen after."""
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
    return str(res.get("screen", ""))
