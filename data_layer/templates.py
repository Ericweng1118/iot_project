"""
data_layer/templates.py
=======================
設備範本（sql/016）：同型設備的「感測器 + 點位 + 警報規則」定義一次，套用到多台設備。

definition 格式：
    {
      "device":  {"device_type": "power_meter", "manufacturer": "XX"},
      "modbus":  {"transport": "tcp", "port": 502, "serial_settings": null},   # protocol=modbus 時
      "sensors": [
        {
          "suffix": "V",                       # 感測器編號 = {設備編號}_{suffix}
          "sensor_type": "voltage", "unit": "V", "nickname": "電壓",
          "upload_condition": "on_change", "upload_threshold": null,
          "min_threshold": null, "max_threshold": null, "state_dictionary": null,
          "point": {                           # protocol=modbus
            "name": "電壓", "function_code": 3, "start_address": 0, "data_type": "float32",
            "byte_order": "BIG", "word_order": "LITTLE",
            "raw_min": null, "raw_max": null, "eng_min": null, "eng_max": null
          },
          # 或 protocol=opcua："point": {"node_pattern": "ns=2;s={device}.Voltage"}
          "alarms": [{"alarm_type": "H", "setpoint": 250, "deadband": 0, "on_delay_sec": 0,
                      "priority": 2, "message": null}]
        }
      ]
    }

套用（每台設備一組參數）：
    共通：device_code、device_name（選填）
    modbus：plc_ip、slave_id、plc_port（選填，預設用範本）、address_offset（選填，同型設備位址整體平移時用）
    opcua：server_name、token（取代 node_pattern 裡的 {device}，預設等於 device_code）
"""

import json
from dataclasses import dataclass, field

import pandas as pd

from data_layer.bulk_io import (
    ALARM_TYPES,
    TRANSPORTS,
    UPLOAD_CONDITIONS,
    CellError,
    p_int,
    p_str,
)
from protocols.modbus_codec import DATA_TYPES, normalize_type

DEVICE_TOKEN = "{device}"
_SENSOR_FIELDS = ("sensor_type", "unit", "nickname", "upload_condition", "upload_threshold",
                  "min_threshold", "max_threshold", "state_dictionary")


# ------------------------------------------------------------------
# 從現有設備產生範本
# ------------------------------------------------------------------
def derive_suffix(sensor_code: str, device_code: str) -> str:
    """B03_TT01 + B03 → TT01；不是以設備編號開頭的感測器編號就整個當 suffix。"""
    for sep in ("_", "-", ".", ""):
        prefix = device_code + sep
        if sensor_code.startswith(prefix) and len(sensor_code) > len(prefix):
            return sensor_code[len(prefix):]
    return sensor_code


def node_pattern(node_id: str, token: str) -> str:
    """ns=2;s=B03.Temp + token B03 → ns=2;s={device}.Temp；找不到 token 時原樣保留（所有設備共用同一個節點）。"""
    if token and token in node_id:
        return node_id.replace(token, DEVICE_TOKEN)
    return node_id


