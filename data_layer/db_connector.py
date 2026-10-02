import os
import logging
import threading
from contextlib import contextmanager
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# 連線已失效（DB 重啟、網路中斷、閒置被防火牆切斷）時 psycopg2 會丟的例外。
# 這類連線不能再放回連線池，否則下一個借到它的人會再失敗一次。
_BROKEN_CONNECTION_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)


class DatabaseConnector:
    _pool = None
    _init_lock = threading.Lock()

    @classmethod
    def initialize_pool(cls):
        # 1. 若連線池已經初始化過，直接回傳 True
        if cls._pool:
            return True

        with cls._init_lock:
            if cls._pool:
                return True
            try:
                # 🔥 改用 ThreadedConnectionPool：支援多執行緒併發同時取用連線
                #    （SimpleConnectionPool 不是執行緒安全的，多執行緒併發採集時會出問題）
                cls._pool = ThreadedConnectionPool(
                    minconn=1,
                    maxconn=int(os.getenv("DB_POOL_MAX", "20").split("#")[0].strip() or 20),
                    host=os.getenv("DB_HOST"),
                    port=int(os.getenv("DB_PORT", 5432)),  # 轉成 int 避免型態錯誤
                    database=os.getenv("DB_NAME"),
                    user=os.getenv("DB_USER"),
                    password=os.getenv("DB_PASSWORD"),
                    # DB 主機無回應時不要無限期卡住整個採集執行緒
                    connect_timeout=10,
                    # TCP keepalive：長時間閒置的連線不會被中間的 NAT / 防火牆默默切斷
                    keepalives=1,
                    keepalives_idle=60,
                    keepalives_interval=10,
                    keepalives_count=3,
                    # 在 pg_stat_activity 可以分辨是哪支程式的連線
                    application_name=os.getenv("DB_APPLICATION_NAME", "iiot_scada"),
                )
                logger.info("PostgreSQL 連線池初始化成功 (ThreadedConnectionPool，支援多執行緒併發)。")
                return True  # 👈 🔥 關鍵修正：成功時傳回 True

            except Exception as e:
                logger.error(f"無法初始化 PostgreSQL 連線池: {e}")
                return False # 👈 🔥 關鍵修正：失敗時傳回 False，不拋出 exception 中斷

    @classmethod
    def _checkout(cls):
        """
        從連線池借一條「還活著」的連線。

        DB 重啟過後，池子裡的舊連線全部都已失效，但 psycopg2 要等真的送出查詢
        才會發現。這裡先丟掉 `closed` 已經被標記的連線，避免把壞連線借出去。
        """
        for _ in range(3):
            conn = cls._pool.getconn()
            if not conn.closed:
                return conn
            cls._pool.putconn(conn, close=True)
        return cls._pool.getconn()

    @classmethod
    @contextmanager
    def get_connection(cls):
        if not cls._pool and not cls.initialize_pool():
            raise psycopg2.OperationalError("PostgreSQL 連線池尚未初始化（請檢查 .env 的 DB 設定與網路）")

        conn = cls._checkout()
        broken = False
        try:
            yield conn
        except _BROKEN_CONNECTION_ERRORS as e:
            broken = True
            logger.error(f"資料庫連線已失效，將丟棄此連線並於下次重新建立: {e}")
            raise
        except Exception as e:
            try:
                conn.rollback()
            except _BROKEN_CONNECTION_ERRORS:
                broken = True
            logger.error(f"資料庫事務執行失敗，已自動回滾: {e}")
            raise
        else:
            try:
                conn.commit()
            except _BROKEN_CONNECTION_ERRORS:
                broken = True
                raise
        finally:
            cls._pool.putconn(conn, close=broken or bool(conn.closed))

    @classmethod
    def close_pool(cls):
        if cls._pool:
            cls._pool.closeall()
            cls._pool = None
            logger.info("PostgreSQL 連線池已安全關閉。")
