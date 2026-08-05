@echo off
title Encrypt StopLossPro Debug Logs
echo Installing 'cryptography' (one-time)...
python -m pip install cryptography --quiet --disable-pip-version-check
if errorlevel 1 (
    echo [ERROR] pip install failed. Is Python on PATH?
    pause
    exit /b 1
)
echo.
python "%~dp0encrypt_logs.py"
