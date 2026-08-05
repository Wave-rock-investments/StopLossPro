@echo off
cd /d "%~dp0"
echo.
echo ============================================================
echo  Deploying stoploss-gist-proxy to Cloudflare Workers
echo ============================================================
echo.
npx --yes wrangler@3 deploy
echo.
echo Done. Press any key to close...
pause >nul
