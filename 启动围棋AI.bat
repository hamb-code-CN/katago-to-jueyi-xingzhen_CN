@echo off
title GoAI - Weiqi AI Dashboard
chcp 65001 >nul
cd /d "%~dp0"

if exist "venv\Scripts\python.exe" goto :run

echo [setup] Creating Python venv ...
py -3.12 -m venv venv
if not errorlevel 1 goto :deps
py -3.11 -m venv venv
if not errorlevel 1 goto :deps
py -3.10 -m venv venv
if not errorlevel 1 goto :deps
python -m venv venv
if not errorlevel 1 goto :deps
echo [ERROR] Python not found. Install Python 3.10-3.12 and tick "Add Python to PATH".
pause
exit /b 1

:deps
echo [setup] Installing dependencies (first run only, needs internet) ...
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install failed. Check internet connection.
    pause
    exit /b 1
)
echo [setup] Done.

:run
echo [start] Opening dashboard: http://127.0.0.1:8123/analysis.html
start "" http://127.0.0.1:8123/analysis.html
venv\Scripts\python.exe go_ai\analysis_watch.py
pause
