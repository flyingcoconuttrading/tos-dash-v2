@echo off
:: ============================================================
:: repo_update.bat — tos-dash-v2 quick commit + push
:: Update the COMMIT_MSG block below before each run.
:: Run from any directory — script resolves its own path.
:: ============================================================

cd /d "%~dp0"

:: ── Commit message (update this each session) ───────────────
set COMMIT_MSG=Add repo_update.bat — single-script commit + push helper

:: ────────────────────────────────────────────────────────────

echo.
echo =========================================================
echo  tos-dash-v2  repo_update
echo =========================================================
echo.

git status
echo.

git add -A
if errorlevel 1 (
    echo [ERROR] git add failed
    pause
    exit /b 1
)

git commit -m "%COMMIT_MSG%"
if errorlevel 1 (
    echo [ERROR] git commit failed - nothing to commit?
    pause
    exit /b 1
)

git push origin main
if errorlevel 1 (
    echo [ERROR] git push failed
    pause
    exit /b 1
)

echo.
echo =========================================================
echo  Done. Check output above for any errors.
echo =========================================================
echo.
pause
