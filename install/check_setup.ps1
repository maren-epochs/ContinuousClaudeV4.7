<#
.SYNOPSIS
  Check this PC for everything the harness and its recommended plugins need. Read-only.

.DESCRIPTION
  Lists each program, setting and plugin as OK, MISSING or OPTIONAL, then prints the
  exact commands to add what is missing. Changes nothing. Run:
    powershell -NoProfile -ExecutionPolicy Bypass -File $HOME\epoch-harness\install\check_setup.ps1
  Works in Windows PowerShell 5.1 and PowerShell 7.
#>
[CmdletBinding()]
param()

$rows = New-Object System.Collections.Generic.List[object]
function Add-Row([string]$Area, [string]$Item, [string]$State, [string]$Detail, [string]$Fix) {
    $rows.Add([pscustomobject]@{ Area = $Area; Item = $Item; State = $State; Detail = $Detail; Fix = $Fix })
}
function Have([string]$Cmd) { [bool](Get-Command $Cmd -ErrorAction SilentlyContinue) }
function FirstLine([scriptblock]$Run) {
    try { $out = & $Run 2>$null; if ($LASTEXITCODE -eq 0 -and $out) { return ([string]($out | Select-Object -First 1)).Trim() } } catch {}
    return $null
}
function Major([string]$Version) { if ($Version -match '(\d+)\.') { return [int]$Matches[1] } return 0 }

$claudeDir = Join-Path $env:USERPROFILE '.claude'
$settings = $null
try { $settings = Get-Content -Raw (Join-Path $claudeDir 'settings.json') | ConvertFrom-Json } catch {}

# --- Windows and core programs ---
$os = [Environment]::OSVersion.Version
if ($os.Major -ge 10 -and $os.Build -ge 17763) { Add-Row 'System' 'Windows 10 1809+ / 11' 'OK' "build $($os.Build)" '' }
else { Add-Row 'System' 'Windows 10 1809+ / 11' 'MISSING' "build $($os.Build)" 'Update Windows (Settings > Windows Update)' }

$v = FirstLine { claude --version }
if ($v) { Add-Row 'Claude' 'Claude Code' 'OK' $v '' } else { Add-Row 'Claude' 'Claude Code' 'MISSING' '' 'irm https://claude.ai/install.ps1 | iex' }

$v = FirstLine { git --version }
if ($v) { Add-Row 'Tools' 'Git for Windows' 'OK' $v '' } else { Add-Row 'Tools' 'Git for Windows' 'MISSING' 'harness installer, Bash tool' 'winget install -e --id Git.Git' }

$v = FirstLine { gh --version }
if (-not $v) { Add-Row 'Tools' 'GitHub CLI (gh)' 'MISSING' 'commit-commands plugin' 'winget install -e --id GitHub.cli' }
else {
    & gh auth status *> $null
    if ($LASTEXITCODE -eq 0) { Add-Row 'Tools' 'GitHub CLI (gh)' 'OK' "$v, signed in" '' }
    else { Add-Row 'Tools' 'GitHub CLI (gh)' 'MISSING' "$v, not signed in" 'gh auth login' }
}

$v = FirstLine { py -3.13 --version }
if ($v) { Add-Row 'Tools' 'Python 3.13 (py launcher)' 'OK' $v '' } else { Add-Row 'Tools' 'Python 3.13 (py launcher)' 'MISSING' 'harness tools' 'winget install -e --id Python.Python.3.13' }
if ($v) {
    & py -3.13 -c "import pandas, plotly, matplotlib, playwright, aiohttp" *> $null
    if ($LASTEXITCODE -eq 0) { Add-Row 'Harness' 'Python packages' 'OK' 'pandas, plotly, matplotlib, playwright, aiohttp' '' }
    else { Add-Row 'Harness' 'Python packages' 'MISSING' 'harness data/chart/search tools' 'Double-click Install-Epoch-Harness.cmd again' }
}

