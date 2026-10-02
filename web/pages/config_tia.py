"""
web/pages/config_tia.py
=======================
Siemens TIA (S7) 點位設定（engineer 以上）。

🆕 v3.3：
    - 支援 DB / M / I / Q 區域、BOOL 位元、Rack / Slot、個別停用、工程單位（需要 sql/019）
    - 新增點位可直接輸入 TIA 位址（%DB1.DBD4、DB1.DBX10.3、MW20、I0.1），自動換算
    - 測試讀取改用與採集程式相同的連線與解碼（protocols/s7_codec.py），支援全部 TIA 型態
    - 刪除點位、每台 PLC 的採集統計、連到「S7 線上調適」
"""

import pandas as pd
import streamlit as st

from data_layer.db_connector import DatabaseConnector
from protocols import s7_codec as C
from protocols.s7_protocol import S7Connection
from web import nav
from web.common import (
    UNBOUND_LABEL,
    _binding_filter_caption,
    _find_binding_conflict,
    _load_sensor_binding_map,
    _load_sensor_options,
    _sensor_select_options,
    audit_ui,
    column_exists,
    execute,
    fmt_age,
    frame_changes,
    get_service_status,
    require_role,
)

TYPE_OPTIONS = C.NUMERIC_TYPES + ["CHAR", "STRING", "STRING[20]", "STRING[50]"]


# ------------------------------------------------------------------
# 即時測試讀取（不寫入 DB）：與採集程式相同的連線與解碼
# ------------------------------------------------------------------
def test_tia_read(ip, db_number, offset, data_type, rack=0, slot=1, area="DB", bit=0):
    try:
        size = 1 if C.normalize_type(data_type) == "BOOL" else C.type_size(data_type)
    except ValueError as e:
        return False, f"❌ {e}"
    conn = S7Connection(ip, int(rack), int(slot))
    try:
        res = conn.read(area, int(db_number or 0), int(offset), size)
    finally:
        conn.close()
    address = C.format_address(area, int(db_number or 0), int(offset), data_type, int(bit))
    if not res.ok:
        return False, f"❌ {address} 讀取失敗（{res.elapsed_ms:.0f} ms）：{res.error}"
    try:
        value = C.decode(res.data, 0, data_type, int(bit))
    except ValueError as e:
        return False, f"❌ 解析失敗：{e}"
    return True, f"🎉 {address} = `{value}`（原始 {res.data.hex(' ').upper()}，回應 {res.elapsed_ms:.0f} ms）"


