"""
services/opcua_subscription_service.py
========================================
OPC UA 訂閱推播服務（取代週期性 browse 輪詢）。

🆕 v2 升級重點（聚焦強化 OPC UA）：
    1. 逐感測器訂閱頻率：sensors.opcua_sampling_interval_ms 可以覆寫個別點位
       的取樣頻率。同一個 Server 底下，取樣頻率相同的點位會被歸進同一組
       Subscription（OPC UA 的 publishing interval 是 Subscription 層級的
       屬性，不是單一 MonitoredItem 可以獨立設定的），沒有設定覆寫值的點位
       則沿用 Server 層級預設頻率（opcua_servers.publish_interval_ms，或
       .env 的 OPCUA_PUBLISH_INTERVAL_MS）。
    2. sensor_readings 的寫入方式改變：本服務不再自己觸發 sensor_readings
       的 DB 寫入，只把最新值丟進 SensorReadingWriter 的記憶體快取
       （update_latest）。實際寫入時機統一交給 SensorReadingWriter 背景
       執行緒依 .env 的 SENSOR_READING_FLUSH_INTERVAL 週期處理（見
       data_layer/timeseries_writer.py），三個協議（Modbus/TIA/OPC UA）
       共用同一套排程。
    3. 維護迴圈精簡：原本的「檢查 resubscribe 旗標」與「定期刷新 sensor_id
       綁定」兩個背景 task，合併成單一 _watch_and_maintain()。除了使用者
       手動觸發的「立即瀏覽」，只要感測器綁定或個別訂閱頻率設定有變化，
       也會在下一個維護週期內被自動偵測到並重建訂閱，不需要使用者手動觸發。
       重建方式為整批重新分組建立（而非逐點位 diff），用較低的重建效率
       換取「依訂閱頻率分組」這個功能在邊界情況下的行為單純、好排查。

執行模型：
    長駐服務，跑在獨立的背景執行緒 + 專屬 asyncio event loop 中，
    跟 main.py 主迴圈（Modbus/TIA 併發採集）完全脫鉤、互不阻塞。

已知限制：
    - 點位被移除後，opcua_tags 裡的舊資料列不會自動刪除，只會停止訂閱。
    - 感測器的 opcua_sampling_interval_ms 變更後，最慢在下一個
      MAINTENANCE_POLL_INTERVAL 週期內生效（預設 5 秒），不需要重啟。
"""

import asyncio
import logging
import os
import threading
import time

from data_layer.db_connector import DatabaseConnector
from data_layer.batch_updater import (
    batch_update_opcua_tags,
    batch_update_opcua_values,
    set_opcua_bound_tags_state,
)
from data_layer import quality as Q
from data_layer.timeseries_writer import sensor_reading_writer
from protocols.opcua_protocol import connect_client, browse_recursive, OPCUAConnectionError
from collector.run_opcua_collector import load_opcua_servers, update_server_status

logger = logging.getLogger("opcua_subscription_service")

# 壓低 asyncua 套件內部 logger 等級（詳見原始設計說明：Publish Response 原始內容
# 會用自己的 logger 印出、不受 root logger 限制，頻率高到會淹沒有用的 log）
logging.getLogger("asyncua").setLevel(logging.WARNING)


class _DuplicateLogFilter(logging.Filter):
    """
    🆕 抑制短時間內重複出現的相同訊息。

    網路品質不穩定的場域，asyncua 內部的背景 Publish Loop 偵測到斷線時，
    會每 1 秒印一次「Publish iteration crashed; retrying in 1s」，這是它
    自己的重試機制，跟我們自己的斷線重連邏輯是分開的兩件事：我們最快也要等
    下一次心跳檢查才會發現斷線並開始重連，這段等待期間 asyncua 會不斷重複
    印出同一則訊息洗版，蓋掉真正有用的 log。
    這裡不是把整個 asyncua logger 完全關掉（等級仍是 WARNING，其他不同內容
    的警告/錯誤照樣看得到），只針對「同一則訊息」在 suppress_window 秒內只印
    第一次，之後重複出現就丟棄，等訊息內容改變或超過時間窗才會再印一次。
    """

    def __init__(self, suppress_window: float = 10.0):
        super().__init__()
        self._last_seen = {}
        self._suppress_window = suppress_window

    def filter(self, record: logging.LogRecord) -> bool:
        now = time.monotonic()
        key = (record.name, record.getMessage())
        last = self._last_seen.get(key)
        if last is not None and (now - last) < self._suppress_window:
            return False
        self._last_seen[key] = now
        return True


logging.getLogger("asyncua").addFilter(_DuplicateLogFilter(suppress_window=10.0))

# ----------------------------------------------------------------
# 可調參數
# ----------------------------------------------------------------
FLUSH_INTERVAL_SECONDS = 2.0          # opcua_tags 即時值 + 最新值快取的更新週期
MAINTENANCE_POLL_INTERVAL = 5.0       # 統一維護迴圈週期：檢查 resubscribe 旗標 /
                                       # sensor_id 綁定 / 訂閱頻率設定是否有變化

