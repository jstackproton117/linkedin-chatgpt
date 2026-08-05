@echo off
title REBEL INTEL — LinkedIn Content Pipeline

echo.
echo   ██████╗ ███████╗██████╗ ███████╗██╗         ██╗███╗   ██╗████████╗███████╗██╗
echo   ██╔══██╗██╔════╝██╔══██╗██╔════╝██║         ██║████╗  ██║╚══██╔══╝██╔════╝██║
echo   ██████╔╝█████╗  ██████╔╝█████╗  ██║         ██║██╔██╗ ██║   ██║   █████╗  ██║
echo   ██╔══██╗██╔══╝  ██╔══██╗██╔══╝  ██║         ██║██║╚██╗██║   ██║   ██╔══╝  ██║
echo   ██║  ██║███████╗██████╔╝███████╗███████╗    ██║██║ ╚████║   ██║   ███████╗███████╗
echo   ╚═╝  ╚═╝╚══════╝╚═════╝ ╚══════╝╚══════╝    ╚═╝╚═╝  ╚═══╝   ╚═╝   ╚══════╝╚══════╝
echo.
echo   May the content be with you.
echo.

cd /d "%~dp0"

:: Use venv if it exists, otherwise fall back to system python
if exist ".venv\Scripts\python.exe" (
    set PYTHON=.venv\Scripts\python.exe
) else if exist ".venv\Scripts\python3.exe" (
    set PYTHON=.venv\Scripts\python3.exe
) else (
    set PYTHON=python
)

echo   Checking dependencies...
%PYTHON% -c "import flask" 2>nul
if errorlevel 1 (
    echo   Flask not found. Installing requirements...
    %PYTHON% -m pip install -r requirements.txt
    echo.
)

echo   Starting server...
echo   Press Ctrl+C to stop.
echo.
echo   On this computer:  http://localhost:5000
echo   On your phone:     http://[your-ip]:5000
echo.

:: Print local IP address for phone access
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4" ^| findstr /v "127.0.0.1"') do (
    for /f "tokens=1" %%b in ("%%a") do (
        echo   Your network IP:   http://%%b:5000
        goto :found_ip
    )
)
:found_ip
echo.

:: Open browser after a short delay (start /b runs in background)
start "" /b cmd /c "timeout /t 2 >nul && start http://localhost:5000"

%PYTHON% app.py

pause
