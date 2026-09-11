import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from dotenv import load_dotenv
import sys


# 設定日誌等級
# 🔥 核心步驟 1：強制讓所有 logger 訊息印在畫面上
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

print("🚀 [測試] 程式已啟動，正在載入模組...")

#logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

from data_layer.db_connector import DatabaseConnector
from data_layer.batch_updater import batch_update_tia_data
from data_layer.timeseries_writer import sensor_reading_writer
from protocols.s7_protocol import SiemensS7Collector
from parsers.plc_parser import parse_s7_value

# 併發連線的最大執行緒數上限（同時連線的 PLC 台數上限，避免瞬間開太多 socket）
MAX_WORKERS = 8


def get_s7_type_size(data_type):
    """根據資料型態判斷其佔用的 Byte 長度"""
    dt = data_type.upper()
    if dt in ['REAL', 'DINT']:
        return 4
    elif dt == 'INT':
        return 2
    elif dt == 'BOOL':
        return 1  # 雖然是 bit，但在記憶體對齊中至少佔 1 byte
    else:
        return 4  # 預設未知型態給 4 bytes 安全邊界

def load_plc_configs():
    """從資料庫撈出所有需要採集的 S7 點位參數"""
    query = """
        SELECT id, name, plc_ip, db_number, "offset", data_type, sensor_id
        FROM tia_scada;
    """
    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
                
        # 將資料整理成 dict 列表
        configs = []
        for row in rows:
            configs.append({
                "id": row[0],
                "name": row[1],
                "plc_ip": row[2],
                "db_number": row[3],
                "offset": row[4],
                "data_type": row[5],
                "sensor_id": row[6]
            })
        return configs
    except Exception as e:
        logger.error(f"從資料庫讀取點位配置失敗: {e}")
        return []

def group_configs_by_db(configs):
    """將點位依 PLC IP 和 DB 號碼進行分組，並計算採集區間"""
    # 結構: grouped[plc_ip][db_number] = [point1, point2, ...]
    grouped = defaultdict(lambda: defaultdict(list))
    for p in configs:
        grouped[p["plc_ip"]][p["db_number"]].append(p)
    return grouped


def _collect_one_plc(plc_ip, db_groups):
    """
    處理單一台 PLC 的所有 DB 區塊採集（在獨立執行緒中執行）。
    回傳這台 PLC 產生的 update_rows 清單。
    這台 PLC 連線逾時或失敗，只會拖慢自己這條執行緒，不影響其他 PLC。
    """
    rows = []
    now = datetime.now().astimezone()
    collector = SiemensS7Collector(ip=plc_ip)

    if not collector.connect():
        logger.error(f"無法連線至 PLC: {plc_ip}，該設備所有點位標記為 OFFLINE")
        # 連線失敗，把這台 PLC 的點位全部標記為 OFFLINE
        for db_num, points in db_groups.items():
            for p in points:
                rows.append((p["id"], {"val": 0.0}, "OFFLINE"))
        return rows

    # 依據同台 PLC 的不同 DB 區塊迭代讀取
    for db_number, points in db_groups.items():
        # 計算此 DB 區塊需要讀取的最小與最大記憶體邊界
        min_offset = min(p["offset"] for p in points)
        max_offset_point = max(points, key=lambda p: p["offset"])
        max_offset = max_offset_point["offset"]

        # 總長度 = 最大點位的 offset + 該型態的長度 - 最小 offset
        total_size = (max_offset + get_s7_type_size(max_offset_point["data_type"])) - min_offset

        logger.info(f"PLC [{plc_ip}] DB{db_number}: 優化打包讀取 Offset {min_offset} 至 {min_offset + total_size} (共 {total_size} Bytes)")

        # 發送一次請求讀取整段記憶體
        buffer = collector.read_db_block(db_number, min_offset, total_size)

        if buffer is None:
            logger.error(f"PLC [{plc_ip}] DB{db_number} 區塊讀取失敗。")
            for p in points:
                rows.append((p["id"], {"val": 0.0}, "ERROR"))
            continue

        # 拆解 Buffer
        for p in points:
            try:
                # 計算該點位在 buffer 內部的「相對偏移量」
                relative_offset = p["offset"] - min_offset

                # 進行解析
                parsed_value = parse_s7_value(buffer, relative_offset, p["data_type"])

                if p["data_type"].upper() == "REAL":
                    parsed_value = round(parsed_value, 2)  # 浮點數四捨五入優化

                logger.debug(f"解析成功 -> {p['name']}: {parsed_value}")

                # 若此點位已綁定感測器，把數值送進時序寫入器暫存；
                # stage() 內部會依規則判斷是否真的要寫入
                # （首次出現 / 數值變化 / 心跳補寫）。
                sensor_reading_writer.stage(p.get("sensor_id"), parsed_value, now)

                # 放入準備更新的陣列 (id, current_data_dict, plc_state)
                rows.append((p["id"], {"val": parsed_value}, "ONLINE"))

            except Exception as parse_err:
                logger.error(f"點位 [{p['name']}] 解析失敗: {parse_err}")
                rows.append((p["id"], {"val": 0.0}, "PARSE_ERROR"))

    # 採集完單台 PLC 後中斷連線，釋放 PLC 連線資源
    collector.disconnect()
    return rows


def collect_s7_data():
    """核心採集流程（多台 PLC 併發版本）"""
    logger.info("開始執行 S7 PLC 資料採集任務...")

    # 1. 載入配置並分組
    configs = load_plc_configs()
    if not configs:
        logger.warning("資料庫中沒有任何 S7 點位配置。")
        return

    grouped_data = group_configs_by_db(configs)

    # 準備存放最後要批次更新進資料庫的資料
    all_update_rows = []

    # 2. 併發連線多台 PLC：每台 PLC 各自跑在獨立執行緒，
    #    某一台離線卡在 connect timeout 不會拖到其他台的採集
    worker_count = min(MAX_WORKERS, len(grouped_data)) or 1
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_ip = {
            executor.submit(_collect_one_plc, plc_ip, db_groups): plc_ip
            for plc_ip, db_groups in grouped_data.items()
        }

        for future in as_completed(future_to_ip):
            plc_ip = future_to_ip[future]
            try:
                rows = future.result()
                all_update_rows.extend(rows)
            except Exception as e:
                logger.error(f"PLC [{plc_ip}] 採集執行緒發生未預期例外: {e}", exc_info=True)

    # 3. 一次性批量寫入 PostgreSQL
    if all_update_rows:
        logger.info(f"正在將 {len(all_update_rows)} 筆點位數據批次寫入資料庫...")
        batch_update_tia_data(all_update_rows)
        logger.info("資料庫批次寫入完成。")

    # 把這一輪暫存的感測器數值批次寫進 sensor_readings 時序表
    sensor_reading_writer.flush()

if __name__ == "__main__":
    try:
        # 初始化連線池
        DatabaseConnector.initialize_pool()

        # 獨立執行本檔案時（不透過 main.py 排程），先載入時序寫入器的初始快取，
        # 確保心跳補寫的時間判斷是接續資料庫裡既有的資料，不會從頭算。
        sensor_reading_writer.load_initial_cache()

        # 執行單次採集測試
        collect_s7_data()
        
    except Exception as e:
        logger.error(f"主程式執行異常: {e}", exc_info=True)
    finally:
        DatabaseConnector.close_pool()
        logger.info("採集任務結束。")