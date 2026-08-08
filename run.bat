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
    ".venv\Scripts\python.exe" -m pip install --quiet -e .
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
echo        3.  Write the design briefs
echo        4.  Build the sites I approved
echo        5.  Draft my outreach messages
echo.
echo      ALL OF IT
echo        6.  Run this morning's batch  ^(1, 2, 3, then 5^)
echo.
echo      LOOK BEFORE YOU SPEND
echo        7.  Preview new leads      ^(costs a search, writes nothing^)
echo        8.  Preview my messages    ^(writes nothing, drafts nothing^)
echo.
echo      HOUSEKEEPING
echo        9.  Check everything works
echo        S.  Open my Sheet
echo        D.  Open the dashboard  ^(easier - approving happens there^)
echo        0.  Quit
echo.
set /p "choice=   Type a number and press Enter: "

if "%choice%"=="1" goto discovery
if "%choice%"=="2" goto research
if "%choice%"=="3" goto sitebrief
if "%choice%"=="4" goto sitebuild
if "%choice%"=="5" goto draft
if "%choice%"=="6" goto fullrun
if "%choice%"=="7" goto previewleads
if "%choice%"=="8" goto previewmsgs
if "%choice%"=="9" goto preflight
if /i "%choice%"=="S" goto opensheet
if /i "%choice%"=="D" goto dashboard
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
echo   Writing the design briefs...
echo.
%OUTREACH% run site-brief
goto done

:sitebuild
cls
echo   Building the sites you marked Approved. This spends Lovable credits,
echo   and it asks before each one.
echo.
%OUTREACH% run site-build
goto done

:dashboard
cls
echo   Starting the dashboard. Close that window to come back here.
echo.
%OUTREACH% dashboard
goto menu

:draft
cls
echo   Drafting messages. Nothing gets sent - drafts land in your Gmail.
echo.
%OUTREACH% run outreach-draft
goto done

:fullrun
cls
echo   Running this morning's batch. Sit back - this takes a few minutes.
echo   It skips every business it has already seen.
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
