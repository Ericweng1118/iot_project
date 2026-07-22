import os
import json
import logging
import time
from datetime import datetime
import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


def _get_clean_env(key: str, default: str = "") -> str:
    """防呆解析器：讀取 .env 變數，自動去除尾端空白與 # 註解"""
    val = os.getenv(key, default)
    if val is None:
        return default
    return str(val).split('#')[0].strip()


class MQTTPublisher:
    def __init__(self):
        # 1. 精準清洗並讀取 .env 設定
        self.broker = _get_clean_env("MQTT_BROKER")
        self.port = int(_get_clean_env("MQTT_PORT"))
        self.username = _get_clean_env("MQTT_USER")
        self.password = _get_clean_env("MQTT_PASSWORD")
        self.group_id = _get_clean_env("MQTT_GROUP_ID")
        self.topic = _get_clean_env("MQTT_TOPIC",)

        # 心跳全量發送週期 (預設 300 秒)
        self.force_full_interval = float(_get_clean_env("MQTT_FORCE_FULL_INTERVAL", "300"))
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
            logger.error(f"❌ MQTT 連線失敗 ({self.broker}:{self.port}): {e}")
            self.is_connected = False

    def publish_incremental(self, current_data: dict):
        """
        傳入最新的點位字典，進行增量比對並發送 MQTT
        """
        if not current_data:
            return

        now_time = time.time()
        is_force_full = (now_time - self.last_full_publish_time) >= self.force_full_interval

        changed_val_dict = {}

        for tag, val in current_data.items():
            # 強制全量上傳 或 數值有所變化（或新點位）才加入 Payload
            if is_force_full or tag not in self.last_sent_cache or self.last_sent_cache[tag] != val:
                changed_val_dict[tag] = val

        # 如果沒有任何數據變化，且未到心跳時間，跳過發送
        if not changed_val_dict:
            logger.debug("⏭️ MQTT 數據無變化，跳過本輪增量上傳。")
            return

        # ISO 8601 時間戳記 (例: 2026-07-21T16:36:00+08:00)
        iso_ts = datetime.now().astimezone().isoformat(timespec='seconds')

        # 組合指定格式 Payload
        payload = {
            "d": {
                self.group_id: {
                    "Val": changed_val_dict
                }
            },
            "ts": iso_ts
        }
        print(f"🔹 MQTT 發送 Payload: {json.dumps(payload, ensure_ascii=False)}")

        try:
            # 💡 加上 default=float，避免 PostgreSQL Decimal 型態導致 JSON 序列化崩潰
            json_str = json.dumps(payload, ensure_ascii=False, default=float)

            info = self.client.publish(self.topic, json_str, qos=1)

            # 更新成功發送後的快照快取
            self.last_sent_cache.update(changed_val_dict)

            if is_force_full:
                self.last_full_publish_time = now_time
                logger.info(f"🔄 [MQTT 全量心跳上傳] 成功發送 {len(changed_val_dict)} 筆點位 -> {self.topic}")
            else:
                logger.info(f"📤 [MQTT 增量上傳] 成功發送 {len(changed_val_dict)} 筆變動點位 -> {self.topic}")

        except Exception as e:
            logger.error(f"❌ MQTT 數據上傳失敗: {e}", exc_info=True)

    def close(self):
        """關閉 MQTT 連線"""
        try:
            self.client.loop_stop()
            self.client.disconnect()
            logger.info("👋 MQTT 連線已安全中斷。")
        except Exception:
            pass