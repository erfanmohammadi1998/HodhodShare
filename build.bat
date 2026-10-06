@echo off
setlocal
cd /d "%~dp0"
title HodhodShare - Build

rem Builds dist\HodhodShare.exe : a single file that runs on any Windows 10/11 PC without Python.

if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo Python was not found. Install Python 3.11+ first.
        pause
        exit /b 1
    )
)

echo Installing build dependencies...
venv\Scripts\python.exe -m pip install -q -r requirements.txt pyinstaller
if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
)

echo Building HodhodShare.exe...
venv\Scripts\python.exe -m PyInstaller --onefile --noconsole --clean --noconfirm --name HodhodShare --icon static\favicon.ico ^
    --add-data "templates;templates" --add-data "static;static" app.py
if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 1
)

echo.
echo Done: dist\HodhodShare.exe
pause
