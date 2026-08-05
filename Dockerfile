# ==========================================
# 第一階段：Builder (編譯與打包套件)
# ==========================================
FROM python:3.12-slim AS builder

WORKDIR /app

# 安裝編譯需要的重型工具
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 先將 Python 套件打包成 Wheel 檔
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir /app/wheels -r requirements.txt


# ==========================================
# 第二階段：Runner (極簡執行環境)
# ==========================================
FROM python:3.12-slim AS runner

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 執行階段只需要 PostgreSQL 的運行函式庫 (libpq5)，剔除 gcc/g++ 等編譯器
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 從 Builder 階段複製編譯好的 Wheels 並安裝
COPY --from=builder /app/wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

# 複製專案原始碼
COPY requirements.txt .

COPY protocols/ ./protocols/
COPY parsers/ ./parsers/
COPY data_layer/ ./data_layer/
COPY messaging/ ./messaging/
COPY collector/ ./collector/

COPY main.py .
COPY admin_app.py .
COPY run_all.py .

EXPOSE 8000

CMD ["python", "run_all.py"]

# sudo docker build -t unified_collector:20260724 .

# sudo docker save unified_collector:20260724 | gzip > .docker_images/unified_collector_20260724.tar.gz