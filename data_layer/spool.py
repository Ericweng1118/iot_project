"""
data_layer/spool.py
===================
本機緩存（Store-and-Forward）：資料庫連不上時，sensor_readings 要寫的資料先存到
本機的 SQLite 檔案，資料庫恢復後由寫入排程依時間順序補寫回去，期間的歷史資料不會遺失。

為什麼用 SQLite：
    - Python 標準函式庫內建，不需要額外安裝或架設服務
    - 寫入是 transaction，程式在緩存途中被殺掉也不會留下寫一半的檔案
    - 程序重啟後還在（記憶體佇列做不到），main.py 重開會接著補寫

容量上限：
    SPOOL_MAX_ROWS（預設 2,000,000 筆，約 100 MB）。超過時丟棄「最舊」的資料並記錄
    丟棄筆數 —— 一直寫不進資料庫時，寧可保留最近的資料，也不要把磁碟塞爆讓整台機器出事。
    以目前約 16 萬筆 / 天的寫入量估算，可以撐十幾天的資料庫中斷。

⚠️ Docker 部署時，緩存檔所在的目錄（SPOOL_DIR，預設 /app/data）要掛 volume，
   否則「刪除重建容器」時緩存會跟著消失（單純 restart 不會）。
"""

import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path(__file__).resolve().parent.parent / "data"


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key, str(default)).split("#")[0].strip()
    try:
        return int(float(raw))
    except ValueError:
        return default


class ReadingSpool:
    def __init__(self, path: str | Path | None = None, max_rows: int | None = None):
        spool_dir = Path(os.getenv("SPOOL_DIR", "").split("#")[0].strip() or _DEFAULT_DIR)
        if not spool_dir.is_absolute():
            spool_dir = _DEFAULT_DIR.parent / spool_dir   # 相對路徑以專案目錄為準，不受啟動目錄影響
        self.path = Path(path) if path else spool_dir / "spool.sqlite3"
        self.max_rows = max_rows if max_rows is not None else _env_int("SPOOL_MAX_ROWS", 2_000_000)
        self._lock = threading.Lock()
        self._conn = None
        self.dropped_total = 0

    # ------------------------------------------------------------------
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS readings (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    sensor_id     INTEGER NOT NULL,
                    reading_time  TEXT    NOT NULL,   -- ISO 8601，含時區
                    value         REAL    NOT NULL,
                    quality       INTEGER
                );
                """
            )
            self._conn.commit()
        return self._conn

    def append(self, rows) -> int:
        """rows: [(sensor_id, reading_time: datetime, value, quality)]，回傳實際存入筆數。"""
        rows = list(rows)
        if not rows:
            return 0
        with self._lock:
            db = self._db()
            db.executemany(
                "INSERT INTO readings (sensor_id, reading_time, value, quality) VALUES (?, ?, ?, ?);",
                [(int(s), t.isoformat(), float(v), q) for s, t, v, q in rows],
            )
            overflow = db.execute("SELECT count(*) FROM readings;").fetchone()[0] - self.max_rows
            if overflow > 0:
                db.execute(
                    "DELETE FROM readings WHERE id IN (SELECT id FROM readings ORDER BY id LIMIT ?);",
                    (overflow,),
                )
                self.dropped_total += overflow
                logger.error(
                    f"❌ 本機緩存已達上限 {self.max_rows:,} 筆，丟棄最舊的 {overflow:,} 筆"
                    "（資料庫已長時間無法寫入，請盡快處理）"
                )
            db.commit()
        return len(rows)

    def count(self) -> int:
        if not self.path.exists() and self._conn is None:
            return 0
        with self._lock:
            return self._db().execute("SELECT count(*) FROM readings;").fetchone()[0]

    def oldest_time(self) -> str | None:
        if not self.path.exists() and self._conn is None:
            return None
        with self._lock:
            row = self._db().execute("SELECT min(reading_time) FROM readings;").fetchone()
            return row[0] if row else None

    def peek(self, limit: int):
        """依寫入順序取出最舊的 limit 筆：[(id, sensor_id, datetime, value, quality)]，不刪除。"""
        with self._lock:
            rows = self._db().execute(
                "SELECT id, sensor_id, reading_time, value, quality FROM readings ORDER BY id LIMIT ?;",
                (limit,),
            ).fetchall()
        return [(i, s, datetime.fromisoformat(t), v, q) for i, s, t, v, q in rows]

    def delete_up_to(self, last_id: int) -> None:
        """補寫成功後刪除 id <= last_id 的資料（peek 是依 id 排序取出的）。"""
        with self._lock:
            db = self._db()
            db.execute("DELETE FROM readings WHERE id <= ?;", (last_id,))
            db.commit()

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
