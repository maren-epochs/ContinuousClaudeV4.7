@echo off
rem Double-click to add the "epoch harness" desktop shortcut (for an existing harness install).
rem Updates %USERPROFILE%\epoch-harness from GitHub, then runs install\desktop_launcher.ps1.
title Create epoch harness shortcut
echo Creating the epoch harness desktop shortcut...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $d=Join-Path $env:USERPROFILE 'epoch-harness'; if (-not (Test-Path (Join-Path $d '.git'))) { throw 'Harness not found in your user folder: run Install-Epoch-Harness.cmd first.' }; git -C $d pull --ff-only; if ($LASTEXITCODE -ne 0) { throw 'Could not update the harness from GitHub: sign in and run this again.' }; powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $d 'install\desktop_launcher.ps1'); exit $LASTEXITCODE"
if errorlevel 1 (
  echo.
  echo Shortcut not created. Read the message above, fix it, then double-click this file again.
) else (
  echo.
  echo Done. Double-click "epoch harness" on your desktop to pick projects to open.
)
echo.
pause