def _clean(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if hasattr(v, "item"):          # numpy / Decimal → 原生型別，才能存成 JSON
        v = v.item()
    if isinstance(v, (int, float, str, bool, dict, list)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v)


def template_from_device(cur, device_code: str, token: str | None = None) -> tuple[dict, str]:
    """回傳 (definition, protocol)。protocol 依這台設備的感測器實際綁定的點位判斷。"""
    token = token or device_code
    cur.execute("SELECT device_type, manufacturer FROM devices WHERE device_code=%s;", (device_code,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"設備 {device_code} 不存在")
    definition = {"device": {"device_type": row[0], "manufacturer": row[1]}, "sensors": []}

    cur.execute("""
        SELECT s.sensor_id, s.sensor_code, s.sensor_type, s.unit, s.nickname, s.upload_condition,
               s.upload_threshold::float8, s.min_threshold::float8, s.max_threshold::float8, s.state_dictionary
        FROM sensors s JOIN devices d ON d.device_id = s.device_id
        WHERE d.device_code = %s ORDER BY s.sensor_code;""", (device_code,))
    sensors = cur.fetchall()
    # 計算點的結果感測器不放進範本：它們的值來自運算式，複製成一般感測器只會多出一堆沒有資料的空感測器
    if _has_table(cur, "calculated_points"):
        cur.execute("SELECT sensor_id FROM calculated_points;")
        calc_targets = {r[0] for r in cur.fetchall()}
        sensors = [r for r in sensors if r[0] not in calc_targets]
    has_transport = _has_column(cur, "modbus_scada", "transport")
    has_rules = _has_table(cur, "alarm_rules")
    protocols = set()
    for sid, code, *fields in sensors:
        item = {"suffix": derive_suffix(code, device_code)}
        item.update({k: _clean(v) for k, v in zip(_SENSOR_FIELDS, fields)})

        cur.execute(f"""
            SELECT name, function_code, start_address, data_type, byte_order, word_order,
                   raw_min, raw_max, eng_min, eng_max, plc_port,
                   {'transport, serial_settings' if has_transport else "'tcp', NULL"}
            FROM modbus_scada WHERE sensor_id=%s LIMIT 1;""", (sid,))
        mb = cur.fetchone()
        if mb:
            protocols.add("modbus")
            name = str(mb[0])
            item["point"] = {
                "name": name.replace(token, DEVICE_TOKEN) if token in name else name,
                "function_code": mb[1], "start_address": mb[2], "data_type": mb[3],
                "byte_order": mb[4], "word_order": mb[5],
                "raw_min": _clean(mb[6]), "raw_max": _clean(mb[7]), "eng_min": _clean(mb[8]), "eng_max": _clean(mb[9]),
            }
            definition.setdefault("modbus", {"port": mb[10] or 502, "transport": mb[11] or "tcp",
                                              "serial_settings": mb[12]})
        else:
            cur.execute("SELECT node_id FROM opcua_tags WHERE sensor_id=%s LIMIT 1;", (sid,))
            ua = cur.fetchone()
            if ua:
                protocols.add("opcua")
                item["point"] = {"node_pattern": node_pattern(ua[0], token)}

        if has_rules:
            cur.execute("""SELECT alarm_type, setpoint::float8, deadband::float8, on_delay_sec, priority, message
                           FROM alarm_rules WHERE sensor_id=%s AND enabled ORDER BY rule_id;""", (sid,))
            alarms = [dict(zip(("alarm_type", "setpoint", "deadband", "on_delay_sec", "priority", "message"),
                               [_clean(x) for x in r])) for r in cur.fetchall()]
            if alarms:
                item["alarms"] = alarms
        definition["sensors"].append(item)

    if len(protocols) > 1:
        raise ValueError("這台設備的感測器同時綁了 Modbus 與 OPC UA 點位，範本一次只支援一種協議")
    protocol = protocols.pop() if protocols else "none"
    if protocol != "modbus":
        definition.pop("modbus", None)
    return definition, protocol


def _has_column(cur, table, column):
    cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s);",
                (table, column))
    return bool(cur.fetchone()[0])


def _has_table(cur, table):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL;", (f"public.{table}",))
    return bool(cur.fetchone()[0])


# ------------------------------------------------------------------
# 範本內容驗證（手動編輯 / 上傳 JSON 時）
# ------------------------------------------------------------------
def validate_definition(definition: dict, protocol: str) -> list:
    errors = []
    sensors = definition.get("sensors") or []
    if not sensors:
        errors.append("範本至少要有一個感測器")
    seen = set()
    for i, s in enumerate(sensors, start=1):
        where = f"第 {i} 個感測器（{s.get('suffix')}）"
        suffix = str(s.get("suffix") or "").strip()
        if not suffix:
            errors.append(f"{where}：suffix 必填")
        elif suffix in seen:
            errors.append(f"{where}：suffix 重複")
        seen.add(suffix)
        if not str(s.get("sensor_type") or "").strip():
            errors.append(f"{where}：sensor_type 必填")
        if s.get("upload_condition") and s["upload_condition"] not in UPLOAD_CONDITIONS:
            errors.append(f"{where}：upload_condition 不正確")
        if s.get("upload_condition") in ("threshold_percent", "threshold_absolute") and s.get("upload_threshold") is None:
            errors.append(f"{where}：門檻型上傳條件需要 upload_threshold")
        point = s.get("point")
        if protocol == "modbus" and point:
            if normalize_type(point.get("data_type")) not in DATA_TYPES:
                errors.append(f"{where}：data_type 不正確")
            if point.get("function_code") not in (1, 2, 3, 4):
                errors.append(f"{where}：function_code 必須是 1~4")
            if not isinstance(point.get("start_address"), int) or point["start_address"] < 0:
                errors.append(f"{where}：start_address 必須是 0 以上的整數")
        if protocol == "opcua" and point and not point.get("node_pattern"):
            errors.append(f"{where}：node_pattern 必填")
        for a in s.get("alarms") or []:
            if a.get("alarm_type") not in ALARM_TYPES or a.get("setpoint") is None:
                errors.append(f"{where}：警報規則需要 alarm_type（{'/'.join(ALARM_TYPES)}）與 setpoint")
    mb = definition.get("modbus") or {}
    if protocol == "modbus" and mb.get("transport", "tcp") not in TRANSPORTS:
        errors.append("modbus.transport 不正確")
    return errors


