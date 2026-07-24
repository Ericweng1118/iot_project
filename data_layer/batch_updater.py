import json
import logging
from datetime import datetime
from psycopg2.extras import execute_values
from .db_connector import DatabaseConnector

logger = logging.getLogger(__name__)

def batch_update_tia_data(update_rows):
    """
    批量更新 TIA (S7) 採集數據
    :param update_rows: 串列，元素為 tuple (id, current_data_dict, plc_state)
    """
    if not update_rows:
        return

    # 將 dict 轉為 JSON 字串供 PostgreSQL 識別
    processed_rows = [
        (row[0], json.dumps(row[1]), row[2]) for row in update_rows
    ]

    query = """
        UPDATE TIA_SCADA AS t
        SET current_data = v.current_data::jsonb,
            plc_state = v.plc_state,
            last_update = CURRENT_TIMESTAMP
        FROM (VALUES %s) AS v(id, current_data, plc_state)
        WHERE t.id = v.id;
    """

    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                execute_values(cur, query, processed_rows)
        logger.debug(f"成功批量更新 {len(update_rows)} 筆 TIA 點位數據。")
    except Exception as e:
        logger.error(f"批量更新 TIA 數據失敗: {e}")

def batch_update_modbus_data(update_rows):
    """
    批量更新 Modbus 採集數據
    :param update_rows: 串列，元素為 tuple (id, current_value, current_data_dict, plc_state)
    """
    if not update_rows:
        return

    processed_rows = [
        (row[0], row[1], json.dumps(row[2]), row[3]) for row in update_rows
    ]

    query = """
        UPDATE modbus_scada AS m
        SET current_value = v.current_value,
            current_data = v.current_data::jsonb,
            plc_state = v.plc_state,
            last_update = CURRENT_TIMESTAMP
        FROM (VALUES %s) AS v(id, current_value, current_data, plc_state)
        WHERE m.id = v.id;
    """

    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                execute_values(cur, query, processed_rows)
        logger.debug(f"成功批量更新 {len(update_rows)} 筆 Modbus 點位數據。")
    except Exception as e:
        logger.error(f"批量更新 Modbus 數據失敗: {e}")


def batch_update_opcua_tags(server_id, server_name, tags):
    """
    批量 UPSERT OPC UA 瀏覽到的點位資料到 opcua_tags 表。
    與 TIA/Modbus 不同，OPC UA 點位是動態瀏覽出來的，
    第一次出現時要 INSERT，之後同一個 node_id 只更新數值，
    所以這裡用 INSERT ... ON CONFLICT 而非 UPDATE FROM VALUES。

    :param server_id: opcua_servers.id
    :param server_name: 用於 MQTT Key 組合的 Server 名稱
    :param tags: list of dict，每筆包含
                 node_id, browse_name, display_name, data_type, value, quality
    """
    if not tags:
        return

    now = datetime.now()
    processed_rows = [
        (
            server_id,
            server_name,
            t["node_id"],
            t.get("browse_name"),
            t.get("display_name"),
            t.get("data_type"),
            json.dumps({"val": t.get("value")}, default=str, ensure_ascii=False),
            t.get("quality"),
            "ONLINE",
            now,
        )
        for t in tags
    ]

    query = """
        INSERT INTO opcua_tags
            (server_id, server_name, node_id, browse_name, display_name,
             data_type, current_data, quality, plc_state, last_update)
        VALUES %s
        ON CONFLICT (server_id, node_id) DO UPDATE SET
            browse_name  = EXCLUDED.browse_name,
            display_name = EXCLUDED.display_name,
            data_type    = EXCLUDED.data_type,
            current_data = EXCLUDED.current_data,
            quality      = EXCLUDED.quality,
            plc_state    = EXCLUDED.plc_state,
            last_update  = EXCLUDED.last_update;
    """

    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                execute_values(cur, query, processed_rows)
        logger.debug(f"成功批量更新 {len(tags)} 筆 OPC UA 點位數據 (Server: {server_name})。")
    except Exception as e:
        logger.error(f"批量更新 OPC UA 數據失敗 (Server: {server_name}): {e}")