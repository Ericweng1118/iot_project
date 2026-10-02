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
         🆕 v3：OPC UA 數值不變就不會推播，所以心跳改用「目前時間」判斷，並在
         資料來源確認存活（confirm_alive / update_latest）時以目前時間補寫，
         斷線時則不補寫，詳見 _plan_write()。

    也就是「多久寫一次」是全域參數（.env），「這次要不要寫」是逐感測器規則
    （sensors 表，可在網頁「感測器階層管理」分頁調整：百分比 / 絕對值 / 不判斷，
    最慢下一輪就生效）。

    stage() / flush() 兩個舊方法仍保留（stage 等同 update_latest 的別名；
    flush 除了是背景排程內部呼叫的核心方法，也可以被其他程式手動呼叫，
    立即觸發一次寫入），維持向下相容，既有採集器程式碼不需要修改也能繼續運作。

🆕 v3.1：
    - 品質（quality）：每筆寫入都帶品質代碼（見 data_layer/quality.py，sql/014）。
      品質改變時不論 upload_condition 一律寫一筆；品質不良只記錄「轉為不良」那一筆。
    - 通訊中斷標記：採集端呼叫 mark_unavailable() 時寫入一筆 quality=COMM_LOST 的標記，
      趨勢圖才看得出「這段期間沒有資料是因為斷線」。
    - 本機緩存（Store-and-Forward）：資料庫寫入失敗時先存進本機 SQLite
      （data_layer/spool.py），資料庫恢復後依序補寫，歷史資料不會因為 DB 重啟 / 網路中斷遺失。
    - sql/014 尚未執行（沒有 quality 欄位）時自動退回不帶品質的寫法，不影響運作。

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

from . import quality as Q
from .db_connector import DatabaseConnector
from .spool import ReadingSpool

logger = logging.getLogger(__name__)

# 每次 flush 最多從本機緩存補寫幾批、每批幾筆（避免 DB 剛恢復時一次灌太多）
SPOOL_DRAIN_BATCH = 20_000
SPOOL_DRAIN_MAX_BATCHES = 5
QUALITY_COLUMN_RECHECK_SECONDS = 600


def _parse_env_float(key: str, default: float) -> float:
    raw = os.getenv(key, str(default))
    try:
        return float(str(raw).split("#")[0].strip())
    except (TypeError, ValueError):
        return float(default)


def _last_parts(last):
    """_last_written 的值：v3.1 起是 (value, time, quality)，相容舊的 (value, time)。"""
    value, ts = last[0], last[1]
    quality = last[2] if len(last) > 2 else Q.GOOD
    return value, ts, (Q.GOOD if quality is None else quality)


