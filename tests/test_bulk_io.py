"""批次匯入的驗證規劃（data_layer/bulk_io.py），不需要資料庫。"""

import pandas as pd

from data_layer.bulk_io import (
    plan_alarm_rules,
    plan_devices,
    plan_modbus,
    plan_opcua_bindings,
    plan_sensors,
    p_bool,
    p_int,
    p_str,
)


def errors_by_row(plan):
    return {r: m for r, m in plan.errors}


def test_cell_parsers():
    assert p_str(1001.0) == "1001"          # Excel 把代碼讀成浮點數
    assert p_int("3.0") == 3
    assert p_bool("是") is True and p_bool("0") is False and p_bool(None) is True


def test_devices_auto_create_lines_and_detect_duplicates():
    ctx = {"devices": {"B03": {"device_id": 3, "device_code": "B03", "device_name": "3號", "device_type": None,
                               "manufacturer": None, "status": "active", "site_name": "廠", "line_name": "線"}},
           "sites": {"廠": 1}, "lines": {("廠", "線"): 1}}
    df = pd.DataFrame([
        {"device_code": "B03", "device_name": "3號", "site_name": "廠", "line_name": "線"},        # 沒變
        {"device_code": "B04", "device_name": "4號", "site_name": "廠", "line_name": "新線"},      # 新增 + 新產線
        {"device_code": "B05", "site_name": "廠", "line_name": "線"},
        {"device_code": "B05", "site_name": "廠", "line_name": "線"},                              # 重複
        {"device_code": "B06", "site_name": "新廠", "line_name": "A"},                             # 新廠區
    ])
    plan = plan_devices(df, ctx)
    assert plan.unchanged == 1
    assert [c.key for c in plan.inserts] == ["B04", "B06"]
    assert set(errors_by_row(plan)) == {4, 5}
    assert plan.extra["new_lines"] == [("廠", "新線"), ("新廠", "A")]
    assert plan.extra["new_sites"] == ["新廠"]


def test_sensors_validation_and_diff():
    ctx = {"devices": {"B03": {}}, "sensors": {"T1": {
        "sensor_id": 1, "device_code": "B03", "sensor_type": "temperature", "nickname": None, "unit": "°C",
        "min_threshold": None, "max_threshold": 180, "upload_condition": "on_change", "upload_threshold": None,
        "opcua_sampling_interval_ms": None, "opcua_deadband_type": "none", "opcua_deadband_value": None,
        "state_dictionary": '{"1": "運轉", "0": "停止"}'}}}
    df = pd.DataFrame([
        {"sensor_code": "T1", "device_code": "B03", "sensor_type": "temperature", "unit": "°C",
         "max_threshold": 185, "upload_condition": "on_change", "state_dictionary": '{"0": "停止", "1": "運轉"}'},
        {"sensor_code": "T2", "device_code": "XX", "sensor_type": "temperature"},
        {"sensor_code": "T3", "device_code": "B03", "sensor_type": "flow", "upload_condition": "threshold_percent"},
        {"sensor_code": "T4", "device_code": "B03", "sensor_type": "flow", "state_dictionary": "not json"},
        {"sensor_code": "T5", "device_code": "B03", "sensor_type": "flow", "min_threshold": 10, "max_threshold": 1},
    ])
    plan = plan_sensors(df, ctx)
    assert [c.key for c in plan.updates] == ["T1"]
    assert set(plan.updates[0].diff) == {"max_threshold"}      # 狀態字典順序不同不算變更
    errs = errors_by_row(plan)
    assert "不存在" in errs[3] and "upload_threshold" in errs[4] and "JSON" in errs[5] and "大於" in errs[6]