# ------------------------------------------------------------------
# 套用範本：規劃（純邏輯）
# ------------------------------------------------------------------
@dataclass
class InstancePlan:
    devices: list = field(default_factory=list)     # 每台：{"device_code", "device_name", "sensors": [...]}
    errors: list = field(default_factory=list)      # [(列, 訊息)]

    @property
    def ok(self):
        return not self.errors and bool(self.devices)

    def counts(self):
        sensors = sum(len(d["sensors"]) for d in self.devices)
        points = sum(1 for d in self.devices for s in d["sensors"] if s.get("point"))
        alarms = sum(len(s.get("alarms") or []) for d in self.devices for s in d["sensors"])
        return {"devices": len(self.devices), "sensors": sensors, "points": points, "alarms": alarms}


def plan_instances(template: dict, protocol: str, instances: pd.DataFrame, ctx: dict) -> InstancePlan:
    """
    ctx: existing_devices(set)、existing_sensors(set)、opcua_tags {(server, node): {"id", "sensor_id"}}、
         modbus_points {(ip, port, slave, fc, address)}（避免重複建同一個位址）
    """
    plan = InstancePlan()
    seen_devices, seen_sensors, seen_nodes = set(), set(), set()
    mb_defaults = template.get("modbus") or {}
    for i, r in instances.reset_index(drop=True).iterrows():
        row_no = i + 1
        try:
            code = p_str(r.get("device_code"), True, 50)
            if code in ctx["existing_devices"]:
                raise CellError(f"設備 {code} 已存在")
            if code in seen_devices:
                raise CellError(f"設備 {code} 重複")
            seen_devices.add(code)
            name = p_str(r.get("device_name"), max_len=100)
            conn = {}
            if protocol == "modbus":
                conn = {
                    "plc_ip": p_str(r.get("plc_ip"), True),
                    "plc_port": p_int(r.get("plc_port"), lo=1, hi=65535) or int(mb_defaults.get("port") or 502),
                    "slave_id": p_int(r.get("slave_id"), True, 0, 255),
                    "offset": p_int(r.get("address_offset")) or 0,
                }
            elif protocol == "opcua":
                server = p_str(r.get("server_name"), True)
                conn = {"server": server, "token": p_str(r.get("token")) or code}
            sensors = []
            for s in template["sensors"]:
                sensor_code = f"{code}_{s['suffix']}"
                if len(sensor_code) > 50:
                    raise CellError(f"感測器編號 {sensor_code} 超過 50 字")
                if sensor_code in ctx["existing_sensors"] or sensor_code in seen_sensors:
                    raise CellError(f"感測器 {sensor_code} 已存在")
                seen_sensors.add(sensor_code)
                item = {"sensor_code": sensor_code, **{k: s.get(k) for k in _SENSOR_FIELDS},
                        "alarms": s.get("alarms") or []}
                point = s.get("point")
                if protocol == "modbus" and point:
                    address = int(point["start_address"]) + conn["offset"]
                    if not 0 <= address <= 65535:
                        raise CellError(f"{sensor_code} 平移後的位址 {address} 超出範圍")
                    key = (conn["plc_ip"], conn["plc_port"], conn["slave_id"], point["function_code"], address)
                    if key in ctx["modbus_points"]:
                        raise CellError(f"{conn['plc_ip']} 站號 {conn['slave_id']} 位址 {address} 已經有點位")
                    # 點位名稱：範本裡有 {device} 就替換，沒有就在前面加設備編號，避免多台設備的點位同名
                    raw_name = str(point.get("name") or s["suffix"])
                    name = raw_name.replace(DEVICE_TOKEN, code) if DEVICE_TOKEN in raw_name else f"{code} {raw_name}"
                    item["point"] = {**point, "start_address": address, "name": name}
                elif protocol == "opcua" and point:
                    node = point["node_pattern"].replace(DEVICE_TOKEN, conn["token"])
                    tag = ctx["opcua_tags"].get((conn["server"], node))
                    if tag is None:
                        raise CellError(f"{conn['server']} 找不到節點 {node}（請先瀏覽該 Server，或確認 token）")
                    if tag["sensor_id"] is not None or (conn["server"], node) in seen_nodes:
                        raise CellError(f"節點 {node} 已綁定其他感測器")
                    seen_nodes.add((conn["server"], node))
                    item["point"] = {"tag_id": tag["id"], "node_id": node}
                sensors.append(item)
        except CellError as e:
            plan.errors.append((row_no, str(e)))
            continue
        plan.devices.append({"device_code": code, "device_name": name, "conn": conn, "sensors": sensors})
    return plan


