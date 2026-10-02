"""
services/alarm/rules.py
=======================
警報判斷的純邏輯：不碰資料庫、不碰網路，方便單元測試（tests/test_alarm_rules.py）。

判斷規則：
    HH / H  數值 >  setpoint 觸發；觸發後要降到 setpoint - deadband 以下（含）才恢復
    L  / LL 數值 <  setpoint 觸發；觸發後要升到 setpoint + deadband 以上（含）才恢復
    EQ      數值 =  setpoint 觸發（例如故障碼 = 8）
    NE      數值 ≠  setpoint 觸發（例如「應為 1」的運轉訊號）

    觸發條件要「連續」成立 on_delay_sec 秒才真的發出警報（延遲觸發），
    濾掉瞬間突波；恢復則是條件一不成立就恢復（遲滯已由 deadband 處理）。

H / L 用「嚴格大於 / 小於」，與舊版「異常監控」分頁對 min_threshold /
max_threshold 的判斷一致，升級後同一個數值不會出現新舊兩邊結論不同的情況。
"""

from dataclasses import dataclass

PRIORITY_LABELS = {1: "緊急", 2: "高", 3: "中", 4: "低"}
PRIORITY_COLORS = {1: "red", 2: "orange", 3: "yellow", 4: "blue"}

ALARM_TYPE_LABELS = {
    "HH": "高高限",
    "H": "高限",
    "L": "低限",
    "LL": "低低限",
    "EQ": "等於",
    "NE": "不等於",
    "OFFLINE": "通訊中斷",
}

RULE_TYPES = ("HH", "H", "L", "LL", "EQ", "NE")

_EQ_TOLERANCE = 1e-9


@dataclass(frozen=True)
class AlarmRule:
    key: str                  # alarm_events.alarm_key，例如 rule:12 / limit:34:H
    source_type: str          # sensor_rule / sensor_limit
    sensor_id: int
    alarm_type: str           # RULE_TYPES 其中之一
    setpoint: float
    deadband: float = 0.0
    on_delay_sec: float = 0.0
    priority: int = 2
    message: str | None = None
    rule_id: int | None = None


def _equals(a: float, b: float) -> bool:
    return abs(a - b) <= _EQ_TOLERANCE * max(1.0, abs(a), abs(b))


def condition_active(alarm_type: str, setpoint: float, deadband: float,
                     value: float, currently_active: bool) -> bool:
    """這個數值在目前狀態下，警報條件是否成立。"""
    deadband = abs(deadband or 0.0)
    if alarm_type in ("HH", "H"):
        if currently_active:
            return value > setpoint - deadband
        return value > setpoint
    if alarm_type in ("LL", "L"):
        if currently_active:
            return value < setpoint + deadband
        return value < setpoint
    if alarm_type == "EQ":
        return _equals(value, setpoint)
    if alarm_type == "NE":
        return not _equals(value, setpoint)
    raise ValueError(f"未知的警報類型: {alarm_type}")


class OnDelayTracker:
    """
    延遲觸發：記錄每個 alarm_key「條件開始連續成立」的時間點。
    條件中斷一次就重新計時。
    """

    def __init__(self):
        self._since = {}

    def update(self, key: str, condition: bool, delay_sec: float, now: float) -> bool:
        """回傳是否已達觸發門檻。now 用單調時鐘秒數（time.monotonic()）。"""
        if not condition:
            self._since.pop(key, None)
            return False
        since = self._since.setdefault(key, now)
        return (now - since) >= (delay_sec or 0.0)

    def reset(self, key: str) -> None:
        self._since.pop(key, None)

    def pending_keys(self):
        return set(self._since)


def implicit_limit_rules(sensor_id: int, min_threshold, max_threshold, priority: int = 3):
    """
    sensors.min_threshold / max_threshold 轉成隱含的 L / H 規則，
    既有的上下限設定升級後直接就有警報，不需要另外搬到 alarm_rules。
    """
    rules = []
    if max_threshold is not None:
        rules.append(AlarmRule(
            key=f"limit:{sensor_id}:H", source_type="sensor_limit", sensor_id=sensor_id,
            alarm_type="H", setpoint=float(max_threshold), priority=priority,
        ))
    if min_threshold is not None:
        rules.append(AlarmRule(
            key=f"limit:{sensor_id}:L", source_type="sensor_limit", sensor_id=sensor_id,
            alarm_type="L", setpoint=float(min_threshold), priority=priority,
        ))
    return rules


def format_number(num: float) -> str:
    """
    工程數值的顯示格式：整數加千分位（1,000,075）；小數依大小保留位數
    （1,234.5 / 12.35 / 0.1235），不用科學記號 —— 累計電表動輒百萬，
    `1e+06` 對現場人員沒有意義。
    """
    if num != num:  # NaN
        return "—"
    a = abs(num)
    if a >= 1e12:
        return f"{num:.3g}"   # 位元組順序錯誤時常見的天文數字，不用把幾十位數全部印出來
    if float(num).is_integer():
        return f"{int(num):,}"
    if a >= 1000:
        text = f"{num:,.1f}"
    elif a >= 1:
        text = f"{num:,.2f}"
    elif a >= 0.0001:
        text = f"{num:.4f}"
    else:
        return f"{num:.3g}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_value(value, unit=None, state_dictionary=None) -> str:
    """數值轉顯示字串；有狀態字典時附上對應文字，例如 `8（大火燃燒）`。"""
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = format_number(num)
    if state_dictionary and isinstance(state_dictionary, dict):
        key = str(int(num)) if float(num).is_integer() else str(num)
        if key in state_dictionary:
            return f"{text}（{state_dictionary[key]}）"
    return f"{text} {unit}".strip() if unit else text


def build_message(rule: AlarmRule, sensor_label: str, value, unit=None, state_dictionary=None) -> str:
    """產生警報訊息，例如：`B03 / TT01（蒸氣溫度） 高限警報：數值 182 °C > 設定 180 °C`"""
    type_label = ALARM_TYPE_LABELS.get(rule.alarm_type, rule.alarm_type)
    comparator = {"HH": ">", "H": ">", "L": "<", "LL": "<", "EQ": "=", "NE": "≠"}.get(rule.alarm_type, "")
    if rule.alarm_type == "EQ":
        # 「數值 8（大火燃燒） = 設定 8（大火燃燒）」是廢話，等於警報只講目前值
        text = f"{sensor_label} 狀態警報：目前為 {format_value(value, unit, state_dictionary)}"
    else:
        text = (
            f"{sensor_label} {type_label}警報：數值 {format_value(value, unit, state_dictionary)} "
            f"{comparator} 設定 {format_value(rule.setpoint, unit, state_dictionary)}"
        )
    if rule.message:
        text += f" — {rule.message}"
    return text
