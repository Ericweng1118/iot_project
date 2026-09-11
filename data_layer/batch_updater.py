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
    用於「結構性瀏覽」情境：第一次掃描，或使用者按下手動瀏覽/訂閱服務
    偵測到 resubscribe_requested 旗標時的重新瀏覽。
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

    now = datetime.now().astimezone()
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


def batch_update_opcua_values(server_id, rows):
    """
    批量更新 OPC UA 點位「數值」（訂閱模式專用，輕量版）。
    與 batch_update_opcua_tags 不同：這裡只更新 current_data / quality /
    plc_state / last_update，不動 browse_name / display_name / data_type
    等結構性欄位，且是 UPDATE 而非 INSERT（假設 node_id 已存在於
    opcua_tags，因為訂閱前一定先做過至少一次結構性瀏覽）。
    目的是讓「每 2 秒一次的高頻數值 flush」盡量輕量，減少 DB 負擔。

    :param server_id: opcua_servers.id
    :param rows: list of tuple (node_id, current_data_dict, quality, plc_state)
    """
    if not rows:
        return

    now = datetime.now().astimezone()
    processed_rows = [
        (
            server_id,
            node_id,
            json.dumps(current_data, default=str, ensure_ascii=False),
            quality,
            plc_state,
            now,
        )
        for node_id, current_data, quality, plc_state in rows
    ]

    query = """
        UPDATE opcua_tags AS o
        SET current_data = v.current_data::jsonb,
            quality = v.quality,
            plc_state = v.plc_state,
            last_update = v.last_update
        FROM (VALUES %s) AS v(server_id, node_id, current_data, quality, plc_state, last_update)
        WHERE o.server_id = v.server_id AND o.node_id = v.node_id;
    """

    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                execute_values(cur, query, processed_rows)
        logger.debug(f"[訂閱模式] 成功批量更新 {len(rows)} 筆 OPC UA 點位數值 (server_id={server_id})。")
    except Exception as e:
        logger.error(f"[訂閱模式] 批量更新 OPC UA 點位數值失敗 (server_id={server_id}): {e}")


def set_opcua_bound_tags_state(server_id, state):
    """
    把某台 Server 底下「已綁定感測器」的點位，整批標記成指定的連線狀態。

    為什麼需要這支：
        原本 opcua_tags.plc_state 只有寫入 'ONLINE' 的路徑
        （batch_update_opcua_tags 瀏覽時寫死 ONLINE、batch_update_opcua_values
        由訂閱 flush 帶入 ONLINE），斷線時只會更新 opcua_servers.conn_state，
        完全不會回頭改 opcua_tags。結果是點位一旦上線就永遠停在 ONLINE，
        網頁「異常監控」的連線異常查詢因此永遠偵測不到 OPC UA 斷線。

    為什麼只動「已綁定」的點位：
        v2 起只有綁定 sensor_id 的點位會真的被訂閱、被持續更新。未綁定的點位
        本來就停留在上次瀏覽的快照，對它們談「連線狀態」沒有意義；若一併標成
        OFFLINE，未綁定的點位（本專案正式庫有 2745 筆）會塞爆異常清單，
        把真正需要注意的點位淹掉。

    :param server_id: opcua_servers.id
    :param state: 'ONLINE' / 'OFFLINE'
    :return: 實際被更新的筆數
    """
    query = """
        UPDATE opcua_tags
        SET plc_state = %s
        WHERE server_id = %s
          AND sensor_id IS NOT NULL
          AND plc_state IS DISTINCT FROM %s;
    """
    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (state, server_id, state))
                affected = cur.rowcount
        if affected:
            logger.info(
                f"[訂閱模式] server_id={server_id} 的 {affected} 個已綁定點位"
                f"連線狀態已標記為 {state}。"
            )
        return affected
    except Exception as e:
        logger.error(
            f"[訂閱模式] 標記 opcua_tags 連線狀態失敗 "
            f"(server_id={server_id}, state={state}): {e}"
        )
        return 0
