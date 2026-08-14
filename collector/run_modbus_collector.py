import logging
import sys
import struct
import json
from datetime import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from data_layer.db_connector import DatabaseConnector
from data_layer.timeseries_writer import sensor_reading_writer
from protocols.modbus_protocol import ModbusTCPCollector

# 配置 日誌
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# 併發連線的最大執行緒數上限（同時連線的設備台數上限，避免瞬間開太多 socket）
MAX_WORKERS = 8

# ----------------------------------------------------
# 🔍 核心工具 1：依據 data_type 自動判定所需的暫存器數量
# ----------------------------------------------------
def get_register_count(data_type: str) -> int:
    dt = str(data_type).lower().strip()
    if dt in ('bool', 'word', 'int', 'int16', 'uint16'):
        return 1
    elif dt in ('dint', 'uint32', 'float', 'float32', 'int32'):
        return 2
    elif dt in ('int64', 'uint64', 'float64', 'double'):
        return 4
    return 1

# ----------------------------------------------------
# 🔍 核心工具 2：處理 Byte Order 與 Word Order 解碼
# ----------------------------------------------------
def parse_registers(registers, data_type: str, byte_order: str = 'BIG', word_order: str = 'BIG'):
    if not registers:
        return None

    dt = str(data_type).lower().strip()

    # 若為布林值 (Coils / Discrete Inputs)
    if dt == 'bool' and isinstance(registers[0], bool):
        return 1.0 if registers[0] else 0.0

    # 1. Word Order 翻轉 (LITTLE 代表 Word Swap，如 CD AB 順序)
    if word_order and str(word_order).upper() == 'LITTLE':
        registers = registers[::-1]

    # 2. Byte Order 重組 (LITTLE 代表 小端序 Byte 對調)
    raw_bytes = bytearray()
    for r in registers:
        if byte_order and str(byte_order).upper() == 'LITTLE':
            raw_bytes.extend(struct.pack('<H', r))
        else:
            raw_bytes.extend(struct.pack('>H', r))

    # 3. 依據資料型態 unpack
    try:
        if dt == 'bool':
            return float(struct.unpack('>H', raw_bytes)[0] != 0)
        elif dt in ('word', 'uint16'):
            return float(struct.unpack('>H', raw_bytes)[0])
        elif dt in ('int', 'int16'):
            return float(struct.unpack('>h', raw_bytes)[0])
        elif dt in ('dint', 'int32'):
            return float(struct.unpack('>i', raw_bytes)[0])
        elif dt == 'uint32':
            return float(struct.unpack('>I', raw_bytes)[0])
        elif dt in ('float', 'float32'):
            return float(struct.unpack('>f', raw_bytes)[0])
        elif dt == 'int64':
            return float(struct.unpack('>q', raw_bytes)[0])
        elif dt == 'uint64':
            return float(struct.unpack('>Q', raw_bytes)[0])
        elif dt in ('float64', 'double'):
            return float(struct.unpack('>d', raw_bytes)[0])
        else:
            logger.warning(f"未知的 data_type: {data_type}")
            return None
    except Exception as e:
        logger.error(f"暫存器數值解碼失敗 ({data_type}): {e}")
        return None

# ----------------------------------------------------
# 🔍 核心工具 3：線性 Scaling 工程值轉換
# ----------------------------------------------------
def apply_linear_scaling(val, raw_min, raw_max, eng_min, eng_max):
    if val is None:
        return None
    if None in (raw_min, raw_max, eng_min, eng_max):
        return val
    if raw_max == raw_min:
        return val
    
    val = max(min(val, raw_max), raw_min)
    return ((val - raw_min) / (raw_max - raw_min)) * (eng_max - eng_min) + eng_min

# ----------------------------------------------------
# 🗄️ 資料庫讀取：撈取 modbus_scada 所有點位設定
# ----------------------------------------------------
def fetch_scada_tags():
    sql = """
        SELECT id, name, plc_ip, plc_port, slave_id, function_code, 
               start_address, data_type, raw_min, raw_max, eng_min, eng_max,
               byte_order, word_order, state_dictionary, sensor_id
        FROM modbus_scada;
    """
    try:
        # 使用 with 語法，離開區塊時自動釋放連線
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql)
                columns = [desc[0] for desc in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"從 modbus_scada 資料表讀取點位失敗: {e}")
        return []


# ----------------------------------------------------
# 🗄️ 資料庫寫入：將採集數據寫回 modbus_scada
# ----------------------------------------------------
def update_scada_results(results):
    if not results:
        return

    sql = """
        UPDATE modbus_scada
        SET current_value = %s,
            current_data = %s,
            plc_state = %s,
            last_update = %s
        WHERE id = %s;
    """
    try:
        # 使用 with 語法，離開區塊時自動釋放連線與 handle commit
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.executemany(sql, results)
                conn.commit()
                logger.info(f"💾 成功回寫 {len(results)} 筆點位數據至 modbus_scada！")
    except Exception as e:
        logger.error(f"寫入 modbus_scada 失敗: {e}")


