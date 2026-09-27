@echo off
chcp 65001 >nul
setlocal
rem  BTC Intelligence Platform - restore
rem  Thin wrapper around scripts\platformctl.py (single cross-platform implementation)
pushd "%~dp0..\.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" scripts\platformctl.py restore %*
set EXITCODE=%ERRORLEVEL%
popd
endlocal & exit /b %EXITCODE%
