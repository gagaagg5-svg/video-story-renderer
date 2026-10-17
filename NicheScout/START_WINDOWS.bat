@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 scout.py
    if errorlevel 1 pause
    exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
    python scout.py
    if errorlevel 1 pause
    exit /b
)
echo Python 3.10+ is required. Install from https://www.python.org/downloads/windows/
echo Include Tcl/Tk and IDLE during installation.
pause
