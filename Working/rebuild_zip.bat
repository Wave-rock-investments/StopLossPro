@echo off
cd /d "C:\Users\trish\OneDrive\Desktop\StoplossApk-mt5\archive"
echo Rebuilding stoplosspro.zip...
if exist stoplosspro.zip del /q stoplosspro.zip
powershell -NoProfile -Command "Compress-Archive -Path 'stoplosspro' -DestinationPath 'stoplosspro.zip'"
echo.
if exist stoplosspro.zip (
    echo SUCCESS: stoplosspro.zip rebuilt.
) else (
    echo FAILED: stoplosspro.zip not found after compress.
)
pause
