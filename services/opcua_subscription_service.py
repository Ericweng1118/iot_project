"""
services/opcua_subscription_service.py
========================================
OPC UA 訂閱推播服務（取代週期性 browse 輪詢）。

設計動機：
    原本 run_opcua_collector.py 每一輪都要對整個 Address Space 重新做
    遞迴 browse（get_children / read_node_class / browse_name / display_name /
    read_data_value 逐節點呼叫），節點數一多，光是結構性探索的 round-trip
    次數就遠遠超過「只是想知道數值變了沒」所需的成本。

    本服務把「結構探索（browse）」與「數值更新（subscribe）」拆開：
    - 結構探索：只在下列時機發生 —
        1) 該 Server 尚無任何快取點位時（第一次啟動）
        2) 使用者在網頁按下「立即瀏覽並寫入資料庫」後，
           opcua_servers.resubscribe_requested 被設為 TRUE，
           本服務輪詢偵測到後才重新 browse
    - 數值更新：browse 完成後改用 OPC UA Subscription（Server Side
      Report by Exception），Server 有變化才推播過來，本服務收到後
      先寫進記憶體緩衝區，每隔 FLUSH_INTERVAL_SECONDS 秒批次寫回 DB，
      避免每一筆變化都各自打一次資料庫。

執行模型：
    這是一個長駐服務，需要維持持續連線，因此不能像 Modbus/TIA 一樣
    「每輪呼叫一次、跑完就斷線」。改用獨立的背景執行緒 + 專屬的
    asyncio event loop 執行，跟 main.py 主迴圈（Modbus/TIA 併發採集）
    完全脫鉤、互不阻塞。

已知限制（v1 範圍）：
    - 點位被移除後，opcua_tags 裡的舊資料列不會自動刪除，
      只會停止訂閱、不再更新數值。

🆕 動態偵測 Server 清單（不需重啟 main.py）：
    _service_main() 每隔 SERVER_LIST_REFRESH_INTERVAL 秒會重新讀取一次
    opcua_servers（只會抓 enabled=TRUE 的清單），跟目前正在監控的 Server
    比對差異：
        - 清單裡多出來的 Server id -> 視為新增，啟動一個新的監控任務
        - 清單裡消失的 Server id（被刪除或 enabled 被關掉）-> 停止對應任務
        - 清單裡都還在、但連線參數（ip/port/帳密/安全性原則/root_node_id/
          browse_depth）有變 -> 取消舊任務、用新設定重新啟動
    也就是說，新增 Server、刪除 Server、關閉 enabled、修改連線參數，
    這些操作最慢在下一次掃描週期內就會自動套用，完全不需要重啟 main.py。
"""

import asyncio
import logging
import os
import threading

from data_layer.db_connector import DatabaseConnector
from data_layer.batch_updater import batch_update_opcua_tags, batch_update_opcua_values
from data_layer.timeseries_writer import sensor_reading_writer
from protocols.opcua_protocol import connect_client, browse_recursive, OPCUAConnectionError
from collector.run_opcua_collector import load_opcua_servers, update_server_status

logger = logging.getLogger("opcua_subscription_service")

# 🔇 壓低 asyncua 套件內部的 logger 等級。
# asyncua 內部會把每一次 Publish Response 的完整原始內容（含 SourceTimestamp /
# ServerTimestamp / StatusCode 等欄位）用 logger 印出來，且不受 root logger 的
# level 限制（它有自己的 logger 名稱 "asyncua"），頻率跟訂閱的 publish interval
# 一樣密集，會把真正有意義的錯誤訊息淹沒、難以排查問題。
# 這裡只壓 asyncua 自己的 logger，我們自己 "opcua_subscription_service" 的
# log（連線成功/失敗、心跳、重連、重新整理點位表等）完全不受影響。
logging.getLogger("asyncua").setLevel(logging.WARNING)

# ----------------------------------------------------------------
# 可調參數
# ----------------------------------------------------------------
FLUSH_INTERVAL_SECONDS = 2.0      # 緩衝區批次寫回 DB 的週期
HEARTBEAT_INTERVAL = 15.0         # 心跳檢查連線是否存活的週期
RESUBSCRIBE_POLL_INTERVAL = 5.0   # 輪詢 resubscribe_requested 旗標的週期
SENSOR_BINDING_REFRESH_INTERVAL = 10.0  # 定期重新讀取 sensor_id 綁定的週期

