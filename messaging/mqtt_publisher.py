from datetime import date, datetime
from decimal import Decimal
import json
import logging
import os
import time
import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


def _get_clean_env(key: str, default: str = "") -> str:
    """防呆解析器：讀取 .env 變數，自動去除尾端空白與 # 註解"""
    val = os.getenv(key, default)
    if val is None:
        return default
    return str(val).split("#")[0].strip()


def _json_serializer(obj):
    """安全的 JSON 序列化轉換器：

    1. PostgreSQL Decimal 型態 -> 自動轉成 float/int
    2. datetime/date 型態 -> 轉 ISO 字串
    3. 其他無法序列化的型態 -> 自動安全轉為 str (徹底避免 float() 轉換失敗引發崩潰)
    """
    if isinstance(obj, Decimal):
        try:
            val_float = float(obj)
            return (
                int(val_float) if val_float.is_integer() else val_float
            )
        except (ValueError, TypeError):
            return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


class MQTTPublisher:

    def __init__(self):
        # 1. 精準清洗並讀取 .env 設定（加入安全預設值，避免未設定時崩潰）
        self.broker = _get_clean_env("MQTT_BROKER", "localhost")
        self.port = int(_get_clean_env("MQTT_PORT", "1883"))
        self.username = _get_clean_env("MQTT_USER")
        self.password = _get_clean_env("MQTT_PASSWORD")
        self.group_id = _get_clean_env("MQTT_GROUP_ID", "default_group")
        self.topic = _get_clean_env("MQTT_TOPIC", "scada/data")

        # 心跳全量發送週期 (預設 300 秒)
        self.force_full_interval = float(
            _get_clean_env("MQTT_FORCE_FULL_INTERVAL", "300")
        )
        self.last_full_publish_time = 0

        # 記憶體快照 (比對增量變化)
        self.last_sent_cache = {}

        # 2. 初始化 Paho MQTT Client (相容 paho-mqtt v1 & v2)
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except AttributeError:
            self.client = mqtt.Client()

        # 3. 若有設定帳密則開啟認證
        if self.username:
            self.client.username_pw_set(self.username, self.password)

        self.is_connected = False
        self._connect()

    def _connect(self):
        """建立 MQTT 連線"""
        try:
            self.client.connect(self.broker, self.port, keepalive=60)
            self.client.loop_start()
            self.is_connected = True
            logger.info(f"📡 MQTT 連線成功 -> {self.broker}:{self.port}")
            logger.info(f"🎯 MQTT 發送目標 Topic: {self.topic}")
            logger.info(f"🏷️  MQTT Group ID: {self.group_id}")
        except Exception as e:
            logger.error(
                f"❌ MQTT 連線失敗 ({self.broker}:{self.port}): {e}"
            )
            self.is_connected = False

    def publish_incremental(self, current_data: dict):
        """傳入最新的點位字典，進行增量比對並發送 MQTT"""
        if not current_data:
            return

        # 斷線自動嘗試重連機制
        if not self.is_connected:
            logger.warning("⚠️ MQTT 未連線，嘗試重新建立連線...")
            self._connect()

        now_time = time.time()
        is_force_full = (
            now_time - self.last_full_publish_time
        ) >= self.force_full_interval

        changed_val_dict = {}

        for tag, val in current_data.items():
            # 強制全量上傳 或 數值有所變化（包含文字狀態改變，如 '待機' -> '壓力到達'）才加入 Payload
            if (
                is_force_full
                or tag not in self.last_sent_cache
                or self.last_sent_cache[tag] != val
            ):
                changed_val_dict[tag] = val

        # 如果沒有任何數據變化，且未到心跳時間，跳過發送
        if not changed_val_dict:
            logger.debug("⏭️ MQTT 數據無變化，跳過本輪增量上傳。")
            return

        # ISO 8601 時間戳記
        iso_ts = datetime.now().astimezone().isoformat(timespec="seconds")

        # 組合指定格式 Payload
        payload = {
            "d": {self.group_id: {"Val": changed_val_dict}},
            "ts": iso_ts,
        }

        try:
            # 💡 改用自訂 _json_serializer 序列化函式
            json_str = json.dumps(
                payload, ensure_ascii=False, default=_json_serializer
            )

            print(f"🔹 MQTT 發送 Payload: {json_str}")

            info = self.client.publish(self.topic, json_str, qos=1)

            # 更新成功發送後的快照快取
            self.last_sent_cache.update(changed_val_dict)

            if is_force_full:
                self.last_full_publish_time = now_time
                logger.info(
                    "🔄 [MQTT 全量心跳上傳] 成功發送"
                    f" {len(changed_val_dict)} 筆點位 -> {self.topic}"
                )
            else:
                logger.info(
                    "📤 [MQTT 增量上傳] 成功發送"
                    f" {len(changed_val_dict)} 筆變動點位 -> {self.topic}"
                )

        except Exception as e:
            logger.error(f"❌ MQTT 數據上傳失敗: {e}", exc_info=True)

    def close(self):
        """關閉 MQTT 連線"""
        try:
            self.client.loop_stop()
            self.client.disconnect()
            self.is_connected = False
            logger.info("👋 MQTT 連線已安全中斷。")
        except Exception:
            pass