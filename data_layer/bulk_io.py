"""
data_layer/bulk_io.py
=====================
設定資料的批次匯入 / 匯出（CSV、Excel）。網頁「批次匯入匯出」頁面使用。

流程：匯出目前資料 → 在 Excel 編輯 → 上傳 → plan()：逐列驗證、比對資料庫，
產生「新增 / 更新（只列有變的欄位）/ 錯誤」預覽 → 使用者確認 → apply()：單一交易寫入，
任何一筆失敗整批回滾。

支援的資料：
    devices          設備（廠區 / 產線不存在時自動建立）
    sensors          感測器（含上傳條件、deadband、上下限、狀態字典）
    modbus           Modbus 點位（id 空白 = 新增，有 id = 更新；可用 sensor_code 綁定感測器）
    opcua_bindings   OPC UA 點位與感測器的綁定（點位本身由瀏覽產生，這裡只改綁定）
    alarm_rules      警報規則

驗證邏輯（plan_*）只依賴 load_context() 撈出來的查詢表，不直接碰資料庫，方便單元測試。
"""

import json
import math
from dataclasses import dataclass, field

import pandas as pd

from protocols import s7_codec
from protocols.modbus_codec import DATA_TYPES, normalize_type

UPLOAD_CONDITIONS = ("always", "on_change", "threshold_percent", "threshold_absolute")
DEADBAND_TYPES = ("none", "percent", "absolute")
ALARM_TYPES = ("HH", "H", "L", "LL", "EQ", "NE")
TRANSPORTS = ("tcp", "rtu_over_tcp", "rtu")


# ------------------------------------------------------------------
# 欄位值解析（Excel 讀進來的型態很雜：1.0、"1"、True、"是"、NaN…）
# ------------------------------------------------------------------
class CellError(ValueError):
    pass


def _blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return isinstance(v, str) and not v.strip()


def p_str(v, required=False, max_len=None):
    if _blank(v):
        if required:
            raise CellError("必填")
        return None
    s = str(v).strip()
    if isinstance(v, float) and v.is_integer():
        s = str(int(v))           # Excel 把 0001 這種代碼讀成 1.0 時至少不要變成 "1.0"
    if max_len and len(s) > max_len:
        raise CellError(f"長度超過 {max_len}")
    return s


def p_int(v, required=False, lo=None, hi=None):
    if _blank(v):
        if required:
            raise CellError("必填")
        return None
    try:
        f = float(str(v).strip())
    except ValueError:
        raise CellError(f"不是整數：{v}")
    if not f.is_integer():
        raise CellError(f"不是整數：{v}")
    n = int(f)
    if lo is not None and n < lo or hi is not None and n > hi:
        raise CellError(f"超出範圍 {lo}~{hi}：{n}")
    return n


def p_float(v, required=False):
    if _blank(v):
        if required:
            raise CellError("必填")
        return None
    try:
        f = float(str(v).strip())
    except ValueError:
        raise CellError(f"不是數字：{v}")
    if math.isnan(f) or math.isinf(f):
        raise CellError(f"不是有效數字：{v}")
    return f


def p_bool(v, default=True):
    if _blank(v):
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "1.0", "true", "t", "yes", "y", "是", "啟用", "v", "✓"):
        return True
    if s in ("0", "0.0", "false", "f", "no", "n", "否", "停用", "x"):
        return False
    raise CellError(f"不是 是/否：{v}")


def p_choice(v, choices, default=None, upper=False):
    if _blank(v):
        if default is None:
            raise CellError(f"必填，可用值：{'/'.join(choices)}")
        return default
    s = str(v).strip()
    s = s.upper() if upper else s
    if s not in choices:
        raise CellError(f"不是可用的值（{'/'.join(choices)}）：{v}")
    return s


def p_json_dict(v):
    if _blank(v):
        return None
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    try:
        parsed = json.loads(str(v))
    except ValueError:
        raise CellError("不是合法的 JSON，例如 {\"1\": \"運轉\"}")
    if not isinstance(parsed, dict):
        raise CellError("狀態字典必須是 JSON 物件，例如 {\"1\": \"運轉\"}")
    return json.dumps(parsed, ensure_ascii=False) if parsed else None


# ------------------------------------------------------------------
# 規劃結果
# ------------------------------------------------------------------
@dataclass
class Change:
    action: str          # insert / update
    key: str             # 給人看的識別（例如 sensor_code）
    values: dict         # 要寫入的欄位
    row_no: int          # Excel 列號（標題列是第 1 列）
    diff: dict = field(default_factory=dict)   # update 時：{欄位: [舊, 新]}
    target_id: object = None                   # update 時的主鍵


