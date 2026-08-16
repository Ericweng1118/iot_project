import os
import sys
import time
import signal
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

# 強制抓取 main.py 所在目錄的 .env
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

# 設定 Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)


def _get_env_bool(key: str, default: bool = True) -> bool:
    """讀取 .env 布林開關，容忍尾端空白與 # 註解"""
    raw = os.getenv(key, str(default)).split('#')[0].strip().lower()
    return raw not in ("false", "0", "no", "off")


# 讀取採集週期（僅套用於 Modbus / TIA）
raw_interval = os.getenv("POLL_INTERVAL", "60.0").split('#')[0].strip()
try:
    POLL_INTERVAL = float(raw_interval)
except ValueError:
    logging.warning(f"⚠️ POLL_INTERVAL 讀取失敗 ('{raw_interval}')，改用預設值 5.0 秒")
    POLL_INTERVAL = 5.0

# 讀取 MQTT 是否啟用
MQTT_ENABLED = _get_env_bool("MQTT_ENABLED", True)

# 🆕 協議啟用開關：這次升級聚焦強化 OPC UA，
#    Modbus / TIA(S7) 可透過 .env 完全屏蔽（不啟動採集執行緒，
#    admin_app.py 也會隱藏對應網頁分頁）。
MODBUS_ENABLED = _get_env_bool("MODBUS_ENABLED", True)
TIA_ENABLED = _get_env_bool("TIA_ENABLED", True)
OPCUA_ENABLED = _get_env_bool("OPCUA_ENABLED", True)

# 匯入 DB 與 MQTT 模組
from data_layer.db_connector import DatabaseConnector
from data_layer.timeseries_writer import sensor_reading_writer
from messaging.mqtt_publisher import MQTTPublisher

# 匯入採集模組：只匯入有啟用的協議，避免停用協議缺少對應套件
# （例如沒裝 python-snap7 / pymodbus）時反而導致程式無法啟動。
run_modbus_collector = None
run_tia_collector = None
OPCUASubscriptionService = None

try:
    if MODBUS_ENABLED:
        from collector.run_modbus_collector import main as run_modbus_collector
    if TIA_ENABLED:
        from collector.run_s7_collector import collect_s7_data as run_tia_collector
    if OPCUA_ENABLED:
        from services.opcua_subscription_service import OPCUASubscriptionService
except ImportError as e:
    logging.error(f"❌ 匯入採集模組失敗: {e}")
    sys.exit(1)

logging.info(
    f"🔧 協議啟用狀態 -> Modbus: {MODBUS_ENABLED} | TIA(S7): {TIA_ENABLED} | OPC UA: {OPCUA_ENABLED}"
)

# 控制主迴圈運行的旗標
is_running = True

def signal_handler(sig, frame):
    global is_running
    logging.info("🛑 收到終止訊號 (SIGINT/SIGTERM)，正在完成當前採集並準備關閉服務...")
    is_running = False

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def run_collectors_concurrently():
    """
    🚀 Modbus / TIA 併發執行：只執行 .env 有啟用的協議。
    兩者皆停用時（純 OPC UA 模式）直接跳過，不啟動任何執行緒。
    """
    tasks = {}
    if MODBUS_ENABLED and run_modbus_collector:
        tasks["Modbus"] = run_modbus_collector
    if TIA_ENABLED and run_tia_collector:
        tasks["TIA(S7)"] = run_tia_collector

    if not tasks:
        return

    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        future_to_name = {
            executor.submit(func): name for name, func in tasks.items()
        }
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                future.result()
                logging.info(f"✅ [{name}] 採集完成")
            except Exception as e:
                logging.error(f"❌ [{name}] 採集過程發生例外: {e}", exc_info=True)


def _safe_parse_val(val):
    """安全解析點位值：數字自動轉 float/int，中文狀態字串原樣保留"""
    if val is None:
        return 0.0
    try:
        num = float(val)
        return int(num) if num.is_integer() else num
    except (ValueError, TypeError):
        return str(val)


