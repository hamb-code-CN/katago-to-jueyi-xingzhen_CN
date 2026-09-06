@echo off
rem =============================================
rem  Go AI - one-click launcher (portable)
rem  Creates venv + installs deps on first run,
rem  then starts the dashboard server (:8123).
rem =============================================
chcp 65001 >nul
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [setup] Creating Python venv ...
    py -3.12 -m venv venv 2>nul
    if errorlevel 1 py -3.11 -m venv venv 2>nul
    if errorlevel 1 py -3.10 -m venv venv 2>nul
    if errorlevel 1 python -m venv venv 2>nul
    if errorlevel 1 (
        echo [ERROR] Python not found. Install Python 3.10-3.12 and tick "Add Python to PATH".
        pause
        exit /b 1
    )
    echo [setup] Installing dependencies (first run only, needs internet) ...
    venv\Scripts\python.exe -m pip install --upgrade pip >nul 2>&1
    venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] pip install failed. Check internet connection.
        pause
        exit /b 1
    )
    echo [setup] Done.
)

echo [start] Opening dashboard: http://127.0.0.1:8123/analysis.html
start "" http://127.0.0.1:8123/analysis.html
venv\Scripts\python.exe go_ai\analysis_watch.py
pause
