@echo off
setlocal
cd /d "%~dp0"

echo.
echo ============================================================
echo   LinkedIn Content Pipeline
echo ============================================================
echo.

REM ── Setup virtual environment on first run ───────────────────
set PYTHON=.venv\Scripts\python.exe

if not exist "%PYTHON%" (
    echo First run: setting up virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Could not create virtual environment.
        echo Make sure Python 3.9+ is installed and on your PATH.
        pause
        exit /b 1
    )
    echo Installing dependencies...
    .venv\Scripts\pip install -q -r requirements.txt
    if errorlevel 1 (
        echo ERROR: pip install failed. Check your internet connection.
        pause
        exit /b 1
    )
    echo Setup complete.
    echo.
)

REM ── Stage 1: Fetch ───────────────────────────────────────────
echo [1/3] Fetching and ranking articles...
"%PYTHON%" 01_fetch.py
if errorlevel 1 goto error
echo.

REM ── Stage 2: Review (interactive) ───────────────────────────
echo [2/3] Review...
echo   A ranked list of articles will appear.
echo   Enter numbers to open articles. d=draft  s=skip  h=hold  q=quit
echo.
"%PYTHON%" 03_review.py
if errorlevel 1 goto error
echo.

REM ── Stage 3: Build prompt files ─────────────────────────────
echo [3/3] Building draft prompt files...
"%PYTHON%" 04_draft.py
if errorlevel 1 goto error

echo.
echo ============================================================
echo   Done. Prompt files are in: pipeline\data\drafts\
echo.
echo   Next steps:
echo     1. Open each _prompt.txt, paste into Claude.ai, save
echo        the response into the matching _draft.md file.
echo     2. Schedule your chosen draft in LinkedIn.
echo     3. Run 05_log_post.py to record the publish date + URL.
echo     4. Run 06_log_metrics.py after capturing screenshot metrics.
echo ============================================================
echo.
pause
exit /b 0

:error
echo.
echo ============================================================
echo   Pipeline stopped. Fix the error above and re-run.
echo ============================================================
echo.
pause
exit /b 1