# 🆕 心跳容忍度：全部改由 .env 控制，方便針對網路品質不穩的場域個別調整，
# 不需要改程式碼重新部署。
#   OPCUA_HEARTBEAT_INTERVAL_SEC   - 心跳檢查週期（秒）
#   OPCUA_HEARTBEAT_TIMEOUT_SEC    - 單次心跳讀取的逾時秒數（避免網路卡住時
#                                     一直傻等，逾時就視為這一次心跳失敗）
#   OPCUA_HEARTBEAT_MAX_FAILURES   - 連續失敗幾次才真的判定斷線、觸發完整
#                                     斷線重連流程；設为 1 等同舊版行為
#                                     （一次失敗就重連）。網路本來就不穩的
#                                     場域可以調高這個值，容忍偶發的心跳
#                                     逾時，不要動不動就整個重建連線/訂閱。
def _get_env_float(key: str, default: float) -> float:
    raw = os.getenv(key, str(default)).split("#")[0].strip()
    try:
        return float(raw)
    except ValueError:
        return float(default)


def _get_env_int(key: str, default: int) -> int:
    raw = os.getenv(key, str(default)).split("#")[0].strip()
    try:
        return int(raw)
    except ValueError:
        return int(default)


HEARTBEAT_INTERVAL = _get_env_float("OPCUA_HEARTBEAT_INTERVAL_SEC", 15.0)
HEARTBEAT_READ_TIMEOUT = _get_env_float("OPCUA_HEARTBEAT_TIMEOUT_SEC", 8.0)
HEARTBEAT_MAX_FAILURES = _get_env_int("OPCUA_HEARTBEAT_MAX_FAILURES", 2)

try:
    DEFAULT_PUBLISH_INTERVAL_MS = float(
        os.getenv("OPCUA_PUBLISH_INTERVAL_MS", "1000").split('#')[0].strip()
    )
except ValueError:
    DEFAULT_PUBLISH_INTERVAL_MS = 1000.0

RECONNECT_BASE_DELAY = 5.0
RECONNECT_MAX_DELAY = 60.0
SESSION_LIMIT_RETRY_DELAY = 120.0

# 🆕 各 Server 的執行統計（server_id -> dict），由 services/status_reporter.py
# 定期讀取後寫進 service_status，網頁「系統狀態」頁面顯示。
# 只在本服務的 event loop 內寫入，讀取端只做淺複製，不需要加鎖。
SERVER_STATS: dict = {}


def _stats(server: dict) -> dict:
    stats = SERVER_STATS.setdefault(server["id"], {
        "server_name": server["server_name"],
        "state": "CONNECTING",
        "connected_since": None,
        "monitored_items": 0,
        "subscriptions": 0,
        "bad_quality_items": 0,
        "last_data_at": None,
        "last_heartbeat_ok": None,
        "reconnect_count": 0,
        "last_error": None,
    })
    stats["server_name"] = server["server_name"]
    return stats


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def get_server_stats() -> dict:
    return {sid: dict(v) for sid, v in SERVER_STATS.items()}



# ----------------------------------------------------------------
# 資料緩衝區：asyncio 為單執行緒協作式排程，
# 只要 update()/pop_all() 之間沒有 await 中斷，就不需要額外加鎖。
# ----------------------------------------------------------------
class _ChangeBuffer:
    def __init__(self):
        self._data = {}

    def update(self, node_id, value, quality):
        self._data[node_id] = (value, quality)

    def pop_all(self):
        data, self._data = self._data, {}
        return data

    def __bool__(self):
        return bool(self._data)


class _DataChangeHandler:
    """asyncua SubHandler：收到資料變化通知時只寫進緩衝區，不做任何 DB I/O"""

    def __init__(self, buffer: _ChangeBuffer, server_name: str = ""):
        self._buffer = buffer
        self._server_name = server_name
        self._logged_quality_warning = False

    def datachange_notification(self, node, val, data):
        try:
            node_id = node.nodeid.to_string()
        except Exception as e:
            logger.error(f"❌ [訂閱服務] Server [{self._server_name}] 無法取得節點 NodeId: {e}")
            return

        quality = "GOOD"
        try:
            status = data.monitored_item.Value.StatusCode
            if status.is_good():
                quality = "GOOD"
            elif status.is_uncertain():
                quality = "UNCERTAIN"
            else:
                quality = "BAD"
        except Exception as e:
            if not self._logged_quality_warning:
                logger.warning(
                    f"⚠️ [訂閱服務] Server [{self._server_name}] 節點 {node_id} "
                    f"無法解析品質狀態（StatusCode），數值仍會照常記錄，quality 先預設為 GOOD: {e}"
                )
                self._logged_quality_warning = True

        self._buffer.update(node_id, val, quality)
        logger.debug(
            f"[訂閱服務] Server [{self._server_name}] 收到節點 {node_id} 資料變化 -> {val}"
        )

    def event_notification(self, event):
        pass


