"""設備範本的純邏輯（data_layer/templates.py）。"""

import pandas as pd

from data_layer import templates as T

MODBUS_TPL = {
    "device": {"device_type": "meter"},
    "modbus": {"transport": "tcp", "port": 502},
    "sensors": [
        {"suffix": "V", "sensor_type": "voltage", "unit": "V", "upload_condition": "on_change",
         "point": {"name": "電壓", "function_code": 3, "start_address": 0, "data_type": "float32",
                   "byte_order": "BIG", "word_order": "LITTLE"},
         "alarms": [{"alarm_type": "H", "setpoint": 250, "priority": 2}]},
        {"suffix": "KWH", "sensor_type": "energy", "unit": "kWh", "upload_condition": "on_change",
         "point": {"name": "{device}-累計", "function_code": 3, "start_address": 10, "data_type": "uint32"}},
    ],
}


def ctx(**kw):
    base = {"existing_devices": set(), "existing_sensors": set(), "modbus_points": set(), "opcua_tags": {}}
    base.update(kw)
    return base


def test_derive_suffix_and_node_pattern():
    assert T.derive_suffix("B03_TT01", "B03") == "TT01"
    assert T.derive_suffix("B03-TT01", "B03") == "TT01"
    assert T.derive_suffix("TEMP1", "B03") == "TEMP1"
    assert T.node_pattern("ns=2;s=B03.Temp", "B03") == "ns=2;s={device}.Temp"
    assert T.node_pattern("ns=2;s=Common.Temp", "B03") == "ns=2;s=Common.Temp"


def test_validate_definition():
    assert T.validate_definition(MODBUS_TPL, "modbus") == []
    bad = {"sensors": [{"suffix": "A", "sensor_type": "x", "point": {"function_code": 9, "start_address": -1,
                                                                    "data_type": "nope"}},
                       {"suffix": "A", "sensor_type": ""}]}
    errs = T.validate_definition(bad, "modbus")
    assert any("data_type" in e for e in errs) and any("function_code" in e for e in errs)
    assert any("重複" in e for e in errs) and any("sensor_type" in e for e in errs)


def test_plan_modbus_instances():
    df = pd.DataFrame([
        {"device_code": "PM01", "plc_ip": "10.0.0.1", "slave_id": 1},
        {"device_code": "PM02", "plc_ip": "10.0.0.1", "slave_id": 2, "address_offset": 100},
        {"device_code": "PM03", "plc_ip": "10.0.0.1", "slave_id": 3},           # 位址已被佔用
        {"device_code": "OLD", "plc_ip": "10.0.0.1", "slave_id": 4},            # 設備已存在
        {"device_code": "PM01", "plc_ip": "10.0.0.1", "slave_id": 5},           # 重複
    ])
    plan = T.plan_instances(MODBUS_TPL, "modbus", df,
                            ctx(existing_devices={"OLD"}, modbus_points={("10.0.0.1", 502, 3, 3, 0)}))
    assert [r for r, _ in plan.errors] == [3, 4, 5]
    assert plan.counts() == {"devices": 2, "sensors": 4, "points": 4, "alarms": 2}
    pm2 = plan.devices[1]["sensors"]
    assert pm2[0]["sensor_code"] == "PM02_V" and pm2[0]["point"]["start_address"] == 100
    assert pm2[0]["point"]["name"] == "PM02 電壓"
    assert pm2[1]["point"]["name"] == "PM02-累計"


def test_plan_opcua_instances():
    tpl = {"sensors": [{"suffix": "T", "sensor_type": "temperature", "point": {"node_pattern": "ns=2;s={device}.Temp"}}]}
    tags = {("SIM", "ns=2;s=Boiler1.Temp"): {"id": 1, "sensor_id": None},
            ("SIM", "ns=2;s=Boiler2.Temp"): {"id": 2, "sensor_id": 9}}
    df = pd.DataFrame([
        {"device_code": "B01", "server_name": "SIM", "token": "Boiler1"},
        {"device_code": "B02", "server_name": "SIM", "token": "Boiler2"},       # 已被綁定
        {"device_code": "B03", "server_name": "SIM"},                            # token 預設 = B03 → 找不到
    ])
    plan = T.plan_instances(tpl, "opcua", df, ctx(opcua_tags=tags))
    assert [r for r, _ in plan.errors] == [2, 3]
    assert plan.devices[0]["sensors"][0]["point"] == {"tag_id": 1, "node_id": "ns=2;s=Boiler1.Temp"}


def test_frame_roundtrip():
    frame = T.definition_to_frame(MODBUS_TPL, "modbus")
    assert list(frame["suffix"]) == ["V", "KWH"]
    back = T.frame_to_definition(frame, "modbus", MODBUS_TPL)
    assert back["modbus"] == MODBUS_TPL["modbus"]
    assert back["sensors"][0]["alarms"] == MODBUS_TPL["sensors"][0]["alarms"]
    assert back["sensors"][1]["point"]["start_address"] == 10
    assert T.validate_definition(back, "modbus") == []


def test_suggest_codes():
    assert T.suggest_codes("PM", 9, 3) == ["PM09", "PM10", "PM11"]
