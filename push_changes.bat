@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM Go to repository root (directory of this script)
cd /d "%~dp0"

REM Commit message (all args or default with date/time)
set MSG=%*
if "%MSG%"=="" set MSG=chore: update %DATE% %TIME%

echo [INFO] Staging changes...
git add -A

git diff --cached --quiet
if %errorlevel%==0 (
  echo [INFO] No changes to commit.
  goto :end
)

echo [INFO] Committing: %MSG%
git commit -m "%MSG%"

echo [INFO] Pushing...
git push

:end
echo [OK] Done.
pause
