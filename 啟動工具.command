#!/bin/bash
# 切換到腳本所在目錄（確保 Mac 不論從哪裡點擊都能正確執行）
cd "$(dirname "$0")"

# 若 8000 port 已被佔用，先強制釋放
EXISTING=$(lsof -ti tcp:8000 2>/dev/null)
if [ -n "$EXISTING" ]; then
    echo "[啟動] 偵測到 port 8000 已被佔用 (PID: $EXISTING)，正在關閉..."
    kill -9 $EXISTING 2>/dev/null
    sleep 1
fi

# 背景等待 3 秒後開啟瀏覽器（讓伺服器先起來）
(sleep 3 && open "http://127.0.0.1:8000") &

# 確保使用官方富邦新一代 API SDK 2.2.8
python3 install_fubon_sdk.py || {
    echo "[啟動失敗] 富邦 SDK 安裝或檢查失敗"
    read
    exit 1
}

# 啟動伺服器
echo "[啟動] 正在啟動伺服器..."
python3 -m uvicorn main:app --host 127.0.0.1 --port 8000

# 伺服器結束後保持視窗開啟（同 Windows 的 pause）
echo ""
echo "[程序已停止] 按 Enter 關閉視窗..."
read
