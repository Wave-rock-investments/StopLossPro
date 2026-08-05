@echo off
title StopLoss — Linux ELF Builds (via WSL)
echo.
echo ============================================================
echo   StopLoss Linux Build Launcher
echo   Runs P1 and P2 Linux builds inside WSL Ubuntu 22.04
echo ============================================================
echo.
echo [*] Checking WSL is available...
wsl --status >nul 2>&1
if errorlevel 1 (
    echo [ERROR] WSL not found. Install Ubuntu 22.04 from the Microsoft Store.
    pause & exit /b 1
)

echo [1/2] Running P1 Linux build in WSL...
wsl bash "/mnt/c/Users/trish/OneDrive/Desktop/StoplossApk-mt5/P1/build_linux.sh"
if errorlevel 1 (
    echo [ERROR] P1 Linux build failed.
    pause & exit /b 1
)
echo.
echo [P1 Done] dist/StopLoss_MT4.run + dist/StopLoss_MT5.run
echo.

echo [2/2] Running P2 Linux build in WSL...
wsl bash "/mnt/c/Users/trish/OneDrive/Desktop/StoplossApk-mt5/P2/build_linux.sh"
if errorlevel 1 (
    echo [ERROR] P2 Linux build failed.
    pause & exit /b 1
)
echo.
echo [P2 Done] dist/StopLoss_MT4.run + dist/StopLoss_MT5.run
echo.

echo ============================================================
echo   ALL LINUX BUILDS COMPLETE
echo   P1: P1\dist\StopLoss_MT4.run  P1\dist\StopLoss_MT5.run
echo   P2: P2\dist\StopLoss_MT4.run  P2\dist\StopLoss_MT5.run
echo ============================================================
echo.
pause