# ----------------------------------------------------------------
# DB 存取（同步函式，皆透過 asyncio.to_thread 呼叫，避免阻塞 event loop）
# ----------------------------------------------------------------
def _split_subscribable(cached_tags):
    """
    🆕 只有已綁定 sensor_id 的點位才需要建立 OPC UA 訂閱：
    沒綁定的點位反正不會寫進 sensor_readings，繼續訂閱只是白白消耗頻寬
    跟這台 Server 的處理資源，對網路品質不穩的場域尤其有感。
    未綁定的點位仍然存在於 opcua_tags（供網頁瀏覽/挑選要綁定哪個），
    只是不會再透過訂閱持續更新數值，停留在上次瀏覽當下的快照，
    直到使用者綁定它或再次手動瀏覽。
    """
    return [t for t in cached_tags if t.get("sensor_id") is not None]


def _load_cached_tags(server_id):
    """從 opcua_tags 讀取此 Server 目前已知的點位清單（含 sensor_id 綁定）"""
    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT node_id, browse_name, display_name, data_type, sensor_id "
                    "FROM opcua_tags WHERE server_id = %s;",
                    (server_id,),
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"讀取快取點位清單失敗 (server_id={server_id}): {e}")
        return []


def _check_and_clear_resubscribe_flag(server_id):
    """檢查該 Server 是否有「重新整理點位表」請求，若有則清除旗標並回傳 True"""
    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT resubscribe_requested FROM opcua_servers WHERE id = %s FOR UPDATE;",
                    (server_id,),
                )
                row = cur.fetchone()
                if not row or not row[0]:
                    return False
                cur.execute(
                    "UPDATE opcua_servers SET resubscribe_requested = FALSE WHERE id = %s;",
                    (server_id,),
                )
                return True
    except Exception as e:
        logger.error(f"檢查重新訂閱旗標失敗 (server_id={server_id}): {e}")
        return False


def _load_sensor_sampling_intervals(sensor_ids):
    """
    🆕 依 sensor_id 清單查詢 sensors.opcua_sampling_interval_ms，
    用來決定每個點位要被分進哪一組訂閱（相同取樣頻率 = 同一個 Subscription）。
    回傳 dict: sensor_id -> interval_ms(int) | None
    """
    if not sensor_ids:
        return {}
    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT sensor_id, opcua_sampling_interval_ms FROM sensors "
                    "WHERE sensor_id = ANY(%s);",
                    (list(sensor_ids),),
                )
                return {row[0]: row[1] for row in cur.fetchall()}
    except Exception as e:
        # 常見原因：尚未執行 sql/006_opcua_upgrade.sql，欄位還不存在，
        # 這裡退回「全部沿用預設頻率」，不讓服務整個掛掉。
        logger.error(
            f"讀取 sensors.opcua_sampling_interval_ms 失敗（若尚未執行 "
            f"sql/006_opcua_upgrade.sql 會出現此錯誤）: {e}"
        )
        return {}


def _build_node_interval_map(cached_tags, default_interval_ms):
    """
    依每個點位是否綁定感測器、該感測器是否設定專屬取樣頻率，
    決定這個 node_id 要用哪個 publishing interval（毫秒）。
    沒綁定感測器、或感測器未設定 opcua_sampling_interval_ms -> 用 Server 層級預設值。
    """
    sensor_ids = {t["sensor_id"] for t in cached_tags if t.get("sensor_id") is not None}
    sensor_intervals = _load_sensor_sampling_intervals(sensor_ids)

    node_interval = {}
    for t in cached_tags:
        sid = t.get("sensor_id")
        override = sensor_intervals.get(sid) if sid is not None else None
        node_interval[t["node_id"]] = int(override) if override else int(default_interval_ms)
    return node_interval


def _group_nodes_by_interval(node_interval_map):
    """把 node_id 依 publishing interval 分組: {interval_ms: [node_id, ...]}"""
    groups = {}
    for node_id, interval in node_interval_map.items():
        groups.setdefault(interval, []).append(node_id)
    return groups


def _load_sensor_deadband_config(sensor_ids):
    """
    🆕 依 sensor_id 清單查詢 sensors.opcua_deadband_type / opcua_deadband_value，
    用來決定要不要在「伺服器端」對這個點位套用 DataChangeFilter（deadband），
    減少不必要的通知透過網路送過來（跟 upload_condition 是不同層級的過濾，
    upload_condition 決定「收到之後要不要寫進 sensor_readings」，這裡決定
    「伺服器要不要把這筆變化送過來」）。
    回傳 dict: sensor_id -> (deadband_type: str, deadband_value: float|None)
    """
    if not sensor_ids:
        return {}
    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT sensor_id, opcua_deadband_type, opcua_deadband_value "
                    "FROM sensors WHERE sensor_id = ANY(%s);",
                    (list(sensor_ids),),
                )
                return {
                    row[0]: ((row[1] or "none").lower(), float(row[2]) if row[2] is not None else None)
                    for row in cur.fetchall()
                }
    except Exception as e:
        # 常見原因：尚未執行 sql/007_opcua_deadband.sql，欄位還不存在，
        # 這裡退回「全部不套用 deadband」，不讓服務整個掛掉。
        logger.error(
            f"讀取 sensors.opcua_deadband_type/value 失敗（若尚未執行 "
            f"sql/007_opcua_deadband.sql 會出現此錯誤）: {e}"
        )
        return {}


