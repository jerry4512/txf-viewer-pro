@echo off
start "" "http://127.0.0.1:8000"
python -m uvicorn main:app --host 127.0.0.1 --port 8000
pause
