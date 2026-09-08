@echo off
REM Why can't my phone reach the dashboard?
REM
REM Four different failures look identical from a phone -- a tab that spins --
REM so each one is asked separately and named. Start the dashboard first with
REM Dashboard.bat, leave that window open, then double-click this.
REM
REM No logic lives here. Batch asks the question; Python answers it, because
REM anything that must be right on Windows and cannot be run on the machine it
REM was written on belongs on the testable side of that line.

cd /d "%~dp0"

set "APP=.venv\Scripts\outreach.exe"
if not exist "%APP%" set "APP=python -m outreach"

"%APP%" phone-check

echo.
pause