def _build_node_deadband_map(cached_tags):
    """
    依每個點位是否綁定感測器、該感測器是否設定 deadband，決定這個 node_id
    要用哪組 (deadband_type, deadband_value)。沒綁定感測器、或未設定
    -> ('none', None)，代表不套用 deadband（伺服器只要有變化就送）。
    """
    sensor_ids = {t["sensor_id"] for t in cached_tags if t.get("sensor_id") is not None}
    sensor_deadbands = _load_sensor_deadband_config(sensor_ids)

    node_deadband = {}
    for t in cached_tags:
        sid = t.get("sensor_id")
        node_deadband[t["node_id"]] = (
            sensor_deadbands.get(sid, ("none", None)) if sid is not None else ("none", None)
        )
    return node_deadband


# 設計註記：伺服器端 deadband 是透過下面 _create_subscriptions_for_tags() 呼叫
# asyncua 內建的 Subscription.deadband_monitor() 建立，該方法內部會自行組出
# ua.DataChangeFilter（Trigger=StatusValue，DeadbandType=1(absolute)/2(percent)，
# DeadbandValue=門檻值），語意與 OPC UA Part 8 標準一致，不需要我們自己再組一次
# filter 物件。
#
# ⚠️ Percent deadband 是依 OPC UA 標準的 EURange（節點的工程量測範圍）計算，
# 節點若沒有設定 EURange，多數伺服器會忽略此設定或視為無效；不確定設備是否有
# 配置 EURange 時，建議優先用 absolute。


def _flush_to_db(server_id, pending, node_to_sensor, bad_nodes=None):
    rows = [
        (node_id, {"val": value}, quality, "ONLINE")
        for node_id, (value, quality) in pending.items()
    ]
    batch_update_opcua_values(server_id, rows)

    # 🔧 v2：這裡只更新「最新值」快取，不再觸發 sensor_readings 的 DB 寫入。
    # 實際寫入時機統一交給 SensorReadingWriter 背景執行緒依
    # SENSOR_READING_FLUSH_INTERVAL 週期處理（三協議共用同一套排程）。
    for node_id, (value, quality) in pending.items():
        # 品質一併交給寫入排程（v3.1）：BAD 只記錄「轉為不良」那一筆，不算來源存活、
        # 不參與警報判斷；UNCERTAIN 照常寫入但標記品質。
        code = _QUALITY_CODES.get(quality, Q.BAD)
        if bad_nodes is not None:
            if code >= Q.BAD:
                bad_nodes.add(node_id)
            else:
                bad_nodes.discard(node_id)
        sensor_id = node_to_sensor.get(node_id)
        if sensor_id is not None:
            sensor_reading_writer.update_latest(sensor_id, value, quality=code)


_QUALITY_CODES = {"GOOD": Q.GOOD, "UNCERTAIN": Q.UNCERTAIN, "BAD": Q.BAD}


def _confirm_bound_sensors_alive(state: dict):
    """心跳成功 = 連線與訂閱都正常，數值沒推播只是因為沒變化。"""
    bad_nodes = state["bad_nodes"]
    sensor_reading_writer.confirm_alive(
        sid for node_id, sid in state["node_to_sensor"].items()
        if sid is not None and node_id not in bad_nodes
    )


# ----------------------------------------------------------------
# 建立訂閱：依取樣頻率分組，各組各自 create_subscription()
# ----------------------------------------------------------------
async def _browse_with_existing_client(client, server: dict) -> list:
    """用【已經連線好的 client】直接瀏覽，避免另開連線觸發 Session 數上限問題"""
    root_node_id = server.get("root_node_id") or "i=85"
    max_depth = server.get("browse_depth") or 5
    root_node = client.get_node(root_node_id)
    return await browse_recursive(root_node, max_depth)


