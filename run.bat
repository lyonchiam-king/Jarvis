@echo off
REM ============================================================
REM  SME Outreach - double-click launcher for Windows
REM  No commands to remember. Pick a number, press Enter.
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
title SME Outreach

REM --- First run: build the environment if it isn't there yet ---
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   First time here - setting things up. This takes a minute or two.
    echo.
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo   ERROR: Python was not found.
        echo   Install Python 3.12 from python.org and tick "Add Python to PATH".
        echo.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    ".venv\Scripts\python.exe" -m pip install --quiet -e ".[dev]"
    echo   Setup done.
    timeout /t 2 >nul
)

set "OUTREACH=.venv\Scripts\outreach.exe"

:menu
cls
echo.
echo   ============================================
echo      SME OUTREACH
echo   ============================================
echo.
echo      GATHER
echo        1.  Find new leads
echo        2.  Research the leads I have
echo.
echo      PREPARE
echo        3.  Write website prompts
echo        4.  Draft my outreach messages
echo.
echo      ALL OF IT
echo        5.  Run the full pipeline  ^(1 then 2 then 3 then 4^)
echo.
echo      LOOK BEFORE YOU SPEND
echo        6.  Preview new leads      ^(costs a search, writes nothing^)
echo        7.  Preview my messages    ^(writes nothing, drafts nothing^)
echo.
echo      HOUSEKEEPING
echo        8.  Check everything works
echo        9.  Open my Sheet
echo        0.  Quit
echo.
set /p "choice=   Type a number and press Enter: "

if "%choice%"=="1" goto discovery
if "%choice%"=="2" goto research
if "%choice%"=="3" goto sitebrief
if "%choice%"=="4" goto draft
if "%choice%"=="5" goto fullrun
if "%choice%"=="6" goto previewleads
if "%choice%"=="7" goto previewmsgs
if "%choice%"=="8" goto preflight
if "%choice%"=="9" goto opensheet
if "%choice%"=="0" exit /b 0
goto menu

:discovery
cls
echo   Finding new leads...
echo.
%OUTREACH% run discovery
goto done

:research
cls
echo   Researching leads. This one costs money - about 6 cents per lead.
echo.
%OUTREACH% run research
goto done

:sitebrief
cls
echo   Writing website prompts...
echo.
%OUTREACH% run site-brief
goto done

:draft
cls
echo   Drafting messages. Nothing gets sent - drafts land in your Gmail.
echo.
%OUTREACH% run outreach-draft
goto done

:fullrun
cls
echo   Running the full pipeline. Sit back - this takes a few minutes.
echo.
%OUTREACH% run discovery
echo.
%OUTREACH% run research
echo.
%OUTREACH% run site-brief
echo.
%OUTREACH% run outreach-draft
goto done

:previewleads
cls
echo   Previewing - nothing will be written to your Sheet.
echo.
%OUTREACH% run discovery --dry-run
goto done

:previewmsgs
cls
echo   Previewing - no drafts created, nothing written.
echo.
%OUTREACH% run outreach-draft --dry-run
goto done

:preflight
cls
%OUTREACH% preflight
goto done

:opensheet
for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    if "%%a"=="SHEET_ID" (
        if not "%%b"=="" start "" "https://docs.google.com/spreadsheets/d/%%b/edit"
    )
)
goto menu

:done
echo.
echo   ------------------------------------------------
echo   Done. Press any key to go back to the menu.
pause >nul
goto menu
