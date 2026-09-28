@echo off
chcp 65001 >nul 2>&1
setlocal enableextensions

rem IMPORTANT: this file must be saved with CRLF line endings.
set "PORT=8000"

echo Stopping the platform listening on port %PORT% ...

set "FOUND=0"
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT%" ^| findstr LISTENING') do (
    echo     killing PID %%P
    taskkill /F /PID %%P >nul 2>&1
    set "FOUND=1"
)

if "%FOUND%"=="0" (
    echo     No running instance found on port %PORT%.
    echo     If you started it on another port, edit PORT in this file.
) else (
    echo Stopped.
)
echo.
pause