def _modbus_ctx():
    return {"modbus_v31": True, "sensor_ids": {"S1": 1, "S2": 2},
            "modbus": {10: {"id": 10, "name": "A", "plc_ip": "10.0.0.1", "plc_port": 502, "slave_id": 1,
                            "function_code": 3, "start_address": 0, "data_type": "float", "byte_order": "BIG",
                            "word_order": "LITTLE", "raw_min": None, "raw_max": None, "eng_min": None,
                            "eng_max": None, "unit": "V", "state_dictionary": None, "transport": "tcp",
                            "serial_settings": None, "enabled": True, "sensor_id": 1}},
            "bindings": [("modbus_scada", 10, 1), ("opcua_tags", 99, 2)]}


def test_modbus_plan_insert_update_and_binding_rules():
    base = {"plc_ip": "10.0.0.1", "slave_id": 1, "function_code": 3, "data_type": "float"}
    df = pd.DataFrame([
        {**base, "id": 10, "name": "A", "start_address": 0, "byte_order": "big", "word_order": "LITTLE",
         "unit": "V", "sensor_code": None},                                     # 解除 S1 綁定
        {**base, "name": "B", "start_address": 2, "sensor_code": "S1"},         # S1 改綁到新點位：允許
        {**base, "name": "C", "start_address": 4, "sensor_code": "S2"},         # S2 已綁 OPC UA：衝突
        {**base, "name": "D", "start_address": 6, "data_type": "float128"},
        {**base, "name": "E", "start_address": 8, "raw_min": 0},                # 換算參數不完整
        {**base, "id": 77, "name": "F", "start_address": 9},
    ])
    plan = plan_modbus(df, _modbus_ctx())
    errs = errors_by_row(plan)
    assert set(errs) == {4, 5, 6, 7}
    assert "已綁定" in errs[4] and "data_type" in errs[5] and "全填" in errs[6] and "不存在" in errs[7]
    assert [c.key for c in plan.updates] == ["#10"] and plan.updates[0].diff == {"sensor_id": [1, None]}
    assert [c.values["name"] for c in plan.inserts] == ["B"]


def test_modbus_duplicate_binding_within_file():
    base = {"plc_ip": "10.0.0.1", "slave_id": 1, "function_code": 3, "data_type": "word"}
    df = pd.DataFrame([{**base, "name": "X", "start_address": 1, "sensor_code": "S1"},
                       {**base, "name": "Y", "start_address": 2, "sensor_code": "S1"}])
    plan = plan_modbus(df, _modbus_ctx())
    assert len(plan.errors) >= 2


def test_opcua_bindings():
    ctx = {"sensor_ids": {"S1": 1, "S2": 2},
           "opcua_tags": {("SIM", "n1"): {"id": 1, "sensor_id": None}, ("SIM", "n2"): {"id": 2, "sensor_id": 2}},
           "bindings": [("opcua_tags", 2, 2)]}
    df = pd.DataFrame([
        {"server_name": "SIM", "node_id": "n1", "sensor_code": "S2"},    # S2 從 n2 移到 n1
        {"server_name": "SIM", "node_id": "n2", "sensor_code": None},
        {"server_name": "SIM", "node_id": "zz", "sensor_code": "S1"},
    ])
    plan = plan_opcua_bindings(df, ctx)
    assert list(errors_by_row(plan)) == [4]
    assert {c.key: c.values["sensor_id"] for c in plan.changes} == {"SIM / n1": 2, "SIM / n2": None}


def test_alarm_rules():
    ctx = {"sensor_ids": {"S1": 1}, "alarm_rules": {5: {"rule_id": 5, "sensor_id": 1, "alarm_type": "H",
                                                        "setpoint": 10.0, "deadband": 0.0, "on_delay_sec": 0,
                                                        "priority": 2, "message": None, "enabled": True}}}
    df = pd.DataFrame([
        {"rule_id": 5, "sensor_code": "S1", "alarm_type": "h", "setpoint": 12},
        {"sensor_code": "S1", "alarm_type": "LL", "setpoint": 1, "priority": 1},
        {"sensor_code": "S1", "alarm_type": "XX", "setpoint": 1},
        {"sensor_code": "S9", "alarm_type": "H", "setpoint": 1},
    ])
    plan = plan_alarm_rules(df, ctx)
    assert plan.updates[0].diff == {"setpoint": [10.0, 12.0]}
    assert len(plan.inserts) == 1 and set(errors_by_row(plan)) == {4, 5}


