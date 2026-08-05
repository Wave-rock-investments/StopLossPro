@echo off
title StopLoss Deploy Clean
echo Running clean deploy script...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy_clean.ps1"
echo.
pause
