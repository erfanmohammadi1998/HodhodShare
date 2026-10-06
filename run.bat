@echo off
setlocal
cd /d "%~dp0"
title HodhodShare

rem --- 1. Use virtual environment if it exists ---
set "PY="
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY set "PY=python"

"%PY%" --version >nul 2>&1
if errorlevel 1 (
    echo Python was not found. Install Python 3.11+ from https://www.python.org/downloads/
    echo and check "Add python.exe to PATH" during installation.
    pause
    exit /b 1
)
echo Using: %PY%

rem --- 2. Install dependencies if needed ---
"%PY%" -c "import flask, qrcode, PIL" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install dependencies.
        pause
        exit /b 1
    )
)

rem --- 3. Run ---
"%PY%" app.py
pause
