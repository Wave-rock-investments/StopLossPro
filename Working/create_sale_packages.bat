@echo off
title StopLoss — Create Sale Packages
cd /d "%~dp0"
setlocal

echo.
echo ============================================================
echo   StopLoss — Building Sale ZIPs
echo ============================================================
echo.

:: ── Output folder ───────────────────────────────────────────
set OUT=%~dp0sale_packages
if not exist "%OUT%" mkdir "%OUT%"

:: ── Check binaries exist ────────────────────────────────────
for %%F in (P1\dist\StopLoss_MT4.exe P1\dist\StopLoss_MT5.exe P1\dist\StopLoss_MT4.run P1\dist\StopLoss_MT5.run P2\dist\StopLoss_MT4.exe P2\dist\StopLoss_MT5.exe P2\dist\StopLoss_MT4.run P2\dist\StopLoss_MT5.run) do (
    if not exist "%%F" (
        echo [ERROR] Missing: %%F
        pause & exit /b 1
    )
)
echo [OK] All 8 binaries found.

:: ── README — P1 ─────────────────────────────────────────────
set R1=%TEMP%\README_P1.txt
(
echo StopLoss Calculator — P1 Edition
echo =================================
echo Lifetime license. Single active session per machine.
echo.
echo WINDOWS : Double-click Windows\StopLoss_MT4.exe  ^(MetaTrader 4^)
echo           Double-click Windows\StopLoss_MT5.exe  ^(MetaTrader 5^)
echo LINUX   : chmod +x Linux/StopLoss_MT4.run ^&^& ./Linux/StopLoss_MT4.run
echo.
echo HOW TO ACTIVATE:
echo   1. Launch the app — it shows your Machine ID ^(16-char code^)
echo   2. Click "Request Access" to submit your ID to the seller
echo   3. Seller approves you — app unlocks immediately, no restart needed
echo   4. Single session only: running on two machines simultaneously is blocked
echo.
echo Support: trishulraj2024@gmail.com
) > "%R1%"

:: ── README — P2 ─────────────────────────────────────────────
set R2=%TEMP%\README_P2.txt
(
echo StopLoss Calculator — P2 Edition
echo =================================
echo Lifetime license. $250 USDT TRC20. Self-activates after payment.
echo.
echo WINDOWS : Double-click Windows\StopLoss_MT4.exe  ^(MetaTrader 4^)
echo           Double-click Windows\StopLoss_MT5.exe  ^(MetaTrader 5^)
echo LINUX   : chmod +x Linux/StopLoss_MT4.run ^&^& ./Linux/StopLoss_MT4.run
echo.
echo HOW TO ACTIVATE:
echo   1. Visit the checkout page, send exactly $250 USDT on TRC20 ^(Tron^)
echo      Wallet: TSPy3m6cY4VdqXyAbtfu8Ei5tT5PmQ5K1S
echo   2. Wait ~1-3 min for TX to confirm, copy your Transaction Hash
echo   3. Launch the app, paste the TX hash, click "Verify and Activate"
echo   4. App verifies on-chain and unlocks instantly — no manual steps
echo   5. Single session only: running on two machines simultaneously is blocked
echo.
echo Support: trishulraj2024@gmail.com
) > "%R2%"

:: ── Temp staging folders ────────────────────────────────────
set T1=%TEMP%\sl_p1_stage
set T2=%TEMP%\sl_p2_stage
if exist "%T1%" rd /s /q "%T1%"
if exist "%T2%" rd /s /q "%T2%"
mkdir "%T1%\Windows" "%T1%\Linux"
mkdir "%T2%\Windows" "%T2%\Linux"

:: ── Stage P1 ────────────────────────────────────────────────
copy /y "P1\dist\StopLoss_MT4.exe"  "%T1%\Windows\" >nul
copy /y "P1\dist\StopLoss_MT5.exe"  "%T1%\Windows\" >nul
copy /y "P1\dist\StopLoss_MT4.run"  "%T1%\Linux\"   >nul
copy /y "P1\dist\StopLoss_MT5.run"  "%T1%\Linux\"   >nul
copy /y "%R1%"                        "%T1%\README.txt" >nul
echo [OK] P1 staged.

:: ── Stage P2 ────────────────────────────────────────────────
copy /y "P2\dist\StopLoss_MT4.exe"  "%T2%\Windows\" >nul
copy /y "P2\dist\StopLoss_MT5.exe"  "%T2%\Windows\" >nul
copy /y "P2\dist\StopLoss_MT4.run"  "%T2%\Linux\"   >nul
copy /y "P2\dist\StopLoss_MT5.run"  "%T2%\Linux\"   >nul
copy /y "%R2%"                        "%T2%\README.txt" >nul
echo [OK] P2 staged.

:: ── ZIP using PowerShell ────────────────────────────────────
set Z1=%OUT%\StopLoss-P1.zip
set Z2=%OUT%\StopLoss-P2.zip

if exist "%Z1%" del /f /q "%Z1%"
if exist "%Z2%" del /f /q "%Z2%"

echo.
echo [1/2] Compressing P1...
powershell -NoProfile -Command "Compress-Archive -Path '%T1%\*' -DestinationPath '%Z1%' -CompressionLevel Optimal"
if errorlevel 1 ( echo [ERROR] P1 zip failed. & pause & exit /b 1 )

echo [2/2] Compressing P2...
powershell -NoProfile -Command "Compress-Archive -Path '%T2%\*' -DestinationPath '%Z2%' -CompressionLevel Optimal"
if errorlevel 1 ( echo [ERROR] P2 zip failed. & pause & exit /b 1 )

:: ── Cleanup staging ─────────────────────────────────────────
rd /s /q "%T1%" 2>nul
rd /s /q "%T2%" 2>nul

:: ── Done ────────────────────────────────────────────────────
echo.
echo ============================================================
echo   DONE — Sale packages ready in:
echo   %OUT%
echo.
for %%F in ("%Z1%" "%Z2%") do (
    echo   %%~nxF
)
echo.
echo   macOS builds: download from GitHub Actions when ready
echo   https://github.com/Wave-rock-investments/stoploss-app/actions
echo ============================================================
echo.
pause