$v = FirstLine { node --version }
if (-not $v) { Add-Row 'Tools' 'Node.js 20+' 'MISSING' 'hooks, npx plugins' 'winget install -e --id OpenJS.NodeJS.LTS' }
elseif ((Major ($v -replace '^v', '')) -lt 20) { Add-Row 'Tools' 'Node.js 20+' 'MISSING' "$v is too old for chrome-devtools-mcp" 'winget upgrade -e --id OpenJS.NodeJS.LTS' }
else { Add-Row 'Tools' 'Node.js 20+' 'OK' $v '' }
if (Have npx) { Add-Row 'Tools' 'npx' 'OK' 'comes with Node.js' '' } else { Add-Row 'Tools' 'npx' 'MISSING' 'playwright, chrome-devtools-mcp' 'winget install -e --id OpenJS.NodeJS.LTS' }

if (Have uvx) { Add-Row 'Tools' 'uv / uvx' 'OK' (FirstLine { uv --version }) '' } else { Add-Row 'Tools' 'uv / uvx' 'MISSING' 'browser-use plugin' 'winget install -e --id astral-sh.uv' }

if (Have pyright-langserver) { Add-Row 'Tools' 'Pyright language server' 'OK' '' '' } else { Add-Row 'Tools' 'Pyright language server' 'MISSING' 'pyright-lsp plugin' 'npm install -g pyright' }
if (Have typescript-language-server) { Add-Row 'Tools' 'TypeScript language server' 'OK' '' '' } else { Add-Row 'Tools' 'TypeScript language server' 'MISSING' 'typescript-lsp plugin' 'npm install -g typescript-language-server typescript' }

if (Have wt) { Add-Row 'Tools' 'Windows Terminal' 'OK' '' '' } else { Add-Row 'Tools' 'Windows Terminal' 'MISSING' '/fleet open, desktop shortcut' 'winget install -e --id Microsoft.WindowsTerminal' }
if (Have pwsh) { Add-Row 'Tools' 'PowerShell 7' 'OK' (FirstLine { pwsh -NoProfile -Command '$PSVersionTable.PSVersion.ToString()' }) '' } else { Add-Row 'Tools' 'PowerShell 7' 'OPTIONAL' 'nicer tabs; Windows PowerShell also works' 'winget install -e --id Microsoft.PowerShell' }

# --- Chrome (or Edge / Brave: Claude in Chrome works in Chromium browsers) ---
$browsers = @(
    @{ Name = 'Google Chrome'; Exe = 'chrome.exe'; Data = 'Google\Chrome\User Data'; Host = 'Google\Chrome' },
    @{ Name = 'Microsoft Edge'; Exe = 'msedge.exe'; Data = 'Microsoft\Edge\User Data'; Host = 'Microsoft\Edge' },
    @{ Name = 'Brave'; Exe = 'brave.exe'; Data = 'BraveSoftware\Brave-Browser\User Data'; Host = 'BraveSoftware\Brave-Browser' }
)
$extId = 'fcoeoabgfenejglbffodgkkbkcdhcgfn'
$found = @(); $withExt = @(); $connected = @()
foreach ($b in $browsers) {
    foreach ($hive in 'HKLM', 'HKCU') {
        try { if ((Get-ItemProperty -Path "${hive}:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\$($b.Exe)" -ErrorAction Stop).'(default)') { $found += $b.Name; break } } catch {}
    }
    $profiles = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA $b.Data) -Directory -ErrorAction SilentlyContinue
    if ($profiles | Where-Object { Test-Path (Join-Path $_.FullName "Extensions\$extId") }) { $withExt += $b.Name }
    if (Test-Path "HKCU:\Software\$($b.Host)\NativeMessagingHosts\com.anthropic.claude_code_browser_extension") { $connected += $b.Name }
}
$found = @($found | Select-Object -Unique)
if ($found) { Add-Row 'Chrome' 'Chrome / Edge / Brave' 'OK' ($found -join ', ') '' }
else { Add-Row 'Chrome' 'Chrome / Edge / Brave' 'MISSING' 'Claude in Chrome, chrome-devtools-mcp' 'winget install -e --id Google.Chrome' }
if ($withExt) { Add-Row 'Chrome' 'Claude in Chrome extension' 'OK' ($withExt -join ', ') '' }
else { Add-Row 'Chrome' 'Claude in Chrome extension' 'MISSING' '' "Install from https://chromewebstore.google.com/detail/claude/$extId" }
if ($connected) { Add-Row 'Chrome' 'Browser connected to Claude Code' 'OK' ($connected -join ', ') '' }
else { Add-Row 'Chrome' 'Browser connected to Claude Code' 'MISSING' '' 'In Claude Code run: /chrome' }

