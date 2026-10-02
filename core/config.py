"""
core/config.py
==============
統一的 .env 讀取工具。

專案裡原本 main.py / admin_app.py / mqtt_publisher.py / opcua_subscription_service.py
各自寫了一份「去掉尾端 # 註解再轉型」的小函式，行為大致相同但細節不一。新程式碼
一律改用這裡的版本；舊程式碼維持原狀，等之後有動到再順手換掉。

用法：
    from core.config import env_bool, env_float, env_int, env_str
    POLL_INTERVAL = env_float("POLL_INTERVAL", 60.0)
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 一律載入專案根目錄的 .env（不依賴目前工作目錄），已設定的環境變數優先
load_dotenv(dotenv_path=PROJECT_ROOT / ".env")

logger = logging.getLogger(__name__)

_FALSE_VALUES = ("false", "0", "no", "off")


def env_str(key: str, default: str = "") -> str:
    """讀取字串，去掉尾端 `# 註解` 與前後空白；未設定時回傳 default。"""
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.split("#")[0].strip()


def env_bool(key: str, default: bool = True) -> bool:
    raw = env_str(key, str(default)).lower()
    if not raw:
        return default
    return raw not in _FALSE_VALUES


def env_float(key: str, default: float) -> float:
    raw = env_str(key, str(default))
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"⚠️ .env 的 {key}='{raw}' 不是合法數字，改用預設值 {default}")
        return float(default)


def env_int(key: str, default: int) -> int:
    return int(env_float(key, default))


def env_list(key: str, default: str = "") -> list:
    """逗號分隔的清單，例如 ALARM_EMAIL_TO=a@x.com,b@x.com"""
    return [item.strip() for item in env_str(key, default).split(",") if item.strip()]


# 協議啟用開關：main.py 與 admin_app.py 共用同一份判斷
MODBUS_ENABLED = env_bool("MODBUS_ENABLED", True)
TIA_ENABLED = env_bool("TIA_ENABLED", True)
OPCUA_ENABLED = env_bool("OPCUA_ENABLED", True)

APP_VERSION = "3.4.0"

# 網頁顯示 / 日期選擇 / 匯出檔案使用的時區。容器沒設 TZ 時系統時區是 UTC，
# 不能依賴 datetime.now().astimezone()，所以獨立一個設定（預設台灣時間）。
try:
    LOCAL_TZ = ZoneInfo(env_str("SCADA_TIMEZONE", "Asia/Taipei"))
except Exception:
    logger.warning("⚠️ SCADA_TIMEZONE 不是合法的時區名稱，改用 Asia/Taipei")
    LOCAL_TZ = ZoneInfo("Asia/Taipei")


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)
