"""稽核紀錄的 detail 格式（web/common.py）：PME 依 audit_log 偵測設定變更，格式約定見 docs/PME_INTERFACE.md。"""

import pandas as pd

from web.common import attach_sensor_ids, frame_changes


def _frames():
    before = pd.DataFrame({"id": [1, 2, 3], "eng_max": [100.0, 200.0, 300.0], "sensor_id": [11, None, 13]})
    after = before.copy()
    after.loc[0, "eng_max"] = 150.0
    after.loc[1, "eng_max"] = 250.0
    return before, after


def test_attach_sensor_ids_adds_bound_sensor_per_changed_row():
    before, after = _frames()
    changes = frame_changes(before.drop(columns=["sensor_id"]), after.drop(columns=["sensor_id"]), "id")
    result = attach_sensor_ids(changes, before)
    assert result["1"]["sensor_id"] == 11
    assert result["2"]["sensor_id"] is None            # 未綁定的點位
    assert "3" not in result                           # 沒改的列不出現
    assert result["1"]["eng_max"] == [100.0, 150.0]


def test_attach_sensor_ids_skips_non_row_entries():
    before, _ = _frames()
    assert attach_sensor_ids({"note": "無法比對"}, before) == {"note": "無法比對"}
    assert attach_sensor_ids({"_truncated": True}, before) == {"_truncated": True}
    assert attach_sensor_ids({"1": {"x": [1, 2]}}, before.drop(columns=["sensor_id"])) == {"1": {"x": [1, 2]}}


class _FakeCursor:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def execute(self, sql, params):
        if self.fail:
            raise RuntimeError("permission denied for table audit_log")
        self.calls.append((sql, params))


def test_audit_in_tx_writes_on_callers_cursor():
    from core.audit import audit_in_tx

    cur = _FakeCursor()
    audit_in_tx(cur, "alice", "modbus.update", "modbus_scada", {"1": {"eng_max": [100, 150], "sensor_id": 11}})
    sql, params = cur.calls[0]
    assert sql.startswith("INSERT INTO audit_log")
    assert params[:3] == ("alice", "modbus.update", "modbus_scada")
    assert '"sensor_id": 11' in params[3]


def test_audit_in_tx_raises_so_config_change_rolls_back():
    # 與 audit() 不同：失敗不能吞掉，否則設定改了 PME 卻看不到（見 core/audit.py）
    import pytest

    from core.audit import audit_in_tx

    with pytest.raises(RuntimeError):
        audit_in_tx(_FakeCursor(fail=True), "alice", "tia.update", "tia_scada", None)
