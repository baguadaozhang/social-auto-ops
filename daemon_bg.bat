@echo off
rem Background daemon supervisor for LinkedIn automation.
rem Keep this file ASCII-only and CRLF: cmd.exe needs CRLF line endings.
rem Encoding is left to the console default (GBK on Chinese Windows), which renders the
rem Chinese output of main.py correctly. Do not force chcp here.
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Python venv not found: %PY%
    echo Fix: python -m venv .venv  then  .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
:start
echo.
echo ==== LinkedIn daemon (supervised) ====
echo Started: %DATE% %TIME%
echo Close this window to stop it.
"%PY%" main.py daemon
echo.
echo Daemon exited at %DATE% %TIME%. Restarting in 30s, Ctrl+C to stop.
timeout /t 30 /nobreak >nul
goto start
