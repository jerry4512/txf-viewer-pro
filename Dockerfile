# TXF Pro Viewer — 對應「啟動工具.command」的容器化版本
#
# 注意：富邦新一代 API SDK 只提供官方 binary wheel，Linux 僅有 x86_64
# （manylinux2014_x86_64），因此映像固定建置為 linux/amd64。
# Apple Silicon / ARM 主機會透過 Rosetta 或 QEMU 模擬執行。
FROM --platform=linux/amd64 python:3.11-slim

# APScheduler 的 Asia/Taipei 排程與盤中時間判斷都依賴系統時區資料
ENV TZ=Asia/Taipei \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 使用 /opt/venv：install_fubon_sdk.py 只有在非虛擬環境時才會加 --user，
# 放進 venv 可讓 SDK 與其他依賴安裝在同一處，也不會被 /app 的 bind mount 蓋掉。
ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH
RUN python -m venv "$VIRTUAL_ENV"

WORKDIR /app

# 依賴層獨立快取：只有 requirements.txt / SDK 安裝腳本變動才重新安裝
COPY requirements.txt install_fubon_sdk.py ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && python install_fubon_sdk.py

# 應用程式碼（compose 預設會再以 bind mount 覆蓋，方便本機改檔即時生效）
COPY . .

EXPOSE 8000

# 容器不開啟瀏覽器（原 .command 的 open 行為由使用者自行連線取代）；
# 健康檢查只讀 /api/status，未登入也會正常回應
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/status', timeout=3).status == 200 else 1)"

# 綁 0.0.0.0 讓容器外（本機與區網其他裝置）都能用瀏覽器連進來
# （.command 綁 127.0.0.1 是給本機直跑用的）
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
