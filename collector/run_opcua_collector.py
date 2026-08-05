"""
collector/run_opcua_collector.py
==================================
OPC UA 資料採集主程式（週期性排程用）。

與 run_s7_collector.py / run_modbus_collector.py 維持一致的架構：
- 用 DatabaseConnector 讀取設定、寫入結果
- 對外提供 collect_opcua_data()，讓 main.py 可以在主迴圈中呼叫

流程：
1. 從 opcua_servers 撈出所有 enabled=TRUE 的 Server
2. 並行連線每台 Server，遞迴瀏覽 Address Space 取得所有 Variable 節點數值
3. 呼叫 batch_update_opcua_tags() 批量 UPSERT 進 opcua_tags
   （該函式內部也會依已綁定的 sensor_id，把數值暫存進 sensor_readings 緩衝區）
4. 更新 opcua_servers 的 conn_state / last_scan / last_error
5. 全部 Server 掃描完後，統一 flush 一次 sensor_readings 緩衝區
"""

import asyncio
import logging
import sys

from data_layer.db_connector import DatabaseConnector
from data_layer.batch_updater import batch_update_opcua_tags
from data_layer.timeseries_writer import sensor_reading_writer
from protocols.opcua_protocol import scan_server, OPCUAConnectionError

logger = logging.getLogger(__name__)


def load_opcua_servers():
    """從資料庫撈出所有啟用中的 OPC UA Server 連線設定"""
    query = """
        SELECT id, server_name, ip, port, username, password,
               security_policy, security_mode, root_node_id, browse_depth
        FROM opcua_servers
        WHERE enabled = TRUE;
    """
    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                cols = [desc[0] for desc in cur.description]
                rows = cur.fetchall()
        return [dict(zip(cols, row)) for row in rows]
    except Exception as e:
        logger.error(f"從資料庫讀取 OPC UA Server 清單失敗: {e}")
        return []


def update_server_status(server_id, state, error=None):
    """更新單一 Server 的連線狀態與最後掃描時間"""
    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE opcua_servers
                    SET conn_state = %s, last_scan = CURRENT_TIMESTAMP, last_error = %s
                    WHERE id = %s;
                    """,
                    (state, error, server_id),
                )
    except Exception as e:
        logger.error(f"更新 OPC UA Server[{server_id}] 狀態失敗: {e}")


async def _scan_one(server):
    """掃描單一 Server，回傳 (server, tags, error_message)"""
    try:
        tags = await scan_server(server)
        return server, tags, None
    except OPCUAConnectionError as e:
        return server, [], str(e)
    except Exception as e:
        return server, [], str(e)


async def _scan_all(servers):
    """並行掃描所有 Server，避免一台卡住拖慢其他台"""
    return await asyncio.gather(*(_scan_one(s) for s in servers))


def collect_opcua_data():
    """
    核心採集流程（同步介面，供 main.py 週期性呼叫）。
    內部用 asyncio.run() 執行所有 Server 的並行瀏覽，
    瀏覽結果用同步的 psycopg2 連線寫回資料庫。
    """
    logger.info("開始執行 OPC UA 點位瀏覽與採集任務...")

    servers = load_opcua_servers()
    if not servers:
        logger.warning("opcua_servers 資料表中沒有任何啟用中的 Server。")
        return

    try:
        results = asyncio.run(_scan_all(servers))
    except Exception as e:
        logger.error(f"OPC UA 並行掃描發生未預期錯誤: {e}", exc_info=True)
        return

    for server, tags, error in results:
        server_id = server["id"]
        server_name = server["server_name"]

        if error:
            logger.error(f"OPC UA Server [{server_name}] 掃描失敗: {error}")
            update_server_status(server_id, "OFFLINE", error)
            continue

        logger.info(f"OPC UA Server [{server_name}] 掃描完成，取得 {len(tags)} 個點位")
        batch_update_opcua_tags(server_id, server_name, tags)
        update_server_status(server_id, "ONLINE")

    # 所有 Server 掃描完成後，統一把本輪暫存的時序資料批次寫進 sensor_readings
    sensor_reading_writer.flush()

    logger.info("OPC UA 採集任務結束。")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    try:
        DatabaseConnector.initialize_pool()
        # 單獨執行本檔案時，需要自行載入 sensor_readings 心跳快取
        sensor_reading_writer.load_initial_cache()
        collect_opcua_data()
    except Exception as e:
        logger.error(f"主程式執行異常: {e}", exc_info=True)
    finally:
        DatabaseConnector.close_pool()
        logger.info("採集任務結束。")