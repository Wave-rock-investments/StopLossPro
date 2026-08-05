@echo off
title StopLoss — Network Verification
echo.
echo ============================================================
echo  StopLoss Network Verification
echo ============================================================
echo.

:: Find Python
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found in PATH
    pause & exit /b 1
)

python "%~dp0net_verify.py"

echo.
echo Press any key to close.
pause
