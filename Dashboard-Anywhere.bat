@echo off
REM The dashboard, on a public https address, reachable from any phone
REM anywhere -- no wifi, no firewall rule, no IP address to find.
REM
REM This PUBLISHES the dashboard to the internet. Your password is the only
REM thing in front of a tool that spends money and messages real businesses,
REM so Python refuses to open the tunnel unless that password is a long one.
REM
REM No logic lives here. Batch asks for the feature; Python decides whether
REM it is allowed, because anything that must be right on Windows and cannot
REM be run where it was written belongs on the testable side of that line.

cd /d "%~dp0"

set "APP=.venv\Scripts\outreach.exe"
if not exist "%APP%" set "APP=python -m outreach"

REM Same relay handling as Dashboard.bat: kill any older one first, because
REM WhatsApp allows one connection per linked device and a second relay takes
REM the connection from the first rather than sitting beside it.
taskkill /fi "WINDOWTITLE eq WhatsApp Relay" /t /f >nul 2>&1

"%APP%" dashboard --tunnel
set "EXITCODE=%ERRORLEVEL%"

taskkill /fi "WINDOWTITLE eq WhatsApp Relay" /t /f >nul 2>&1

echo.
if not "%EXITCODE%"=="0" echo   The dashboard stopped with an error ^(code %EXITCODE%^).
echo.
pause
