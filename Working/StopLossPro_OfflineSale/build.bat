@echo off
setlocal enabledelayedexpansion
title StopLossPro — Offline Sale EXE Builder

echo.
echo ============================================================
echo  StopLossPro — Single EXE Builder (offline-sale package)
echo  Builds: dist\StopLossPro.exe  (one unified app, online activation)
echo  kivy 2.3.1+ / kivymd 1.2.0+ / PyInstaller / system Python
echo ============================================================
echo.

:: ── Locate Python on PATH ──────────────────────────────────────
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found on PATH.
    echo Download Python 3.10+ from https://www.python.org/downloads/
    echo Make sure to tick "Add Python to PATH" during install.
    pause & exit /b 1
)

echo [*] Using Python:
python --version
echo.

cd /d "%~dp0"

:: ── Install app requirements + PyInstaller ─────────────────────
echo [1/4] Installing requirements (kivy, kivymd, MetaTrader5, PyInstaller)...
python -m pip install -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 ( echo [ERROR] requirements install failed & pause & exit /b 1 )

python -m pip install pyinstaller --quiet --disable-pip-version-check
if errorlevel 1 ( echo [ERROR] pyinstaller install failed & pause & exit /b 1 )

:: ── Clean previous build artifacts ──────────────────────────────
echo [2/4] Cleaning previous build...
if exist "dist\StopLossPro.exe" del /f "dist\StopLossPro.exe"
if exist "StopLossPro.spec" del /f "StopLossPro.spec"
if exist build rmdir /s /q build

:: ── Build ─────────────────────────────────────────────────────
echo [3/4] Building StopLossPro.exe ...
python -m PyInstaller --onefile --windowed ^
  --name "StopLossPro" ^
  --icon "app_icon.ico" ^
  --version-file "version_info.txt" ^
  --add-data "app_icon.ico;." ^
  --add-data "lib\layout.kv;lib" ^
  --paths "lib" ^
  --collect-data kivy ^
  --collect-data kivymd ^
  --collect-submodules kivymd ^
  --hidden-import kivymd.icon_definitions ^
  --hidden-import kivymd.icon_definitions.md_icons ^
  --hidden-import kivy ^
  --hidden-import kivy.core.window ^
  --hidden-import kivy.core.image ^
  --hidden-import kivy.core.text ^
  --hidden-import kivy.core.clipboard ^
  --hidden-import kivy.core.gl ^
  --hidden-import kivy.graphics ^
  --hidden-import kivy.graphics.texture ^
  --hidden-import kivy.graphics.context ^
  --hidden-import kivy.uix.label ^
  --hidden-import kivy.uix.button ^
  --hidden-import kivy.uix.textinput ^
  --hidden-import kivy.uix.boxlayout ^
  --hidden-import kivy.uix.popup ^
  --hidden-import kivy.uix.scrollview ^
  --hidden-import kivy.uix.widget ^
  --hidden-import kivy.uix.spinner ^
  --hidden-import kivy.uix.slider ^
  --hidden-import kivy.uix.switch ^
  --hidden-import kivymd.app ^
  --hidden-import kivymd.uix.boxlayout ^
  --hidden-import kivymd.uix.button ^
  --hidden-import kivymd.uix.label ^
  --hidden-import kivymd.uix.textfield ^
  --hidden-import kivymd.uix.card ^
  --hidden-import kivymd.uix.dialog ^
  --hidden-import kivymd.uix.menu ^
  --hidden-import kivymd.uix.snackbar ^
  --hidden-import MetaTrader5 ^
  --hidden-import numpy ^
  --collect-submodules numpy ^
  --exclude-module matplotlib ^
  --exclude-module pandas ^
  --noconfirm ^
  "Product Sell.py"

if errorlevel 1 ( echo [ERROR] Build failed & pause & exit /b 1 )

echo [4/4] Done!
echo.
echo ============================================================
echo  BUILD COMPLETE
echo  Output: dist\StopLossPro.exe
echo ============================================================
echo.
if exist "dist\StopLossPro.exe" (
    for %%A in ("dist\StopLossPro.exe") do echo  Size: %%~zA bytes
)
echo.
pause
