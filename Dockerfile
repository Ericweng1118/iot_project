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

# 預設開放 Streamlit 埠號
EXPOSE 8501

CMD ["python", "run_all.py"]

# sudo docker build -t unified_collector:20260724 .

# sudo docker save unified_collector:20260724 | gzip > .docker_images/unified_collector_20260724.tar.gz