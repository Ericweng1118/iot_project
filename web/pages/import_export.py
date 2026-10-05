"""
web/pages/import_export.py
==========================
📥 批次匯入匯出（engineer 以上）

上百個點位一個一個在網頁表格改太慢。這一頁讓你：
    1. 匯出目前的設定（CSV 或 Excel）
    2. 在 Excel 裡批次修改 / 新增（複製貼上、填滿、公式都可以）
    3. 上傳 → 先看預覽：新增幾筆、更新幾筆（每筆列出改了哪些欄位）、哪幾列有錯、錯在哪
    4. 確認後才寫入；整批在同一個交易裡，任何一筆失敗全部回滾

驗證與套用邏輯在 data_layer/bulk_io.py。
"""

import io

import pandas as pd
import streamlit as st

from data_layer import bulk_io
from data_layer.import_templates import build_template
from data_layer.db_connector import DatabaseConnector
from web.common import OPCUA_ENABLED, audit_ui, now_local, require_role, table_exists, to_csv_bytes, to_excel_bytes

ENTITIES = {
    "devices": {
        "label": "🏭 設備",
        "help": "以 device_code 對應：已存在就更新、不存在就新增。廠區 / 產線不存在時會自動建立。",
        "columns": [
            ("device_code", "✅", "設備編號（唯一）"),
            ("device_name", "", "設備名稱"),
            ("device_type", "", "設備類型"),
            ("manufacturer", "", "製造商"),
            ("site_name", "✅", "廠區名稱"),
            ("line_name", "✅", "產線名稱"),
            ("status", "", "預設 active"),
        ],
    },
    "sensors": {
        "label": "🌡️ 感測器",
        "help": "sensor_code 留空 = 新增感測器並自動編號；填既有編號 = 更新該感測器。"
                "device_code 必須已存在（設備請先在「設備」分頁匯入）。",
        "columns": [
            ("sensor_code", "✅（欄位）", "空白 = 新增並自動編號（建議）；填既有編號 = 更新"),
            ("device_code", "✅", "所屬設備"),
            ("sensor_type", "✅", "temperature / pressure / power / energy / flow …"),
            ("nickname", "", "暱稱"),
            ("unit", "", "工程單位"),
            ("min_threshold / max_threshold", "", "上下限（會自動產生 L / H 警報）"),
            ("upload_condition", "", "always / on_change（預設）/ threshold_percent / threshold_absolute"),
            ("upload_threshold", "", "門檻型條件必填"),
            ("opcua_sampling_interval_ms", "", "OPC UA 取樣頻率（毫秒），空白沿用 Server 預設"),
            ("opcua_deadband_type / _value", "", "none / percent / absolute"),
            ("state_dictionary", "", 'JSON，例如 {"1": "運轉", "0": "停止"}'),
        ],
    },
    "modbus": {
        "label": "📡 Modbus 點位",
        "help": "id 空白 = 新增；有 id = 更新該點位。sensor_code 填了就綁定該感測器，空白 = 不綁定。"
                "刪除點位請到「Modbus 點位」頁面。",
        "columns": [
            ("id", "", "空白 = 新增"),
            ("name", "✅", "點位名稱"),
            ("transport", "", "tcp（預設）/ rtu_over_tcp / rtu"),
            ("plc_ip", "✅", "IP，或 rtu 時的序列埠路徑"),
            ("plc_port", "", "預設 502"),
            ("serial_settings", "", "rtu 時：9600,8,N,1"),
            ("slave_id", "✅", "站號 0~255"),
            ("function_code", "✅", "1 / 2 / 3 / 4"),
            ("start_address", "✅", "協議位址（0 起算；手冊 40001 = 0）"),
            ("data_type", "✅", "bool / uint16 / int16 / uint32 / int32 / float32 / … 或 word / int / dint / float"),
            ("byte_order / word_order", "", "BIG / LITTLE（CDAB = BIG + LITTLE）"),
            ("raw_min / raw_max / eng_min / eng_max", "", "線性換算，要嘛全填要嘛全空"),
            ("unit", "", "工程單位"),
            ("state_dictionary", "", "JSON"),
            ("sensor_code", "", "綁定的感測器"),
            ("enabled", "", "是 / 否（預設是）"),
        ],
    },
    "s7": {
        "label": "📡 S7 點位",
        "help": "id 空白 = 新增；有 id = 更新。address 欄位可直接填 TIA 位址（DB1.DBD4、DB1.DBX10.3、MW20、I0.1），"
                "有填就以它為準；沒填就看 area / db_number / offset / bit_offset。刪除點位請到「TIA (S7) 點位」頁面。",
        "columns": [
            ("id", "", "空白 = 新增"),
            ("name", "✅", "點位名稱"),
            ("plc_name / plc_ip", "plc_ip ✅", "PLC 名稱與 IP"),
            ("rack / slot", "", "預設 0 / 1（S7-300 用 0 / 2）"),
            ("address", "", "TIA 位址，填了就覆蓋下面四個欄位"),
            ("area / db_number / offset / bit_offset", "", "DB / M / I / Q、DB 編號、byte、bit（BOOL 用）"),
            ("data_type", "✅", "BOOL / BYTE / INT / WORD / DINT / DWORD / REAL / LREAL / STRING[20] …"),
            ("unit", "", "工程單位"),
            ("sensor_code", "", "綁定的感測器"),
            ("enabled", "", "是 / 否（預設是）"),
        ],
    },
    "opcua_bindings": {
        "label": "📡 OPC UA 綁定",
        "help": "點位由「立即瀏覽」產生，這裡只改綁定：填 sensor_code 就綁定，清空就解除綁定。"
                "幾千個點位可以先在 Excel 篩選 display_name 再批次填入。綁定變更約 5 秒內生效。",
        "columns": [
            ("server_name", "✅", "OPC UA Server 名稱"),
            ("node_id", "✅", "節點 ID"),
            ("display_name / data_type", "", "僅供參考，匯入時忽略"),
            ("sensor_code", "✅（欄位）", "空白 = 解除綁定"),
        ],
    },
    "alarm_rules": {
        "label": "🔔 警報規則",
        "help": "rule_id 空白 = 新增；有 rule_id = 更新。刪除規則請到「警報規則」頁面。",
        "columns": [
            ("rule_id", "", "空白 = 新增"),
            ("sensor_code", "✅", "感測器"),
            ("alarm_type", "✅", "HH / H / L / LL / EQ / NE"),
            ("setpoint", "✅", "設定值"),
            ("deadband", "", "遲滯（預設 0）"),
            ("on_delay_sec", "", "延遲觸發秒數（預設 0）"),
            ("priority", "", "1 緊急 / 2 高（預設）/ 3 中 / 4 低"),
            ("message", "", "附加訊息"),
            ("enabled", "", "是 / 否（預設是）"),
        ],
    },
}


