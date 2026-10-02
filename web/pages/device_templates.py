"""
web/pages/device_templates.py
=============================
🧩 設備範本（engineer 以上，需要 sql/016）

同型設備（同型號電表、同款鍋爐控制器…）的感測器、點位、警報規則定義一次，之後：
    輸入「設備編號 + IP + 站號」（OPC UA 則是 Server + 節點代號）→ 預覽 → 一次建好整台設備

建範本最快的方法是「從現有設備建立」：先手動把第一台設定好、確認採集正確，
再用它產生範本套用到其他台。邏輯在 data_layer/templates.py。
"""

import json

import pandas as pd
import streamlit as st

from data_layer import templates as T
from data_layer.db_connector import DatabaseConnector
from protocols.modbus_codec import DATA_TYPES
from web.common import audit_ui, current_user, execute, fetch_df, require_role, table_exists

PROTOCOL_LABELS = {"modbus": "Modbus（同時建立點位）", "opcua": "OPC UA（綁定已瀏覽的節點）",
                   "none": "只建設備與感測器"}


def _templates() -> pd.DataFrame:
    df = fetch_df("SELECT template_id, name, description, protocol, definition, created_by, updated_at "
                  "FROM device_templates ORDER BY name;")
    if not df.empty:
        df["definition"] = df["definition"].map(lambda d: d if isinstance(d, dict) else json.loads(d))
        df["sensor_count"] = df["definition"].map(lambda d: len(d.get("sensors") or []))
    return df


def _save_template(name, description, protocol, definition, template_id=None):
    errors = T.validate_definition(definition, protocol)
    if errors:
        for e in errors:
            st.error(e)
        return False
    payload = json.dumps(definition, ensure_ascii=False)
    if template_id is None:
        execute("INSERT INTO device_templates (name, description, protocol, definition, created_by) "
                "VALUES (%s, %s, %s, %s, %s);", (name, description, protocol, payload, current_user()["username"]))
        audit_ui("template.create", f"template:{name}", {"protocol": protocol, "sensors": len(definition["sensors"])})
    else:
        execute("UPDATE device_templates SET name=%s, description=%s, definition=%s, updated_at=now() "
                "WHERE template_id=%s;", (name, description, payload, int(template_id)))
        audit_ui("template.update", f"template:{name}", {"sensors": len(definition["sensors"])})
    return True


# ------------------------------------------------------------------
# 建立設備
# ------------------------------------------------------------------
def _instance_columns(protocol):
    cols = ["device_code", "device_name"]
    if protocol == "modbus":
        cols += ["plc_ip", "slave_id", "plc_port", "address_offset"]
    elif protocol == "opcua":
        cols += ["server_name", "token"]
    return cols


