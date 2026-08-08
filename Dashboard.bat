@echo off
REM ============================================================
REM  SME Outreach - Dashboard
REM  Right-click this file, "Send to > Desktop (create shortcut)"
REM  and you have a desktop icon that opens your dashboard.
REM ============================================================
cd /d "%~dp0"
title SME Outreach Dashboard

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   First time here - setting things up. This takes a minute or two.
    echo.
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo   ERROR: Python was not found.
        echo   Install Python 3.12 from python.org, tick "Add Python to PATH",
        echo   then double-click this file again.
        echo.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    ".venv\Scripts\python.exe" -m pip install --quiet -e ".[dev]"
)

REM Keep dependencies current after a git pull. Quiet and quick if nothing changed.
".venv\Scripts\python.exe" -m pip install --quiet -e ".[dev]" >nul 2>&1

echo.
echo   Starting your dashboard...
echo   A browser tab will open in a moment.
echo.
echo   Leave this window open while you work.
echo   Close it when you're done.
echo.

".venv\Scripts\outreach.exe" dashboard

echo.
echo   Dashboard stopped.
pause