def load_instance_context(cur) -> dict:
    cur.execute("SELECT device_code FROM devices;")
    devices = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT sensor_code FROM sensors;")
    sensors = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT plc_ip, plc_port, slave_id, function_code, start_address FROM modbus_scada;")
    points = {(r[0], int(r[1] or 502), int(r[2]), int(r[3]), int(r[4])) for r in cur.fetchall()}
    cur.execute("SELECT id, server_name, node_id, sensor_id FROM opcua_tags;")
    tags = {(r[1], r[2]): {"id": r[0], "sensor_id": r[3]} for r in cur.fetchall()}
    return {"existing_devices": devices, "existing_sensors": sensors, "modbus_points": points, "opcua_tags": tags}


# ------------------------------------------------------------------
# 套用範本：寫入（呼叫端提供交易）
# ------------------------------------------------------------------
def apply_instances(cur, template: dict, protocol: str, plan: InstancePlan, line_id: int) -> dict:
    dev = template.get("device") or {}
    mb = template.get("modbus") or {}
    has_transport = _has_column(cur, "modbus_scada", "transport")
    has_rules = _has_table(cur, "alarm_rules")
    for d in plan.devices:
        cur.execute(
            "INSERT INTO devices (line_id, device_code, device_name, device_type, manufacturer) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING device_id;",
            (line_id, d["device_code"], d["device_name"], dev.get("device_type"), dev.get("manufacturer")),
        )
        device_id = cur.fetchone()[0]
        for s in d["sensors"]:
            sd = s.get("state_dictionary")
            cur.execute(
                """INSERT INTO sensors (device_id, sensor_code, sensor_type, unit, nickname, upload_condition,
                                        upload_threshold, min_threshold, max_threshold, state_dictionary)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING sensor_id;""",
                (device_id, s["sensor_code"], s["sensor_type"], s.get("unit"), s.get("nickname"),
                 s.get("upload_condition") or "on_change", s.get("upload_threshold"),
                 s.get("min_threshold"), s.get("max_threshold"),
                 json.dumps(sd, ensure_ascii=False) if isinstance(sd, dict) else sd),
            )
            sensor_id = cur.fetchone()[0]
            point = s.get("point")
            if protocol == "modbus" and point:
                c = d["conn"]
                cols = ["name", "plc_ip", "plc_port", "slave_id", "function_code", "start_address", "data_type",
                        "byte_order", "word_order", "raw_min", "raw_max", "eng_min", "eng_max", "unit",
                        "sensor_id", "plc_state"]
                vals = [point["name"], c["plc_ip"], c["plc_port"], c["slave_id"], point["function_code"],
                        point["start_address"], point["data_type"], point.get("byte_order") or "BIG",
                        point.get("word_order") or "BIG", point.get("raw_min"), point.get("raw_max"),
                        point.get("eng_min"), point.get("eng_max"), s.get("unit"), sensor_id, "OFFLINE"]
                if has_transport:
                    cols += ["transport", "serial_settings"]
                    vals += [mb.get("transport") or "tcp", mb.get("serial_settings")]
                cur.execute(f"INSERT INTO modbus_scada ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))});",
                            vals)
            elif protocol == "opcua" and point:
                cur.execute("UPDATE opcua_tags SET sensor_id=%s WHERE id=%s AND sensor_id IS NULL;",
                            (sensor_id, point["tag_id"]))
                if cur.rowcount != 1:
                    raise ValueError(f"節點 {point['node_id']} 在建立過程中被其他人綁定，已全部取消")
            if has_rules:
                for a in s.get("alarms") or []:
                    cur.execute(
                        """INSERT INTO alarm_rules (sensor_id, alarm_type, setpoint, deadband, on_delay_sec,
                                                    priority, message) VALUES (%s, %s, %s, %s, %s, %s, %s);""",
                        (sensor_id, a["alarm_type"], a["setpoint"], a.get("deadband") or 0,
                         a.get("on_delay_sec") or 0, a.get("priority") or 2, a.get("message")),
                    )
    return plan.counts()