def _tab_create(tpls: pd.DataFrame):
    if tpls.empty:
        st.info("還沒有任何範本。請到「新增範本」分頁，從一台已設定好的設備建立。")
        return
    labels = {int(r["template_id"]): f"{r['name']}（{PROTOCOL_LABELS[r['protocol']]}，{r['sensor_count']} 個感測器）"
              for _, r in tpls.iterrows()}
    tid = st.selectbox("範本", list(labels), format_func=labels.get, key="tpc_tid")
    tpl = tpls[tpls["template_id"] == tid].iloc[0]
    definition, protocol = tpl["definition"], tpl["protocol"]

    lines = fetch_df("SELECT pl.line_id, si.site_name, pl.line_name FROM production_lines pl "
                     "JOIN sites si ON si.site_id = pl.site_id ORDER BY si.site_name, pl.line_name;")
    if lines.empty:
        st.warning("請先在「感測器階層」建立廠區與產線。")
        return
    line_labels = {int(r["line_id"]): f"{r['site_name']} / {r['line_name']}" for _, r in lines.iterrows()}
    line_id = st.selectbox("建立在哪條產線", list(line_labels), format_func=line_labels.get, key="tpc_line")

    key = f"tpc_rows_{tid}"
    cols = _instance_columns(protocol)
    if key not in st.session_state:
        st.session_state[key] = pd.DataFrame([{c: None for c in cols}])

    with st.expander("⚡ 批次產生設備清單"):
        c1, c2, c3, c4 = st.columns(4)
        prefix = c1.text_input("編號前綴", "PM", key="tpc_prefix")
        start = c2.number_input("起始號碼", 1, 9999, 1, key="tpc_start")
        count = c3.number_input("台數", 1, 200, 5, key="tpc_count")
        width = c4.number_input("號碼位數", 1, 5, 2, key="tpc_width")
        extra = {}
        if protocol == "modbus":
            c5, c6, c7 = st.columns(3)
            extra["plc_ip"] = c5.text_input("IP（同一個閘道）", "192.168.1.100", key="tpc_ip")
            slave0 = c6.number_input("起始站號", 0, 247, 1, key="tpc_slave")
            step = c7.number_input("站號間隔", 1, 10, 1, key="tpc_step")
        elif protocol == "opcua":
            servers = fetch_df("SELECT server_name FROM opcua_servers ORDER BY 1;")
            extra["server_name"] = st.selectbox("OPC UA Server", servers["server_name"].tolist(), key="tpc_srv") \
                if not servers.empty else None
        if st.button("產生清單（覆蓋下方表格）", key="tpc_gen"):
            codes = T.suggest_codes(prefix, int(start), int(count), int(width))
            rows = []
            for n, code in enumerate(codes):
                row = {c: None for c in cols}
                row.update(device_code=code, **extra)
                if protocol == "modbus":
                    row["slave_id"] = int(slave0) + n * int(step)
                rows.append(row)
            st.session_state[key] = pd.DataFrame(rows, columns=cols)
            st.session_state.pop("tpc_editor", None)
            st.rerun()

    help_text = {"modbus": "plc_port 空白用範本預設；address_offset 用在同型設備位址整體平移（例如多迴路電表）",
                 "opcua": "token 會取代節點樣式中的 {device}，空白則等於設備編號",
                 "none": ""}[protocol]
    st.caption("每一列是一台要建立的設備。" + help_text)
    instances = st.data_editor(st.session_state[key], num_rows="dynamic", hide_index=True, width="stretch",
                               key="tpc_editor")
    instances = instances.dropna(how="all")

    if st.button("🔍 預覽", key="tpc_preview", disabled=instances.empty):
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                st.session_state["tpc_plan"] = (tid, T.plan_instances(definition, protocol, instances,
                                                                     T.load_instance_context(cur)))
    stored = st.session_state.get("tpc_plan")
    if not stored or stored[0] != tid:
        return
    plan = stored[1]
    n = plan.counts()
    m = st.columns(4)
    m[0].metric("設備", n["devices"], border=True)
    m[1].metric("感測器", n["sensors"], border=True)
    m[2].metric("點位 / 綁定", n["points"], border=True)
    m[3].metric("警報規則", n["alarms"], border=True)
    if plan.errors:
        st.error("以下設備有問題，修正後再預覽一次（有錯誤時不會建立任何東西）：")
        st.dataframe(pd.DataFrame(plan.errors, columns=["列", "錯誤"]), hide_index=True, width="stretch")
    if plan.devices:
        preview = [{"設備": d["device_code"], "感測器": s["sensor_code"], "類型": s["sensor_type"],
                    "單位": s.get("unit") or "",
                    "點位": (f"{s['point']['name']}｜站號 {d['conn']['slave_id']}｜FC{int(s['point']['function_code']):02d} "
                             f"@ {s['point']['start_address']}" if protocol == "modbus" and s.get("point")
                             else s["point"]["node_id"] if protocol == "opcua" and s.get("point") else ""),
                    "警報": "、".join(f"{a['alarm_type']} {a['setpoint']}" for a in s.get("alarms") or [])}
                   for d in plan.devices for s in d["sensors"]]
        st.dataframe(pd.DataFrame(preview), hide_index=True, width="stretch", height=min(36 * (len(preview) + 1), 420))
    if st.button(f"✅ 建立 {n['devices']} 台設備", type="primary", disabled=not plan.ok, key="tpc_go"):
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    fresh = T.plan_instances(definition, protocol, instances, T.load_instance_context(cur))
                    if not fresh.ok or fresh.counts() != n:
                        raise ValueError("預覽之後資料有變動，請重新預覽")
                    result = T.apply_instances(cur, definition, protocol, fresh, int(line_id))
        except Exception as e:
            st.error(f"❌ 建立失敗，沒有寫入任何資料：{e}")
            return
        audit_ui("template.apply", f"template:{tpl['name']}",
                 {**result, "devices": [d["device_code"] for d in plan.devices], "line_id": int(line_id)})
        st.session_state.pop("tpc_plan", None)
        st.session_state.pop(key, None)
        st.success(f"✅ 已建立 {result['devices']} 台設備、{result['sensors']} 個感測器、"
                   f"{result['points']} 個點位 / 綁定、{result['alarms']} 條警報規則")


