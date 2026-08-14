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

# 讀取採集週期
raw_interval = os.getenv("POLL_INTERVAL", "60.0").split('#')[0].strip()
try:
    POLL_INTERVAL = float(raw_interval)
except ValueError:
    logging.warning(f"⚠️ POLL_INTERVAL 讀取失敗 ('{raw_interval}')，改用預設值 5.0 秒")
    POLL_INTERVAL = 5.0

# 讀取 MQTT 是否啟用（🔧 修正：先前版本完全沒讀取這個環境變數，
# 導致 MQTT_ENABLED=false 時 MQTTPublisher 仍然無條件被建立、無條件發送）
raw_mqtt_enabled = os.getenv("MQTT_ENABLED", "true").split('#')[0].strip().lower()
MQTT_ENABLED = raw_mqtt_enabled not in ("false", "0", "no", "off")

# 匯入 DB 與 MQTT 模組
from data_layer.db_connector import DatabaseConnector
from data_layer.timeseries_writer import sensor_reading_writer
from messaging.mqtt_publisher import MQTTPublisher

# 匯入採集模組
# 🔥 注意：OPC UA 不再放進每輪的併發採集，改由常駐的訂閱服務處理（見下方）
try:
    from collector.run_modbus_collector import main as run_modbus_collector
    from collector.run_s7_collector import collect_s7_data as run_tia_collector
    from services.opcua_subscription_service import OPCUASubscriptionService

except ImportError as e:
    logging.error(f"❌ 匯入採集模組失敗: {e}")
    sys.exit(1)

# 控制主迴圈運行的旗標
is_running = True

def signal_handler(sig, frame):
    global is_running
    logging.info("🛑 收到終止訊號 (SIGINT/SIGTERM)，正在完成當前採集並準備關閉服務...")
    is_running = False

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ----------------------------------------------------
# 🚀 Modbus / TIA 併發執行：兩協議各自跑在獨立執行緒，
#    任一個整體卡住（例如該協議下所有設備都離線在等 timeout），
#    也不會拖到另一個協議完全沒開始採集。
#    （OPC UA 已改為常駐訂閱服務，不在此併發清單內）
# ----------------------------------------------------
def run_collectors_concurrently():
    tasks = {
        "Modbus": run_modbus_collector,
        "TIA(S7)": run_tia_collector,
    }

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
        # 嘗試轉成數字
        num = float(val)
        # 如果是整數 (例如 25.0)，轉成乾淨的整數 25
        return int(num) if num.is_integer() else num
    except (ValueError, TypeError):
        # 轉型失敗代表這是中文狀態字串 (如 '壓力到達')，直接保留原字串
        return str(val)
    
def fetch_latest_scada_map():
    """從資料庫一次抓出 Modbus & TIA & OPC UA 最新數值，轉為 MQTT 所需的 Dict 格式"""
    data_map = {}

    # 1. 撈 Modbus 點位 (拿掉強轉)
    sql_modbus = """
    SELECT name, current_data->>'val' AS current_value 
    FROM modbus_scada 
    WHERE current_data IS NOT NULL;
    """

    # 2. 撈 TIA 點位 (把 ::numeric 拿掉，避免 SQL 轉型失敗)
    sql_tia = """
    SELECT name, current_data->>'val' AS current_value 
    FROM tia_scada 
    WHERE current_data IS NOT NULL;
    """

    # 3. OPC UA 目前先不併入 MQTT 上傳（測試階段，只採集存 DB）
    #    之後要打包時，把下面這段 SQL 跟下方的 cur.execute(sql_opcua) 取消註解即可：
    # sql_opcua = """
    # SELECT server_name || '_' || browse_name AS name, current_data->>'val' AS current_value
    # FROM opcua_tags
    # WHERE current_data IS NOT NULL;
    # """

    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                # 執行 Modbus
                cur.execute(sql_modbus)
                for row in cur.fetchall():
                    data_map[row[0]] = _safe_parse_val(row[1])

                # 執行 TIA
                cur.execute(sql_tia)
                for row in cur.fetchall():
                    data_map[row[0]] = _safe_parse_val(row[1])

                # OPC UA 先不撈（測試階段），詳見上方註解

    except Exception as e:
        logging.error(f"❌ 讀取 MQTT 數據來源失敗: {e}")

    return data_map