# --- Settings the plugins need ---
if ([Environment]::GetEnvironmentVariable('GITHUB_PERSONAL_ACCESS_TOKEN', 'User') -or $env:GITHUB_PERSONAL_ACCESS_TOKEN) { Add-Row 'Plugins' 'GITHUB_PERSONAL_ACCESS_TOKEN' 'OK' 'set (value not shown)' '' }
else { Add-Row 'Plugins' 'GITHUB_PERSONAL_ACCESS_TOKEN' 'MISSING' 'github plugin' "Create a token at https://github.com/settings/tokens, then: [Environment]::SetEnvironmentVariable('GITHUB_PERSONAL_ACCESS_TOKEN','<token>','User')" }

# --- Harness ---
if (Test-Path (Join-Path $claudeDir '.ccv47-manifest.json')) { Add-Row 'Harness' 'Harness installed' 'OK' '' '' } else { Add-Row 'Harness' 'Harness installed' 'MISSING' '' 'Double-click Install-Epoch-Harness.cmd' }
$settingsText = ''
try { $settingsText = Get-Content -Raw (Join-Path $claudeDir 'settings.json') } catch {}
$hooks = @('status.mjs', 'harness-guard.mjs', 'auto-handoff-stop.mjs', 'pre-compact.mjs') | Where-Object { $settingsText -notmatch [regex]::Escape($_) }
if (-not $hooks) { Add-Row 'Harness' 'Hooks and status line registered' 'OK' '' '' }
else { Add-Row 'Harness' 'Hooks and status line registered' 'MISSING' ("missing: " + ($hooks -join ', ')) 'Double-click Install-Epoch-Harness.cmd again' }
if (Test-Path (Join-Path ([Environment]::GetFolderPath('Desktop')) 'epoch harness.lnk')) { Add-Row 'Harness' 'Desktop shortcut' 'OK' '' '' }
else { Add-Row 'Harness' 'Desktop shortcut' 'MISSING' '' 'Double-click Create-Epoch-Shortcut.cmd' }
foreach ($t in 'tldr', 'ouros', 'bloks') {
    if (Have $t) { Add-Row 'Harness' "$t (optional)" 'OK' '' '' } else { Add-Row 'Harness' "$t (optional)" 'OPTIONAL' 'see README "Dependencies"' '' }
}

# --- Recommended plugins ---
$enabled = @()
if ($settings -and $settings.enabledPlugins) {
    $enabled = $settings.enabledPlugins.PSObject.Properties | Where-Object { $_.Value -eq $true } | ForEach-Object { $_.Name }
}
foreach ($p in 'github', 'commit-commands', 'context7', 'claude-md-management', 'chrome-devtools-mcp', 'browser-use', 'playwright', 'pyright-lsp', 'typescript-lsp') {
    if ($enabled | Where-Object { $_ -like "$p@*" }) { Add-Row 'Plugins' $p 'OK' 'installed' '' }
    else { Add-Row 'Plugins' $p 'MISSING' '' "In Claude Code run: /plugin install $p@claude-plugins-official" }
}

# --- Report ---
$rows | Format-Table Area, Item, State, Detail -AutoSize | Out-String -Width 160 | Write-Host
$missing = @($rows | Where-Object { $_.State -eq 'MISSING' })
if (-not $missing) { Write-Host 'Everything needed is installed.' -ForegroundColor Green; exit 0 }
Write-Host "$($missing.Count) item(s) to add. Run these, then open a new PowerShell window and run this check again:" -ForegroundColor Yellow
$missing | ForEach-Object { $_.Fix } | Select-Object -Unique | ForEach-Object { Write-Host "  $_" }
exit 0
