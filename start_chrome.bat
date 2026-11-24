@echo off
rem Start the dedicated Chrome for automation (ASCII only - do not add Chinese here)
title LinkedIn Automation Chrome
setlocal

set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

if not exist "%CHROME%" (
    echo [ERROR] Chrome not found.
    echo Please edit this file and set CHROME to your chrome.exe path.
    pause
    exit /b 1
)

set "PROFILE=%~dp0profiles\linkedin"
if not exist "%PROFILE%" mkdir "%PROFILE%"

start "" "%CHROME%" ^
 --remote-debugging-port=9222 ^
 --remote-debugging-address=127.0.0.1 ^
 --user-data-dir="%PROFILE%" ^
 --no-first-run ^
 --no-default-browser-check ^
 --disable-blink-features=AutomationControlled ^
 "https://www.linkedin.com/feed/"

echo.
echo Chrome started (debug port 9222).
echo Profile: %PROFILE%
echo You can close this black window. Chrome keeps running.
echo.
rem use ping instead of timeout: works even when PATH is polluted by Git/MSYS
ping -n 6 127.0.0.1 >nul
endlocal
