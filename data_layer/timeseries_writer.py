"""
data_layer/timeseries_writer.py
================================
依 README_DB.md 定義的「資料寫入規則」把即時採集到的數值寫進
TimescaleDB 的 sensor_readings（時序表）。

規則（對應 README_DB.md）：
    - 該 sensor_id 第一次出現              -> 寫入
    - 新值 與 快取中的上一筆數值 不同         -> 寫入
    - 數值相同，但距離上次寫入超過心跳週期     -> 仍寫入（心跳，證明沒斷線）
    - 其餘情況（數值相同 且 尚未到心跳時間）   -> 不寫入

設計重點：
    - 用「記憶體快取」記住每個 sensor_id 的上一筆值/時間，
      避免每一輪採集都要查一次 DB 才能判斷是否重複（比對邏輯全部在記憶體做）。
    - 服務啟動時呼叫 load_initial_cache()，把每個 sensor_id 目前資料庫裡
      最新的一筆讀回來，避免程式重啟後心跳判斷從頭算，导致心跳時間錯亂。
    - stage() 只是「先放進緩衝區」，真正寫進 DB 要呼叫 flush()（批次 INSERT，
      跟現有 batch_updater.py 的風格一致）。
    - sensor_readings.value 是 NUMERIC，非數值（例如 modbus 的中文狀態字）
      會被直接跳過並記一筆 debug log，不會讓程式崩潰。
"""

import logging
import os
from datetime import datetime, timedelta

from psycopg2.extras import execute_values

from .db_connector import DatabaseConnector

logger = logging.getLogger(__name__)


class SensorReadingWriter:
    def __init__(self, heartbeat_interval_seconds=None):
        # 心跳週期：數值沒變化時，最少多久還是要補寫一筆進資料庫
        raw = heartbeat_interval_seconds or os.getenv(
            "SENSOR_HEARTBEAT_INTERVAL", "3600"
        )
        self.heartbeat_interval = timedelta(seconds=float(raw))

        # sensor_id -> (last_value: float, last_time: datetime)
        self._last_values = {}
        # 待寫入緩衝區： [(sensor_id, reading_time, value), ...]
        self._pending = []
        self._cache_loaded = False

    # ------------------------------------------------------------
    # 啟動時載入每個 sensor 目前資料庫裡最新一筆數值
    # ------------------------------------------------------------
    def load_initial_cache(self):
        query = """
            SELECT DISTINCT ON (sensor_id) sensor_id, value, reading_time
            FROM sensor_readings
            ORDER BY sensor_id, reading_time DESC;
        """
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(query)
                    rows = cur.fetchall()
                    for sensor_id, value, reading_time in rows:
                        self._last_values[sensor_id] = (
                            float(value),
                            reading_time,
                        )
            self._cache_loaded = True
            logger.info(
                f"📥 SensorReadingWriter 快取初始化完成，"
                f"共載入 {len(self._last_values)} 個 sensor 的最新值。"
            )
        except Exception as e:
            logger.error(f"載入 sensor_readings 最新值快取失敗: {e}")

    # ------------------------------------------------------------
    # 判斷是否要寫入，要的話先放進緩衝區（不會立刻打 DB）
    # ------------------------------------------------------------
    def stage(self, sensor_id, value, reading_time=None):
        if sensor_id is None:
            # 這個點位還沒被綁定到 sensors 階層，不寫進時序表
            return

        try:
            num_value = float(value)
        except (TypeError, ValueError):
            logger.info(
                f"⚠️ sensor_id={sensor_id} 的值 '{value}' (type={type(value).__name__}) "
                "無法轉成數字，sensor_readings 僅支援數字，已略過。"
            )
            return

        now = reading_time or datetime.now().astimezone()
        last = self._last_values.get(sensor_id)

        should_write = False
        reason = ""
        if last is None:
            should_write = True
            reason = "首次出現"
        else:
            last_value, last_time = last
            if num_value != last_value:
                should_write = True
                reason = f"數值變化 {last_value} -> {num_value}"
            elif now - last_time > self.heartbeat_interval:
                should_write = True
                reason = "心跳補寫"
            else:
                reason = f"數值未變化且未到心跳時間（距上次 {now - last_time}）"

        logger.info(
            f"🧾 stage 判斷: sensor_id={sensor_id}, value={num_value}, "
            f"要寫入={should_write}（{reason}），writer_id={id(self)}"
        )

        if should_write:
            self._pending.append((sensor_id, now, num_value))
            self._last_values[sensor_id] = (num_value, now)
            logger.info(
                f"📦 已放入緩衝區，目前緩衝區筆數={len(self._pending)}, writer_id={id(self)}"
            )

    # ------------------------------------------------------------
    # 把緩衝區的資料批次寫進資料庫
    # ------------------------------------------------------------
    def flush(self):
        logger.info(
            f"🚿 flush() 被呼叫，目前緩衝區筆數={len(self._pending)}, writer_id={id(self)}"
        )
        if not self._pending:
            return 0

        rows = self._pending
        self._pending = []

        query = """
            INSERT INTO sensor_readings (sensor_id, reading_time, value)
            VALUES %s
            ON CONFLICT (sensor_id, reading_time) DO NOTHING;
        """
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    execute_values(cur, query, rows)
            logger.info(
                f"💾 SensorReadingWriter 成功寫入 {len(rows)} "
                "筆時序資料到 sensor_readings。"
            )
            return len(rows)
        except Exception as e:
            logger.error(f"寫入 sensor_readings 失敗: {e}")
            # 失敗的話塞回緩衝區開頭，下一輪再試一次，避免資料遺失
            self._pending = rows + self._pending
            return 0


# 跨模組共用的單例：main.py 與各 collector 都 import 這個同一個實例，
# 這樣同一輪採集（S7 + Modbus + OPC UA）的資料最後可以一起 flush，
# 也可以各自獨立 flush（單獨執行某個 collector 腳本時仍然正常運作）。
sensor_reading_writer = SensorReadingWriter()