@dataclass
class Plan:
    entity: str
    changes: list = field(default_factory=list)
    errors: list = field(default_factory=list)      # [(row_no, 訊息)]
    unchanged: int = 0
    notes: list = field(default_factory=list)       # 例如「將自動建立產線 X」
    extra: dict = field(default_factory=dict)       # apply 需要的額外資訊

    @property
    def inserts(self):
        return [c for c in self.changes if c.action == "insert"]

    @property
    def updates(self):
        return [c for c in self.changes if c.action == "update"]

    @property
    def ok(self) -> bool:
        return not self.errors


def _row_no(i: int) -> int:
    return i + 2


def _as_json(v):
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str) and v.strip()[:1] in ("{", "["):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return None


def _same(a, b) -> bool:
    if _blank(a) and _blank(b):
        return True
    if _blank(a) or _blank(b):
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        pass
    ja, jb = _as_json(a), _as_json(b)
    if ja is not None and jb is not None:
        return ja == jb          # jsonb 會重排鍵的順序，要比內容不比字串
    return a == b or str(a) == str(b)


def _diff(existing: dict, values: dict) -> dict:
    return {k: [existing.get(k), v] for k, v in values.items() if not _same(existing.get(k), v)}


def _require_columns(df, required, plan) -> bool:
    missing = [c for c in required if c not in df.columns]
    if missing:
        plan.errors.append((1, f"缺少欄位：{', '.join(missing)}（請用匯出的檔案當範本）"))
        return False
    return True


def _upsert(plan, existing, key, values, row_no, target_id=None):
    if existing is None:
        plan.changes.append(Change("insert", key, values, row_no))
        return
    diff = _diff(existing, values)
    if diff:
        plan.changes.append(Change("update", key, values, row_no, diff, target_id))
    else:
        plan.unchanged += 1


def _check_duplicates(df, column, plan):
    if column not in df.columns:
        return set()
    keys = df[column].map(lambda v: None if _blank(v) else str(v).strip())
    dup = keys[keys.notna() & keys.duplicated(keep=False)]
    for i, k in dup.items():
        plan.errors.append((_row_no(i), f"{column}「{k}」在檔案中重複出現"))
    return set(dup.index)


def _binding_conflicts(ctx, table, assignments, plan):
    """
    檢查綁定衝突：同一個感測器不能同時綁兩個點位（跨 Modbus / TIA / OPC UA / 計算點）。
    assignments: {點位 key: (sensor_id, row_no)} —— 檔案裡這張表的點位「之後」的綁定。
    資料庫裡同一張表、而且這次檔案有出現的點位，以檔案為準（允許在同一次匯入中把綁定從 A 換到 B）。
    """
    owners = {}
    for t, point, sid in ctx.get("bindings", []):
        if t == table and point in assignments:
            continue
        owners.setdefault(sid, []).append(f"{t}:{point}")
    for point, (sid, row_no) in assignments.items():
        if sid is None:
            continue
        owners.setdefault(sid, []).append(f"{table}:{point}")
    code_of = {v: k for k, v in ctx.get("sensor_ids", {}).items()}
    bad_rows = set()
    for point, (sid, row_no) in assignments.items():
        if sid is not None and len(owners.get(sid, [])) > 1:
            others = [o for o in owners[sid] if o != f"{table}:{point}"]
            plan.errors.append((row_no, f"感測器 {code_of.get(sid, sid)} 已綁定其他點位：{'、'.join(others)}"))
            bad_rows.add(row_no)
    # 有錯的列不列入預覽的新增 / 更新（有任何錯誤時本來就不允許套用，這裡只是讓數字正確）
    plan.changes = [c for c in plan.changes if c.row_no not in bad_rows]


