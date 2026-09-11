"""
data_layer/timeseries_writer.py
================================
把即時採集到的數值寫進 TimescaleDB 的 sensor_readings（時序表）。

🆕 v2 設計（統一週期性寫入）：
    v1 版本由各採集器在每輪採集結束時呼叫 stage() + flush()，
    依「首次出現 / 數值變化 / 心跳補寫」逐點位判斷是否要寫入，
    寫入時機分散、跟各協議自己的採集週期綁在一起。

    v2 拆成兩層，彼此獨立：
      1. 最新值快取（高頻、純記憶體）
         採集端（OPC UA 訂閱服務為主，Modbus/TIA 若啟用也共用同一套）
         每次讀到新值就呼叫 update_latest()，只更新記憶體快取，不碰資料庫。

      2. 統一寫入排程（低頻、固定週期，由 .env 控制）
         背景執行緒依 SENSOR_READING_FLUSH_INTERVAL（秒）固定週期醒來，
         把「目前所有感測器的最新值」一次性依各自的 sensors.upload_condition
         判斷後批次寫入 sensor_readings。upload_condition 四選一：
             - always            : 不判斷，每一輪都寫
             - on_change         : 只有數值與上次「實際寫入」的值不同才寫
             - threshold_percent : 變化「百分比」達到 upload_threshold（例如 1 = 1%）才寫，
                                    以「上次實際寫入的值」為基準；上次值為 0 時無法算百分比，
                                    退化為「數值不再是 0 就寫」
             - threshold_absolute: 變化「絕對值」達到 upload_threshold 才寫

      3. 心跳保底（SENSOR_HEARTBEAT_INTERVAL，預設 3600 秒）
         不論 upload_condition 判斷結果如何，只要距離上次實際寫入超過這個時間
         就強制補寫一筆。這是 v1 就有、v2 初版漏掉後又補回來的保護：沒有它，
         門檻設錯的點位會靜悄悄地完全沒有資料，且不會有任何錯誤訊息。

    也就是「多久寫一次」是全域參數（.env），「這次要不要寫」是逐感測器規則
    （sensors 表，可在網頁「感測器階層管理」分頁調整：百分比 / 絕對值 / 不判斷，
    最慢下一輪就生效）。

    stage() / flush() 兩個舊方法仍保留（stage 等同 update_latest 的別名；
    flush 除了是背景排程內部呼叫的核心方法，也可以被其他程式手動呼叫，
    立即觸發一次寫入），維持向下相容，既有採集器程式碼不需要修改也能繼續運作。

🔒 併發安全性：
    Modbus / TIA 各自的多執行緒併發採集、OPC UA 訂閱服務的背景執行緒，
    都可能同時呼叫 update_latest()，寫入排程的背景執行緒則會定期讀取快照，
    因此用 threading.Lock 保護 _latest_values / _last_written / _upload_config。
"""

import logging
import os
import threading
import time
from datetime import datetime

from psycopg2.extras import execute_values

from .db_connector import DatabaseConnector

logger = logging.getLogger(__name__)


def _parse_env_float(key: str, default: float) -> float:
    raw = os.getenv(key, str(default))
    try:
        return float(str(raw).split("#")[0].strip())
    except (TypeError, ValueError):
        return float(default)