async def _create_subscriptions_for_tags(client, server: dict, cached_tags: list, handler):
    """
    依每個點位解析出的 publishing interval 分組，各組分別 create_subscription()；
    同一個 handler 在所有 Subscription 間共用（handler 只是把資料丟進共用的
    buffer，不需要區分來源 Subscription）。

    🆕 同一個 interval 分組內，再依 deadband 設定（none/percent/absolute + 門檻值）
    細分子群組：deadband 是 MonitoredItem 層級的屬性、不是 Subscription 層級的，
    所以同一個 Subscription 底下可以混合不同 deadband 設定的點位，不需要為了
    deadband 另外多開 Subscription。
      - deadband_type = 'none' 的點位：用 subscribe_data_change() 建立監控項目
      - deadband_type = 'percent' / 'absolute' 的點位：用 asyncua 內建的
        Subscription.deadband_monitor() 建立監控項目，讓伺服器端就依設定的
        門檻過濾，減少不必要的通知透過網路送過來
        （asyncua 的 deadbandtype 慣例：1=absolute, 2=percent）

    回傳:
        subscriptions: list[Subscription]，供之後統一 delete()
        handle_map: dict node_id -> (subscription, monitored_item_handle)
        node_to_sensor: dict node_id -> sensor_id | None
        node_interval_map: dict node_id -> publishing interval(ms)
        node_deadband_map: dict node_id -> (deadband_type, deadband_value)
    """
    default_interval = server.get("publish_interval_ms") or DEFAULT_PUBLISH_INTERVAL_MS
    node_interval_map = await asyncio.to_thread(
        _build_node_interval_map, cached_tags, default_interval
    )
    node_deadband_map = await asyncio.to_thread(_build_node_deadband_map, cached_tags)
    groups = _group_nodes_by_interval(node_interval_map)

    subscriptions = []
    handle_map = {}
    node_to_sensor = {t["node_id"]: t.get("sensor_id") for t in cached_tags}
    server_name = server["server_name"]

    for interval_ms, node_ids in groups.items():
        subscription = await client.create_subscription(interval_ms, handler)
        subscriptions.append(subscription)

        # 依 deadband 設定把這一組再細分：(deadband_type, deadband_value) 相同的
        # 點位一起建立監控項目。
        deadband_subgroups = {}
        for node_id in node_ids:
            key = node_deadband_map.get(node_id, ("none", None))
            deadband_subgroups.setdefault(key, []).append(node_id)

        deadband_summary = []
        for (deadband_type, deadband_value), sub_node_ids in deadband_subgroups.items():
            nodes = [client.get_node(n) for n in sub_node_ids]

            if deadband_type in ("percent", "absolute") and deadband_value is not None:
                deadbandtype_code = 2 if deadband_type == "percent" else 1
                handles = await subscription.deadband_monitor(
                    nodes, deadband_val=float(deadband_value), deadbandtype=deadbandtype_code
                )
            else:
                handles = await subscription.subscribe_data_change(nodes)

            if not isinstance(handles, list):
                handles = [handles]
            for node_id, h in zip(sub_node_ids, handles):
                handle_map[node_id] = (subscription, h)

            deadband_summary.append(
                f"{deadband_type}"
                + (f"={deadband_value}" if deadband_value is not None else "")
                + f"×{len(sub_node_ids)}"
            )

        logger.info(
            f"📡 [訂閱服務] Server [{server_name}] 建立取樣頻率 {interval_ms}ms 的訂閱，"
            f"監控 {len(node_ids)} 個點位（deadband 分組: {', '.join(deadband_summary)}）"
        )

    return subscriptions, handle_map, node_to_sensor, node_interval_map, node_deadband_map


async def _delete_all_subscriptions(subscriptions):
    for sub in subscriptions:
        try:
            await sub.delete()
        except Exception:
            pass


# ----------------------------------------------------------------
# 背景 async task
# ----------------------------------------------------------------
async def _flush_loop(buffer: _ChangeBuffer, server_id, server_name: str, state: dict, stop_event: asyncio.Event):
    while not stop_event.is_set():
        await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
        pending = buffer.pop_all()
        if pending:
            try:
                await asyncio.to_thread(
                    _flush_to_db, server_id, pending, state["node_to_sensor"], state["bad_nodes"]
                )
                stats = SERVER_STATS.get(server_id)
                if stats is not None:
                    stats["last_data_at"] = _now_iso()
                    stats["bad_quality_items"] = len(state["bad_nodes"])
            except Exception as e:
                logger.error(
                    f"❌ [訂閱服務] Server [{server_name}] 更新即時值失敗: {e}",
                    exc_info=True,
                )


