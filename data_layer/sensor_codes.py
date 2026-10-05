"""
data_layer/sensor_codes.py
==========================
感測器編號（sensor_code）自動編號。

既有的 sensor_code 都是流水號（1、2、…、182），MQTT 的 Key 與計算點運算式也用它，
所以新增感測器時不再讓人手動輸入（容易打錯、撞號、跳號），一律取「目前最大的
數字編號 + 1」。範本產生的「設備編號_後綴」這類非數字編號不參與計算。

    with DatabaseConnector.get_connection() as conn:
        with conn.cursor() as cur:
            code = allocate_sensor_codes(cur)[0]
            cur.execute("INSERT INTO sensors (..., sensor_code) VALUES (..., %s);", (..., code))

allocate_sensor_codes 會取 advisory lock 直到交易結束，兩個人同時新增時第二個人
會等第一個 commit 後才拿到下一號，不會撞號（撞了也有 sensor_code UNIQUE 擋著）。
"""

import re

# pg_advisory_xact_lock 的鍵值（綁定操作用 72_310_001，這裡用不同的值）
_LOCK_KEY = 72_310_002

# 18 位數以內才當成流水號，避免 bigint 溢位
_NUMERIC = r"^[0-9]{1,18}$"


def next_code(codes) -> int:
    """從既有編號算出下一個流水號（純函式，給匯入預覽與單元測試用）。"""
    nums = [int(c) for c in codes if isinstance(c, str) and re.fullmatch(r"[0-9]{1,18}", c)]
    return max(nums, default=0) + 1


def peek_next_sensor_code(cur) -> str:
    """只看不鎖：畫面上預告「下一個編號」用，實際寫入時要用 allocate_sensor_codes。"""
    cur.execute(f"SELECT COALESCE(MAX(sensor_code::bigint), 0) + 1 FROM sensors WHERE sensor_code ~ '{_NUMERIC}';")
    return str(cur.fetchone()[0])


def allocate_sensor_codes(cur, count: int = 1) -> list[str]:
    """在目前的交易中保留 count 個連號的新編號（持有鎖直到 commit / rollback）。"""
    cur.execute("SELECT pg_advisory_xact_lock(%s);", (_LOCK_KEY,))
    start = int(peek_next_sensor_code(cur))
    return [str(start + i) for i in range(count)]
