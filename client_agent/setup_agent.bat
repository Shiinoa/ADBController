@echo off
chcp 65001 >nul 2>&1
title Scrcpy Client Agent - Setup
echo ============================================
echo   Scrcpy Client Agent - One-Click Setup
echo ============================================
echo.

:: Get the directory where this batch file is located
set "SCRIPT_DIR=%~dp0"

:: Check if client_agent.py exists next to this batch file
if not exist "%SCRIPT_DIR%client_agent.py" (
    echo ERROR: client_agent.py not found!
    echo.
    echo Please EXTRACT the zip file first before running this script.
    echo Right-click the zip file and select "Extract All..."
    echo Then run setup_agent.bat from the extracted folder.
    echo.
    pause
    exit /b 1
)

:: Check Python - try 'py' first (Windows launcher), then 'python'
set PYTHON_CMD=
py --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py"
    goto :found_python
)
python --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    goto :found_python
)

echo ERROR: Python is not installed or not in PATH.
echo Please install Python from https://www.python.org/downloads/
echo Make sure to check "Add Python to PATH" during installation.
echo.
pause
exit /b 1

:found_python
echo Found Python: %PYTHON_CMD%
echo.

echo [1/3] Installing dependencies...
%PYTHON_CMD% -m pip install fastapi uvicorn --quiet --disable-pip-version-check
if errorlevel 1 (
    echo.
    echo ERROR: Failed to install dependencies.
    echo Try running: %PYTHON_CMD% -m pip install fastapi uvicorn
    pause
    exit /b 1
)
echo       Done.
echo.

:: Ask for server URL
echo The server URL is the address of the ADB Control Center server.
echo Example: http://192.168.1.100:8000
echo Leave empty if scrcpy is already in the SCRCPY subfolder.
echo.
set /p SERVER_URL="Server URL: "
if "%SERVER_URL%"=="" (
    echo.
    echo No server URL provided. Scrcpy will be loaded from local SCRCPY folder.
    set "SERVER_FLAG="
) else (
    echo.
    echo Server: %SERVER_URL%
    set "SERVER_FLAG=--server %SERVER_URL%"
)

echo.
echo [2/3] Checking SCRCPY files...
if exist "%SCRIPT_DIR%SCRCPY\scrcpy.exe" (
    echo       Found scrcpy.exe in SCRCPY subfolder.
) else (
    if "%SERVER_URL%"=="" (
        echo WARNING: scrcpy.exe not found in SCRCPY subfolder.
        echo          Agent will start but cannot launch scrcpy until installed.
    ) else (
        echo       Will auto-download from server on startup.
    )
)
echo.

echo [3/3] Starting Scrcpy Client Agent on port 18080...
echo.
echo ============================================
echo   Agent is running! Keep this window open.
echo   Press CTRL+C to stop.
echo ============================================
echo.

%PYTHON_CMD% "%SCRIPT_DIR%client_agent.py" --port 18080 %SERVER_FLAG%

echo.
echo Agent stopped.
pause
