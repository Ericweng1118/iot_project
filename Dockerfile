# ==========================================
# 第一階段：Builder (編譯與打包套件)
# ==========================================
FROM python:3.12-slim AS builder

WORKDIR /app

# 安裝編譯需要的工具與 PostgreSQL 開發庫
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 打包 Python wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir /app/wheels -r requirements.txt


# ==========================================
# 第二階段：Runner (極簡執行環境)
# ==========================================
FROM python:3.12-slim AS runner

WORKDIR /app

# 設定 Python 效能與 Streamlit 容器設定
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_HEADLESS=true

# 安裝運行階段必要的動態庫
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 從 Builder 複製並安裝 Wheels
COPY --from=builder /app/wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

# 複製專案全部程式碼 (搭配 .dockerignore 使用)
COPY . .

# 網頁後台埠號（實際以 .env 的 ADMIN_PORT 為準，正式環境目前是 1118）
ENV ADMIN_PORT=1118
EXPOSE 1118

# 🆕 健康檢查：網頁後台有回應才算健康（採集服務是否存活請看網頁「系統狀態」頁，
#    它由 main.py 每 10 秒寫入 service_status 的心跳判斷）
#    啟用 HTTPS（ADMIN_SSL_CERT）時改走 https，自簽憑證用 -k 略過驗證。
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fs "http://127.0.0.1:${ADMIN_PORT}/_stcore/health" \
     || curl -fsk "https://127.0.0.1:${ADMIN_PORT}/_stcore/health" || exit 1

# 🆕 本機緩存（資料庫斷線時暫存 sensor_readings）放在 /app/data，請掛 volume：
#    docker run -v /opt/scada/data:/app/data ...  否則刪除重建容器時緩存會消失
VOLUME ["/app/data"]

# run_all.py 收到 SIGTERM 會通知子程序收尾（OPC UA 斷線、最後一次寫入），最多需要約 25 秒，
# 請用 `docker stop -t 30` 或 compose 的 `stop_grace_period: 30s`，預設 10 秒可能來不及。
STOPSIGNAL SIGTERM

CMD ["python", "run_all.py"]

# sudo docker build -t unified_collector:20260911 .

# sudo docker save unified_collector:20260911 | gzip > .docker_images/unified_collector_20260911.tar.gz