# 全域預設的訂閱 Publishing Interval（毫秒），可透過 .env 的
# OPCUA_PUBLISH_INTERVAL_MS 調整，不用改程式碼。
# 個別 Server 若在 opcua_servers 設定了 publish_interval_ms 欄位，優先套用該值，
# 沒有設定才會退回這個全域預設值。
# 注意：Server 仍可能依自己的限制回覆修正過的 RevisedPublishingInterval，
# 這是正常的 OPC UA 行為（見 asyncua 建立訂閱時的 WARNING log），
# 調整這個值只是改變我們「要求」的頻率，不保證 Server 一定完全照辦。
try:
    DEFAULT_PUBLISH_INTERVAL_MS = float(os.getenv("OPCUA_PUBLISH_INTERVAL_MS", "1000").split('#')[0].strip())
except ValueError:
    DEFAULT_PUBLISH_INTERVAL_MS = 1000.0
                                   # （感測器綁定可以隨時透過網頁表格直接修改，
                                   #  跟「是否需要重新瀏覽點位表」是兩件獨立的事，
                                   #  所以用獨立的較短週期定期刷新，讓時序寫入
                                   #  能盡快反映最新的綁定狀態）
RECONNECT_BASE_DELAY = 5.0        # 斷線後第一次重試等待秒數
RECONNECT_MAX_DELAY = 60.0        # 斷線重試等待秒數上限（指數退避）
SESSION_LIMIT_RETRY_DELAY = 120.0 # Server 回報「Session 數已滿」時，固定等待這麼久再重試
                                   # （比一般斷線重連間隔更長；此時通常是 Server 端有殘留
                                   #  連線尚未逾時釋放，重試太快只會一直搶不到名額）


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
        self._logged_quality_warning = False  # 避免同一種錯誤每次通知都洗版

    def datachange_notification(self, node, val, data):
        # 🔧 修正：node_id 取得失敗就直接放棄這筆沒得救；
        # 但 quality（品質狀態）解析失敗不該連累整筆資料——
        # 先確保「值」有進緩衝區，quality 解析失敗就退回預設 GOOD，
        # 並且把錯誤訊息改成 WARNING/ERROR（原本是 DEBUG，
        # 在預設 INFO log 等級下完全看不到，等於靜默吃掉錯誤）。
        try:
            node_id = node.nodeid.to_string()
        except Exception as e:
            logger.error(f"❌ [訂閱服務] Server [{self._server_name}] 無法取得節點 NodeId: {e}")
            return

        quality = "GOOD"
        try:
            status = data.monitored_item.Value.StatusCode
            quality = "GOOD" if status.is_good() else "BAD"
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
        # 本服務不訂閱 Event，僅實作介面避免 asyncua 內部呼叫報錯
        pass


# ----------------------------------------------------------------
# DB 存取（同步函式，皆透過 asyncio.to_thread 呼叫，避免阻塞 event loop）
# ----------------------------------------------------------------
def _load_cached_tags(server_id):
    """從 opcua_tags 讀取此 Server 目前已知的點位清單（第一次瀏覽後即會有資料）"""
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


def _load_node_to_sensor_map(server_id):
    """重新讀取 node_id -> sensor_id 對照表（供時序寫入判斷這個點位有沒有綁定感測器）"""
    tags = _load_cached_tags(server_id)
    return {t["node_id"]: t.get("sensor_id") for t in tags}


def _flush_to_db(server_id, pending, node_to_sensor):
    rows = [
        (node_id, {"val": value}, quality, "ONLINE")
        for node_id, (value, quality) in pending.items()
    ]
    batch_update_opcua_values(server_id, rows)

    # 已綁定感測器的點位，同步把數值送進時序寫入器暫存並批次寫入 sensor_readings
    for node_id, (value, quality) in pending.items():
        sensor_id = node_to_sensor.get(node_id)
        if sensor_id is not None:
            sensor_reading_writer.stage(sensor_id, value)
    sensor_reading_writer.flush()


# ----------------------------------------------------------------
# 背景 async task：定期 flush 緩衝區、定期檢查是否要重新整理點位表
# ----------------------------------------------------------------
async def _browse_with_existing_client(client, server: dict) -> list:
    """
    用【已經連線好的 client】直接瀏覽，不像 scan_server() 那樣會另外再開一條連線。
    訂閱服務本身已經對 Server 保持一條常駐連線，若瀏覽時又呼叫 scan_server()
    多開一條連線，對 Session 數上限很低的 Server（常見於 PLC 內建的簡化版
    OPC UA Server）很容易直接把 Session 配額用完，觸發 BadTooManySessions。
    """
    root_node_id = server.get("root_node_id") or "i=85"
    max_depth = server.get("browse_depth") or 5
    root_node = client.get_node(root_node_id)
    return await browse_recursive(root_node, max_depth)


