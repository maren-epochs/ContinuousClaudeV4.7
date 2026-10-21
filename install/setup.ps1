<#
.SYNOPSIS
  PowerShell twin of the repo Makefile, for Windows shells without make.

.DESCRIPTION
  Runs the same commands as `make setup|lint|format|typecheck|test|readiness`.
  Switches combine and run in that order; the script stops at the first failing step
  and exits with its exit code. `make sync` has no switch on purpose (it writes
  ~/.claude); run `py -3.13 install/sync_global.py --diff` / `--apply` by hand.
  Shell suites need Git Bash: set $env:BASH to its bash.exe if it is not found.
  -Setup keeps an existing .git/hooks/pre-commit whose interpreter still exists;
  add -ForceHook to repoint it at -Python (make: FORCE_HOOK=1).

.EXAMPLE
  pwsh install/setup.ps1 -Setup -Test
#>
#Requires -Version 7.0
[CmdletBinding()]
param(
    [switch]$Setup,
    [switch]$Test,
    [switch]$Lint,
    [switch]$Format,
    [switch]$Typecheck,
    [switch]$Readiness,
    [switch]$ForceHook,
    [string]$Python = 'py -3.13'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Py = @($Python -split '\s+' | Where-Object { $_ })

function Invoke-Py {
    param([Parameter(ValueFromRemainingArguments)][string[]]$Arguments)
    $exe = $Py[0]
    $pre = @($Py | Select-Object -Skip 1)
    Write-Host "> $Python $($Arguments -join ' ')"
    # Out-Host keeps tool output off the pipeline so the function returns only the code.
    & $exe @pre @Arguments | Out-Host
    return $LASTEXITCODE
}

function Find-Bash {
    if ($env:BASH -and (Test-Path -LiteralPath $env:BASH)) { return $env:BASH }
    if (-not $IsWindows) { return 'bash' }
    # Git for Windows: <root>\bin\bash.exe sets up the MSYS PATH. Walk up from git.exe.
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) {
        $dir = Split-Path -Parent $git.Source
        while ($dir) {
            $candidate = Join-Path $dir 'bin\bash.exe'
            if (Test-Path -LiteralPath $candidate) { return $candidate }
            $dir = Split-Path -Parent $dir
        }
    }
    # Any bash on PATH except the WSL launchers (System32 / WindowsApps).
    $any = Get-Command bash -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notmatch '\\(System32|WindowsApps)\\' } |
        Select-Object -First 1
    if ($any) { return $any.Source }
    throw 'Git Bash not found: install Git for Windows or set $env:BASH to bash.exe'
}

function Invoke-Bash {
    param([string]$Script)
    $bash = Find-Bash
    Write-Host "> bash $Script"
    & $bash $Script | Out-Host
    return $LASTEXITCODE
}

function Step-Setup {
    $rc = Invoke-Py install/setup_deps.py
    if ($rc -ne 0) { return $rc }
    & $Py[0] @($Py | Select-Object -Skip 1) -m playwright --version *> $null
    if ($LASTEXITCODE -eq 0) {
        $rc = Invoke-Py -m playwright install chromium
        if ($rc -ne 0) { return $rc }
    } else {
        Write-Host 'skip: playwright not installed'
    }
    # Guarded: keeps an existing hook whose interpreter still exists unless -ForceHook.
    if ($ForceHook) { return (Invoke-Py install/setup_deps.py --hook --force-hook) }
    return (Invoke-Py install/setup_deps.py --hook)
}

function Step-Test {
    $failed = @()
    Write-Host '== pytest'
    if ((Invoke-Py -m pytest -q) -ne 0) { $failed += 'pytest' }
    $suites = @(Get-ChildItem -Path '.claude/hooks' -Filter 'test_*.sh' | Sort-Object Name |
        ForEach-Object { ".claude/hooks/$($_.Name)" })
    if (Test-Path -LiteralPath 'scripts/test_readiness.sh') { $suites += 'scripts/test_readiness.sh' }
    foreach ($s in $suites) {
        Write-Host "== $s"
        if ((Invoke-Bash $s) -ne 0) { $failed += $s }
    }
    if ($failed.Count -gt 0) {
        Write-Host "FAILED: $($failed -join ' ')"
        return 1
    }
    Write-Host 'ALL SUITES PASSED'
    return 0
}

$steps = [ordered]@{
    Setup     = { Step-Setup }
    Lint      = { Invoke-Py -m ruff check . }
    Format    = { Invoke-Py -m ruff format . }
    Typecheck = { Invoke-Py -m mypy }
    Test      = { Step-Test }
    Readiness = { Invoke-Bash scripts/readiness.sh }
}
$selected = @($steps.Keys | Where-Object { $PSBoundParameters.ContainsKey($_) })
if ($selected.Count -eq 0) {
    Get-Help $PSCommandPath -Detailed
    exit 0
}

Push-Location $Root
try {
    foreach ($name in $selected) {
        # Steps return their exit code as the last pipeline value.
        $rc = & $steps[$name] | Select-Object -Last 1
        if ($rc -ne 0) {
            Write-Host "setup.ps1: step $name failed (exit $rc)"
            exit $rc
        }
    }
} finally {
    Pop-Location
}
exit 0
