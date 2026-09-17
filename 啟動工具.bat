@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 goto failed

set "PYTHON=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON=%~dp0.venv\Scripts\python.exe"
"%PYTHON%" --version
if errorlevel 1 goto failed

"%PYTHON%" install_fubon_sdk.py
if errorlevel 1 goto failed

start "" "http://127.0.0.1:8000"
"%PYTHON%" -m uvicorn main:app --host 127.0.0.1 --port 8000
if errorlevel 1 goto failed
pause
exit /b 0

:failed
echo.
echo [ERROR] Startup failed. See the error message above.
pause
exit /b 1
