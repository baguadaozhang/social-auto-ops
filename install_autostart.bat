@echo off
rem Install autostart entry (ASCII only - do not add Chinese here)
title Install Autostart
setlocal
cd /d "%~dp0"

set "DIR=%~dp0"
set "DIR=%DIR:~0,-1%"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"

rem Python: env var SOCIAL_AUTO_PY > project .venv > python on PATH
set "PY=%SOCIAL_AUTO_PY%"
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY set "PY=python"

echo ==========================================
echo   Install autostart
echo ==========================================
echo.
echo Project : %DIR%
echo Chrome  : %CHROME%
echo Python  : %PY%
echo.

if not exist "%DIR%\daemon_bg.bat" (
    echo [ERROR] daemon_bg.bat not found. Keep this file in the project folder.
    pause
    exit /b 1
)

> "%STARTUP%\LinkedIn-Auto.bat" echo @echo off
>>"%STARTUP%\LinkedIn-Auto.bat" echo cd /d "%DIR%"
>>"%STARTUP%\LinkedIn-Auto.bat" echo start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%DIR%\profiles\linkedin" --no-first-run --no-default-browser-check --disable-blink-features=AutomationControlled "https://www.linkedin.com/feed/"
>>"%STARTUP%\LinkedIn-Auto.bat" echo ping -n 9 127.0.0.1 ^>nul
>>"%STARTUP%\LinkedIn-Auto.bat" echo start "" /min "%DIR%\daemon_bg.bat"

if exist "%STARTUP%\LinkedIn-Auto.bat" (
    echo [OK] Written to: %STARTUP%\LinkedIn-Auto.bat
    echo.
    echo Next login will automatically:
    echo   1. open the dedicated Chrome on port 9222
    echo   2. run the scheduler daemon minimized
    echo      log: data\logs\daemon.log
    echo.
    echo To remove it later: run uninstall_autostart.bat
) else (
    echo [ERROR] Failed to write to Startup folder.
)
echo.
pause
