@echo off
python install_fubon_sdk.py
if errorlevel 1 (
  echo [啟動失敗] 富邦 SDK 安裝或檢查失敗
  pause
  exit /b 1
)
start "" "http://127.0.0.1:8000"
python -m uvicorn main:app --host 127.0.0.1 --port 8000
pause