async def _watch_and_maintain(client, server: dict, state: dict, stop_event: asyncio.Event):
    """
    🆕 統一維護迴圈：合併原本的「檢查 resubscribe 旗標」與「定期刷新 sensor_id
    綁定」兩個背景 task。每 MAINTENANCE_POLL_INTERVAL 秒檢查一次：
        1. 使用者是否按下「立即瀏覽」（resubscribe_requested 旗標）
           -> 重新 browse 該 Server，更新 opcua_tags
        2. 不論有沒有按「立即瀏覽」，只要目前的點位表 + sensor_id 綁定 +
           個別訂閱頻率設定跟上一輪解析出來的結果不同，就重建全部訂閱
           （整批重建，換取「依訂閱頻率分組」這個功能在邊界情況下行為單純）。
    """
    server_id = server["id"]
    server_name = server["server_name"]

    while not stop_event.is_set():
        await asyncio.sleep(MAINTENANCE_POLL_INTERVAL)

        explicit_request = await asyncio.to_thread(_check_and_clear_resubscribe_flag, server_id)

        try:
            if explicit_request:
                logger.info(f"🔄 [訂閱服務] 收到 Server [{server_name}] 重新整理點位表請求，開始重新瀏覽...")
                tags = await _browse_with_existing_client(client, server)
                if tags:
                    await asyncio.to_thread(batch_update_opcua_tags, server_id, server_name, tags)

            cached_tags = await asyncio.to_thread(_load_cached_tags, server_id)
            if not cached_tags:
                continue

            subscribable_tags = _split_subscribable(cached_tags)

            default_interval = server.get("publish_interval_ms") or DEFAULT_PUBLISH_INTERVAL_MS
            new_interval_map = await asyncio.to_thread(
                _build_node_interval_map, subscribable_tags, default_interval
            )
            new_deadband_map = await asyncio.to_thread(_build_node_deadband_map, subscribable_tags)
            new_node_to_sensor = {t["node_id"]: t.get("sensor_id") for t in subscribable_tags}

            structure_changed = (
                explicit_request
                or new_interval_map != state["node_interval_map"]
                or new_deadband_map != state["node_deadband_map"]
                or new_node_to_sensor != state["node_to_sensor"]
            )
            if not structure_changed:
                continue

            logger.info(
                f"🔧 [訂閱服務] Server [{server_name}] 偵測到點位表 / 感測器綁定 / "
                "取樣頻率 / deadband 設定變更，重建訂閱中..."
            )

            old_subscriptions = state["subscriptions"]
            (
                new_subscriptions,
                new_handle_map,
                updated_node_to_sensor,
                updated_interval_map,
                updated_deadband_map,
            ) = await _create_subscriptions_for_tags(client, server, subscribable_tags, state["handler"])

            # 先切換狀態，再刪除舊訂閱，避免切換過程中 flush_loop 讀到不一致的 node_to_sensor
            state["subscriptions"] = new_subscriptions
            state["handle_map"] = new_handle_map
            state["node_to_sensor"] = updated_node_to_sensor
            state["node_interval_map"] = updated_interval_map
            state["node_deadband_map"] = updated_deadband_map
            state["heartbeat_node_id"] = cached_tags[0]["node_id"]

            await _delete_all_subscriptions(old_subscriptions)

            logger.info(
                f"✅ [訂閱服務] Server [{server_name}] 訂閱重建完成，共發現 {len(cached_tags)} 個點位，"
                f"其中 {len(subscribable_tags)} 個已綁定感測器並建立訂閱，"
                f"目前共監控 {len(new_handle_map)} 個點位，分成 {len(new_subscriptions)} 組取樣頻率"
            )

        except Exception as e:
            logger.error(f"❌ [訂閱服務] Server [{server_name}] 維護迴圈執行例外: {e}", exc_info=True)