# ------------------------------------------------------------------
# 各類資料的驗證規劃
# ------------------------------------------------------------------
def plan_devices(df, ctx) -> Plan:
    plan = Plan("devices")
    if not _require_columns(df, ["device_code", "site_name", "line_name"], plan):
        return plan
    dup_rows = _check_duplicates(df, "device_code", plan)
    new_sites, new_lines = set(), set()
    for i, r in df.iterrows():
        if i in dup_rows:
            continue
        try:
            code = p_str(r.get("device_code"), True, 50)
            site = p_str(r.get("site_name"), True, 100)
            line = p_str(r.get("line_name"), True, 100)
            values = {
                "device_name": p_str(r.get("device_name"), max_len=100),
                "device_type": p_str(r.get("device_type"), max_len=50),
                "manufacturer": p_str(r.get("manufacturer"), max_len=100),
                "status": p_str(r.get("status"), max_len=20) or "active",
            }
        except CellError as e:
            plan.errors.append((_row_no(i), str(e)))
            continue
        if site not in ctx["sites"]:
            new_sites.add(site)
        if (site, line) not in ctx["lines"]:
            new_lines.add((site, line))
        values.update(site_name=site, line_name=line)
        existing = ctx["devices"].get(code)
        _upsert(plan, existing, code, values, _row_no(i), existing and existing["device_id"])
    if new_sites:
        plan.notes.append(f"將自動建立廠區：{'、'.join(sorted(new_sites))}")
    if new_lines:
        plan.notes.append("將自動建立產線：" + "、".join(f"{s} / {ln}" for s, ln in sorted(new_lines)))
    plan.extra.update(new_sites=sorted(new_sites), new_lines=sorted(new_lines))
    return plan


def plan_sensors(df, ctx) -> Plan:
    plan = Plan("sensors")
    if not _require_columns(df, ["sensor_code", "device_code", "sensor_type"], plan):
        return plan
    dup_rows = _check_duplicates(df, "sensor_code", plan)
    for i, r in df.iterrows():
        if i in dup_rows:
            continue
        try:
            code = p_str(r.get("sensor_code"), True, 50)
            device_code = p_str(r.get("device_code"), True, 50)
            if device_code not in ctx["devices"]:
                raise CellError(f"設備 {device_code} 不存在（請先匯入設備）")
            condition = p_choice(r.get("upload_condition"), UPLOAD_CONDITIONS, default="on_change")
            threshold = p_float(r.get("upload_threshold"))
            if condition in ("threshold_percent", "threshold_absolute") and threshold is None:
                raise CellError(f"upload_condition={condition} 時 upload_threshold 必填")
            values = {
                "device_code": device_code,
                "sensor_type": p_str(r.get("sensor_type"), True, 50),
                "nickname": p_str(r.get("nickname"), max_len=100),
                "unit": p_str(r.get("unit"), max_len=20),
                "min_threshold": p_float(r.get("min_threshold")),
                "max_threshold": p_float(r.get("max_threshold")),
                "upload_condition": condition,
                "upload_threshold": threshold,
                "opcua_sampling_interval_ms": p_int(r.get("opcua_sampling_interval_ms"), lo=50),
                "opcua_deadband_type": p_choice(r.get("opcua_deadband_type"), DEADBAND_TYPES, default="none"),
                "opcua_deadband_value": p_float(r.get("opcua_deadband_value")),
                "state_dictionary": p_json_dict(r.get("state_dictionary")),
            }
            if (values["min_threshold"] is not None and values["max_threshold"] is not None
                    and values["min_threshold"] > values["max_threshold"]):
                raise CellError("min_threshold 大於 max_threshold")
        except CellError as e:
            plan.errors.append((_row_no(i), str(e)))
            continue
        existing = ctx["sensors"].get(code)
        _upsert(plan, existing, code, values, _row_no(i), existing and existing["sensor_id"])
    return plan


MODBUS_COLUMNS = ["id", "name", "transport", "plc_ip", "plc_port", "serial_settings", "slave_id",
                  "function_code", "start_address", "data_type", "byte_order", "word_order",
                  "raw_min", "raw_max", "eng_min", "eng_max", "unit", "state_dictionary",
                  "sensor_code", "enabled"]


