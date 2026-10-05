"""
data_layer/bindings.py
======================
點位 ↔ 感測器綁定的寫入操作：綁定、解除、改綁、互換。

規則：同一個感測器同時只能被一個來源使用（跨 Modbus / TIA / OPC UA / 計算點）。

每個操作都在呼叫端的單一交易內完成：
    1. 先取 advisory lock，所有綁定變更排隊執行，兩個人同時綁同一個感測器時
       後到的那個會看到前一個人的結果，而不是兩筆都寫進去。
    2. 用 SELECT ... FOR UPDATE 鎖住點位，並比對「畫面上看到的綁定」（expected），
       不一致代表別人剛改過，直接拒絕（樂觀鎖），避免蓋掉別人的修改。
    3. 檢查跨表衝突後才寫入。

呼叫端用法：
    with DatabaseConnector.get_connection() as conn:
        with conn.cursor() as cur:
            bind(cur, "opcua_tags", point_id, sensor_id, expected=None)
"""

# 允許操作的點位表 -> 主鍵欄位（表名會直接放進 SQL，只能用這份白名單）
POINT_TABLES = {
    "opcua_tags": "id",
    "modbus_scada": "id",
    "tia_scada": "id",
}

# pg_advisory_xact_lock 的鍵值，只要全專案的綁定操作都用同一個即可
_LOCK_KEY = 72_310_001

_UNSET = object()


class BindingError(Exception):
    """綁定規則不允許（衝突、資料已被別人改過、點位不存在），訊息可直接顯示給使用者。"""


def _check_table(table: str) -> str:
    if table not in POINT_TABLES:
        raise ValueError(f"不支援的點位表：{table}")
    return POINT_TABLES[table]


def _lock(cur) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(%s);", (_LOCK_KEY,))


def _current(cur, table: str, point_id: int):
    """鎖住點位並回傳目前的 sensor_id；點位不存在時丟 BindingError。"""
    id_col = _check_table(table)
    cur.execute(f"SELECT sensor_id FROM {table} WHERE {id_col} = %s FOR UPDATE;", (int(point_id),))
    row = cur.fetchone()
    if row is None:
        raise BindingError(f"點位 {table}.{point_id} 不存在（可能已被重新瀏覽或刪除），請重新整理頁面")
    return row[0]


def _check_expected(table: str, point_id: int, actual, expected) -> None:
    if expected is _UNSET:
        return
    norm = None if expected is None else int(expected)
    if actual != norm:
        raise BindingError(
            f"點位 {table}.{point_id} 的綁定剛被其他人修改過，請重新整理頁面後再操作"
        )


def sensor_owners(cur, sensor_id: int) -> list[tuple[str, int]]:
    """回傳目前使用這個感測器的所有 (表名, 點位 id)。"""
    parts = [f"SELECT '{t}', {c} FROM {t} WHERE sensor_id = %(s)s" for t, c in POINT_TABLES.items()]
    cur.execute("SELECT to_regclass('calculated_points') IS NOT NULL;")
    if cur.fetchone()[0]:
        parts.append("SELECT 'calculated_points', calc_id FROM calculated_points WHERE sensor_id = %(s)s")
    cur.execute(" UNION ALL ".join(parts) + ";", {"s": int(sensor_id)})
    return [(t, int(pid)) for t, pid in cur.fetchall()]


def _assert_free(cur, sensor_id: int, table: str, point_id: int, ignore=()) -> None:
    """感測器若已被【其他】點位使用就丟 BindingError；ignore 是本次操作會一起改掉的點位。"""
    skip = {(table, int(point_id))} | {(table, int(p)) for p in ignore}
    others = [o for o in sensor_owners(cur, sensor_id) if o not in skip]
    if others:
        where = "、".join(f"{t}.{pid}" for t, pid in others)
        raise BindingError(f"感測器已被其他點位使用：{where}。同一個感測器不能同時綁定多個點位")


def _set(cur, table: str, point_id: int, sensor_id) -> None:
    id_col = POINT_TABLES[table]
    cur.execute(
        f"UPDATE {table} SET sensor_id = %s WHERE {id_col} = %s;",
        (None if sensor_id is None else int(sensor_id), int(point_id)),
    )


def bind(cur, table: str, point_id: int, sensor_id: int, expected=_UNSET):
    """
    把點位綁到 sensor_id（點位原本有綁定就是改綁）。
    :param expected: 畫面上看到的這個點位目前的 sensor_id（未綁定傳 None）；不傳就不檢查
    :return: 點位原本的 sensor_id
    """
    _check_table(table)
    _lock(cur)
    old = _current(cur, table, point_id)
    _check_expected(table, point_id, old, expected)
    _assert_free(cur, sensor_id, table, point_id)
    _set(cur, table, point_id, sensor_id)
    return old


def unbind(cur, table: str, expected: dict) -> dict:
    """
    解除多個點位的綁定。
    :param expected: {point_id: 畫面上看到的 sensor_id}，任何一筆對不上就整批不做
    :return: {point_id: 原本的 sensor_id}
    """
    _check_table(table)
    _lock(cur)
    old = {}
    for pid, exp in expected.items():
        cur_sid = _current(cur, table, pid)
        _check_expected(table, pid, cur_sid, exp)
        old[int(pid)] = cur_sid
    for pid in old:
        _set(cur, table, pid, None)
    return old


def swap(cur, table: str, point_a: int, point_b: int, expected_a=_UNSET, expected_b=_UNSET) -> None:
    """互換同一張表兩個點位的感測器（任一邊可以是未綁定）。"""
    _check_table(table)
    if int(point_a) == int(point_b):
        raise BindingError("請選擇兩個不同的點位")
    _lock(cur)
    sa = _current(cur, table, point_a)
    sb = _current(cur, table, point_b)
    _check_expected(table, point_a, sa, expected_a)
    _check_expected(table, point_b, sb, expected_b)
    # 先清空再寫入：即使 sensor_id 有非延遲的唯一約束也不會在中途撞到
    _set(cur, table, point_a, None)
    _set(cur, table, point_b, sa)
    _set(cur, table, point_a, sb)