# ------------------------------------------------------------------
# 範本管理
# ------------------------------------------------------------------
def _tab_manage(tpls: pd.DataFrame):
    if tpls.empty:
        st.info("還沒有任何範本。")
        return
    labels = {int(r["template_id"]): r["name"] for _, r in tpls.iterrows()}
    tid = st.selectbox("範本", list(labels), format_func=labels.get, key="tpm_tid")
    tpl = tpls[tpls["template_id"] == tid].iloc[0]
    protocol = tpl["protocol"]
    c1, c2 = st.columns([1, 2])
    name = c1.text_input("名稱", tpl["name"], key=f"tpm_name_{tid}")
    desc = c2.text_input("說明", tpl["description"] or "", key=f"tpm_desc_{tid}")
    st.caption(f"協議：{PROTOCOL_LABELS[protocol]}｜最後修改：{str(tpl['updated_at'])[:16]}｜建立者：{tpl['created_by'] or '—'}")
    if protocol == "modbus":
        mb = tpl["definition"].get("modbus") or {}
        st.caption(f"連線預設：{mb.get('transport', 'tcp')}，Port {mb.get('port', 502)}"
                   + (f"，序列埠 {mb['serial_settings']}" if mb.get("serial_settings") else ""))

    frame = T.definition_to_frame(tpl["definition"], protocol)
    config = {
        "suffix": st.column_config.TextColumn("suffix（感測器編號 = 設備編號_suffix）", required=True),
        "upload_condition": st.column_config.SelectboxColumn(
            options=["always", "on_change", "threshold_percent", "threshold_absolute"]),
        "data_type": st.column_config.SelectboxColumn(options=sorted(DATA_TYPES)),
        "function_code": st.column_config.SelectboxColumn(options=[1, 2, 3, 4]),
        "byte_order": st.column_config.SelectboxColumn(options=["BIG", "LITTLE"]),
        "word_order": st.column_config.SelectboxColumn(options=["BIG", "LITTLE"]),
        "node_pattern": st.column_config.TextColumn("node_pattern（{device} 會被替換）"),
        "alarms": st.column_config.TextColumn(
            "alarms（JSON）", help='例如 [{"alarm_type": "H", "setpoint": 250, "priority": 2}]'),
    }
    edited = st.data_editor(frame, num_rows="dynamic", hide_index=True, width="stretch", column_config=config,
                            key=f"tpm_editor_{tid}")
    b1, b2, b3 = st.columns(3)
    if b1.button("💾 儲存範本", type="primary", key=f"tpm_save_{tid}"):
        try:
            definition = T.frame_to_definition(edited, protocol, tpl["definition"])
        except ValueError as e:
            st.error(str(e))
            return
        if _save_template(name.strip(), desc.strip() or None, protocol, definition, tid):
            st.success("✅ 已儲存")
            st.rerun()
    b2.download_button(
        "⬇️ 匯出 JSON",
        json.dumps({"name": tpl["name"], "description": tpl["description"], "protocol": protocol,
                    "definition": tpl["definition"]}, ensure_ascii=False, indent=2).encode("utf-8"),
        file_name=f"template_{tpl['name']}.json", mime="application/json", key=f"tpm_json_{tid}",
    )
    with b3.popover("🗑️ 刪除範本"):
        st.caption("只刪除範本，已用範本建立的設備不受影響。")
        if st.button("確定刪除", key=f"tpm_del_{tid}"):
            execute("DELETE FROM device_templates WHERE template_id=%s;", (int(tid),))
            audit_ui("template.delete", f"template:{tpl['name']}")
            st.rerun()