def _read_upload(uploaded) -> pd.DataFrame:
    """一律用字串讀進來（保留 0001 這種代碼），型別轉換交給 bulk_io 的解析器。"""
    data = uploaded.getvalue()
    if uploaded.name.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(data), dtype=str, keep_default_na=False)
    for encoding in ("utf-8-sig", "cp950", "big5"):
        try:
            return pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("無法辨識檔案編碼，請存成 UTF-8 或 Excel 格式")


def _fmt(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "（空）"
    return str(v)


def _changes_table(plan) -> pd.DataFrame:
    rows = []
    for c in plan.changes:
        if c.action == "insert":
            detail = "、".join(f"{k}={_fmt(v)}" for k, v in c.values.items() if v is not None)
        else:
            detail = "、".join(f"{k}: {_fmt(old)} → {_fmt(new)}" for k, (old, new) in c.diff.items())
        rows.append({"列": c.row_no, "動作": "➕ 新增" if c.action == "insert" else "✏️ 更新",
                     "對象": c.key, "內容": detail})
    return pd.DataFrame(rows)


def _plan(entity, df):
    with DatabaseConnector.get_connection() as conn:
        with conn.cursor() as cur:
            return bulk_io.plan_import(cur, entity, df)


def _apply(entity, df, expected):
    """在同一個交易裡重新驗證後套用：預覽之後資料庫若被別人改過，數字不同就不套用。"""
    with DatabaseConnector.get_connection() as conn:
        with conn.cursor() as cur:
            plan = bulk_io.plan_import(cur, entity, df)
            if not plan.ok:
                raise ValueError("重新驗證時發現錯誤，請重新上傳檔案檢查")
            if (len(plan.inserts), len(plan.updates)) != expected:
                raise ValueError("預覽之後資料庫內容有變動，請重新上傳檔案再確認一次")
            result = bulk_io.apply_plan(cur, plan)
    return plan, result


def _render_entity(entity):
    spec = ENTITIES[entity]
    st.caption(spec["help"])
    with st.expander("欄位說明"):
        st.dataframe(pd.DataFrame(spec["columns"], columns=["欄位", "必填", "說明"]),
                     hide_index=True, width="stretch")

    c1, c2, c3 = st.columns([1, 1, 2])
    if c1.button("準備匯出檔", key=f"ie_prep_{entity}"):
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                st.session_state[f"ie_export_{entity}"] = bulk_io.export_frame(cur, entity)
    exported = st.session_state.get(f"ie_export_{entity}")
    if exported is not None:
        stamp = now_local().strftime("%Y%m%d_%H%M")
        c2.download_button(f"⬇️ CSV（{len(exported)} 筆）", to_csv_bytes(exported),
                           file_name=f"{entity}_{stamp}.csv", mime="text/csv", key=f"ie_csv_{entity}")
        xlsx = to_excel_bytes({entity: exported})
        if xlsx:
            c3.download_button("⬇️ Excel", xlsx, file_name=f"{entity}_{stamp}.xlsx", key=f"ie_xlsx_{entity}",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    t1, t2, _ = st.columns([1, 1, 2])
    if t1.button("📄 準備匯入範本", key=f"ie_tpl_prep_{entity}",
                 help="空白的 Excel 範本：必填欄位標色、標題有說明、選項欄位有下拉選單（依目前資料庫內容產生）。"
                 + ("已預先列出所有未綁定的點位，只要填 sensor_code。" if entity == "opcua_bindings" else "")):
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                st.session_state[f"ie_tpl_{entity}"] = build_template(cur, entity, spec["columns"])
    template = st.session_state.get(f"ie_tpl_{entity}")
    if template is not None:
        t2.download_button("⬇️ 下載範本（Excel）", template, file_name=f"{entity}_範本.xlsx", key=f"ie_tpl_dl_{entity}",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    nonce = st.session_state.get(f"ie_nonce_{entity}", 0)
    uploaded = st.file_uploader("上傳修改後的檔案（CSV 或 Excel）", type=["csv", "xlsx"],
                                key=f"ie_upload_{entity}_{nonce}")
    if uploaded is None:
        return
    try:
        df = _read_upload(uploaded)
    except Exception as e:
        st.error(f"讀取檔案失敗：{e}")
        return
    if df.empty:
        st.warning("檔案沒有任何資料列。")
        return

    plan = _plan(entity, df)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("新增", len(plan.inserts), border=True)
    m2.metric("更新", len(plan.updates), border=True)
    m3.metric("未變動", plan.unchanged, border=True)
    m4.metric("錯誤", len(plan.errors), border=True)
    for note in plan.notes:
        st.info(note)
    if plan.errors:
        st.error("有錯誤的列必須修正後重新上傳，修正前不會寫入任何資料。")
        st.dataframe(pd.DataFrame(plan.errors, columns=["列", "錯誤"]), hide_index=True, width="stretch")
    if plan.changes:
        st.markdown("**變更預覽**")
        st.dataframe(_changes_table(plan), hide_index=True, width="stretch",
                     height=min(36 * (len(plan.changes) + 1), 480))
    elif not plan.errors:
        st.success("檔案內容與資料庫一致，沒有需要套用的變更。")
        return

    if st.button(f"✅ 套用 {len(plan.changes)} 筆變更", type="primary", disabled=not plan.ok or not plan.changes,
                 key=f"ie_apply_{entity}"):
        try:
            applied, result = _apply(entity, df, (len(plan.inserts), len(plan.updates)))
        except Exception as e:
            st.error(f"❌ 套用失敗，沒有寫入任何資料：{e}")
            return
        audit_ui(f"import.{entity}", entity, {
            "file": uploaded.name, **result,
            "keys": [c.key for c in applied.changes][:200],
            "diffs": {c.key: c.diff for c in applied.updates[:200]},
        })
        st.session_state[f"ie_nonce_{entity}"] = nonce + 1     # 清掉上傳的檔案
        st.session_state.pop(f"ie_export_{entity}", None)
        done = f"✅ {spec['label']}：新增 {result['inserted']} 筆、更新 {result['updated']} 筆"
        codes = result.get("generated_codes")
        if codes:
            done += f"；自動編號 {codes[0]}" + (f" ~ {codes[-1]}" if len(codes) > 1 else "")
        st.session_state["ie_done"] = done
        st.rerun()


def render():
    require_role("engineer")
    st.title("📥 批次匯入匯出")
    st.caption("修改既有資料：匯出 → 用 Excel 批次修改 → 上傳。從零建立：下載匯入範本 → 填寫 → 上傳。"
               "上傳後會先預覽，確認才套用；整批在同一個交易內完成，有任何錯誤都不會寫入。")
    if st.session_state.get("ie_done"):
        st.success(st.session_state.pop("ie_done"))

    from web.common import TIA_ENABLED
    entities = ["devices", "sensors", "modbus"]
    if TIA_ENABLED or table_exists("tia_scada"):
        entities.append("s7")
    if OPCUA_ENABLED:
        entities.append("opcua_bindings")
    if table_exists("alarm_rules"):
        entities.append("alarm_rules")
    tabs = st.tabs([ENTITIES[e]["label"] for e in entities])
    for tab, entity in zip(tabs, entities):
        with tab:
            _render_entity(entity)