def test_missing_columns_reported():
    plan = plan_sensors(pd.DataFrame([{"sensor_code": "x"}]), {"devices": {}, "sensors": {}})
    assert plan.errors and "缺少欄位" in plan.errors[0][1]


def test_s7_plan_address_and_validation():
    from data_layer.bulk_io import plan_s7
    ctx = {"s7_v33": True, "sensor_ids": {"S1": 1}, "bindings": [],
           "s7": {3: {"id": 3, "name": "T", "plc_name": "PLC", "plc_ip": "10.0.0.5", "db_number": 1, "offset": 0,
                      "data_type": "REAL", "area": "DB", "bit_offset": 0, "rack": 0, "slot": 1, "unit": None,
                      "enabled": True, "sensor_id": None}}}
    df = pd.DataFrame([
        {"id": 3, "name": "T", "plc_ip": "10.0.0.5", "address": "DB1.DBD0", "data_type": "real", "sensor_code": "S1"},
        {"name": "Run", "plc_ip": "10.0.0.5", "address": "%DB1.DBX10.3", "data_type": "BOOL"},
        {"name": "MW", "plc_ip": "10.0.0.5", "area": "M", "offset": 20, "data_type": "INT"},
        {"name": "Bad", "plc_ip": "10.0.0.5", "address": "DB1.DBD4", "data_type": "BOOL"},
        {"name": "Bad2", "plc_ip": "10.0.0.5", "address": "DB1.DBD4", "data_type": "FLOAT"},
    ])
    plan = plan_s7(df, ctx)
    assert set(errors_by_row(plan)) == {5, 6}
    assert plan.updates[0].diff == {"sensor_id": [None, 1]}
    run = next(c for c in plan.inserts if c.values["name"] == "Run")
    assert (run.values["area"], run.values["db_number"], run.values["offset"], run.values["bit_offset"]) == ("DB", 1, 10, 3)
    mw = next(c for c in plan.inserts if c.values["name"] == "MW")
    assert mw.values["area"] == "M" and mw.values["db_number"] == 0


def test_sensors_blank_code_is_auto_numbered():
    ctx = {"devices": {"B03": {}}, "sensors": {"181": {}, "182": {}, "B03_TEMP": {}}}
    df = pd.DataFrame([
        {"sensor_code": "", "device_code": "B03", "sensor_type": "power"},
        {"sensor_code": None, "device_code": "B03", "sensor_type": "energy"},
        {"sensor_code": "500", "device_code": "B03", "sensor_type": "flow"},   # 指定編號：流水號接在它後面
    ])
    plan = plan_sensors(df, ctx)
    assert plan.ok
    auto = [c for c in plan.inserts if c.auto_key]
    assert len(auto) == 2 and [c.key for c in plan.inserts if not c.auto_key] == ["500"]
    assert any("501 ~ 502" in n for n in plan.notes)


def test_sensor_ref_accepts_template_dropdown_label():
    from data_layer.bulk_io import p_sensor_ref
    assert p_sensor_ref("45｜1-C10602-161｜洗滌塔即時功耗") == "45"
    assert p_sensor_ref(45.0) == "45" and p_sensor_ref("") is None
    ctx = {"sensor_ids": {"45": 1}, "opcua_tags": {("SIM", "n1"): {"id": 1, "sensor_id": None}}, "bindings": []}
    plan = plan_opcua_bindings(pd.DataFrame([{"server_name": "SIM", "node_id": "n1",
                                              "sensor_code": "45｜1-C10602-161｜洗滌塔"}]), ctx)
    assert plan.ok and plan.changes[0].values["sensor_id"] == 1


def test_next_sensor_code():
    from data_layer.sensor_codes import next_code
    assert next_code([]) == 1
    assert next_code(["1", "182", "B03_TEMP", "²", "0099"]) == 183
