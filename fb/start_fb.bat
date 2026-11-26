@echo off
rem Start the dedicated Chrome for Facebook (ASCII only - do not add Chinese here)
title Facebook Automation Chrome
setlocal

rem Proxy: Clash Verge mixed port (change if yours differs)
set "PROXY=http://127.0.0.1:7897"

set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

if not exist "%CHROME%" (
    echo [ERROR] Chrome not found.
    echo Please edit this file and set CHROME to your chrome.exe path.
    pause
    exit /b 1
)

set "PROFILE=%~dp0..\profiles\facebook"
if not exist "%PROFILE%" mkdir "%PROFILE%"

start "" "%CHROME%" --remote-debugging-port=9223 --remote-debugging-address=127.0.0.1 --proxy-server="%PROXY%" --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check --disable-blink-features=AutomationControlled "https://www.facebook.com/"

echo.
echo Chrome started (debug port 9223).
echo Proxy: %PROXY%
echo Profile: %PROFILE%
echo Log in to Facebook in the window, then keep it open.
echo You can close this black window.
echo.
rem use ping instead of timeout: works even when PATH is polluted by Git/MSYS
ping -n 6 127.0.0.1 >nul
endlocal