def plan_modbus(df, ctx) -> Plan:
    plan = Plan("modbus")
    required = ["name", "plc_ip", "slave_id", "function_code", "start_address", "data_type"]
    if not _require_columns(df, required, plan):
        return plan
    has_v31 = ctx.get("modbus_v31", False)
    type_choices = tuple(sorted(set(DATA_TYPES) | {"word", "int", "dint", "float", "real", "double"}))
    assignments = {}
    seen_ids = set()
    for i, r in df.iterrows():
        row_no = _row_no(i)
        try:
            point_id = p_int(r.get("id"))
            if point_id is not None:
                if point_id not in ctx["modbus"]:
                    raise CellError(f"id {point_id} 不存在（新增點位請把 id 留空）")
                if point_id in seen_ids:
                    raise CellError(f"id {point_id} 在檔案中重複出現")
                seen_ids.add(point_id)
            transport = p_choice(r.get("transport"), TRANSPORTS, default="tcp")
            raw_type = (p_str(r.get("data_type")) or "").lower()
            data_type = normalize_type(raw_type)
            if data_type not in DATA_TYPES:
                raise CellError(f"data_type 必填且必須是：{'/'.join(type_choices)}（收到「{raw_type}」）")
            values = {
                "name": p_str(r.get("name"), True),
                "plc_ip": p_str(r.get("plc_ip"), True),
                "plc_port": p_int(r.get("plc_port"), lo=1, hi=65535) or 502,
                "slave_id": p_int(r.get("slave_id"), True, 0, 255),
                "function_code": p_int(r.get("function_code"), True, 1, 4),
                "start_address": p_int(r.get("start_address"), True, 0, 65535),
                # 保留使用者原本的寫法（word / float…），採集程式兩種都認得
                "data_type": raw_type if raw_type in type_choices else data_type,
                "byte_order": p_choice(r.get("byte_order"), ("BIG", "LITTLE"), default="BIG", upper=True),
                "word_order": p_choice(r.get("word_order"), ("BIG", "LITTLE"), default="BIG", upper=True),
                "raw_min": p_float(r.get("raw_min")),
                "raw_max": p_float(r.get("raw_max")),
                "eng_min": p_float(r.get("eng_min")),
                "eng_max": p_float(r.get("eng_max")),
                "unit": p_str(r.get("unit")),
                "state_dictionary": p_json_dict(r.get("state_dictionary")),
            }
            scale = [values[k] for k in ("raw_min", "raw_max", "eng_min", "eng_max")]
            if any(v is not None for v in scale) and not all(v is not None for v in scale):
                raise CellError("線性換算的 raw_min / raw_max / eng_min / eng_max 要嘛全填、要嘛全空")
            if has_v31:
                values["transport"] = transport
                values["serial_settings"] = p_str(r.get("serial_settings")) if transport == "rtu" else None
                values["enabled"] = p_bool(r.get("enabled"), True)
            elif transport != "tcp":
                raise CellError("尚未執行 sql/015，只支援 transport=tcp")
            sensor_code = p_str(r.get("sensor_code"))
            sensor_id = None
            if sensor_code:
                sensor_id = ctx["sensor_ids"].get(sensor_code)
                if sensor_id is None:
                    raise CellError(f"感測器 {sensor_code} 不存在")
            values["sensor_id"] = sensor_id
        except CellError as e:
            plan.errors.append((row_no, str(e)))
            continue
        key = f"#{point_id}" if point_id is not None else f"新：{values['name']}"
        assignments[point_id if point_id is not None else f"new{i}"] = (sensor_id, row_no)
        existing = ctx["modbus"].get(point_id) if point_id is not None else None
        _upsert(plan, existing, key, values, row_no, point_id)
    _binding_conflicts(ctx, "modbus_scada", assignments, plan)
    return plan


S7_COLUMNS = ["id", "name", "plc_name", "plc_ip", "rack", "slot", "address", "area", "db_number", "offset",
              "bit_offset", "data_type", "unit", "sensor_code", "enabled"]