async def _flush_loop(
    buffer: _ChangeBuffer,
    server_id,
    server_name: str,
    node_to_sensor: dict,
    stop_event: asyncio.Event,
):
    while not stop_event.is_set():
        await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
        pending = buffer.pop_all()
        if pending:
            # 🔧 修正：原本沒有 try/except，一旦 _flush_to_db 拋例外，
            # 這個背景 task 會直接整個中止且不會自動重啟，之後就再也不會
            # 嘗試寫入資料庫，但因為是背景 task，例外不會出現在我們自己的
            # log 格式裡（只會被 asyncio 預設處理器印出不易注意到的警告），
            # 表面上看起來像「訂閱成功但完全沒有寫入」。
            # 現在加上 try/except，寫入失敗只會跳過這一批、下一輪繼續嘗試，
            # 並且成功/失敗都會印出清楚的 log，方便直接從 log 判斷卡在哪裡。
            try:
                await asyncio.to_thread(_flush_to_db, server_id, pending, node_to_sensor)
                logger.info(
                    f"💾 [訂閱服務] Server [{server_name}] 批次寫入 {len(pending)} 筆數值變化"
                )
            except Exception as e:
                logger.error(
                    f"❌ [訂閱服務] Server [{server_name}] 批次寫入資料庫失敗: {e}",
                    exc_info=True,
                )


async def _watch_sensor_bindings(server_id, server_name: str, node_to_sensor: dict, stop_event: asyncio.Event):
    """
    定期重新讀取 opcua_tags 目前的 sensor_id 綁定狀態，同步更新 node_to_sensor 對照表。
    感測器綁定可以隨時透過網頁「OPC UA 點位設定」分頁的表格直接修改，跟
    「是否需要重新瀏覽點位表」（resubscribe_requested）是兩件獨立的事，
    所以獨立用一個較短的週期定期刷新，確保綁定變更能盡快反映到時序寫入，
    不需要等使用者剛好又觸發一次「立即瀏覽」。
    """
    while not stop_event.is_set():
        await asyncio.sleep(SENSOR_BINDING_REFRESH_INTERVAL)
        try:
            latest = await asyncio.to_thread(_load_node_to_sensor_map, server_id)
            node_to_sensor.clear()
            node_to_sensor.update(latest)
        except Exception as e:
            logger.error(
                f"❌ [訂閱服務] Server [{server_name}] 重新整理 sensor_id 綁定對照表失敗: {e}"
            )


async def _watch_resubscribe(client, server, subscription, handle_map, node_to_sensor: dict, stop_event: asyncio.Event):
    server_id = server["id"]
    server_name = server["server_name"]

    while not stop_event.is_set():
        await asyncio.sleep(RESUBSCRIBE_POLL_INTERVAL)

        requested = await asyncio.to_thread(_check_and_clear_resubscribe_flag, server_id)
        if not requested:
            continue

        logger.info(f"🔄 [訂閱服務] 收到 Server [{server_name}] 重新整理點位表請求，開始重新瀏覽...")
        try:
            tags = await _browse_with_existing_client(client, server)
        except Exception as e:
            logger.error(f"❌ [訂閱服務] Server [{server_name}] 重新瀏覽失敗: {e}")
            continue

        if tags:
            await asyncio.to_thread(batch_update_opcua_tags, server_id, server_name, tags)

        new_node_ids = {t["node_id"] for t in tags}
        old_node_ids = set(handle_map.keys())
        to_remove = old_node_ids - new_node_ids
        to_add = new_node_ids - old_node_ids

        if to_remove:
            handles_to_remove = [handle_map.pop(n) for n in to_remove]
            try:
                await subscription.unsubscribe(handles_to_remove)
            except Exception as e:
                logger.error(f"取消訂閱 {len(handles_to_remove)} 個已移除點位失敗: {e}")

        if to_add:
            nodes_to_add = [client.get_node(n) for n in to_add]
            try:
                new_handles = await subscription.subscribe_data_change(nodes_to_add)
                if not isinstance(new_handles, list):
                    new_handles = [new_handles]
                for node_id, h in zip(to_add, new_handles):
                    handle_map[node_id] = h
            except Exception as e:
                logger.error(f"訂閱 {len(nodes_to_add)} 個新增點位失敗: {e}")

        # 點位表結構變了（新增/移除），順便把 sensor_id 綁定對照表也重新整理一次，
        # 不用等下一次 _watch_sensor_bindings 的定期刷新。
        try:
            latest = await asyncio.to_thread(_load_node_to_sensor_map, server_id)
            node_to_sensor.clear()
            node_to_sensor.update(latest)
        except Exception as e:
            logger.error(f"重新整理 sensor_id 綁定對照表失敗: {e}")

        logger.info(
            f"✅ [訂閱服務] Server [{server_name}] 點位表已更新："
            f"新增 {len(to_add)} 個、移除 {len(to_remove)} 個，目前共監控 {len(handle_map)} 個點位"
        )


