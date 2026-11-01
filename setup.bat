@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title Music Metadata Studio - Setup Wizard
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

echo ======================================================================
echo           Music Metadata Studio - Setup & Dependency Installer
echo ======================================================================
echo.

:: 1. Verify Python Installation
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not found in system PATH.
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo and ensure "Add Python to PATH" is checked during setup.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"') do set PY_VER=%%v
echo [+] Detected Python version: %PY_VER%

:: 2. Prepare Config Template
if not exist "%SCRIPT_DIR%credentials.json" (
    if exist "%SCRIPT_DIR%credentials.example.json" (
        copy /Y "%SCRIPT_DIR%credentials.example.json" "%SCRIPT_DIR%credentials.json" >nul
        echo [+] Created credentials.json from template.
    )
)

:: 3. Ensure data directory exists
if not exist "%SCRIPT_DIR%data" (
    mkdir "%SCRIPT_DIR%data"
    echo [+] Created data directory.
)

:: 4. Install / Update Requirements
echo [+] Installing runtime dependencies from requirements.txt...
python -m pip install --quiet --upgrade pip
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install requirements. Please check your internet connection.
    pause
    exit /b 1
)
echo [+] All dependencies successfully installed and verified.

echo.
echo ======================================================================
echo [SUCCESS] Music Metadata Studio is configured and ready.
echo ======================================================================
echo.

set /p LAUNCH_NOW="Launch Music Metadata Studio Web Dashboard now? [Y/n]: "
if /i "%LAUNCH_NOW%"=="" goto :launch
if /i "%LAUNCH_NOW%"=="y" goto :launch
if /i "%LAUNCH_NOW%"=="yes" goto :launch
exit /b 0

:launch
cd /d "%SCRIPT_DIR%"
call "%SCRIPT_DIR%run_ui.bat"

