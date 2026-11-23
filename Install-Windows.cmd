@echo off
rem Windows: double-click to install (or update) the Continuous Claude v4.7 harness for
rem Claude Code from this unzipped folder (no git or GitHub account needed).
rem Runs install\quick_install.ps1 -Yes: Python 3.13 and Node via winget if missing,
rem packages, sync into %USERPROFILE%\.claude, hooks, profile, desktop shortcut.
title Install Continuous Claude harness
cd /d "%~dp0"
echo Installing the Continuous Claude v4.7 harness for Claude Code...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -Recurse -File | Unblock-File"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install\quick_install.ps1" -Yes
set CODE=%ERRORLEVEL%
echo.
pause
exit /b %CODE%