# ------------------------------------------------------------------
# 範本 ↔ 表格（網頁編輯用：一個感測器一列）
# ------------------------------------------------------------------
POINT_COLUMNS = {
    "modbus": ["point_name", "function_code", "start_address", "data_type", "byte_order", "word_order",
               "raw_min", "raw_max", "eng_min", "eng_max"],
    "opcua": ["node_pattern"],
    "none": [],
}
SENSOR_COLUMNS = ["suffix", "sensor_type", "unit", "nickname", "upload_condition", "upload_threshold",
                  "min_threshold", "max_threshold", "state_dictionary"]


def definition_to_frame(definition: dict, protocol: str) -> pd.DataFrame:
    rows = []
    for s in definition.get("sensors") or []:
        row = {k: s.get(k) for k in SENSOR_COLUMNS}
        if isinstance(row["state_dictionary"], dict):
            row["state_dictionary"] = json.dumps(row["state_dictionary"], ensure_ascii=False)
        p = s.get("point") or {}
        for col in POINT_COLUMNS[protocol]:
            row[col] = p.get("name" if col == "point_name" else col)
        row["alarms"] = json.dumps(s.get("alarms") or [], ensure_ascii=False) if s.get("alarms") else ""
        rows.append(row)
    return pd.DataFrame(rows, columns=SENSOR_COLUMNS + POINT_COLUMNS[protocol] + ["alarms"])


def frame_to_definition(df: pd.DataFrame, protocol: str, base: dict) -> dict:
    def val(v):
        return None if v is None or (isinstance(v, float) and pd.isna(v)) or (isinstance(v, str) and not v.strip()) else v

    sensors = []
    for _, r in df.iterrows():
        if val(r.get("suffix")) is None:
            continue
        s = {k: val(r.get(k)) for k in SENSOR_COLUMNS}
        for k in ("upload_threshold", "min_threshold", "max_threshold"):
            s[k] = float(s[k]) if s[k] is not None else None
        if isinstance(s["state_dictionary"], str):
            try:
                s["state_dictionary"] = json.loads(s["state_dictionary"])
            except ValueError:
                raise ValueError(f"{s['suffix']} 的 state_dictionary 不是合法 JSON")
        if protocol == "modbus" and val(r.get("start_address")) is not None:
            s["point"] = {
                "name": val(r.get("point_name")) or s["suffix"],
                "function_code": int(float(r["function_code"])) if val(r.get("function_code")) is not None else 3,
                "start_address": int(float(r["start_address"])),
                "data_type": val(r.get("data_type")) or "uint16",
                "byte_order": (val(r.get("byte_order")) or "BIG").upper(),
                "word_order": (val(r.get("word_order")) or "BIG").upper(),
                **{k: float(r[k]) if val(r.get(k)) is not None else None
                   for k in ("raw_min", "raw_max", "eng_min", "eng_max")},
            }
        elif protocol == "opcua" and val(r.get("node_pattern")):
            s["point"] = {"node_pattern": str(r["node_pattern"]).strip()}
        alarms = val(r.get("alarms"))
        if alarms:
            try:
                s["alarms"] = json.loads(alarms)
            except ValueError:
                raise ValueError(f"{s['suffix']} 的 alarms 不是合法 JSON")
        sensors.append(s)
    out = {k: v for k, v in base.items() if k != "sensors"}
    out["sensors"] = sensors
    return out


def suggest_codes(prefix: str, start: int, count: int, width: int = 2) -> list:
    """PM + 1 + 3 → PM01, PM02, PM03（網頁「批次產生」用）。"""
    return [f"{prefix}{n:0{width}d}" for n in range(start, start + count)]