def render():
    require_role("engineer")
    st.header("📡 Siemens TIA (S7) 點位配置")
    v33 = column_exists("tia_scada", "area")
    if not v33:
        st.info("尚未執行 `sql/019_s7_extensions.sql`：目前只能讀 DB 區域、BOOL 只能讀第 0 bit、Rack/Slot 固定 0/1。")
    debug_page = nav.get("s7-debug")
    if debug_page:
        st.page_link(debug_page, label="開啟 S7 線上調適工具（CPU 資訊、讀記憶體找 offset、驗證點位）", icon="🔧")
    _render_poll_stats()

    def load_tia_tags():
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    query = """
                    SELECT 
                        id, name, plc_name, plc_ip, {extra} db_number, "offset", data_type,
                        sensor_id, plc_state, current_data, last_update
                    FROM tia_scada
                    ORDER BY id ASC;
                    """.format(extra="rack, slot, area, bit_offset, unit, enabled," if v33 else "")
                    cur.execute(query)
                    cols = [desc[0] for desc in cur.description]
                    rows = cur.fetchall()
                    return pd.DataFrame(rows, columns=cols)
        except Exception as e:
            st.error(f"無法讀取 TIA 點位資料: {e}")
            return pd.DataFrame()

    df_tia = load_tia_tags()
    tia_label_to_id, tia_id_to_label = _load_sensor_options()
    # 綁定選單共用：只列出尚未被任何點位綁走的感測器。
    # 放在分頁層級（不是 if not df.empty 裡面），因為下方的「單筆新增」表單也要用，
    # 表格為空時仍必須有值可用。
    tia_binding_map = _load_sensor_binding_map()
    tia_free_options, _ = _sensor_select_options(tia_label_to_id, tia_binding_map)

    st.subheader("📋 TIA 點位列表（可直接於表格內修改參數）")
    st.caption(
        "此表格為**編輯既有資料**用：新增請使用下方的專用表單。"
        "刪除目前沒有提供網頁入口，需要時請直接在資料庫執行 DELETE。"
    )
    if not df_tia.empty:
        df_tia["sensor_label"] = df_tia["sensor_id"].apply(
            lambda sid: tia_id_to_label.get(int(sid), UNBOUND_LABEL)
            if pd.notnull(sid)
            else UNBOUND_LABEL
        )
        df_tia_display = df_tia.drop(columns=["sensor_id"])

        disabled_cols_tia = ["id", "plc_state", "current_data", "last_update"]

        # keep_ids 保留本畫面各列自己的綁定，否則值不在 options 內會被 Streamlit 清空。
        tia_options, tia_hidden = _sensor_select_options(
            tia_label_to_id,
            tia_binding_map,
            keep_ids=df_tia["sensor_id"].dropna().astype(int).tolist(),
        )
        _cap = _binding_filter_caption(tia_hidden)
        if _cap:
            st.caption(_cap)

        edited_tia_df = st.data_editor(
            df_tia_display,
            # ⚠️ 這裡刻意用 "fixed" 而非 "dynamic"：儲存邏輯只會對既有列做 UPDATE，
            #    表格上新增的列（id 為空）會被略過、刪掉的列也不會真的從資料庫移除。
            #    開著 dynamic 會讓使用者以為新增/刪除成功（還會跳「儲存成功」），
            #    實際上什麼都沒發生。新增請用下方的專用表單。
            num_rows="fixed",
            key="tia_editor",
            disabled=disabled_cols_tia,
            width="stretch",
            column_config={
                "area": st.column_config.SelectboxColumn("區域", options=list(C.AREAS), required=True),
                "data_type": st.column_config.SelectboxColumn("資料型態", options=TYPE_OPTIONS, required=True),
                "bit_offset": st.column_config.NumberColumn("bit", min_value=0, max_value=7,
                                                            help="BOOL 的位元 0~7，例如 DBX10.3 填 3"),
                "rack": st.column_config.NumberColumn("Rack", min_value=0, max_value=7),
                "slot": st.column_config.NumberColumn("Slot", min_value=0, max_value=31,
                                                      help="S7-1200/1500 = 1，S7-300 = 2"),
                "enabled": st.column_config.CheckboxColumn("採集"),
                "sensor_label": st.column_config.SelectboxColumn(
                    "綁定感測器 (sensor_code)",
                    options=tia_options,
                    help="只列出尚未被其他點位綁定的感測器。"
                    "選單內容請先在「感測器階層管理」分頁建立。",
                )
            },
        )

        if st.button("💾 儲存 TIA 修改", type="primary"):
            binding_map = _load_sensor_binding_map()
            try:
                with DatabaseConnector.get_connection() as conn:
                    with conn.cursor() as cur:
                        for index, row in edited_tia_df.iterrows():
                            if pd.notnull(row["id"]):
                                point_id = int(row["id"])

                                sensor_label = row.get("sensor_label", UNBOUND_LABEL)
                                sensor_id = (
                                    tia_label_to_id.get(sensor_label)
                                    if sensor_label != UNBOUND_LABEL
                                    else None
                                )
                                conflict = _find_binding_conflict(
                                    binding_map, sensor_id, "tia_scada", point_id
                                )
                                if conflict:
                                    st.error(
                                        f"❌ id={point_id}（{row['name']}）想綁定的感測器"
                                        f"已被其他點位使用：{conflict}，該筆未儲存。"
                                        "同一個感測器不能同時綁定多個點位。"
                                    )
                                    continue

                                if v33:
                                    cur.execute(
                                        "UPDATE tia_scada SET area=%s, bit_offset=%s, rack=%s, slot=%s, "
                                        "unit=%s, enabled=%s WHERE id=%s;",
                                        (row["area"] or "DB", int(row["bit_offset"] or 0), int(row["rack"] or 0),
                                         int(row["slot"]) if pd.notnull(row["slot"]) else 1,
                                         str(row["unit"]).strip() if pd.notnull(row["unit"]) and str(row["unit"]).strip() else None,
                                         bool(row["enabled"]), point_id),
                                    )
                                sql = """
                                UPDATE tia_scada SET
                                    name=%s, plc_name=%s, plc_ip=%s, db_number=%s,
                                    "offset"=%s, data_type=%s, sensor_id=%s
                                WHERE id=%s;
                                """
                                cur.execute(
                                    sql,
                                    (
                                        row["name"],
                                        row["plc_name"],
                                        row["plc_ip"],
                                        int(row["db_number"] or 0),
                                        int(row["offset"]),
                                        row["data_type"],
                                        sensor_id,
                                        point_id,
                                    ),
                                )
                        conn.commit()
                audit_ui("tia.update", "tia_scada", frame_changes(df_tia_display, edited_tia_df, "id"))
                st.success("✅ TIA 點位參數更新成功！")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 儲存失敗: {e}")

    st.divider()
    st.subheader("➕ 單筆新增 TIA S7 點位")
    with st.form("add_tia_form", clear_on_submit=False):
        quick = st.text_input("TIA 位址（選填，填了就以它為準）", placeholder="%DB1.DBD4、DB1.DBX10.3、MW20、I0.1",
                              help="直接貼 TIA Portal 的位址，自動換算區域 / DB / byte / bit")
        col1, col2, col3 = st.columns(3)
        with col1:
            t_name = st.text_input("點位名稱 (name)", "T3_PM:kW")
            t_plc_name = st.text_input("PLC 設備名稱 (plc_name)", "PLC_MAIN")
            t_plc_ip = st.text_input("PLC IP 地址 (plc_ip)", "192.168.1.200")
            t_unit = st.text_input("工程單位", "", disabled=not v33)
        with col2:
            t_area = st.selectbox("區域", list(C.AREAS), format_func=C.AREAS.get, disabled=not v33)
            t_db_number = st.number_input("DB 區塊號碼 (db_number)", value=1, min_value=0)
            t_offset = st.number_input("byte 偏移量 (offset)", value=0, min_value=0)
            t_bit = st.number_input("bit（BOOL 用）", value=0, min_value=0, max_value=7, disabled=not v33)
        with col3:
            t_data_type = st.selectbox("資料型態 (data_type)", TYPE_OPTIONS, index=TYPE_OPTIONS.index("REAL"))
            r1, r2 = st.columns(2)
            t_rack = r1.number_input("Rack", value=0, min_value=0, max_value=7, disabled=not v33)
            t_slot = r2.number_input("Slot", value=1, min_value=0, max_value=31, disabled=not v33,
                                     help="S7-1200/1500 = 1，S7-300 = 2")
            t_sensor_label = st.selectbox(
                "綁定感測器 (sensor_code，可留空)",
                tia_free_options,
                help="選填，只列出尚未被其他點位綁定的感測器；"
                "之後也可以在上方表格內再綁定/修改。",
            )

        btn_col1, btn_col2 = st.columns([1, 1])
        with btn_col1:
            submit_tia = st.form_submit_button(
                "新增 TIA 點位", type="primary", use_container_width=True
            )
        with btn_col2:
            test_tia = st.form_submit_button(
                "🧪 測試連線與讀取", use_container_width=True
            )

        if quick.strip():
            try:
                parsed = C.parse_address(quick)
                t_area, t_offset, t_bit = parsed["area"], parsed["byte"], parsed["bit"]
                if parsed["area"] == "DB":
                    t_db_number = parsed["db"]
                if parsed["width"] == 0:
                    t_data_type = "BOOL"
                st.caption(f"↳ 區域 {t_area}" + (f"、DB{t_db_number}" if t_area == "DB" else "")
                           + f"、byte {t_offset}" + (f"、bit {t_bit}" if t_data_type == "BOOL" else "")
                           + f"、型態 {t_data_type}")
            except ValueError as e:
                st.error(str(e))
                test_tia = submit_tia = False
        if not v33 and (t_area != "DB" or int(t_bit) != 0):
            st.error("尚未執行 sql/019，只能建立 DB 區域、bit 0 的點位")
            test_tia = submit_tia = False

        if test_tia:
            with st.spinner("📡 正在連線 Siemens PLC 並嘗試讀取 DB 數據..."):
                ok, msg = test_tia_read(
                    t_plc_ip, t_db_number, t_offset, t_data_type, t_rack, t_slot, t_area, t_bit
                )
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)

        if submit_tia:
            try:
                new_sensor_id = (
                    tia_label_to_id.get(t_sensor_label)
                    if t_sensor_label != UNBOUND_LABEL
                    else None
                )
                conflict = None
                if new_sensor_id is not None:
                    binding_map = _load_sensor_binding_map()
                    conflict = _find_binding_conflict(
                        binding_map, new_sensor_id, "tia_scada", None
                    )

                if conflict:
                    st.error(
                        f"❌ 想綁定的感測器已被其他點位使用：{conflict}，"
                        "未新增。同一個感測器不能同時綁定多個點位。"
                    )
                else:
                    with DatabaseConnector.get_connection() as conn:
                        with conn.cursor() as cur:
                            cols = ["name", "plc_name", "plc_ip", "db_number", '"offset"', "data_type",
                                    "sensor_id", "plc_state"]
                            vals = [t_name, t_plc_name, t_plc_ip, int(t_db_number) if t_area == "DB" else 0,
                                    int(t_offset), t_data_type, new_sensor_id, "OFFLINE"]
                            if v33:
                                cols += ["area", "bit_offset", "rack", "slot", "unit"]
                                vals += [t_area, int(t_bit), int(t_rack), int(t_slot), t_unit.strip() or None]
                            cur.execute(
                                f"INSERT INTO tia_scada ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))});",
                                vals,
                            )
                            conn.commit()
                    audit_ui("tia.create", f"tia:{t_name}")
                    st.success(f"🎉 成功新增 TIA 點位: {t_name}")
                    st.rerun()
            except Exception as e:
                st.error(f"❌ 新增失敗: {e}")

    _render_delete(df_tia)