def plan_s7(df, ctx) -> Plan:
    """
    S7 點位（tia_scada）。id 空白 = 新增。address 欄位（TIA 表示法，例如 DB1.DBX10.3、MW20）
    有填就以它為準，自動換算 area / db_number / offset / bit_offset；沒填就看那四個欄位。
    """
    plan = Plan("s7")
    if not _require_columns(df, ["name", "plc_ip", "data_type"], plan):
        return plan
    v33 = ctx.get("s7_v33", False)
    assignments, seen_ids = {}, set()
    for i, r in df.iterrows():
        row_no = _row_no(i)
        try:
            point_id = p_int(r.get("id"))
            if point_id is not None:
                if point_id not in ctx["s7"]:
                    raise CellError(f"id {point_id} 不存在（新增點位請把 id 留空）")
                if point_id in seen_ids:
                    raise CellError(f"id {point_id} 在檔案中重複出現")
                seen_ids.add(point_id)
            data_type = (p_str(r.get("data_type"), True) or "").upper()
            try:
                s7_codec.type_size(data_type)
            except ValueError:
                raise CellError(f"data_type 不正確：{data_type}（BOOL / INT / DINT / REAL / LREAL / WORD / STRING[20]…）")
            address = p_str(r.get("address"))
            if address:
                try:
                    a = s7_codec.parse_address(address)
                except ValueError as e:
                    raise CellError(str(e))
                area, db, offset, bit = a["area"], a["db"], a["byte"], a["bit"]
                if (a["width"] == 0) != (data_type == "BOOL"):
                    raise CellError(f"位址 {address} 與型態 {data_type} 不符（位元位址只能配 BOOL）")
            else:
                area = p_choice(r.get("area"), tuple(s7_codec.AREAS), default="DB", upper=True)
                db = p_int(r.get("db_number"), area == "DB", 0, 65535) or 0
                offset = p_int(r.get("offset"), True, 0, 65535)
                bit = p_int(r.get("bit_offset"), lo=0, hi=7) or 0
            values = {
                "name": p_str(r.get("name"), True),
                "plc_name": p_str(r.get("plc_name")) or "PLC",
                "plc_ip": p_str(r.get("plc_ip"), True),
                "db_number": db if area == "DB" else 0,
                "offset": offset,
                "data_type": data_type,
            }
            if v33:
                values.update(area=area, bit_offset=bit, rack=p_int(r.get("rack"), lo=0, hi=7) or 0,
                              slot=1 if _blank(r.get("slot")) else p_int(r.get("slot"), lo=0, hi=31),
                              unit=p_str(r.get("unit")), enabled=p_bool(r.get("enabled"), True))
            elif area != "DB" or bit:
                raise CellError("尚未執行 sql/019，只支援 DB 區域、bit 0")
            sensor_code = p_str(r.get("sensor_code"))
            sensor_id = None
            if sensor_code:
                sensor_id = ctx["sensor_ids"].get(sensor_code)
                if sensor_id is None:
                    raise CellError(f"感測器 {sensor_code} 不存在")
            values["sensor_id"] = sensor_id
        except CellError as e:
            plan.errors.append((row_no, str(e)))
            continue
        key = f"#{point_id}" if point_id is not None else f"新：{values['name']}"
        assignments[point_id if point_id is not None else f"new{i}"] = (sensor_id, row_no)
        existing = ctx["s7"].get(point_id) if point_id is not None else None
        _upsert(plan, existing, key, values, row_no, point_id)
    _binding_conflicts(ctx, "tia_scada", assignments, plan)
    return plan


def plan_opcua_bindings(df, ctx) -> Plan:
    plan = Plan("opcua_bindings")
    if not _require_columns(df, ["server_name", "node_id", "sensor_code"], plan):
        return plan
    assignments = {}
    seen = set()
    for i, r in df.iterrows():
        row_no = _row_no(i)
        try:
            server = p_str(r.get("server_name"), True)
            node = p_str(r.get("node_id"), True)
            tag = ctx["opcua_tags"].get((server, node))
            if tag is None:
                raise CellError(f"Server {server} 底下沒有點位 {node}（點位由瀏覽產生，請先瀏覽）")
            if (server, node) in seen:
                raise CellError("同一個點位在檔案中重複出現")
            seen.add((server, node))
            sensor_code = p_str(r.get("sensor_code"))
            sensor_id = None
            if sensor_code:
                sensor_id = ctx["sensor_ids"].get(sensor_code)
                if sensor_id is None:
                    raise CellError(f"感測器 {sensor_code} 不存在")
        except CellError as e:
            plan.errors.append((row_no, str(e)))
            continue
        assignments[tag["id"]] = (sensor_id, row_no)
        _upsert(plan, {"sensor_id": tag["sensor_id"]}, f"{server} / {node}",
                {"sensor_id": sensor_id}, row_no, tag["id"])
    _binding_conflicts(ctx, "opcua_tags", assignments, plan)
    return plan


def plan_alarm_rules(df, ctx) -> Plan:
    plan = Plan("alarm_rules")
    if not _require_columns(df, ["sensor_code", "alarm_type", "setpoint"], plan):
        return plan
    for i, r in df.iterrows():
        row_no = _row_no(i)
        try:
            rule_id = p_int(r.get("rule_id"))
            if rule_id is not None and rule_id not in ctx["alarm_rules"]:
                raise CellError(f"rule_id {rule_id} 不存在（新增規則請把 rule_id 留空）")
            sensor_code = p_str(r.get("sensor_code"), True)
            sensor_id = ctx["sensor_ids"].get(sensor_code)
            if sensor_id is None:
                raise CellError(f"感測器 {sensor_code} 不存在")
            values = {
                "sensor_id": sensor_id,
                "alarm_type": p_choice(r.get("alarm_type"), ALARM_TYPES, upper=True),
                "setpoint": p_float(r.get("setpoint"), True),
                "deadband": p_float(r.get("deadband")) or 0.0,
                "on_delay_sec": p_int(r.get("on_delay_sec"), lo=0) or 0,
                "priority": p_int(r.get("priority"), lo=1, hi=4) or 2,
                "message": p_str(r.get("message"), max_len=200),
                "enabled": p_bool(r.get("enabled"), True),
            }
            if values["deadband"] < 0:
                raise CellError("deadband 不能是負數")
        except CellError as e:
            plan.errors.append((row_no, str(e)))
            continue
        key = f"#{rule_id}" if rule_id is not None else f"新：{sensor_code} {values['alarm_type']}"
        existing = ctx["alarm_rules"].get(rule_id) if rule_id is not None else None
        _upsert(plan, existing, key, values, row_no, rule_id)
    return plan