def main():
    logging.info("🚀 [IIoT 數據採集與 MQTT 上傳主服務] 啟動中...")

    # 1. 初始化 PostgreSQL 連線池
    if not DatabaseConnector.initialize_pool():
        logging.error("❌ PostgreSQL 連線池初始化失敗，主服務無法啟動！")
        return

    # 1.5 載入時序寫入器（sensor_readings）的初始快取：
    #     把每個已綁定感測器目前資料庫裡最新一筆數值/時間讀回來，
    #     避免程式重啟後，心跳補寫的時間判斷從頭算、導致短時間內
    #     誤判「數值沒變也要寫」而灌入一堆不必要的心跳資料。
    sensor_reading_writer.load_initial_cache()

    # 2. 初始化 MQTT Publisher（尊重 MQTT_ENABLED 開關，false 時完全不建立連線、不發送任何訊息）
    mqtt_pub = None
    if MQTT_ENABLED:
        try:
            mqtt_pub = MQTTPublisher()
        except Exception as e:
            logging.error(f"⚠️ MQTT 客戶端初始化失敗: {e}")
    else:
        logging.info("🔕 MQTT_ENABLED=false，本次啟動不會建立 MQTT 連線、也不會發送任何訊息。")

    # 3. 啟動 OPC UA 訂閱服務（獨立背景執行緒，跟下方主迴圈完全脫鉤）
    opcua_service = OPCUASubscriptionService()
    opcua_service.start()

    logging.info(f"⏱️ 當前設定採集週期: {POLL_INTERVAL} 秒 (僅套用於 Modbus / TIA)")
    cycle_count = 0

    try:
        while is_running:
            cycle_count += 1
            start_time = time.time()
            logging.info(f"\n================ 🔄 第 {cycle_count} 輪採集開始 ================")

            # 步驟 1: 併發執行 Modbus / TIA (S7) 採集
            # （OPC UA 由常駐訂閱服務在背景持續處理，不佔用本迴圈時間）
            logging.info("📡 [1/2] 併發執行 Modbus / TIA(S7) 採集...")
            run_collectors_concurrently()

            # 步驟 2: 抓取 DB 最新點位並進行 MQTT 增量上傳
            if mqtt_pub:
                try:
                    logging.info("📤 [2/2] 正在執行 MQTT 增量上傳...")
                    current_scada_data = fetch_latest_scada_map()
                    mqtt_pub.publish_incremental(current_scada_data)
                except Exception as e:
                    logging.error(f"❌ MQTT 發送過程發生例外: {e}")

            # 步驟 3: 計算動態休眠時間
            elapsed_time = time.time() - start_time
            sleep_time = max(0.0, POLL_INTERVAL - elapsed_time)

            logging.info(f"⏱️ 本輪總耗時: {elapsed_time:.3f} 秒 | 預計休眠: {sleep_time:.3f} 秒")

            # 可即時響應 Ctrl+C 的 Sleep 邏輯
            sleep_end = time.time() + sleep_time
            while is_running and time.time() < sleep_end:
                time.sleep(0.1)

    except Exception as main_err:
        logging.error(f"💥 主迴圈發生未預期的例外: {main_err}", exc_info=True)
    finally:
        # 安全關閉資源
        opcua_service.stop()
        if mqtt_pub:
            mqtt_pub.close()
        DatabaseConnector.close_pool()
        logging.info("👋 已安全關閉 OPC UA 訂閱服務、MQTT 與 DB 連線池，主程式退出。")


if __name__ == "__main__":
    main()