def _render_poll_stats():
    svc = get_service_status()
    stats = ((svc or {}).get("info") or {}).get("s7") if svc and svc.get("exists") else None
    if not stats:
        return
    rows = [{
        "PLC": label,
        "狀態": {"ONLINE": "🟢 正常", "PARTIAL": "🟡 部分失敗", "OFFLINE": "🔴 離線"}.get(v.get("state"), v.get("state")),
        "點位（成功/總數）": f"{v.get('ok_tags', 0)} / {v.get('tags', 0)}",
        "區塊數": v.get("blocks"), "請求數": v.get("requests"), "耗時 ms": v.get("last_cycle_ms"),
        "單獨讀取的點位": v.get("isolated_tags"), "最後錯誤": v.get("last_error") or "",
    } for label, v in stats.items()]
    with st.expander(f"📈 採集狀況（每台 PLC，最後回報 {fmt_age(svc['age_seconds'])}）"):
        st.caption("同一個 DB / 區域裡位址相近的點位合併成一次讀取。「單獨讀取的點位」是曾讓整塊讀取失敗的點位，"
                   "建議用 S7 線上調適確認 offset 與 DB 長度。")
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def _render_delete(df_tia: pd.DataFrame):
    if df_tia.empty:
        return
    st.divider()
    with st.expander("🗑️ 刪除點位"):
        labels = {int(r["id"]): f"#{int(r['id'])} {r['name']}（{r['plc_ip']} DB{r['db_number']} @ {r['offset']}）"
                  for _, r in df_tia.iterrows()}
        chosen = st.multiselect("選擇要刪除的點位", list(labels), format_func=labels.get, key="tia_del")
        st.caption("只刪除點位設定；感測器的歷史資料會保留。只是暫停採集的話取消「採集」勾選即可。")
        confirm = st.checkbox(f"我確定要刪除這 {len(chosen)} 個點位", key="tia_del_ok", disabled=not chosen)
        if st.button("刪除", disabled=not (chosen and confirm), key="tia_del_btn"):
            n = execute("DELETE FROM tia_scada WHERE id = ANY(%s);", ([int(i) for i in chosen],))
            audit_ui("tia.delete", "tia_scada", {"ids": chosen, "names": [labels[i] for i in chosen]})
            st.success(f"✅ 已刪除 {n} 個點位")
            st.rerun()
