@echo off
setlocal
title Music Metadata Studio
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in your PATH.
    pause
    exit /b 1
)

:: Verify core dependencies exist before launching; otherwise launch setup.bat
python -c "import fastapi, uvicorn, mutagen, selenium, aiohttp, send2trash, pydantic" >nul 2>nul
if %errorlevel% neq 0 (
    echo [SETUP] Missing Python dependencies detected. Launching setup wizard...
    call "%SCRIPT_DIR%setup.bat"
    exit /b 0
)

where pythonw >nul 2>nul
if %errorlevel% equ 0 (
    set PY_CMD=pythonw
) else (
    set PY_CMD=python
)

echo [STUDIO] Launching Music Metadata Studio on http://127.0.0.1:8484...
start "" /b /d "%SCRIPT_DIR%" %PY_CMD% "%SCRIPT_DIR%ui_server.py" --open-browser
exit /b 0

