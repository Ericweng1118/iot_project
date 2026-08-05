import json
import logging
from datetime import datetime
from psycopg2.extras import execute_values
from .db_connector import DatabaseConnector
from .timeseries_writer import sensor_reading_writer

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

    注意：sensor_id 刻意不放進 INSERT/UPDATE 的 SET 清單中，
    這樣 ON CONFLICT 更新時「不會」覆蓋掉你在 admin_app.py
    手動綁定好的 sensor_id；新出現的節點 sensor_id 預設為 NULL，
    需要你事後手動去對應到 sensors 階層。

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

                # 讀回這批 node_id 目前綁定的 sensor_id（可能是舊值，也可能仍是 NULL）
                node_ids = [t["node_id"] for t in tags]
                cur.execute(
                    """
                    SELECT node_id, sensor_id
                    FROM opcua_tags
                    WHERE server_id = %s AND node_id = ANY(%s);
                    """,
                    (server_id, node_ids),
                )
                sensor_id_map = dict(cur.fetchall())

        logger.debug(f"成功批量更新 {len(tags)} 筆 OPC UA 點位數據 (Server: {server_name})。")

        # 🔍 診斷用 log：有幾個點位查到了 sensor_id（非 None）
        bound_count = sum(1 for v in sensor_id_map.values() if v is not None)
        logger.info(
            f"🔗 OPC UA Server [{server_name}]：本輪 {len(tags)} 個點位中，"
            f"有 {bound_count} 個已綁定 sensor_id，將嘗試寫入 sensor_readings。"
        )

        # 依 README_DB.md 的規則，把有綁定 sensor_id 的點位暫存進 sensor_readings 緩衝區
        now_ts = now.astimezone()
        staged_count = 0
        for t in tags:
            sensor_id = sensor_id_map.get(t["node_id"])
            if sensor_id is not None:
                logger.info(
                    f"   └─ 🧪 stage 嘗試: node_id={t['node_id']}, "
                    f"sensor_id={sensor_id}, value={t.get('value')} (type={type(t.get('value')).__name__})"
                )
                staged_count += 1
            sensor_reading_writer.stage(sensor_id, t.get("value"), now_ts)

        if staged_count == 0:
            logger.info(
                f"ℹ️ OPC UA Server [{server_name}]：本輪沒有任何點位綁定 sensor_id，"
                "所以不會寫入 sensor_readings（這是正常行為，不是錯誤）。"
            )

    except Exception as e:
        logger.error(f"批量更新 OPC UA 數據失敗 (Server: {server_name}): {e}")