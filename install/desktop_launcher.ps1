<#
.SYNOPSIS
  Create the "epoch harness" desktop shortcut (Windows).

.DESCRIPTION
  The shortcut opens Windows Terminal with an "epoch harness" profile that runs
  `fleet.py open --all`: it lists your Claude Code projects, asks which to open, and
  opens each as a tab resuming from its handoff. Writes, for the current user only:
    ~/.claude/launcher/epoch-harness.ps1 and epoch-harness.ico  (launch script, icon)
    %LOCALAPPDATA%\Microsoft\Windows Terminal\Fragments\EpochHarness\epoch-harness.json
    <Desktop>\epoch harness.lnk
  Safe to re-run (overwrites the same files). Called by install/quick_install.ps1 and by
  Create-Epoch-Shortcut.cmd. Works in Windows PowerShell 5.1 and PowerShell 7.
#>
[CmdletBinding()]
param(
    [string]$Desktop = [Environment]::GetFolderPath('Desktop')  # tests pass a temp folder
)

$ErrorActionPreference = 'Stop'
$launcher = Join-Path $env:USERPROFILE '.claude\launcher'
New-Item -ItemType Directory -Force -Path $launcher | Out-Null

$icon = Join-Path $launcher 'epoch-harness.ico'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'epoch-harness.ico') -Destination $icon -Force

$script = Join-Path $launcher 'epoch-harness.ps1'
@'
# epoch harness launcher: asks which projects to open, then opens them as
# tabs of one Windows Terminal window, each running /resume-handoff.
py -3.13 "$HOME\.claude\tools\fleet\fleet.py" open --all --profile "epoch harness"
if ($LASTEXITCODE -ne 0) {
    Write-Host "`nfleet open exited with code $LASTEXITCODE" -ForegroundColor Yellow
    Read-Host 'Press Enter to close'
}
'@ | Set-Content -LiteralPath $script -Encoding UTF8

$shell = if (Get-Command pwsh -ErrorAction SilentlyContinue) { 'pwsh.exe' } else { 'powershell.exe' }
$fragmentDir = Join-Path $env:LOCALAPPDATA 'Microsoft\Windows Terminal\Fragments\EpochHarness'
New-Item -ItemType Directory -Force -Path $fragmentDir | Out-Null
$fragment = @{
    profiles = @(@{
        name                     = 'epoch harness'
        commandline              = "$shell -NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$script`""
        icon                     = $icon
        startingDirectory        = $env:USERPROFILE
        tabTitle                 = 'epoch harness'
        suppressApplicationTitle = $true
    })
}
# UTF-8 without BOM (Windows PowerShell 5.1's -Encoding UTF8 adds one).
[IO.File]::WriteAllText((Join-Path $fragmentDir 'epoch-harness.json'), ($fragment | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding $false))

$wt = Get-Command wt.exe -ErrorAction SilentlyContinue
if (-not $wt) {
    Write-Warning 'Windows Terminal (wt.exe) not found: install it from the Microsoft Store, then run this again to add the desktop shortcut.'
    exit 0
}
$lnk = Join-Path $Desktop 'epoch harness.lnk'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = $wt.Source
$s.Arguments = '-w new -p "epoch harness"'
$s.WorkingDirectory = $env:USERPROFILE
$s.IconLocation = "$icon,0"
$s.Description = 'Open Claude Code projects (epoch harness)'
$s.Save()
Write-Host "Desktop shortcut created: $lnk"