# ----------------------------------------------------
# 🚀 單一設備採集邏輯（在獨立執行緒中執行）
# ----------------------------------------------------
def _collect_one_device(plc_ip, plc_port, slave_id, device_tags, now):
    """
    處理單一台設備 (plc_ip, plc_port, slave_id) 底下所有點位的採集。
    回傳這台設備產生的 results 清單，供最後統一批次寫入資料庫。
    這台設備連線逾時或失敗，只會拖慢自己這條執行緒，不影響其他設備。
    """
    results = []
    collector = ModbusTCPCollector(host=plc_ip, port=plc_port, slave_id=slave_id)

    # 連線失敗：該設備下所有點位自動標註為 OFFLINE
    if not collector.connect():
        logger.error(
            f"❌ 無法連線至 PLC [{plc_ip}:{plc_port}] (Slave ID={slave_id})"
        )
        for tag in device_tags:
            results.append((None, None, "OFFLINE", now, tag["id"]))
        return results

    # 連線成功：開始依 function_code 讀取暫存器
    for tag in device_tags:
        fc = tag["function_code"]
        addr = tag["start_address"]
        dt = tag["data_type"]
        count = get_register_count(dt)

        regs = None
        try:
            if fc == 1:
                regs = collector.read_coils(address=addr, count=1)
            elif fc == 2:
                regs = collector.read_discrete_inputs(
                    address=addr, count=1
                )
            elif fc == 3:
                regs = collector.read_holding_registers(
                    address=addr, count=count
                )
            elif fc == 4:
                regs = collector.read_input_registers(
                    address=addr, count=count
                )
        except Exception as e:
            logger.error(f"讀取點位 [{tag['name']}] 失敗: {e}")

        if regs is not None:
            # 1. 解碼暫存器原始數值
            raw_val = parse_registers(
                regs, dt, tag["byte_order"], tag["word_order"]
            )

            # 2. 進行工程 Scaling 計算
            final_val = apply_linear_scaling(
                raw_val,
                tag["raw_min"],
                tag["raw_max"],
                tag["eng_min"],
                tag["eng_max"],
            )

            if final_val is not None:
                rounded_val = round(final_val, 4)
                val_for_payload = rounded_val

                # 3. 狀態字典 Mapping：若匹配成功，直接將 val 替換為狀態文字
                state_dict = tag["state_dictionary"]
                if state_dict and isinstance(state_dict, dict):
                    str_key = (
                        str(int(final_val))
                        if hasattr(final_val, "is_integer")
                        and final_val.is_integer()
                        else str(final_val)
                    )
                    if str_key in state_dict:
                        val_for_payload = state_dict[
                            str_key
                        ]  # 直接覆蓋為文字

                # 4. 封裝 JSON Payload (val 直接為數字或轉換後的文字)
                current_data_payload = {"val": val_for_payload}

                logger.info(
                    f"   └─ 📊 [{tag['name']}] (ID:{tag['id']}) ="
                    f" {final_val} | JSON: {current_data_payload}"
                )

                # 5. 若此點位已綁定感測器，把「原始數值」(rounded_val) 送進
                #    時序寫入器暫存；stage() 內部會依規則判斷是否真的要寫入
                #    （首次出現 / 數值變化 / 心跳補寫），這裡不用自己判斷。
                sensor_reading_writer.stage(tag.get("sensor_id"), rounded_val, now)

                results.append((
                    rounded_val,  # 數值欄位 (current_value) 依然保留原始數字供數據分析
                    json.dumps(
                        current_data_payload, ensure_ascii=False
                    ),  # ensure_ascii=False 避免中文變成 unicode 碼
                    "ONLINE",
                    now,
                    tag["id"],
                ))
            else:
                results.append(
                    (None, None, "ERROR", now, tag["id"])
                )
        else:
            results.append((None, None, "ERROR", now, tag["id"]))

    collector.disconnect()
    return results


# ----------------------------------------------------
# 🚀 主程式執行邏輯（多台設備併發版本）
# ----------------------------------------------------
def main():
    if not DatabaseConnector.initialize_pool():
        logger.error("PostgreSQL 連線池初始化失敗，採集程序終止。")
        return

    print("\n🚀 [Modbus SCADA 動態採集服務] 啟動中...")

    tags = fetch_scada_tags()
    if not tags:
        logger.warning("modbus_scada 資料表中沒有找到任何點位設定。")
        return

    logger.info(
        f"📋 成功載入 {len(tags)} 個 SCADA"
        " 點位，準備按 IP/Port/Slave 分組併發連線..."
    )

    # 按 (plc_ip, plc_port, slave_id) 分組，減少重複開啟建立 Socket 的開銷
    grouped_tags = defaultdict(list)
    for tag in tags:
        key = (tag["plc_ip"], tag["plc_port"] or 502, tag["slave_id"] or 1)
        grouped_tags[key].append(tag)

    results_to_update = []
    now = datetime.now()

    # 併發連線多台設備：每台設備各自跑在獨立執行緒，
    # 某一台離線卡在 connect timeout 不會拖到其他台的採集
    worker_count = min(MAX_WORKERS, len(grouped_tags)) or 1
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_key = {
            executor.submit(
                _collect_one_device, plc_ip, plc_port, slave_id, device_tags, now
            ): (plc_ip, plc_port, slave_id)
            for (plc_ip, plc_port, slave_id), device_tags in grouped_tags.items()
        }

        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                results_to_update.extend(future.result())
            except Exception as e:
                logger.error(f"設備 {key} 採集執行緒發生未預期例外: {e}", exc_info=True)

    # 批次更新回資料庫
    update_scada_results(results_to_update)

    # 把這一輪暫存的感測器數值批次寫進 sensor_readings 時序表
    # （不管是被 main.py 呼叫，或單獨執行本檔案測試，都會在這裡自行 flush，
    #  確保時序資料不會漏寫）
    sensor_reading_writer.flush()
    logger.info("🏁 本輪 Modbus SCADA 數據採集與更新完畢。\n")

if __name__ == "__main__":
    # 獨立執行本檔案時（不透過 main.py 排程），先載入時序寫入器的初始快取，
    # 確保心跳補寫的時間判斷是接續資料庫裡既有的資料，不會從頭算。
    # （透過 main.py 啟動時，這個快取只在 main.py 啟動當下載入一次即可，
    #  不需要也不應該每輪都重載，所以特意放在這裡而不是 main() 內部。）
    sensor_reading_writer.load_initial_cache()
    main()