@echo off
rem One-click start: Chrome + daemon (ASCII only - do not add Chinese here)
title LinkedIn Automation
cd /d "%~dp0"

rem Python: env var SOCIAL_AUTO_PY > project .venv > python on PATH
set "PY=%SOCIAL_AUTO_PY%"
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY set "PY=python"

echo ==========================================
echo   LinkedIn Automation
echo ==========================================
echo.
echo [1/2] Starting Chrome...
call "%~dp0start_chrome.bat"

echo [2/2] Starting scheduler daemon...
echo       Keep this window open. Closing it stops the automation.
echo       Press Ctrl+C to stop.
echo.
"%PY%" main.py daemon
echo.
echo Daemon exited.
pause
