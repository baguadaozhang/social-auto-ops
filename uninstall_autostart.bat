@echo off
rem Remove autostart entry (ASCII only - do not add Chinese here)
title Remove Autostart
setlocal

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"

echo Removing autostart entry...
if exist "%STARTUP%\LinkedIn-Auto.bat" (
    del /q "%STARTUP%\LinkedIn-Auto.bat"
    echo   [OK] Removed: %STARTUP%\LinkedIn-Auto.bat
) else (
    echo   [INFO] Autostart entry not found.
)

echo.
echo Done. Note:
echo   - The Chrome window that is already running will NOT close.
echo     Please close it manually.
echo   - To stop the background daemon, end python.exe in Task Manager.
echo.
pause