# ----------------------------------------------------------------
# 單一 Server 的完整生命週期：連線 → (視情況)首次瀏覽 → 建立訂閱 →
# 心跳存活監控 → 斷線 → 清理 → 退避重連
# ----------------------------------------------------------------
async def _run_server_subscription(server: dict, stop_event: asyncio.Event):
    server_id = server["id"]
    server_name = server["server_name"]
    reconnect_delay = RECONNECT_BASE_DELAY

    while not stop_event.is_set():
        client = None
        subscription = None
        handle_map = {}
        node_to_sensor = {}
        buffer = _ChangeBuffer()
        flush_task = None
        resubscribe_task = None
        binding_refresh_task = None
        session_limit_hit = False

        try:
            client = await connect_client(server)
            logger.info(f"✅ [訂閱服務] 已連線至 OPC UA Server [{server_name}]")
            await asyncio.to_thread(update_server_status, server_id, "ONLINE")
            reconnect_delay = RECONNECT_BASE_DELAY  # 連線成功，重置重試間隔

            # 1. 取得點位清單：優先用快取，沒有才做一次完整結構性瀏覽
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

            # sensor_id 綁定對照表：直接從剛載入的 cached_tags 建立，不用多查一次 DB
            node_to_sensor = {t["node_id"]: t.get("sensor_id") for t in cached_tags}

            # 2. 建立訂閱，一次把所有已知點位加入監控
            handler = _DataChangeHandler(buffer, server_name)
            publish_interval_ms = server.get("publish_interval_ms") or DEFAULT_PUBLISH_INTERVAL_MS
            subscription = await client.create_subscription(publish_interval_ms, handler)

            nodes = [client.get_node(t["node_id"]) for t in cached_tags]
            handles = await subscription.subscribe_data_change(nodes)
            if not isinstance(handles, list):
                handles = [handles]
            for t, h in zip(cached_tags, handles):
                handle_map[t["node_id"]] = h

            logger.info(f"📡 [訂閱服務] Server [{server_name}] 訂閱建立完成，監控 {len(handle_map)} 個點位")

            # 心跳目標：優先用「已知讀得到」的實際點位（訂閱成功代表這個節點肯定存在），
            # 比依賴系統節點 i=2258 更保險；真的沒有任何點位時才退回系統節點。
            heartbeat_node_id = cached_tags[0]["node_id"] if cached_tags else "i=2258"

            # 3. 背景 task：定期 flush 緩衝區 / 定期檢查重新整理點位表請求 / 定期刷新感測器綁定
            flush_task = asyncio.create_task(
                _flush_loop(buffer, server_id, server_name, node_to_sensor, stop_event)
            )
            resubscribe_task = asyncio.create_task(
                _watch_resubscribe(client, server, subscription, handle_map, node_to_sensor, stop_event)
            )
            binding_refresh_task = asyncio.create_task(
                _watch_sensor_bindings(server_id, server_name, node_to_sensor, stop_event)
            )

            # 4. 心跳迴圈：定期讀取心跳目標節點確認連線存活，
            #    讀取失敗代表連線已斷，丟例外進入下方重連流程
            #    🔧 修正：原本固定讀 i=2259（ServerStatus 底下的 State 子節點），
            #    但這個子節點不是所有 OPC UA Server（尤其 PLC 內建簡化版 Server）
            #    都有完整實作，讀取失敗會被誤判成斷線，導致訂閱被反覆砍掉重建，
            #    真正的資料變化很容易在重建過程中被錯過。
            #    現在優先用已訂閱成功的實際點位當心跳目標（見上方 heartbeat_node_id），
            #    真的沒有點位時才退回 i=2258（ServerStatus_CurrentTime，比 i=2259 通用）。
            while not stop_event.is_set():
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await client.get_node(heartbeat_node_id).read_value()

        except Exception as e:
            error_str = str(e)
            logger.error(f"❌ [訂閱服務] Server [{server_name}] 連線/訂閱發生例外: {e}")
            await asyncio.to_thread(update_server_status, server_id, "OFFLINE", error_str)
            session_limit_hit = "TooManySessions" in error_str

        finally:
            for t in (flush_task, resubscribe_task, binding_refresh_task):
                if t:
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

            # 斷線前，把緩衝區內尚未寫入的資料先 flush 一次，避免遺漏
            # 🔧 修正：這裡在 finally 區塊內，如果 _flush_to_db 拋例外又沒接住，
            # 會導致下面的 subscription.delete() / client.disconnect() 整個被跳過，
            # 讓 Session 沒有正常關閉、變成殘留在 Server 端的殭屍連線
            # （正是先前 BadTooManySessions 問題的成因之一）。
            pending = buffer.pop_all()
            if pending:
                try:
                    await asyncio.to_thread(_flush_to_db, server_id, pending, node_to_sensor)
                except Exception as e:
                    logger.error(
                        f"❌ [訂閱服務] Server [{server_name}] 斷線前補寫緩衝區資料失敗: {e}"
                    )

            if subscription:
                try:
                    await subscription.delete()
                except Exception:
                    pass
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        if stop_event.is_set():
            break

        if session_limit_hit:
            # Server 回報 Session 數已滿：通常代表 Server 端還有殘留連線尚未逾時釋放，
            # 用一般的短間隔重試只會一直搶不到名額、甚至讓情況更難恢復，
            # 改用固定的較長等待時間，讓殘留連線有機會自然逾時釋放。
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
SERVER_LIST_REFRESH_INTERVAL = 15.0  # 定期重新掃描 opcua_servers 清單的週期
                                      # （偵測新增/刪除/停用/連線參數變更的 Server，
                                      #  不需要重啟 main.py 就能在下一次掃描時套用）