def fetch_latest_scada_map():
    """從資料庫抓出目前啟用中協議的最新數值，轉為 MQTT 所需的 Dict 格式"""
    data_map = {}

    sql_modbus = """
    SELECT name, current_data->>'val' AS current_value 
    FROM modbus_scada 
    WHERE current_data IS NOT NULL;
    """
    sql_tia = """
    SELECT name, current_data->>'val' AS current_value 
    FROM tia_scada 
    WHERE current_data IS NOT NULL;
    """

    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                if MODBUS_ENABLED:
                    cur.execute(sql_modbus)
                    for row in cur.fetchall():
                        data_map[row[0]] = _safe_parse_val(row[1])

                if TIA_ENABLED:
                    cur.execute(sql_tia)
                    for row in cur.fetchall():
                        data_map[row[0]] = _safe_parse_val(row[1])

                # OPC UA 目前先不併入 MQTT 上傳（測試階段，只採集存 DB）

    except Exception as e:
        logging.error(f"❌ 讀取 MQTT 數據來源失敗: {e}")

    return data_map


def main():
    logging.info("🚀 [IIoT 數據採集與 MQTT 上傳主服務] 啟動中...")

    # 1. 初始化 PostgreSQL 連線池
    if not DatabaseConnector.initialize_pool():
        logging.error("❌ PostgreSQL 連線池初始化失敗，主服務無法啟動！")
        return

    # 2. 🆕 啟動 sensor_readings 統一週期性寫入排程：
    #    載入初始快取後啟動背景執行緒，寫入週期由 .env 的
    #    SENSOR_READING_FLUSH_INTERVAL 控制，不再掛在各採集器的
    #    輪次結束時個別手動 flush。
    sensor_reading_writer.load_initial_cache()
    sensor_reading_writer.start()

    # 3. 初始化 MQTT Publisher
    mqtt_pub = None
    if MQTT_ENABLED:
        try:
            mqtt_pub = MQTTPublisher()
        except Exception as e:
            logging.error(f"⚠️ MQTT 客戶端初始化失敗: {e}")
    else:
        logging.info("🔕 MQTT_ENABLED=false，本次啟動不會建立 MQTT 連線、也不會發送任何訊息。")

    # 4. 啟動 OPC UA 訂閱服務（若啟用）
    opcua_service = None
    if OPCUA_ENABLED and OPCUASubscriptionService:
        opcua_service = OPCUASubscriptionService()
        opcua_service.start()
    else:
        logging.info("🔕 OPCUA_ENABLED=false，本次啟動不會啟動 OPC UA 訂閱服務。")

    if MODBUS_ENABLED or TIA_ENABLED:
        logging.info(f"⏱️ 當前設定採集週期: {POLL_INTERVAL} 秒 (套用於已啟用的 Modbus / TIA)")
    else:
        logging.info("⏱️ Modbus / TIA(S7) 皆已停用，主迴圈僅負責 MQTT 上傳排程（若啟用），採集全由 OPC UA 訂閱服務負責。")

    cycle_count = 0

    try:
        while is_running:
            cycle_count += 1
            start_time = time.time()
            logging.info(f"\n================ 🔄 第 {cycle_count} 輪採集開始 ================")

            if MODBUS_ENABLED or TIA_ENABLED:
                logging.info("📡 [1/2] 併發執行 Modbus / TIA(S7) 採集...")
                run_collectors_concurrently()
            else:
                logging.info("📡 [1/2] Modbus / TIA(S7) 皆已停用，略過本步驟。")

            if mqtt_pub:
                try:
                    logging.info("📤 [2/2] 正在執行 MQTT 增量上傳...")
                    current_scada_data = fetch_latest_scada_map()
                    mqtt_pub.publish_incremental(current_scada_data)
                except Exception as e:
                    logging.error(f"❌ MQTT 發送過程發生例外: {e}")

            elapsed_time = time.time() - start_time
            sleep_time = max(0.0, POLL_INTERVAL - elapsed_time)

            logging.info(f"⏱️ 本輪總耗時: {elapsed_time:.3f} 秒 | 預計休眠: {sleep_time:.3f} 秒")

            sleep_end = time.time() + sleep_time
            while is_running and time.time() < sleep_end:
                time.sleep(0.1)

    except Exception as main_err:
        logging.error(f"💥 主迴圈發生未預期的例外: {main_err}", exc_info=True)
    finally:
        if opcua_service:
            opcua_service.stop()
        sensor_reading_writer.stop()
        if mqtt_pub:
            mqtt_pub.close()
        DatabaseConnector.close_pool()
        logging.info("👋 已安全關閉所有服務與 DB 連線池，主程式退出。")


if __name__ == "__main__":
    main()