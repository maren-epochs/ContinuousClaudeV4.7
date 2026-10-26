@echo off
rem Double-click to install (or update) the Continuous Claude v4.7 harness for Claude Code.
rem Installs git with winget if missing, clones the repo to %USERPROFILE%\epoch-harness
rem (GitHub may open a browser sign-in once), then runs install\quick_install.ps1 -Yes.
title Install Continuous Claude harness
echo Installing the Continuous Claude v4.7 harness for Claude Code...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $d=Join-Path $env:USERPROFILE 'epoch-harness'; if (-not (Get-Command git -ErrorAction SilentlyContinue)) { winget install -e --id Git.Git --accept-source-agreements --accept-package-agreements; $env:Path=[Environment]::GetEnvironmentVariable('Path','Machine')+';'+[Environment]::GetEnvironmentVariable('Path','User') }; if (Test-Path (Join-Path $d '.git')) { git -C $d pull --ff-only } else { git clone https://github.com/maren-epochs/epoch-harness-share-10-26 $d }; if ($LASTEXITCODE -ne 0) { throw 'Download failed: accept the GitHub invite and sign in, then run this again.' }; powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $d 'install\quick_install.ps1') -Yes; exit $LASTEXITCODE"
if errorlevel 1 (
  echo.
  echo Install did not finish. Read the message above, fix it, then double-click this file again.
) else (
  echo.
  echo Installed. Close every Claude Code window, start Claude Code again, then type /bootup
)
echo.
pause
