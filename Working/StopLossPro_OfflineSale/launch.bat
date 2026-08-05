@echo off
title StopLoss Pro
cls
echo.
echo  ===========================================
echo       StopLoss Pro  --  Launcher
echo  ===========================================
echo.

REM ── Check Python is available ─────────────────────────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  ERROR: Python not found.
    echo  Download Python 3.10+ from https://www.python.org/downloads/
    echo  Make sure to tick "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

REM ── Auto-install / upgrade requirements ──────────────────────────────────
echo  [1/2] Checking requirements...
python -m pip install -r "%~dp0requirements.txt" --quiet --no-warn-script-location
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: pip install failed.
    echo  Check your internet connection and try again.
    echo.
    pause
    exit /b 1
)

REM ── Launch app ────────────────────────────────────────────────────────────
echo  [2/2] Launching StopLoss Pro...
echo.
cd /d "%~dp0"
python "Product Sell.py"

REM Keep window open if app exits with an error
if %errorlevel% neq 0 (
    echo.
    echo  App exited with error code %errorlevel%.
    pause
)