# ----------------------------------------------------------------
# 單一 Server 的完整生命週期
# ----------------------------------------------------------------
async def _run_server_subscription(server: dict, stop_event: asyncio.Event):
    server_id = server["id"]
    server_name = server["server_name"]
    reconnect_delay = RECONNECT_BASE_DELAY

    while not stop_event.is_set():
        client = None
        buffer = _ChangeBuffer()
        handler = _DataChangeHandler(buffer, server_name)
        state = {
            "subscriptions": [],
            "handle_map": {},
            "node_to_sensor": {},
            "node_interval_map": {},
            "node_deadband_map": {},
            "heartbeat_node_id": "i=2258",
            "handler": handler,
            "bad_nodes": set(),
        }
        stats = _stats(server)
        stats["state"] = "CONNECTING"
        flush_task = None
        maintain_task = None
        session_limit_hit = False

        try:
            client = await connect_client(server)
            logger.info(f"✅ [訂閱服務] 已連線至 OPC UA Server [{server_name}]")
            await asyncio.to_thread(update_server_status, server_id, "ONLINE")
            reconnect_delay = RECONNECT_BASE_DELAY

            cached_tags = await asyncio.to_thread(_load_cached_tags, server_id)
            if not cached_tags:
                logger.info(f"🔍 [訂閱服務] Server [{server_name}] 尚無快取點位，執行首次完整瀏覽...")
                tags = await _browse_with_existing_client(client, server)
                if tags:
                    await asyncio.to_thread(batch_update_opcua_tags, server_id, server_name, tags)
                cached_tags = await asyncio.to_thread(_load_cached_tags, server_id)

            if not cached_tags:
                logger.warning(
                    f"⚠️ [訂閱服務] Server [{server_name}] 瀏覽後仍無任何點位，"
                    f"{RECONNECT_BASE_DELAY:.0f} 秒後重試..."
                )
                await client.disconnect()
                await asyncio.sleep(RECONNECT_BASE_DELAY)
                continue

            # 🆕 只有已綁定 sensor_id 的點位才建立訂閱，未綁定的點位不佔訂閱資源
            # （仍然存在於 opcua_tags，供網頁瀏覽/挑選要綁定哪個，只是不會透過
            # 訂閱持續更新，停留在上次瀏覽當下的快照）
            subscribable_tags = _split_subscribable(cached_tags)
            if not subscribable_tags:
                logger.info(
                    f"ℹ️ [訂閱服務] Server [{server_name}] 共發現 {len(cached_tags)} 個點位，"
                    "但目前沒有任何一個綁定感測器，暫不建立訂閱（僅維持連線與心跳）。"
                    "到「感測器階層管理」綁定後，最慢 5 秒內會自動建立訂閱。"
                )

            subscriptions, handle_map, node_to_sensor, node_interval_map, node_deadband_map = (
                await _create_subscriptions_for_tags(client, server, subscribable_tags, handler)
            )
            state["subscriptions"] = subscriptions
            state["handle_map"] = handle_map
            state["node_to_sensor"] = node_to_sensor
            state["node_interval_map"] = node_interval_map
            state["node_deadband_map"] = node_deadband_map
            state["heartbeat_node_id"] = cached_tags[0]["node_id"]
            stats.update(
                state="ONLINE",
                connected_since=_now_iso(),
                monitored_items=len(handle_map),
                subscriptions=len(subscriptions),
                last_error=None,
            )

            logger.info(
                f"📡 [訂閱服務] Server [{server_name}] 訂閱建立完成，"
                f"共發現 {len(cached_tags)} 個點位，其中 {len(subscribable_tags)} 個已綁定感測器並建立訂閱，"
                f"共監控 {len(handle_map)} 個點位（{len(subscriptions)} 組取樣頻率）"
            )

            # 🔧 訂閱建立成功 = 這些點位重新回到「有在監控」的狀態，明確標回 ONLINE。
            # 不能只依賴 _flush_to_db（它只會更新「這一輪有收到變化」的點位）：
            # 斷線重連後若數值剛好都沒變，點位會一直停在 OFFLINE 造成假警報。
            if subscribable_tags:
                await asyncio.to_thread(set_opcua_bound_tags_state, server_id, "ONLINE")

            flush_task = asyncio.create_task(
                _flush_loop(buffer, server_id, server_name, state, stop_event)
            )
            maintain_task = asyncio.create_task(
                _watch_and_maintain(client, server, state, stop_event)
            )

            # 心跳迴圈：優先用已訂閱成功的實際點位（不使用系統節點 i=2259，
            # 因為並非所有 PLC 內建簡化版 OPC UA Server 都完整實作該子節點）。
            # 🆕 容忍偶發逾時/失敗：網路品質不穩的場域，單次心跳讀取失敗不代表
            # 真的斷線，可能只是這一次回應比較慢。連續失敗達到
            # HEARTBEAT_MAX_FAILURES 次才視為真正斷線、丟出例外交給外層走
            # 完整的斷線重連流程；未達門檻則記一筆 warning 後，等下一次心跳
            # 週期再試，不會整組連線/訂閱重建。
            consecutive_heartbeat_failures = 0
            while not stop_event.is_set():
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                try:
                    await asyncio.wait_for(
                        client.get_node(state["heartbeat_node_id"]).read_value(),
                        timeout=HEARTBEAT_READ_TIMEOUT,
                    )
                    consecutive_heartbeat_failures = 0
                    _confirm_bound_sensors_alive(state)
                    stats["last_heartbeat_ok"] = _now_iso()
                    stats["monitored_items"] = len(state["handle_map"])
                    stats["subscriptions"] = len(state["subscriptions"])
                except Exception as heartbeat_err:
                    consecutive_heartbeat_failures += 1
                    if consecutive_heartbeat_failures < HEARTBEAT_MAX_FAILURES:
                        logger.warning(
                            f"⚠️ [訂閱服務] Server [{server_name}] 心跳讀取失敗"
                            f"（第 {consecutive_heartbeat_failures}/{HEARTBEAT_MAX_FAILURES} 次，"
                            f"未達斷線判定門檻，先容忍並等待下一次心跳）: {heartbeat_err}"
                        )
                        continue
                    logger.error(
                        f"❌ [訂閱服務] Server [{server_name}] 心跳連續失敗 "
                        f"{consecutive_heartbeat_failures} 次，判定為斷線"
                    )
                    raise

        except Exception as e:
            error_str = str(e)
            logger.error(f"❌ [訂閱服務] Server [{server_name}] 連線/訂閱發生例外: {e}")
            stats.update(
                state="OFFLINE",
                last_error=error_str[:300],
                reconnect_count=stats["reconnect_count"] + 1,
            )
            await asyncio.to_thread(update_server_status, server_id, "OFFLINE", error_str)
            # 🔧 同步把「已綁定」的點位標記為 OFFLINE。
            # 原本只更新 opcua_servers.conn_state，opcua_tags.plc_state 會永遠
            # 停在 ONLINE，導致網頁「異常監控」的連線異常永遠偵測不到 OPC UA
            # 斷線。未綁定的點位不動（它們本來就不會被訂閱更新，標成 OFFLINE
            # 只會塞爆異常清單）。
            await asyncio.to_thread(set_opcua_bound_tags_state, server_id, "OFFLINE")
            # 🆕 斷線期間不要讓心跳用舊值補寫、也不要用舊值判斷數值警報
            sensor_reading_writer.mark_unavailable(
                sid for sid in state["node_to_sensor"].values() if sid is not None
            )
            session_limit_hit = "TooManySessions" in error_str

        finally:
            for t in (flush_task, maintain_task):
                if t:
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

            pending = buffer.pop_all()
            if pending:
                try:
                    await asyncio.to_thread(
                        _flush_to_db, server_id, pending, state["node_to_sensor"], state["bad_nodes"]
                    )
                except Exception as e:
                    logger.error(
                        f"❌ [訂閱服務] Server [{server_name}] 斷線前補寫緩衝區資料失敗: {e}"
                    )

            await _delete_all_subscriptions(state["subscriptions"])
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        if stop_event.is_set():
            break

        if session_limit_hit:
            wait_time = SESSION_LIMIT_RETRY_DELAY
            logger.warning(
                f"⚠️ [訂閱服務] Server [{server_name}] 回報 Session 數已滿"
                f"（可能有殘留連線尚未逾時釋放），將於 {wait_time:.0f} 秒後重試..."
            )
        else:
            wait_time = reconnect_delay
            logger.warning(f"🔄 [訂閱服務] Server [{server_name}] 將於 {wait_time:.0f} 秒後嘗試重新連線...")

        await asyncio.sleep(wait_time)
        reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX_DELAY)


