import os
import logging
from contextlib import contextmanager
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

class DatabaseConnector:
    _pool = None

    @classmethod
    def initialize_pool(cls):
        # 1. 若連線池已經初始化過，直接回傳 True
        if cls._pool:
            return True

        try:
            cls._pool = SimpleConnectionPool(
                minconn=1,
                maxconn=10,
                host=os.getenv("DB_HOST"),
                port=int(os.getenv("DB_PORT", 5432)),  # 轉成 int 避免型態錯誤
                database=os.getenv("DB_NAME"),
                user=os.getenv("DB_USER"),
                password=os.getenv("DB_PASSWORD")
            )
            logger.info("PostgreSQL 連線池初始化成功。")
            return True  # 👈 🔥 關鍵修正：成功時傳回 True

        except Exception as e:
            logger.error(f"無法初始化 PostgreSQL 連線池: {e}")
            return False # 👈 🔥 關鍵修正：失敗時傳回 False，不拋出 exception 中斷

    @classmethod
    @contextmanager
    def get_connection(cls):
        if not cls._pool:
            cls.initialize_pool()
        
        conn = cls._pool.getconn()
        try:
            yield conn
        except Exception as e:
            conn.rollback()
            logger.error(f"資料庫事務執行失敗，已自動回滾: {e}")
            raise
        else:
            conn.commit()
        finally:
            cls._pool.putconn(conn)

    @classmethod
    def close_pool(cls):
        if cls._pool:
            cls._pool.closeall()
            logger.info("PostgreSQL 連線池已安全關閉。")