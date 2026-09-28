@echo off
chcp 65001 >nul 2>&1
setlocal enableextensions
title BTC Intelligence Platform - Portable

rem ------------------------------------------------------------------
rem Every path is derived from THIS file's own location, never from the
rem current working directory. That is what keeps the package relocatable:
rem you can drop the folder anywhere, rename it, or copy it to another PC.
rem IMPORTANT: this file must be saved with CRLF line endings. LF-only
rem batch files are mis-parsed by cmd.exe (lines get glued together).
rem ------------------------------------------------------------------
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\runtime\python.exe"
set "APP=%ROOT%\app"
set "PORT=8000"

if not exist "%PY%" (
    echo [ERROR] Bundled Python runtime not found:
    echo     "%PY%"
    echo Please extract the WHOLE package ^(especially the runtime folder^).
    echo.
    pause
    exit /b 1
)

if not exist "%APP%\scripts\btcctl.py" (
    echo [ERROR] Application folder is incomplete:
    echo     "%APP%\scripts\btcctl.py"
    echo Please extract the WHOLE package.
    echo.
    pause
    exit /b 1
)

"%PY%" -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] The bundled Python cannot start.
    echo Most likely a DLL is missing ^(extract was incomplete^), or the
    echo folder sits on a path with characters Python cannot handle.
    echo.
    pause
    exit /b 1
)

echo ============================================================
echo    BTC Intelligence Platform   -   Portable Edition
echo ============================================================
echo.
echo    Python    : "%PY%"
echo    App       : "%APP%"
echo    Database  : chosen in the browser setup wizard on first launch
echo                ^(SQLite / PostgreSQL / MySQL^)
echo.

echo Starting server ... keep this window open.
echo.
echo    Home      :  http://127.0.0.1:%PORT%
echo    API docs  :  http://127.0.0.1:%PORT%/docs
echo    Stop      :  Ctrl+C in this window, or double-click stop.bat
echo.
echo    First launch: the browser opens the SETUP WIZARD.
echo    Pick a database, fill the connection fields, press "Test",
echo    and save once every check is green.
echo.

start "" "http://127.0.0.1:%PORT%"

"%PY%" "%APP%\scripts\btcctl.py" serve --host 127.0.0.1 --port %PORT%

echo.
echo Server exited.
pause