# ------------------------------------------------------------------
# 新增範本
# ------------------------------------------------------------------
def _tab_new():
    mode = st.segmented_control("建立方式", ["從現有設備", "上傳 JSON", "空白範本"], default="從現有設備", key="tpn_mode")
    if mode == "從現有設備":
        devices = fetch_df("SELECT d.device_code, d.device_name, count(s.sensor_id) AS n FROM devices d "
                           "LEFT JOIN sensors s ON s.device_id = d.device_id GROUP BY 1, 2 HAVING count(s.sensor_id) > 0 "
                           "ORDER BY 1;")
        if devices.empty:
            st.info("目前沒有含感測器的設備。")
            return
        labels = {r["device_code"]: f"{r['device_code']} {r['device_name'] or ''}（{r['n']} 個感測器）"
                  for _, r in devices.iterrows()}
        c1, c2 = st.columns(2)
        code = c1.selectbox("來源設備", list(labels), format_func=labels.get, key="tpn_dev", index=None,
                            placeholder="選擇一台已設定好、採集正常的設備…")
        if code is None:
            return
        token = c2.text_input("節點 / 點位名稱中的設備代號", code, key=f"tpn_token_{code}",
                              help="OPC UA 節點（例如 ns=2;s=B03.Temp）裡代表這台設備的字串，會被替換成 {device}；"
                                   "預設等於設備編號")
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    definition, protocol = T.template_from_device(cur, code, token)
        except ValueError as e:
            st.error(str(e))
            return
        st.caption(f"偵測到的協議：{PROTOCOL_LABELS[protocol]}")
        st.dataframe(T.definition_to_frame(definition, protocol), hide_index=True, width="stretch")
        c3, c4 = st.columns([1, 2])
        name = c3.text_input("範本名稱", key="tpn_name", placeholder="例如：三相電表 PM-2000")
        desc = c4.text_input("說明", key="tpn_desc")
        if st.button("💾 存成範本", type="primary", disabled=not name.strip(), key="tpn_save"):
            if _save_template(name.strip(), desc.strip() or None, protocol, definition):
                st.success(f"✅ 已建立範本「{name.strip()}」，到「建立設備」分頁套用。")
    elif mode == "上傳 JSON":
        up = st.file_uploader("範本 JSON（由「匯出 JSON」產生）", type=["json"], key="tpn_upload")
        if up is not None:
            try:
                data = json.loads(up.getvalue().decode("utf-8"))
                name = st.text_input("範本名稱", data.get("name") or "", key="tpn_up_name")
                st.dataframe(T.definition_to_frame(data["definition"], data["protocol"]), hide_index=True,
                             width="stretch")
                if st.button("💾 匯入範本", type="primary", key="tpn_up_save"):
                    if _save_template(name.strip(), data.get("description"), data["protocol"], data["definition"]):
                        st.success("✅ 已匯入")
            except (ValueError, KeyError) as e:
                st.error(f"不是有效的範本檔：{e}")
    else:
        c1, c2 = st.columns(2)
        name = c1.text_input("範本名稱", key="tpn_blank_name")
        protocol = c2.selectbox("協議", list(PROTOCOL_LABELS), format_func=PROTOCOL_LABELS.get, key="tpn_blank_proto")
        if st.button("建立空白範本", disabled=not name.strip(), key="tpn_blank_go"):
            definition = {"device": {}, "sensors": [{"suffix": "S1", "sensor_type": "temperature",
                                                     "upload_condition": "on_change"}]}
            if protocol == "modbus":
                definition["modbus"] = {"transport": "tcp", "port": 502}
                definition["sensors"][0]["point"] = {"name": "S1", "function_code": 3, "start_address": 0,
                                                     "data_type": "float32", "byte_order": "BIG", "word_order": "BIG"}
            elif protocol == "opcua":
                definition["sensors"][0]["point"] = {"node_pattern": "ns=2;s={device}.S1"}
            if _save_template(name.strip(), None, protocol, definition):
                st.success("✅ 已建立，到「範本管理」分頁編輯感測器清單。")


def render():
    require_role("engineer")
    st.title("🧩 設備範本")
    if not table_exists("device_templates"):
        st.warning("尚未建立範本資料表，請用資料表擁有者執行 `sql/016_device_templates.sql`。")
        return
    st.caption("同型設備的感測器、點位、警報規則定義一次，之後輸入編號 / IP / 站號就能一次建好。"
               "建議先手動設好第一台並確認採集正確，再「從現有設備建立」範本。")
    tpls = _templates()
    t1, t2, t3 = st.tabs(["⚡ 建立設備", "📝 範本管理", "➕ 新增範本"])
    with t1:
        _tab_create(tpls)
    with t2:
        _tab_manage(tpls)
    with t3:
        _tab_new()