_SERVER_CONNECTION_FIELDS = (
    "ip", "port", "username", "password",
    "security_policy", "security_mode", "root_node_id", "browse_depth",
)


def _server_config_changed(old_server: dict, new_server: dict) -> bool:
    """比較連線相關欄位是否有變更，有變更就需要重啟該 Server 的監控任務"""
    return any(old_server.get(f) != new_server.get(f) for f in _SERVER_CONNECTION_FIELDS)


async def _bridge_stop_signal(stop_signal: threading.Event, async_stop_event: asyncio.Event):
    """把 threading.Event 的停止訊號橋接進 asyncio.Event，讓 async 任務能收到"""
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

    # server_id -> (asyncio.Task, server_dict)
    # server_dict 保留起來是為了跟下一次掃描結果比對連線參數有沒有變
    running: dict = {}

    first_scan = True
    while not async_stop_event.is_set():
        try:
            servers = await asyncio.to_thread(load_opcua_servers)
        except Exception as e:
            logger.error(f"❌ [訂閱服務] 讀取 OPC UA Server 清單失敗: {e}")
            servers = list({sid: s for sid, (_, s) in running.items()}.values())  # 讀取失敗就沿用現況，不動它

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

        # 清掉已被刪除或 enabled 被關掉的 Server（load_opcua_servers 只回傳 enabled=TRUE 的清單，
        # 所以「消失於這次掃描結果」就代表被刪除或停用了）
        removed_ids = set(running.keys()) - current_ids
        for sid in removed_ids:
            task, server = running.pop(sid)
            logger.info(f"🛑 [訂閱服務] Server [{server['server_name']}] 已被刪除或停用，停止監控")
            await _cancel_task(task)

        await asyncio.sleep(SERVER_LIST_REFRESH_INTERVAL)

    # 收到停止訊號：把所有還在跑的 Server 監控任務一起收掉
    for task, _ in running.values():
        task.cancel()
    await asyncio.gather(bridge_task, *[t for t, _ in running.values()], return_exceptions=True)


def _thread_main(stop_signal: threading.Event):
    try:
        asyncio.run(_service_main(stop_signal))
    except Exception as e:
        logger.error(f"💥 [訂閱服務] 背景執行緒發生未預期例外: {e}", exc_info=True)


class OPCUASubscriptionService:
    """
    管理 OPC UA 訂閱服務的生命週期，供 main.py 呼叫。
    此服務跑在獨立的背景執行緒 + 專屬 asyncio event loop 中，
    與主迴圈的 Modbus/TIA 併發採集完全脫鉤、互不阻塞。
    """

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