@echo off
title StopLoss — Push to GitHub
cd /d "%~dp0"

echo.
echo ============================================================
echo   StopLoss — Initialise git and push to GitHub
echo   Repo: https://github.com/Wave-rock-investments/stoploss-app
echo ============================================================
echo.

:: Check if git is available
git --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] git not found. Install Git for Windows from https://git-scm.com
    pause & exit /b 1
)

:: Init repo if not already
if not exist ".git" (
    echo [1] Initialising git repo...
    git init -b main
) else (
    echo [1] Git repo already initialised.
)

:: Write .gitignore
echo [2] Writing .gitignore...
(
echo # Python
echo __pycache__/
echo *.pyc
echo *.pyo
echo .venv/
echo .venv_linux/
echo # Build artefacts
echo build/
echo dist/
echo *.spec
echo # OS
echo .DS_Store
echo Thumbs.db
echo # IDE
echo .idea/
echo .vscode/
echo # Local only
echo session_setup_result.txt
) > .gitignore

:: Stage only source + workflow (skip large venvs and dist)
echo [3] Staging files...
git add .gitignore
git add .github/
git add P1/stoploss_mt4.py P1/stoploss_mt5.py P1/app_icon.ico
git add P1/build.bat P1/build_linux.sh
git add P2/stoploss_mt4.py P2/stoploss_mt5.py P2/app_icon.ico
git add P2/build.bat P2/build_linux.sh
git add run_linux_builds.bat
git add add_session_file.py add_session_file.bat push_to_github.bat

:: Commit
echo [4] Committing...
git -c user.email="build@stoploss.app" -c user.name="StopLoss Build" commit -m "Initial commit: P1+P2 source + cross-platform build system"

:: Set remote — NO credentials in the URL.
:: SECURITY 2026-08-05: an embedded PAT was removed from this line (leaked, revoked
:: — see docs/CREDENTIAL_INCIDENT.md). Authenticate with Git Credential Manager or
:: the GitHub CLI instead; both store the credential in the OS keychain, not on disk.
::   gh auth login                (recommended)
::   git config --global credential.helper manager
echo [5] Setting remote...
git remote remove origin 2>nul
git remote add origin https://github.com/Wave-rock-investments/stoploss-app.git

:: Push
echo [6] Pushing to GitHub...
git push -u origin main

if errorlevel 1 (
    echo.
    echo [ERROR] Push failed. Check the output above.
    pause & exit /b 1
)

echo.
echo ============================================================
echo   DONE — Code pushed to GitHub
echo   Repo: https://github.com/Wave-rock-investments/stoploss-app
echo.
echo   Next: Go to GitHub Actions tab and run "macOS App Build"
echo   URL:  https://github.com/Wave-rock-investments/stoploss-app/actions
echo ============================================================
echo.
pause
