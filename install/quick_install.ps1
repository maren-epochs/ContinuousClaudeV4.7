<#
.SYNOPSIS
  One-step install of Continuous Claude v4.7 into ~/.claude (Windows).

.DESCRIPTION
  Run from an unzipped download or a clone of this repo:
    powershell -ExecutionPolicy Bypass -File install\quick_install.ps1

  1. Checks Python 3.13 (py launcher) and Node.js 18+; offers to install a missing one
     with winget. Warns when Claude Code (`claude`) is not on PATH.
  2. Installs the Python packages (install/setup_deps.py) and Playwright Chromium
     (the /visualize renderer). -SkipDeps skips both.
  3. Copies skills, agents, hooks, scripts and tools into ~/.claude
     (install/sync_global.py --apply; files it replaces are backed up first).
  4. Registers every hook and the status line in ~/.claude/settings.json
     (install/register_hooks.py --all; backup first, never duplicates, keeps the rest).
  Then restart Claude Code and run /bootup.

  Safe to re-run: every step skips what is already in place.
  Works in Windows PowerShell 5.1 and PowerShell 7.
#>
[CmdletBinding()]
param(
    [switch]$SkipDeps,   # skip pip packages and Playwright Chromium
    [switch]$Yes         # answer yes to the winget install prompts
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

function Step([string]$Text) { Write-Host "`n== $Text" -ForegroundColor Cyan }
function Have([string]$Cmd) { [bool](Get-Command $Cmd -ErrorAction SilentlyContinue) }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
        [Environment]::GetEnvironmentVariable('Path', 'User')
}
function Ask([string]$Question) {
    if ($Yes) { return $true }
    return (Read-Host "$Question [Y/n]") -notmatch '^\s*[nN]'
}
function Install-WithWinget([string]$Id, [string]$What) {
    if (-not (Have winget)) { throw "$What is missing and winget is not available. Install $What, then run this again." }
    if (-not (Ask "$What is missing. Install it now with winget ($Id)?")) { throw "$What is required." }
    winget install -e --id $Id --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) { throw "winget could not install $Id (exit $LASTEXITCODE)." }
    Refresh-Path
}
function Run-Py {
    param([Parameter(ValueFromRemainingArguments)][string[]]$PyArgs)
    Write-Host "> py -3.13 $($PyArgs -join ' ')"
    & py -3.13 @PyArgs
    if ($LASTEXITCODE -ne 0) { throw "py -3.13 $($PyArgs -join ' ') failed (exit $LASTEXITCODE)." }
}
function Test-Python313 {
    if (-not (Have py)) { return $false }
    & py -3.13 -c "import sys" 2>$null
    return $LASTEXITCODE -eq 0
}

Step 'Python 3.13'
if (-not (Test-Python313)) { Install-WithWinget 'Python.Python.3.13' 'Python 3.13' }
if (-not (Test-Python313)) { throw 'Python 3.13 is still not reachable as `py -3.13`; open a new terminal and run this again.' }
Write-Host (& py -3.13 --version)

Step 'Node.js 18+ (runs the hooks)'
if (-not (Have node)) { Install-WithWinget 'OpenJS.NodeJS.LTS' 'Node.js' }
$nodeVersion = (& node --version) -replace '^v', ''
if ([int]($nodeVersion.Split('.')[0]) -lt 18) { throw "Node $nodeVersion found; the hooks need Node 18 or newer." }
Write-Host "node $nodeVersion"

Step 'Claude Code'
if (Have claude) { Write-Host (& claude --version) }
else { Write-Warning 'Claude Code (`claude`) is not on PATH. Install it (https://claude.com/claude-code) before using the harness; the install below still works.' }

if ($SkipDeps) {
    Step 'Python packages: skipped (-SkipDeps)'
} else {
    Step 'Python packages'
    Run-Py install/setup_deps.py
    Step 'Playwright Chromium (chart rendering for /visualize)'
    Run-Py -m playwright install chromium
}

Step 'Install into ~/.claude'
Run-Py install/sync_global.py --apply

Step 'Register hooks and status line in ~/.claude/settings.json'
Run-Py install/register_hooks.py --all

Step 'Done'
Write-Host @'
Restart Claude Code (close every session), then run /bootup in any project.
Optional extras (see README.md "Dependencies"): tldr, ouros and bloks on PATH;
EXA_API_KEY / NIA_API_KEY in a .env for web and docs search.
'@ -ForegroundColor Green
