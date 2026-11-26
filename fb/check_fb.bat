@echo off
rem Check Facebook login state (ASCII only - do not add Chinese here)
title Facebook Login Check
cd /d "%~dp0.."

rem Python: env var SOCIAL_AUTO_PY > project .venv > python on PATH
set "PY=%SOCIAL_AUTO_PY%"
if not defined PY if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"
if not defined PY set "PY=python"

echo Checking Facebook login state on port 9223...
echo.
"%PY%" main.py check --platform facebook
echo.
echo If it says "not logged in", run fb\start_fb.bat and log in first.
pause
