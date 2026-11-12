@echo off
rem Double-click to start Grab and open it in your browser.
rem Close this window (or press Ctrl+C) to stop the server.
title Grab - media downloader
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo First run: setting up Python environment...
    python -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :error
)

rem Open the browser a few seconds after the server starts.
start "" /b cmd /c "ping -n 4 127.0.0.1 >nul & start "" http://127.0.0.1:8000"
".venv\Scripts\python.exe" -m app
goto :eof

:error
echo.
echo Setup failed. Make sure Python 3.11+ is installed and on PATH.
pause