# ------------------------------------------------------------------
# 資料庫：匯出、查詢表、套用
# ------------------------------------------------------------------
def _query(cur, sql, params=None) -> pd.DataFrame:
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=cols)


def _has_column(cur, table, column) -> bool:
    cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name=%s AND column_name=%s);", (table, column))
    return bool(cur.fetchone()[0])


def _has_table(cur, table) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL;", (f"public.{table}",))
    return bool(cur.fetchone()[0])


EXPORT_SQL = {
    "devices": """
        SELECT d.device_code, d.device_name, d.device_type, d.manufacturer,
               si.site_name, pl.line_name, d.status
        FROM devices d
        LEFT JOIN production_lines pl ON pl.line_id = d.line_id
        LEFT JOIN sites si ON si.site_id = pl.site_id
        ORDER BY d.device_code;""",
    "sensors": """
        SELECT s.sensor_code, d.device_code, s.sensor_type, s.nickname, s.unit,
               s.min_threshold::float8, s.max_threshold::float8, s.upload_condition,
               s.upload_threshold::float8, s.opcua_sampling_interval_ms, s.opcua_deadband_type,
               s.opcua_deadband_value::float8, s.state_dictionary::text
        FROM sensors s LEFT JOIN devices d ON d.device_id = s.device_id
        ORDER BY d.device_code, s.sensor_code;""",
    "opcua_bindings": """
        SELECT t.server_name, t.node_id, t.display_name, t.data_type, s.sensor_code
        FROM opcua_tags t LEFT JOIN sensors s ON s.sensor_id = t.sensor_id
        ORDER BY t.server_name, t.node_id;""",
    "alarm_rules": """
        SELECT r.rule_id, s.sensor_code, r.alarm_type, r.setpoint::float8, r.deadband::float8,
               r.on_delay_sec, r.priority, r.message, r.enabled
        FROM alarm_rules r JOIN sensors s ON s.sensor_id = r.sensor_id
        ORDER BY s.sensor_code, r.alarm_type;""",
}


def export_frame(cur, entity) -> pd.DataFrame:
    if entity == "s7":
        v33 = _has_column(cur, "tia_scada", "area")
        extra = "t.rack, t.slot, t.area, t.bit_offset, t.unit, t.enabled" if v33 \
            else "0 AS rack, 1 AS slot, 'DB' AS area, 0 AS bit_offset, NULL AS unit, TRUE AS enabled"
        df = _query(cur, f"""
            SELECT t.id, t.name, t.plc_name, t.plc_ip, {extra}, t.db_number, t."offset", t.data_type,
                   s.sensor_code
            FROM tia_scada t LEFT JOIN sensors s ON s.sensor_id = t.sensor_id
            ORDER BY t.plc_ip, t.area, t.db_number, t."offset";""" if v33 else f"""
            SELECT t.id, t.name, t.plc_name, t.plc_ip, {extra}, t.db_number, t."offset", t.data_type,
                   s.sensor_code
            FROM tia_scada t LEFT JOIN sensors s ON s.sensor_id = t.sensor_id
            ORDER BY t.plc_ip, t.db_number, t."offset";""")
        df["address"] = [s7_codec.format_address(r["area"], int(r["db_number"] or 0), int(r["offset"]),
                                                 r["data_type"], int(r["bit_offset"] or 0)).split("（")[0]
                         for _, r in df.iterrows()]
        return df[S7_COLUMNS]
    if entity == "modbus":
        v31 = _has_column(cur, "modbus_scada", "transport")
        extra = "m.transport, m.serial_settings, m.enabled" if v31 \
            else "'tcp' AS transport, NULL AS serial_settings, TRUE AS enabled"
        df = _query(cur, f"""
            SELECT m.id, m.name, {extra}, m.plc_ip, m.plc_port, m.slave_id, m.function_code,
                   m.start_address, m.data_type, m.byte_order, m.word_order, m.raw_min, m.raw_max,
                   m.eng_min, m.eng_max, m.unit, m.state_dictionary::text, s.sensor_code
            FROM modbus_scada m LEFT JOIN sensors s ON s.sensor_id = m.sensor_id
            ORDER BY m.plc_ip, m.slave_id, m.function_code, m.start_address;""")
        return df[MODBUS_COLUMNS]
    return _query(cur, EXPORT_SQL[entity])


