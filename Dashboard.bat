@echo off
REM ============================================================
REM  SME Outreach - Dashboard
REM  Right-click this file, "Send to > Desktop (create shortcut)"
REM  and you have a desktop icon that opens your dashboard.
REM
REM  Everything this script does is safe to repeat. If it fails,
REM  it says why in plain English and stays open so you can read it.
REM ============================================================
cd /d "%~dp0"
title SME Outreach Dashboard

set "PY=.venv\Scripts\python.exe"
set "APP=.venv\Scripts\outreach.exe"

REM ---------------------------------------------------------- first run setup
REM Written as a subroutine rather than a parenthesised block on purpose:
REM inside a block, %VAR% is expanded when the block is *parsed*, so a
REM variable set in the same block reads back empty.
if not exist "%PY%" call :setup
if not exist "%PY%" exit /b 1
goto :installed

:setup
echo.
echo   First time here - setting things up. This takes a minute or two.
echo.

REM `py` is the Windows launcher and the reliable way to find a real Python.
REM Bare `python` may be the Microsoft Store stub, which opens the Store
REM instead of running anything.
set "BOOTSTRAP="
py -3 --version >nul 2>&1 && set "BOOTSTRAP=py -3"
if not defined BOOTSTRAP python --version >nul 2>&1 && set "BOOTSTRAP=python"

if not defined BOOTSTRAP (
    echo   ERROR: Python is not installed, or not on your PATH.
    echo.
    echo   Install Python from python.org, and on the first screen of the
    echo   installer TICK "Add Python to PATH". Then double-click this
    echo   file again.
    echo.
    pause
    goto :eof
)

%BOOTSTRAP% -m venv .venv
if not exist "%PY%" (
    echo.
    echo   ERROR: could not create the .venv folder.
    echo   Usually this means the folder is read-only, or antivirus blocked
    echo   it. Try moving this project somewhere under your Documents
    echo   folder and run it again.
    echo.
    pause
    goto :eof
)
"%PY%" -m pip install --quiet --upgrade pip
goto :eof

:installed

REM ------------------------------------------------- install / update the app
REM Reinstalling is quick when nothing changed, and it is what picks up new
REM code after a `git pull`.
echo   Checking for updates...
"%PY%" -m pip install --quiet -e . 2>installer-log.txt
if not exist "%APP%" (
    echo.
    echo   ERROR: the app did not install. The details are in:
    echo     %cd%\installer-log.txt
    echo.
    echo   The usual cause is no internet connection on first run.
    echo.
    pause
    exit /b 1
)
del installer-log.txt >nul 2>&1

REM ------------------------------------------------------- configuration check
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo.
        echo   Created a blank .env for you.
    )
    echo.
    echo   SETUP NEEDED: your keys are not filled in yet.
    echo.
    echo   Open .env in Notepad and fill in PLACES_API_KEY, ANTHROPIC_API_KEY
    echo   and SHEET_ID. README.md walks through where each one comes from.
    echo.
    echo   Opening it for you now...
    timeout /t 3 >nul
    start "" notepad ".env"
    echo   Save the file, then double-click this launcher again.
    echo.
    pause
    exit /b 1
)

echo.
echo   Starting your dashboard...
echo   A browser tab will open in a moment.
echo.
echo   ^>^> LEAVE THIS WINDOW OPEN while you work. ^<^<
echo      Closing it stops the dashboard.
echo.

"%APP%" dashboard
set "EXITCODE=%ERRORLEVEL%"

echo.
if not "%EXITCODE%"=="0" (
    echo   The dashboard stopped with an error ^(code %EXITCODE%^).
    echo.
    echo   If it says the address is already in use, the dashboard is
    echo   already running - look for another window like this one, or
    echo   just open  http://127.0.0.1:8765  in your browser.
) else (
    echo   Dashboard stopped.
)
echo.
pause
