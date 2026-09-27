@echo off
chcp 65001 >nul
setlocal
rem ============================================================
rem  BTC Intelligence Platform - installer (Windows)
rem  Creates .venv, installs dependencies, generates .env,
rem  initialises the database. Safe to re-run (idempotent).
rem  Usage: install.bat [--upgrade] [--backfill] [--python PATH]
rem ============================================================
pushd "%~dp0..\.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
python scripts\platformctl.py install %*
set EXITCODE=%ERRORLEVEL%
popd
endlocal & exit /b %EXITCODE%