def load_context(cur, entity) -> dict:
    ctx = {}
    sensors = _query(cur, "SELECT s.*, d.device_code FROM sensors s LEFT JOIN devices d USING (device_id);")
    ctx["sensor_ids"] = dict(zip(sensors["sensor_code"], sensors["sensor_id"].astype(int)))
    if entity in ("devices", "sensors"):
        devices = _query(cur, """
            SELECT d.device_id, d.device_code, d.device_name, d.device_type, d.manufacturer, d.status,
                   si.site_name, pl.line_name
            FROM devices d LEFT JOIN production_lines pl ON pl.line_id = d.line_id
            LEFT JOIN sites si ON si.site_id = pl.site_id;""")
        ctx["devices"] = {r["device_code"]: r for r in devices.to_dict("records")}
        sites = _query(cur, "SELECT site_id, site_name FROM sites;")
        ctx["sites"] = dict(zip(sites["site_name"], sites["site_id"]))
        lines = _query(cur, "SELECT pl.line_id, si.site_name, pl.line_name FROM production_lines pl "
                            "JOIN sites si ON si.site_id = pl.site_id;")
        ctx["lines"] = {(r["site_name"], r["line_name"]): r["line_id"] for r in lines.to_dict("records")}
        records = sensors.to_dict("records")
        for rec in records:
            sd = rec.get("state_dictionary")
            rec["state_dictionary"] = json.dumps(sd, ensure_ascii=False) if isinstance(sd, dict) else sd
        ctx["sensors"] = {r["sensor_code"]: r for r in records}
    if entity == "s7":
        ctx["s7_v33"] = _has_column(cur, "tia_scada", "area")
        t7 = _query(cur, "SELECT * FROM tia_scada;")
        ctx["s7"] = {int(r["id"]): r for r in t7.to_dict("records")}
    if entity == "modbus":
        ctx["modbus_v31"] = _has_column(cur, "modbus_scada", "transport")
        mb = _query(cur, "SELECT * FROM modbus_scada;")
        records = mb.to_dict("records")
        for rec in records:
            sd = rec.get("state_dictionary")
            rec["state_dictionary"] = json.dumps(sd, ensure_ascii=False) if isinstance(sd, dict) else sd
        ctx["modbus"] = {int(r["id"]): r for r in records}
    if entity == "opcua_bindings":
        tags = _query(cur, "SELECT id, server_name, node_id, sensor_id FROM opcua_tags;")
        ctx["opcua_tags"] = {(r["server_name"], r["node_id"]): {
            "id": int(r["id"]), "sensor_id": None if pd.isna(r["sensor_id"]) else int(r["sensor_id"])}
            for r in tags.to_dict("records")}
    if entity == "alarm_rules":
        rules = _query(cur, "SELECT rule_id, sensor_id, alarm_type, setpoint::float8, deadband::float8, "
                            "on_delay_sec, priority, message, enabled FROM alarm_rules;")
        ctx["alarm_rules"] = {int(r["rule_id"]): r for r in rules.to_dict("records")}
    if entity in ("modbus", "opcua_bindings", "s7"):
        bindings = []
        for table, key in (("modbus_scada", "id"), ("tia_scada", "id"), ("opcua_tags", "id")):
            b = _query(cur, f"SELECT {key} AS point, sensor_id FROM {table} WHERE sensor_id IS NOT NULL;")
            bindings += [(table, int(p), int(s)) for p, s in zip(b["point"], b["sensor_id"])]
        if _has_table(cur, "calculated_points"):
            b = _query(cur, "SELECT calc_id AS point, sensor_id FROM calculated_points;")
            bindings += [("calculated_points", int(p), int(s)) for p, s in zip(b["point"], b["sensor_id"])]
        ctx["bindings"] = bindings
    return ctx


PLANNERS = {
    "s7": plan_s7,
    "devices": plan_devices,
    "sensors": plan_sensors,
    "modbus": plan_modbus,
    "opcua_bindings": plan_opcua_bindings,
    "alarm_rules": plan_alarm_rules,
}