# ----------------------------------------------------------------
# 服務進入點：背景執行緒 + 專屬 event loop，管理所有 enabled Server
# ----------------------------------------------------------------
SERVER_LIST_REFRESH_INTERVAL = 15.0

_SERVER_CONNECTION_FIELDS = (
    "ip", "port", "username", "password",
    "security_policy", "security_mode", "root_node_id", "browse_depth",
)


def _server_config_changed(old_server: dict, new_server: dict) -> bool:
    return any(old_server.get(f) != new_server.get(f) for f in _SERVER_CONNECTION_FIELDS)


async def _bridge_stop_signal(stop_signal: threading.Event, async_stop_event: asyncio.Event):
    while not stop_signal.is_set():
        await asyncio.sleep(0.5)
    async_stop_event.set()


async def _cancel_task(task: asyncio.Task):
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def _service_main(stop_signal: threading.Event):
    async_stop_event = asyncio.Event()
    bridge_task = asyncio.create_task(_bridge_stop_signal(stop_signal, async_stop_event))

    running: dict = {}  # server_id -> (asyncio.Task, server_dict)

    first_scan = True
    while not async_stop_event.is_set():
        try:
            servers = await asyncio.to_thread(load_opcua_servers)
        except Exception as e:
            logger.error(f"❌ [訂閱服務] 讀取 OPC UA Server 清單失敗: {e}")
            servers = list({sid: s for sid, (_, s) in running.items()}.values())

        if first_scan and not servers:
            logger.warning("⚠️ [訂閱服務] opcua_servers 目前沒有任何啟用中的 Server，訂閱服務待命中。")
        first_scan = False

        current_ids = set()
        for server in servers:
            sid = server["id"]
            current_ids.add(sid)

            if sid not in running:
                logger.info(f"🆕 [訂閱服務] 偵測到新的 OPC UA Server [{server['server_name']}]，開始監控")
                task = asyncio.create_task(_run_server_subscription(server, async_stop_event))
                running[sid] = (task, server)
            else:
                old_task, old_server = running[sid]
                if _server_config_changed(old_server, server):
                    logger.info(
                        f"🔄 [訂閱服務] Server [{server['server_name']}] 連線參數已變更，"
                        "重新啟動監控任務套用新設定"
                    )
                    await _cancel_task(old_task)
                    task = asyncio.create_task(_run_server_subscription(server, async_stop_event))
                    running[sid] = (task, server)

        removed_ids = set(running.keys()) - current_ids
        for sid in removed_ids:
            task, server = running.pop(sid)
            logger.info(f"🛑 [訂閱服務] Server [{server['server_name']}] 已被刪除或停用，停止監控")
            await _cancel_task(task)
            SERVER_STATS.pop(sid, None)

        await asyncio.sleep(SERVER_LIST_REFRESH_INTERVAL)

    for task, _ in running.values():
        task.cancel()
    await asyncio.gather(bridge_task, *[t for t, _ in running.values()], return_exceptions=True)


def _thread_main(stop_signal: threading.Event):
    try:
        asyncio.run(_service_main(stop_signal))
    except Exception as e:
        logger.error(f"💥 [訂閱服務] 背景執行緒發生未預期例外: {e}", exc_info=True)


class OPCUASubscriptionService:
    """管理 OPC UA 訂閱服務的生命週期，供 main.py 呼叫。"""

    def __init__(self):
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=_thread_main,
            args=(self._stop_event,),
            name="opcua-subscription-service",
            daemon=True,
        )
        self._thread.start()
        logger.info("🚀 [訂閱服務] OPC UA 訂閱服務背景執行緒已啟動")

    def stop(self, timeout=10):
        if not self._thread:
            return
        self._stop_event.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning("⚠️ [訂閱服務] 執行緒未能在時限內結束（可能仍在等待網路 I/O）")
        else:
            logger.info("👋 [訂閱服務] OPC UA 訂閱服務已停止")