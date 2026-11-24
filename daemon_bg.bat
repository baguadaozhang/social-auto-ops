@echo off
rem Background daemon for LinkedIn automation (ASCII only - do not add Chinese here)
rem Supervisor loop: if the daemon crashes, restart it after ~30s instead of dying silently.
cd /d "%~dp0"
rem Python: env var SOCIAL_AUTO_PY > project .venv > python on PATH
set "PY=%SOCIAL_AUTO_PY%"
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY set "PY=python"
if not exist "data\logs" mkdir "data\logs"
:loop
echo. >> "data\logs\daemon.log"
echo ===== start %DATE% %TIME% ===== >> "data\logs\daemon.log"
"%PY%" main.py daemon >> "data\logs\daemon.log" 2>&1
echo ===== exit %DATE% %TIME% - restarting in 30s ===== >> "data\logs\daemon.log"
rem use ping instead of timeout: works even when PATH is polluted by Git/MSYS
ping -n 31 127.0.0.1 >nul
goto loop