class SensorReadingWriter:
    def __init__(self, flush_interval_seconds=None, heartbeat_interval_seconds=None):
        # 統一寫入週期（秒）：來源 .env SENSOR_READING_FLUSH_INTERVAL，預設 60 秒
        self.flush_interval = (
            float(flush_interval_seconds)
            if flush_interval_seconds is not None
            else _parse_env_float("SENSOR_READING_FLUSH_INTERVAL", 60.0)
        )

        # 心跳保底（秒）：來源 .env SENSOR_HEARTBEAT_INTERVAL，預設 3600 秒。
        # 距離上次實際寫入超過這個時間，無論 upload_condition 判斷結果如何都強制
        # 補寫一筆。設為 0（或負數）代表關閉心跳。
        #
        # 為什麼需要：v1 的寫入邏輯本來就有「值沒變也每小時補一筆」的保底，v2 改成
        # 統一排程 + upload_condition 判斷時漏掉了這段，結果是門檻設得不合理的點位
        # 會**完全沒有任何紀錄、也不會有任何錯誤 log**，只能靠人工發現。實際踩過的
        # 案例：累計型電表（值百萬等級、日增量幾百）套用預設 1% 的 threshold_percent，
        # 要累積四十幾天才寫得進一筆，看起來就像採集壞掉，但其實 OPC UA 一切正常。
        # 心跳補寫讓「沒有新資料」跟「系統掛了」在資料上可以區分開來。
        self.heartbeat_interval = (
            float(heartbeat_interval_seconds)
            if heartbeat_interval_seconds is not None
            else _parse_env_float("SENSOR_HEARTBEAT_INTERVAL", 3600.0)
        )

        # sensor_id -> (value: float, time: datetime)　最新收到的值（尚未必然寫入）
        self._latest_values = {}
        # sensor_id -> (value: float, time: datetime)　最後一次「實際寫入」DB 的值
        self._last_written = {}
        # sensor_id -> (upload_condition: str, upload_threshold: float|None)
        self._upload_config = {}

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._cache_loaded = False

    # ------------------------------------------------------------
    # 啟動時載入每個 sensor 目前資料庫裡最新一筆數值，
    # 當作 _last_written 的起始狀態（避免程式重啟後 on_change/threshold
    # 判斷從頭算，誤判「值變了」而重複寫入相同數值）
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
                    with self._lock:
                        for sensor_id, value, reading_time in rows:
                            self._last_written[sensor_id] = (float(value), reading_time)
                        self._cache_loaded = True
            logger.info(
                f"📥 SensorReadingWriter 快取初始化完成，"
                f"共載入 {len(self._last_written)} 個 sensor 的最新值。"
            )
        except Exception as e:
            logger.error(f"載入 sensor_readings 最新值快取失敗: {e}")

        self._refresh_upload_config()

    # ------------------------------------------------------------
    # 重新讀取 sensors 表的 upload_condition / upload_threshold 設定。
    # 背景排程每一輪都會呼叫，讓網頁上剛改的設定最慢下一個週期就生效，
    # 不需要重啟服務。
    # ------------------------------------------------------------
    def _refresh_upload_config(self):
        query = """
            SELECT sensor_id, upload_condition, upload_threshold
            FROM sensors;
        """
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(query)
                    rows = cur.fetchall()
            new_config = {}
            for sensor_id, condition, threshold in rows:
                new_config[sensor_id] = (
                    (condition or "always").lower(),
                    float(threshold) if threshold is not None else None,
                )
            with self._lock:
                self._upload_config = new_config
        except Exception as e:
            # 常見原因：尚未執行 sql/006_opcua_upgrade.sql，欄位還不存在。
            # 不讓整個排程掛掉，退回「全部視為 always」，等 migration 補跑後自動恢復。
            logger.error(
                f"重新整理 sensors.upload_condition 設定失敗（尚未執行 "
                f"sql/006_opcua_upgrade.sql 的話會出現這個錯誤）: {e}"
            )

    # ------------------------------------------------------------
    # 更新「最新值」快取：高頻呼叫，僅記憶體操作，沒有任何 DB I/O。
    # ------------------------------------------------------------
    def update_latest(self, sensor_id, value, reading_time=None):
        if sensor_id is None:
            # 這個點位還沒被綁定到 sensors 階層，不進時序表
            return

        try:
            num_value = float(value)
        except (TypeError, ValueError):
            logger.debug(
                f"⚠️ sensor_id={sensor_id} 的值 '{value}' "
                f"(type={type(value).__name__}) 無法轉成數字，已略過。"
            )
            return

        now = reading_time or datetime.now().astimezone()
        with self._lock:
            self._latest_values[sensor_id] = (num_value, now)

    # 向下相容：v1 的呼叫方式（各採集器既有程式碼）繼續可用
    def stage(self, sensor_id, value, reading_time=None):
        self.update_latest(sensor_id, value, reading_time)

    # ------------------------------------------------------------
    # 執行一次「統一週期性寫入」：檢查目前所有有最新值的 sensor，
    # 依各自 upload_condition 決定要不要寫，批次 INSERT。
    # 可被背景排程自動呼叫，也可以手動呼叫立即觸發一次寫入。
    # ------------------------------------------------------------
    def flush(self):
        with self._lock:
            snapshot = dict(self._latest_values)
            upload_config = dict(self._upload_config)
            last_written = dict(self._last_written)

        rows_to_write = []
        for sensor_id, (value, ts) in snapshot.items():
            condition, threshold = upload_config.get(sensor_id, ("always", None))
            last = last_written.get(sensor_id)
            write_it, reason = self._should_write_with(
                condition, threshold, last, value,
                now=ts, heartbeat_interval=self.heartbeat_interval,
            )
            logger.debug(
                f"🧾 統一寫入判斷: sensor_id={sensor_id}, value={value}, "
                f"要寫入={write_it}（{reason}）"
            )
            if write_it:
                rows_to_write.append((sensor_id, ts, value))

        if not rows_to_write:
            return 0

        query = """
            INSERT INTO sensor_readings (sensor_id, reading_time, value)
            VALUES %s
            ON CONFLICT (sensor_id, reading_time) DO NOTHING;
        """
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    execute_values(cur, query, rows_to_write)
            with self._lock:
                for sensor_id, ts, value in rows_to_write:
                    self._last_written[sensor_id] = (value, ts)
            logger.info(
                f"💾 [統一寫入] 本輪共 {len(snapshot)} 個感測器有最新值，"
                f"依 upload_condition 判斷後寫入 {len(rows_to_write)} 筆到 sensor_readings。"
            )
            return len(rows_to_write)
        except Exception as e:
            logger.error(f"統一寫入 sensor_readings 失敗: {e}")
            return 0

    @staticmethod
    def _should_write_with(
        condition, threshold, last, value, now=None, heartbeat_interval=0.0
    ):
        """
        判斷這個 sensor 這一輪要不要寫。condition 四選一：
            always            - 不判斷，每輪都寫
            on_change         - 數值與上次「實際寫入」的值不同才寫
            threshold_percent - 變化百分比（以上次寫入值為基準）達到 threshold 才寫，
                                 threshold 以「百分比數字」表示（例如 1 代表 1%）
            threshold_absolute- 變化絕對值達到 threshold 才寫

        不論上面哪一種，只要距離上次實際寫入超過 heartbeat_interval 秒，就強制
        補寫一筆（心跳保底），避免門檻設定不合理的點位在資料上完全消失。
        傳入 heartbeat_interval <= 0 代表關閉心跳。
        """
        if last is None:
            return True, "首次出現"
        if condition == "always":
            return True, "always（不判斷）"

        last_value, last_time = last

        # 心跳保底：擺在所有門檻判斷之前，確保任何 condition 都有最低寫入頻率。
        # 時間資訊缺漏或 naive/aware 混用時，寧可跳過心跳也不要讓整輪寫入炸掉。
        if heartbeat_interval and heartbeat_interval > 0 and now is not None and last_time is not None:
            try:
                elapsed = (now - last_time).total_seconds()
            except TypeError:
                elapsed = None
            if elapsed is not None and elapsed >= heartbeat_interval:
                return True, f"心跳補寫（距上次寫入 {elapsed:.0f} 秒 >= {heartbeat_interval:.0f} 秒）"

        if condition == "on_change":
            if value != last_value:
                return True, f"數值變化 {last_value} -> {value}"
            return False, "數值未變化"

        if condition == "threshold_absolute":
            th = threshold if threshold is not None else 0.0
            delta = abs(value - last_value)
            if delta >= th:
                return True, f"變化絕對值 {delta} >= 門檻 {th}"
            return False, f"變化絕對值 {delta} < 門檻 {th}"

        if condition == "threshold_percent":
            th = threshold if threshold is not None else 1.0  # 預設 1%
            if last_value == 0:
                # 上次寫入值為 0，百分比無法定義：只要新值不再是 0 就視為顯著變化
                if value != 0:
                    return True, "上次寫入值為 0，數值不再是 0，視為顯著變化"
                return False, "上次寫入值為 0，數值仍為 0"
            delta_pct = abs(value - last_value) / abs(last_value) * 100.0
            if delta_pct >= th:
                return True, f"變化百分比 {delta_pct:.4f}% >= 門檻 {th}%"
            return False, f"變化百分比 {delta_pct:.4f}% < 門檻 {th}%"

        # 相容舊版遺留的 'threshold'（v2 初版曾用過，語意為絕對值）
        if condition == "threshold":
            th = threshold if threshold is not None else 0.0
            delta = abs(value - last_value)
            if delta >= th:
                return True, f"[相容舊設定] 變化絕對值 {delta} >= 門檻 {th}"
            return False, f"[相容舊設定] 變化絕對值 {delta} < 門檻 {th}"

        return True, f"未知 upload_condition='{condition}'，安全預設寫入"

    # ------------------------------------------------------------
    # 背景執行緒：固定週期呼叫 flush()
    # ------------------------------------------------------------
    def _loop(self):
        logger.info(
            f"🕒 SensorReadingWriter 統一寫入排程已啟動，週期 = {self.flush_interval} 秒，"
            f"心跳保底 = "
            + (f"{self.heartbeat_interval} 秒" if self.heartbeat_interval > 0 else "關閉")
        )
        while not self._stop_event.is_set():
            # 每一輪先重新整理 upload_condition 設定，
            # 讓網頁上剛改的設定最慢下一輪就生效，不需要重啟服務
            self._refresh_upload_config()
            try:
                self.flush()
            except Exception as e:
                logger.error(f"統一寫入排程執行例外: {e}", exc_info=True)

            waited = 0.0
            step = 0.5
            while waited < self.flush_interval and not self._stop_event.is_set():
                time.sleep(min(step, self.flush_interval - waited))
                waited += step

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        if not self._cache_loaded:
            self.load_initial_cache()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="sensor-reading-writer", daemon=True
        )
        self._thread.start()

    def stop(self, timeout=10):
        if not self._thread:
            return
        self._stop_event.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning("⚠️ SensorReadingWriter 執行緒未能在時限內結束。")
        # 停止前，把目前累積的最新值再寫一次，避免漏掉最後一小段資料
        try:
            self.flush()
        except Exception:
            pass
        logger.info("👋 SensorReadingWriter 統一寫入排程已停止。")


# 跨模組共用的單例：main.py 與各 collector / OPC UA 訂閱服務都 import 這個
# 同一個實例，所有協議的最新值最終由同一個背景排程統一寫入。
sensor_reading_writer = SensorReadingWriter()