def plan_import(cur, entity, df) -> Plan:
    df = df.reset_index(drop=True)
    df.columns = [str(c).strip() for c in df.columns]
    return PLANNERS[entity](df, load_context(cur, entity))


def apply_plan(cur, plan: Plan) -> dict:
    """在呼叫端提供的 cursor（同一個交易）中套用；回傳 {inserted, updated}。"""
    entity = plan.entity
    if entity == "devices":
        for site in plan.extra.get("new_sites", []):
            cur.execute("INSERT INTO sites (site_name) VALUES (%s);", (site,))
        for site, line in plan.extra.get("new_lines", []):
            cur.execute("INSERT INTO production_lines (site_id, line_name) "
                        "SELECT site_id, %s FROM sites WHERE site_name = %s ORDER BY site_id LIMIT 1;",
                        (line, site))
        line_lookup = "(SELECT pl.line_id FROM production_lines pl JOIN sites si ON si.site_id = pl.site_id " \
                      "WHERE si.site_name = %s AND pl.line_name = %s ORDER BY pl.line_id LIMIT 1)"
        for c in plan.changes:
            v = c.values
            params = (v["device_name"], v["device_type"], v["manufacturer"], v["status"],
                      v["site_name"], v["line_name"])
            if c.action == "insert":
                cur.execute(f"INSERT INTO devices (device_name, device_type, manufacturer, status, line_id, device_code) "
                            f"VALUES (%s, %s, %s, %s, {line_lookup}, %s);", params + (c.key,))
            else:
                cur.execute(f"UPDATE devices SET device_name=%s, device_type=%s, manufacturer=%s, status=%s, "
                            f"line_id={line_lookup} WHERE device_id=%s;", params + (int(c.target_id),))
    elif entity == "sensors":
        cols = ["sensor_type", "nickname", "unit", "min_threshold", "max_threshold", "upload_condition",
                "upload_threshold", "opcua_sampling_interval_ms", "opcua_deadband_type",
                "opcua_deadband_value", "state_dictionary"]
        device_lookup = "(SELECT device_id FROM devices WHERE device_code = %s)"
        for c in plan.changes:
            params = tuple(c.values[k] for k in cols) + (c.values["device_code"],)
            if c.action == "insert":
                cur.execute(f"INSERT INTO sensors ({', '.join(cols)}, device_id, sensor_code) "
                            f"VALUES ({', '.join(['%s'] * len(cols))}, {device_lookup}, %s);", params + (c.key,))
            else:
                sets = ", ".join(f"{k}=%s" for k in cols)
                cur.execute(f"UPDATE sensors SET {sets}, device_id={device_lookup} WHERE sensor_id=%s;",
                            params + (int(c.target_id),))
    elif entity in ("modbus", "alarm_rules", "s7"):
        table, pk = {"modbus": ("modbus_scada", "id"), "alarm_rules": ("alarm_rules", "rule_id"),
                     "s7": ("tia_scada", "id")}[entity]
        for c in plan.changes:
            cols = list(c.values)
            quoted = ['"offset"' if k == "offset" else k for k in cols]      # offset 是 SQL 保留字
            if c.action == "insert":
                extra_cols = ", plc_state" if entity in ("modbus", "s7") else ""
                extra_vals = ", 'OFFLINE'" if entity in ("modbus", "s7") else ""
                cur.execute(f"INSERT INTO {table} ({', '.join(quoted)}{extra_cols}) "
                            f"VALUES ({', '.join(['%s'] * len(cols))}{extra_vals});",
                            tuple(c.values[k] for k in cols))
            else:
                sets = ", ".join(f"{k}=%s" for k in quoted)
                if entity == "alarm_rules":
                    sets += ", updated_at=now()"
                cur.execute(f"UPDATE {table} SET {sets} WHERE {pk}=%s;",
                            tuple(c.values[k] for k in cols) + (int(c.target_id),))
    elif entity == "opcua_bindings":
        # 訂閱服務的維護迴圈會在 5 秒內偵測到綁定變更並重建訂閱；不要設 resubscribe_requested，
        # 那個旗標會觸發整台 Server 重新瀏覽（幾千個節點），只是改綁定不需要
        for c in plan.changes:
            cur.execute("UPDATE opcua_tags SET sensor_id=%s WHERE id=%s;", (c.values["sensor_id"], int(c.target_id)))
    return {"inserted": len(plan.inserts), "updated": len(plan.updates)}
