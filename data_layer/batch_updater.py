import json
import logging
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