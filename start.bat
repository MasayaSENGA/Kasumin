@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [Kasumin] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 goto :error
)

rem Install dependencies on the first run and whenever requirements.txt has changed since the last install
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
if errorlevel 1 (
    echo [Kasumin] Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :error
    copy /y requirements.txt ".venv\requirements.installed" >nul
)

".venv\Scripts\python.exe" -m kasumin %*
goto :eof

:error
echo.
echo [Kasumin] Setup failed. Make sure Python 3.10 or later is installed and on PATH.
pause
exit /b 1
