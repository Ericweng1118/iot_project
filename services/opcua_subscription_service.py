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
    - 新增一台全新的 OPC UA Server 目前不會被本服務動態偵測到，
      需要重新啟動 main.py 才會套用（enabled 開關的變更也是）。
    - 點位被移除後，opcua_tags 裡的舊資料列不會自動刪除，
      只會停止訂閱、不再更新數值。
"""

import asyncio
import logging
import threading

from data_layer.db_connector import DatabaseConnector
from data_layer.batch_updater import batch_update_opcua_tags, batch_update_opcua_values
from protocols.opcua_protocol import connect_client, scan_server, OPCUAConnectionError
from collector.run_opcua_collector import load_opcua_servers, update_server_status

logger = logging.getLogger("opcua_subscription_service")

# ----------------------------------------------------------------
# 可調參數
# ----------------------------------------------------------------
FLUSH_INTERVAL_SECONDS = 2.0      # 緩衝區批次寫回 DB 的週期
HEARTBEAT_INTERVAL = 15.0         # 心跳檢查連線是否存活的週期
RESUBSCRIBE_POLL_INTERVAL = 5.0   # 輪詢 resubscribe_requested 旗標的週期
RECONNECT_BASE_DELAY = 5.0        # 斷線後第一次重試等待秒數
RECONNECT_MAX_DELAY = 60.0        # 斷線重試等待秒數上限（指數退避）


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

    def __init__(self, buffer: _ChangeBuffer):
        self._buffer = buffer

    def datachange_notification(self, node, val, data):
        try:
            node_id = node.nodeid.to_string()
            status = data.monitored_item.Value.StatusCode
            quality = "GOOD" if status.is_good() else "BAD"
            self._buffer.update(node_id, val, quality)
        except Exception as e:
            logger.debug(f"處理 DataChange 通知失敗: {e}")

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
                    "SELECT node_id, browse_name, display_name, data_type "
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


def _flush_to_db(server_id, pending):
    rows = [
        (node_id, {"val": value}, quality, "ONLINE")
        for node_id, (value, quality) in pending.items()
    ]
    batch_update_opcua_values(server_id, rows)


# ----------------------------------------------------------------
# 背景 async task：定期 flush 緩衝區、定期檢查是否要重新整理點位表
# ----------------------------------------------------------------
async def _flush_loop(buffer: _ChangeBuffer, server_id, stop_event: asyncio.Event):
    while not stop_event.is_set():
        await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
        pending = buffer.pop_all()
        if pending:
            await asyncio.to_thread(_flush_to_db, server_id, pending)


async def _watch_resubscribe(client, server, subscription, handle_map, stop_event: asyncio.Event):
    server_id = server["id"]
    server_name = server["server_name"]

    while not stop_event.is_set():
        await asyncio.sleep(RESUBSCRIBE_POLL_INTERVAL)

        requested = await asyncio.to_thread(_check_and_clear_resubscribe_flag, server_id)
        if not requested:
            continue

        logger.info(f"🔄 [訂閱服務] 收到 Server [{server_name}] 重新整理點位表請求，開始重新瀏覽...")
        try:
            tags = await scan_server(server)
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
        buffer = _ChangeBuffer()
        flush_task = None
        resubscribe_task = None

        try:
            client = await connect_client(server)
            logger.info(f"✅ [訂閱服務] 已連線至 OPC UA Server [{server_name}]")
            await asyncio.to_thread(update_server_status, server_id, "ONLINE")
            reconnect_delay = RECONNECT_BASE_DELAY  # 連線成功，重置重試間隔

            # 1. 取得點位清單：優先用快取，沒有才做一次完整結構性瀏覽
            cached_tags = await asyncio.to_thread(_load_cached_tags, server_id)
            if not cached_tags:
                logger.info(f"🔍 [訂閱服務] Server [{server_name}] 尚無快取點位，執行首次完整瀏覽...")
                tags = await scan_server(server)
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

            # 2. 建立訂閱，一次把所有已知點位加入監控
            handler = _DataChangeHandler(buffer)
            publish_interval_ms = server.get("publish_interval_ms") or 1000
            subscription = await client.create_subscription(publish_interval_ms, handler)

            nodes = [client.get_node(t["node_id"]) for t in cached_tags]
            handles = await subscription.subscribe_data_change(nodes)
            if not isinstance(handles, list):
                handles = [handles]
            for t, h in zip(cached_tags, handles):
                handle_map[t["node_id"]] = h

            logger.info(f"📡 [訂閱服務] Server [{server_name}] 訂閱建立完成，監控 {len(handle_map)} 個點位")

            # 3. 背景 task：定期 flush 緩衝區 / 定期檢查重新整理點位表請求
            flush_task = asyncio.create_task(_flush_loop(buffer, server_id, stop_event))
            resubscribe_task = asyncio.create_task(
                _watch_resubscribe(client, server, subscription, handle_map, stop_event)
            )

            # 4. 心跳迴圈：定期讀取 ServerStatus 節點確認連線存活，
            #    讀取失敗代表連線已斷，丟例外進入下方重連流程
            while not stop_event.is_set():
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await client.get_node("i=2259").read_value()  # i=2259 = ServerStatus

        except Exception as e:
            logger.error(f"❌ [訂閱服務] Server [{server_name}] 連線/訂閱發生例外: {e}")
            await asyncio.to_thread(update_server_status, server_id, "OFFLINE", str(e))

        finally:
            for t in (flush_task, resubscribe_task):
                if t:
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

            # 斷線前，把緩衝區內尚未寫入的資料先 flush 一次，避免遺漏
            pending = buffer.pop_all()
            if pending:
                await asyncio.to_thread(_flush_to_db, server_id, pending)

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

        logger.warning(f"🔄 [訂閱服務] Server [{server_name}] 將於 {reconnect_delay:.0f} 秒後嘗試重新連線...")
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX_DELAY)


# ----------------------------------------------------------------
# 服務進入點：背景執行緒 + 專屬 event loop，管理所有 enabled Server
# ----------------------------------------------------------------
async def _bridge_stop_signal(stop_signal: threading.Event, async_stop_event: asyncio.Event):
    """把 threading.Event 的停止訊號橋接進 asyncio.Event，讓 async 任務能收到"""
    while not stop_signal.is_set():
        await asyncio.sleep(0.5)
    async_stop_event.set()


async def _service_main(stop_signal: threading.Event):
    async_stop_event = asyncio.Event()

    servers = load_opcua_servers()
    if not servers:
        logger.warning(
            "⚠️ [訂閱服務] opcua_servers 目前沒有任何啟用中的 Server，"
            "訂閱服務待命中（新增 Server 後需重啟 main.py 才會套用）。"
        )

    tasks = [asyncio.create_task(_bridge_stop_signal(stop_signal, async_stop_event))]
    tasks += [
        asyncio.create_task(_run_server_subscription(s, async_stop_event))
        for s in servers
    ]

    await asyncio.gather(*tasks, return_exceptions=True)


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