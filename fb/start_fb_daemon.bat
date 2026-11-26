@echo off
rem Facebook scheduler daemon (ASCII only - do not add Chinese here)
title Facebook Automation
cd /d "%~dp0.."

echo ==========================================
echo   Facebook Automation
echo ==========================================
echo.
echo 1) First open fb\start_fb.bat (Chrome on port 9223) and stay logged in.
echo 2) Keep this window open. Closing it stops the automation.
echo    Press Ctrl+C to stop.
echo.

.venv\Scripts\python.exe main.py daemon --platform facebook
echo.
echo Daemon exited.
pause
