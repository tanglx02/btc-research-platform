@echo off
chcp 65001 >nul 2>&1
setlocal enableextensions
title BTC - Backfill history

rem IMPORTANT: this file must be saved with CRLF line endings.
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\runtime\python.exe"
set "APP=%ROOT%\app"
set "DAYS=730"

if not exist "%PY%" (
    echo [ERROR] Bundled Python runtime not found:
    echo     "%PY%"
    echo Please extract the WHOLE package.
    echo.
    pause
    exit /b 1
)

echo Backfilling the last %DAYS% days of market data ...
echo This may take several minutes. Ctrl+C is safe: the job resumes
echo from where it stopped the next time you run this script.
echo.

"%PY%" "%APP%\scripts\btcctl.py" backfill --days %DAYS%

echo.
echo Done. Tip: run "collect-all" as well to pull onchain / derivatives /
echo sentiment / macro / ETF datasets.
echo.
pause