class SensorReadingWriter:
    def __init__(self, flush_interval_seconds=None, heartbeat_interval_seconds=None, spool=None):
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
        self.heartbeat_interval = (
            float(heartbeat_interval_seconds)
            if heartbeat_interval_seconds is not None
            else _parse_env_float("SENSOR_HEARTBEAT_INTERVAL", 3600.0)
        )

        # 來源存活判定窗口（秒）：心跳補寫與 always 條件需要「以目前時間」補一筆時，
        # 必須先確認資料來源在這段時間內還活著，否則斷線期間會一直補寫最後一個舊值。
        self.alive_window = _parse_env_float("SENSOR_ALIVE_WINDOW", 300.0)

        # sensor_id -> (value, sample_time, quality)　最新收到的值（尚未必然寫入）
        self._latest_values = {}
        # sensor_id -> datetime　最後一次確認「資料來源還活著」的時間（牆上時鐘）
        self._alive_at = {}
        # sensor_id -> (value, time, quality)　最後一次「實際寫入」（DB 或本機緩存）的值
        self._last_written = {}
        # sensor_id -> (value, time, quality)　待寫入的通訊中斷標記
        self._pending_markers = {}
        # sensor_id -> (upload_condition, upload_threshold)
        self._upload_config = {}

        self.spool = spool if spool is not None else ReadingSpool()
        self._has_quality_column = None
        self._quality_checked_at = 0.0

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._cache_loaded = False

        # 執行統計，供 services/status_reporter.py 回報到 service_status（網頁「系統狀態」）
        self._stats = {
            "last_flush_at": None,
            "last_flush_rows": 0,
            "total_rows": 0,
            "last_error": None,
            "last_error_at": None,
            "last_success_at": None,
            "spooled_total": 0,
            "drained_total": 0,
        }

    # ------------------------------------------------------------
    # quality 欄位偵測（sql/014 跑之前沒有這個欄位）
    # ------------------------------------------------------------
    def _check_quality_column(self, force=False) -> bool:
        if not force and self._has_quality_column is not None and \
                time.monotonic() - self._quality_checked_at < QUALITY_COLUMN_RECHECK_SECONDS:
            return self._has_quality_column
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = 'sensor_readings' AND column_name = 'quality');"
                    )
                    has = bool(cur.fetchone()[0])
            if has != self._has_quality_column and not has:
                logger.warning("⚠️ sensor_readings 沒有 quality 欄位（尚未執行 sql/014），暫時不記錄品質。")
            self._has_quality_column = has
            self._quality_checked_at = time.monotonic()
        except Exception:
            pass  # DB 連不上：沿用上一次的判斷
        return bool(self._has_quality_column)

    # ------------------------------------------------------------
    # 啟動時載入每個 sensor 目前資料庫裡最新一筆數值，
    # 當作 _last_written 的起始狀態（避免程式重啟後 on_change/threshold
    # 判斷從頭算，誤判「值變了」而重複寫入相同數值）
    # ------------------------------------------------------------
    def load_initial_cache(self):
        has_q = self._check_quality_column(force=True)
        query = f"""
            SELECT DISTINCT ON (sensor_id) sensor_id, value, reading_time,
                   {'quality' if has_q else 'NULL::smallint'}
            FROM sensor_readings
            ORDER BY sensor_id, reading_time DESC;
        """
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(query)
                    rows = cur.fetchall()
                    with self._lock:
                        for sensor_id, value, reading_time, quality in rows:
                            self._last_written[sensor_id] = (
                                float(value), reading_time, Q.GOOD if quality is None else quality,
                            )
                        self._cache_loaded = True
            logger.info(
                f"📥 SensorReadingWriter 快取初始化完成，"
                f"共載入 {len(self._last_written)} 個 sensor 的最新值。"
            )
        except Exception as e:
            logger.error(f"載入 sensor_readings 最新值快取失敗: {e}")

        pending = self.spool.count()
        if pending:
            logger.warning(f"📦 本機緩存中有 {pending:,} 筆尚未補寫的資料，將在寫入排程中依序補寫。")
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
            if getattr(self, "_config_error", False):
                logger.info("✅ sensors.upload_condition 設定恢復讀取")
            self._config_error = False
        except Exception as e:
            # 常見原因：DB 暫時連不上，或尚未執行 sql/006。沿用上一輪的設定。
            # 只在第一次失敗時印 ERROR，資料庫斷線期間不要每個寫入週期都洗一次版。
            if not getattr(self, "_config_error", False):
                logger.error(f"重新整理 sensors.upload_condition 設定失敗，沿用上一輪設定: {e}")
            self._config_error = True

    # ------------------------------------------------------------
    # 更新「最新值」快取：高頻呼叫，僅記憶體操作，沒有任何 DB I/O。
    # ------------------------------------------------------------
    def update_latest(self, sensor_id, value, reading_time=None, quality=Q.GOOD):
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

        wall_now = datetime.now().astimezone()
        now = reading_time or wall_now
        with self._lock:
            self._latest_values[sensor_id] = (num_value, now, quality)
            if quality >= Q.BAD:
                # 品質不良不算「來源存活」：心跳不會用不良值補寫
                self._alive_at.pop(sensor_id, None)
            else:
                self._alive_at[sensor_id] = wall_now

    # 向下相容：v1 的呼叫方式（各採集器既有程式碼）繼續可用
    def stage(self, sensor_id, value, reading_time=None, quality=Q.GOOD):
        self.update_latest(sensor_id, value, reading_time, quality)

    # ------------------------------------------------------------
    # 確認資料來源還活著，但數值沒有變化。
    # OPC UA 是「有變化才推播」，數值長時間不變的點位不會再呼叫 update_latest()，
    # 由訂閱服務在每次連線心跳成功時呼叫這支，讓心跳補寫知道「值沒變、但連線正常」。
    # ------------------------------------------------------------
    def confirm_alive(self, sensor_ids):
        wall_now = datetime.now().astimezone()
        with self._lock:
            for sensor_id in sensor_ids:
                latest = self._latest_values.get(sensor_id)
                if latest is not None and latest[2] < Q.BAD:
                    self._alive_at[sensor_id] = wall_now

    def mark_unavailable(self, sensor_ids):
        """
        資料來源確定斷線（OPC UA Server 連不上、Modbus 設備讀不到）時呼叫：
          1. 立即取消「存活」狀態：心跳不會再用舊值補寫，警報引擎也停止用舊值判斷
          2. 排入一筆 quality=COMM_LOST 的標記（每次斷線只會記一筆），
             讓歷史資料看得出斷線從什麼時候開始
        """
        wall_now = datetime.now().astimezone()
        with self._lock:
            for sensor_id in sensor_ids:
                if sensor_id is None:
                    continue
                self._alive_at.pop(sensor_id, None)
                latest = self._latest_values.get(sensor_id)
                if latest is None or latest[2] == Q.COMM_LOST:
                    continue
                self._latest_values[sensor_id] = (latest[0], latest[1], Q.COMM_LOST)
                self._pending_markers[sensor_id] = (latest[0], wall_now, Q.COMM_LOST)

    def snapshot_latest(self):
        """
        回傳目前記憶體中的最新值快照，給警報引擎使用（不碰資料庫、不必等寫入週期）。
        格式：{sensor_id: (value, sample_time, alive_at, quality)}
        """
        with self._lock:
            return {
                sid: (value, ts, self._alive_at.get(sid), quality)
                for sid, (value, ts, quality) in self._latest_values.items()
            }

    def get_stats(self):
        with self._lock:
            stats = dict(self._stats)
            stats["latest_count"] = len(self._latest_values)
        stats["flush_interval"] = self.flush_interval
        stats["heartbeat_interval"] = self.heartbeat_interval
        stats["quality_column"] = self._has_quality_column
        try:
            stats["spool_rows"] = self.spool.count()
            stats["spool_oldest"] = self.spool.oldest_time()
            stats["spool_dropped"] = self.spool.dropped_total
        except Exception as e:
            stats["spool_error"] = str(e)
        return stats

    # ------------------------------------------------------------
    # 執行一次「統一週期性寫入」：檢查目前所有有最新值的 sensor，
    # 依各自 upload_condition 決定要不要寫，批次 INSERT。
    # DB 寫入失敗時改存本機緩存；DB 正常時順便補寫緩存中的舊資料。
    # 可被背景排程自動呼叫，也可以手動呼叫立即觸發一次寫入。
    # ------------------------------------------------------------
    def flush(self):
        with self._lock:
            snapshot = dict(self._latest_values)
            upload_config = dict(self._upload_config)
            last_written = dict(self._last_written)
            alive_at = dict(self._alive_at)
            markers, self._pending_markers = self._pending_markers, {}

        wall_now = datetime.now().astimezone()
        rows_to_write = [(sid, ts, value, quality) for sid, (value, ts, quality) in markers.items()]
        for sensor_id, (value, ts, quality) in snapshot.items():
            if sensor_id in markers:
                continue  # 這一輪已經寫斷線標記
            condition, threshold = upload_config.get(sensor_id, ("always", None))
            write_time, reason = self._plan_write(
                condition, threshold, last_written.get(sensor_id), value, ts,
                wall_now=wall_now,
                alive_at=alive_at.get(sensor_id),
                heartbeat_interval=self.heartbeat_interval,
                alive_window=self.alive_window,
                quality=quality,
            )
            logger.debug(
                f"🧾 統一寫入判斷: sensor_id={sensor_id}, value={value}, quality={quality}, "
                f"要寫入={write_time is not None}（{reason}）"
            )
            if write_time is not None:
                row_quality = Q.HELD if (write_time != ts and quality == Q.GOOD) else quality
                rows_to_write.append((sensor_id, write_time, value, row_quality))

        with self._lock:
            self._stats["last_flush_at"] = wall_now
            self._stats["last_flush_rows"] = 0

        db_ok = True
        if rows_to_write:
            try:
                self._insert(rows_to_write)
                logger.info(
                    f"💾 [統一寫入] 本輪共 {len(snapshot)} 個感測器有最新值，"
                    f"依 upload_condition 判斷後寫入 {len(rows_to_write)} 筆到 sensor_readings。"
                )
                with self._lock:
                    self._stats["last_flush_rows"] = len(rows_to_write)
                    self._stats["total_rows"] += len(rows_to_write)
                    self._stats["last_success_at"] = wall_now
            except Exception as e:
                db_ok = False
                self._record_error(f"寫入 sensor_readings 失敗，改存本機緩存: {e}", wall_now)
                try:
                    self.spool.append(rows_to_write)
                    with self._lock:
                        self._stats["spooled_total"] += len(rows_to_write)
                    logger.warning(f"📦 已將 {len(rows_to_write)} 筆存入本機緩存，資料庫恢復後自動補寫。")
                except Exception as spool_err:
                    logger.error(f"❌ 本機緩存也寫入失敗，本輪 {len(rows_to_write)} 筆資料遺失: {spool_err}")

            # 不論寫進 DB 還是本機緩存，都視為已寫入（之後的判斷以這筆為基準）
            with self._lock:
                for sensor_id, ts, value, quality in rows_to_write:
                    self._last_written[sensor_id] = (value, ts, quality)

        if db_ok:
            self._drain_spool(wall_now)
        return len(rows_to_write) if db_ok else 0

    def _record_error(self, message, when):
        logger.error(message)
        with self._lock:
            self._stats["last_error"] = message[:300]
            self._stats["last_error_at"] = when

    def _insert(self, rows):
        """rows: [(sensor_id, time, value, quality)]"""
        if self._check_quality_column():
            query = """
                INSERT INTO sensor_readings (sensor_id, reading_time, value, quality)
                VALUES %s
                ON CONFLICT (sensor_id, reading_time) DO NOTHING;
            """
            data = rows
        else:
            query = """
                INSERT INTO sensor_readings (sensor_id, reading_time, value)
                VALUES %s
                ON CONFLICT (sensor_id, reading_time) DO NOTHING;
            """
            data = [(s, t, v) for s, t, v, _q in rows]
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                execute_values(cur, query, data, page_size=1000)

    def _drain_spool(self, wall_now):
        """資料庫正常時，把本機緩存的資料依寫入順序補寫回去。"""
        try:
            if not self.spool.count():
                return
        except Exception as e:
            logger.error(f"讀取本機緩存失敗: {e}")
            return
        drained = 0
        for _ in range(SPOOL_DRAIN_MAX_BATCHES):
            batch = self.spool.peek(SPOOL_DRAIN_BATCH)
            if not batch:
                break
            try:
                self._insert([(s, t, v, q) for _id, s, t, v, q in batch])
            except Exception as e:
                self._record_error(f"補寫本機緩存失敗，下一輪再試: {e}", wall_now)
                break
            self.spool.delete_up_to(batch[-1][0])
            drained += len(batch)
        if drained:
            remaining = self.spool.count()
            with self._lock:
                self._stats["drained_total"] += drained
            logger.info(
                f"📤 已從本機緩存補寫 {drained:,} 筆到 sensor_readings"
                + (f"，尚餘 {remaining:,} 筆" if remaining else "，緩存已清空")
            )

    @classmethod
    def _plan_write(
        cls, condition, threshold, last, value, ts,
        wall_now, alive_at=None, heartbeat_interval=0.0, alive_window=300.0, quality=Q.GOOD,
    ):
        """
        決定這個 sensor 這一輪「要不要寫、寫在哪個時間點」。
        回傳 (write_time | None, 原因說明)。

        與 _should_write_with() 的差別在於時間戳與品質的處理：
          - 數值依 upload_condition 判斷「有顯著變化」時，寫在樣本實際收到的時間 ts
          - always 條件、或心跳補寫時，如果「沒有新樣本」（OPC UA 數值不變就不會推播），
            但資料來源在 alive_window 秒內確認過還活著，就以目前時間 wall_now 補寫一筆
            —— 值沒變、連線正常，代表「此刻的值仍然是這個」，這筆紀錄是成立的
          - 來源沒有確認存活時不補寫，避免斷線期間一直重複寫最後一個舊值、把斷線掩蓋掉
          - 🆕 品質改變（例如 GOOD → UNCERTAIN、通訊中斷後恢復）時，有新樣本就一定寫
          - 🆕 品質不良（BAD）只寫「轉為不良」的那一筆，不良期間的值不持續寫入

        v2 原本的實作把心跳的「現在」當成 ts（最後收到樣本的時間），對 OPC UA 數值
        長時間不變的點位來說 ts 永遠不會前進，心跳因此永遠不會觸發。
        """
        if last is None:
            return ts, "首次出現"

        last_value, last_time, last_quality = _last_parts(last)
        try:
            is_new_sample = last_time is None or ts > last_time
        except TypeError:  # naive / aware 混用
            is_new_sample = True

        if quality >= Q.BAD:
            if is_new_sample and last_quality != quality:
                return ts, "品質轉為不良，記錄轉變點"
            return None, "品質不良期間不持續寫入"
        if is_new_sample and last_quality != quality:
            return ts, f"品質改變 {last_quality} -> {quality}"

        alive = False
        if alive_at is not None:
            try:
                alive = (wall_now - alive_at).total_seconds() <= alive_window
            except TypeError:
                alive = False

        def _after_last(t):
            try:
                return last_time is None or t > last_time
            except TypeError:
                return True

        if condition == "always":
            if is_new_sample:
                return ts, "always（新樣本）"
            if alive and _after_last(wall_now):
                return wall_now, "always（數值未變、來源存活，以目前時間補寫）"
            return None, "always，但沒有新樣本且來源未確認存活"

        write_it, reason = cls._should_write_with(
            condition, threshold, last, value, now=None, heartbeat_interval=0
        )
        if write_it and is_new_sample:
            return ts, reason

        if heartbeat_interval and heartbeat_interval > 0 and last_time is not None:
            try:
                elapsed = (wall_now - last_time).total_seconds()
            except TypeError:
                elapsed = None
            if elapsed is not None and elapsed >= heartbeat_interval:
                if alive:
                    return wall_now, (
                        f"心跳補寫（距上次寫入 {elapsed:.0f} 秒 >= {heartbeat_interval:.0f} 秒，來源存活）"
                    )
                if is_new_sample:
                    return ts, (
                        f"心跳補寫（距上次寫入 {elapsed:.0f} 秒，來源未確認存活，以最後樣本時間寫入）"
                    )
                return None, "已達心跳間隔，但來源未確認存活且沒有新樣本，不補寫（避免掩蓋斷線）"

        return None, reason

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

        last_value, last_time, _ = _last_parts(last)

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
            + f"，本機緩存 = {self.spool.path}"
        )
        while not self._stop_event.is_set():
            # 每一輪先重新整理 upload_condition 設定，
            # 讓網頁上剛改的設定最慢下一輪就生效，不需要重啟服務
            self._refresh_upload_config()
            try:
                self.flush()
            except Exception as e:
                logger.error(f"統一寫入排程執行例外: {e}", exc_info=True)
            self._stop_event.wait(self.flush_interval)

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
        # （DB 連不上時會進本機緩存，下次啟動補寫）
        try:
            self.flush()
        except Exception:
            pass
        self.spool.close()
        logger.info("👋 SensorReadingWriter 統一寫入排程已停止。")


# 跨模組共用的單例：main.py 與各 collector / OPC UA 訂閱服務都 import 這個
# 同一個實例，所有協議的最新值最終由同一個背景排程統一寫入。
sensor_reading_writer = SensorReadingWriter()
