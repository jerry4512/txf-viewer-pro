#!/bin/bash
# 切換到腳本所在目錄（確保 Mac 不論從哪裡點擊都能正確執行）
cd "$(dirname "$0")"

# 背景等待 2 秒後開啟瀏覽器（讓伺服器先起來）
(sleep 2 && open "http://127.0.0.1:8000") &

# 啟動伺服器
python3 -m uvicorn main:app --host 127.0.0.1 --port 8000
