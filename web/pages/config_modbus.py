"""
web/pages/config_modbus.py
==========================
Modbus TCP 點位設定（v2 admin_app.py 的「📡 Modbus 點位設定」分頁，原封不動搬過來）。
需要 engineer 以上權限。
"""

import json

import pandas as pd
import streamlit as st

from data_layer.db_connector import DatabaseConnector
from web.common import (
    UNBOUND_LABEL,
    _binding_filter_caption,
    _find_binding_conflict,
    _load_sensor_binding_map,
    _load_sensor_options,
    _normalize_state_dict,
    _sensor_select_options,
    audit_ui,
    frame_changes,
    require_role,
)

from protocols.modbus_codec import (
    BIT_FUNCTIONS,
    DATA_TYPES,
    ORDER_PRESETS,
    apply_linear_scaling,
    decode,
    register_count,
)
from protocols.modbus_protocol import TRANSPORTS, ModbusConnection
from web import nav
from web.common import column_exists, execute, fmt_age, get_service_status


# ------------------------------------------------------------------
# 即時測試讀取（不寫入 DB）：v3.1 改用與採集程式相同的連線類別與解碼，支援三種傳輸方式
# ------------------------------------------------------------------
def test_modbus_read(ip, port, slave_id, function_code, start_address, data_type, byte_order,
                     word_order, use_scale=False, raw_min=0.0, raw_max=10000.0, eng_min=0.0,
                     eng_max=10000.0, transport="tcp", serial_settings=None):
    conn = ModbusConnection(transport, ip, int(port), timeout=3, serial_settings=serial_settings)
    try:
        count = 1 if int(function_code) in BIT_FUNCTIONS else register_count(data_type)
        res = conn.read(int(function_code), int(start_address), count, int(slave_id))
    finally:
        conn.close()
    if not res.ok:
        return False, f"❌ 讀取失敗（{res.elapsed_ms:.0f} ms）：{res.error}"
    raw = decode(res.values, data_type, byte_order, word_order)
    msg = f"✅ 讀取成功（{res.elapsed_ms:.0f} ms）｜原始暫存器：{res.values}｜解碼值：{raw}"
    if use_scale:
        msg += f"｜工程值：{apply_linear_scaling(raw, raw_min, raw_max, eng_min, eng_max)}"
    if count >= 2:
        alts = {name: decode(res.values, data_type, bo, wo) for name, (bo, wo) in ORDER_PRESETS.items()}
        msg += "｜四種位元組順序對照：" + "、".join(f"{k}={v:.6g}" if v is not None else f"{k}=—"
                                                   for k, v in alts.items())
    return True, msg


def render():
    require_role("engineer")
    st.header("📡 Modbus 點位配置")
    has_v31 = column_exists("modbus_scada", "transport")
    extra_cols = ", transport, serial_settings, enabled" if has_v31 else ""
    if not has_v31:
        st.info("尚未執行 `sql/015_modbus_transport.sql`：目前只支援 Modbus TCP，也無法個別停用點位。")

    debug_page = nav.get("modbus-debug")
    if debug_page:
        st.page_link(debug_page, label="開啟 Modbus 線上調適工具（讀暫存器、比對位元組順序、掃描站號）", icon="🔧")
    _render_poll_stats()

    def load_modbus_tags():
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    query = """
                    SELECT 
                        id, name, plc_ip, plc_port, slave_id, function_code, start_address, 
                        data_type, raw_min, raw_max, eng_min, eng_max, byte_order, word_order,
                        state_dictionary, sensor_id, plc_state, current_value,current_data,unit, last_update
                        {extra_cols}
                    FROM modbus_scada 
                    ORDER BY id ASC;
                    """.format(extra_cols=extra_cols)
                    cur.execute(query)
                    cols = [desc[0] for desc in cur.description]
                    rows = cur.fetchall()
                    return pd.DataFrame(rows, columns=cols)
        except Exception as e:
            st.error(f"無法讀取 Modbus 點位資料: {e}")
            return pd.DataFrame()

    df_modbus = load_modbus_tags()
    modbus_label_to_id, modbus_id_to_label = _load_sensor_options()
    # 綁定選單共用：只列出尚未被任何點位綁走的感測器。
    # 放在分頁層級（不是 if not df.empty 裡面），因為下方的「單筆新增」表單也要用，
    # 表格為空時仍必須有值可用。
    modbus_binding_map = _load_sensor_binding_map()
    modbus_free_options, _ = _sensor_select_options(modbus_label_to_id, modbus_binding_map)

    st.subheader("📋 Modbus 點位列表（可直接於表格內修改參數）")
    st.caption(
        "此表格為**編輯既有資料**用：新增請使用下方的專用表單。"
        "刪除目前沒有提供網頁入口，需要時請直接在資料庫執行 DELETE。"
    )
    if not df_modbus.empty:
        # 把 sensor_id 轉成人類可讀的 sensor_label 欄位，供下拉選單編輯，
        # 存檔時再反查回 sensor_id。
        df_modbus["sensor_label"] = df_modbus["sensor_id"].apply(
            lambda sid: modbus_id_to_label.get(int(sid), UNBOUND_LABEL)
            if pd.notnull(sid)
            else UNBOUND_LABEL
        )
        df_modbus_display = df_modbus.drop(columns=["sensor_id"])

        # current_data / current_value / plc_state / last_update 都是採集程式寫入的
        # 執行期欄位，儲存邏輯不會把它們寫回去，因此一律鎖住避免使用者白改一場。
        disabled_cols_modbus = [
            "id",
            "plc_state",
            "current_value",
            "current_data",
            "last_update",
        ]

        # keep_ids 保留本畫面各列自己的綁定，否則值不在 options 內會被 Streamlit 清空。
        modbus_options, modbus_hidden = _sensor_select_options(
            modbus_label_to_id,
            modbus_binding_map,
            keep_ids=df_modbus["sensor_id"].dropna().astype(int).tolist(),
        )
        _cap = _binding_filter_caption(modbus_hidden)
        if _cap:
            st.caption(_cap)

        edited_modbus_df = st.data_editor(
            df_modbus_display,
            # ⚠️ 這裡刻意用 "fixed" 而非 "dynamic"：儲存邏輯只會對既有列做 UPDATE，
            #    表格上新增的列（id 為空）會被略過、刪掉的列也不會真的從資料庫移除。
            #    開著 dynamic 會讓使用者以為新增/刪除成功（還會跳「儲存成功」），
            #    實際上什麼都沒發生。新增請用下方的專用表單。
            num_rows="fixed",
            key="modbus_editor",
            disabled=disabled_cols_modbus,
            width="stretch",
            column_config={
                "transport": st.column_config.SelectboxColumn(
                    "傳輸方式", options=list(TRANSPORTS), required=True,
                    help="tcp=Modbus TCP｜rtu_over_tcp=序列閘道透通模式｜rtu=本機序列埠（plc_ip 填 /dev/ttyUSB0）",
                ),
                "serial_settings": st.column_config.TextColumn(
                    "序列埠參數", help="transport=rtu 時填「鮑率,資料位元,同位,停止位元」，例如 9600,8,N,1"),
                "enabled": st.column_config.CheckboxColumn("採集", help="取消勾選 = 暫停採集這個點位"),
                "function_code": st.column_config.SelectboxColumn("功能碼", options=[1, 2, 3, 4], required=True),
                "data_type": st.column_config.SelectboxColumn(
                    "資料型態", options=sorted(set(DATA_TYPES) | {"word", "int", "dint", "float", "double"}),
                    required=True),
                "byte_order": st.column_config.SelectboxColumn("byte_order", options=["BIG", "LITTLE"], required=True),
                "word_order": st.column_config.SelectboxColumn("word_order", options=["BIG", "LITTLE"], required=True),
                "sensor_label": st.column_config.SelectboxColumn(
                    "綁定感測器 (sensor_code)",
                    options=modbus_options,
                    help="只列出尚未被其他點位綁定的感測器。"
                    "選單內容請先在「感測器階層管理」分頁建立。",
                )
            },
        )

        if st.button("💾 儲存 Modbus 修改", type="primary"):
            binding_map = _load_sensor_binding_map()
            try:
                with DatabaseConnector.get_connection() as conn:
                    with conn.cursor() as cur:
                        for index, row in edited_modbus_df.iterrows():
                            if pd.notnull(row["id"]):
                                point_id = int(row["id"])

                                state_dict_val, state_dict_err = _normalize_state_dict(
                                    row["state_dictionary"]
                                )
                                if state_dict_err:
                                    st.error(
                                        f"❌ id={point_id} 的 "
                                        f"state_dictionary 格式錯誤，該筆未儲存：{state_dict_err}"
                                    )
                                    continue

                                sensor_label = row.get("sensor_label", UNBOUND_LABEL)
                                sensor_id = (
                                    modbus_label_to_id.get(sensor_label)
                                    if sensor_label != UNBOUND_LABEL
                                    else None
                                )
                                conflict = _find_binding_conflict(
                                    binding_map, sensor_id, "modbus_scada", point_id
                                )
                                if conflict:
                                    st.error(
                                        f"❌ id={point_id}（{row['name']}）想綁定的感測器"
                                        f"已被其他點位使用：{conflict}，該筆未儲存。"
                                        "同一個感測器不能同時綁定多個點位。"
                                    )
                                    continue

                                if has_v31:
                                    cur.execute(
                                        "UPDATE modbus_scada SET transport=%s, serial_settings=%s, enabled=%s "
                                        "WHERE id=%s;",
                                        (row["transport"] or "tcp",
                                         str(row["serial_settings"]).strip()
                                         if pd.notnull(row["serial_settings"]) and str(row["serial_settings"]).strip()
                                         else None,
                                         bool(row["enabled"]), point_id),
                                    )
                                sql = """
                                UPDATE modbus_scada SET
                                    name=%s, plc_ip=%s, plc_port=%s, slave_id=%s,
                                    function_code=%s, start_address=%s, data_type=%s,
                                    raw_min=%s, raw_max=%s, eng_min=%s, eng_max=%s,
                                    byte_order=%s, word_order=%s, state_dictionary=%s,
                                    unit=%s, sensor_id=%s
                                WHERE id=%s;
                                """
                                cur.execute(
                                    sql,
                                    (
                                        row["name"],
                                        row["plc_ip"],
                                        int(row["plc_port"]),
                                        int(row["slave_id"]),
                                        int(row["function_code"]),
                                        int(row["start_address"]),
                                        row["data_type"],
                                        float(row["raw_min"])
                                        if pd.notnull(row["raw_min"])
                                        else None,
                                        float(row["raw_max"])
                                        if pd.notnull(row["raw_max"])
                                        else None,
                                        float(row["eng_min"])
                                        if pd.notnull(row["eng_min"])
                                        else None,
                                        float(row["eng_max"])
                                        if pd.notnull(row["eng_max"])
                                        else None,
                                        row["byte_order"],
                                        row["word_order"],
                                        state_dict_val,  # 已由 _normalize_state_dict 正規化為合法 JSON 字串或 None
                                        # unit 原本漏掉沒帶進 UPDATE：欄位在表格裡可以編輯、
                                        # 存檔也會顯示成功，但值其實從來沒被寫回去
                                        str(row["unit"]).strip()
                                        if pd.notnull(row["unit"]) and str(row["unit"]).strip()
                                        else None,
                                        sensor_id,
                                        point_id,
                                    ),
                                )
                        conn.commit()
                audit_ui("modbus.update", "modbus_scada", frame_changes(df_modbus_display, edited_modbus_df, "id"))
                st.success("✅ Modbus 點位參數更新成功！")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 儲存失敗: {e}")

    st.divider()
    st.subheader("➕ 單筆新增 Modbus 點位")
    with st.form("add_modbus_form", clear_on_submit=False):
        col1, col2, col3 = st.columns(3)
        with col1:
            m_name = st.text_input("點位名稱 (name)", "Test")
            m_transport = st.selectbox(
                "傳輸方式", list(TRANSPORTS), format_func=TRANSPORTS.get,
                disabled=not has_v31, help="需要 sql/015；未執行時固定為 Modbus TCP",
            ) if has_v31 else "tcp"
            m_plc_ip = st.text_input("IP 地址 / 序列埠路徑 (plc_ip)", "192.168.1.1",
                                     help="傳輸方式為「RTU 序列埠」時填 /dev/ttyUSB0 之類的路徑")
            m_serial = st.text_input("序列埠參數（僅 RTU 序列埠）", "9600,8,N,1") if has_v31 else None
            m_plc_port = st.number_input("Modbus 埠號 (plc_port)", value=502)
            m_slave_id = st.number_input(
                "從站 ID (slave_id)", value=1, min_value=1, max_value=247
            )
            m_unit = st.text_input("單位 (unit)", "單位")

        with col2:
            fc_mapping = {
                "1 = Read Coils": 1,
                "2 = Read Discrete Inputs": 2,
                "3 = Read Holding Registers": 3,
                "4 = Read Input Registers": 4,
            }
            fc_label = st.selectbox(
                "功能碼 (function_code)", list(fc_mapping.keys()), index=2
            )
            m_function_code = fc_mapping[fc_label]
            m_start_address = st.number_input("起始位址 (start_address)", value=0)
            m_data_type = st.selectbox(
                "資料型態 (data_type)",
                [
                    "bool",
                    "word",
                    "int",
                    "dint",
                    "float",
                    "uint32",
                    "int64",
                    "float64",
                    "uint64",
                ],
                index=4,
            )
            m_byte_order = st.selectbox(
                "位元組順序 (byte_order)", ["BIG", "LITTLE"], index=0
            )
            m_word_order = st.selectbox(
                "字組順序 (word_order)", ["BIG", "LITTLE"], index=0
            )

        with col3:
            m_use_scale = st.checkbox("啟用 Scaling (線性轉換)", value=False)
            m_raw_min = st.number_input("原始最小值 (raw_min)", value=0.0)
            m_raw_max = st.number_input("原始最大值 (raw_max)", value=10000.0)
            m_eng_min = st.number_input("工程最小值 (eng_min)", value=0.0)
            m_eng_max = st.number_input("工程最大值 (eng_max)", value=1000.0)
            m_state_dict = st.text_input(
                "狀態字典 JSON (state_dictionary)",
                value="",
                placeholder='{"1": "待機", "2": "運轉"}',
            )
            m_sensor_label = st.selectbox(
                "綁定感測器 (sensor_code，可留空)",
                modbus_free_options,
                help="選填，只列出尚未被其他點位綁定的感測器；"
                "之後也可以在上方表格內再綁定/修改。",
            )

        btn_col1, btn_col2 = st.columns([1, 1])
        with btn_col1:
            submit_modbus = st.form_submit_button(
                "新增 Modbus 點位", type="primary", use_container_width=True
            )
        with btn_col2:
            test_modbus = st.form_submit_button(
                "🧪 測試連線與讀取", use_container_width=True
            )

        if test_modbus:
            with st.spinner("📡 正在連線 PLC 並嘗試讀取數據..."):
                ok, msg = test_modbus_read(
                    m_plc_ip,
                    m_plc_port,
                    m_slave_id,
                    m_function_code,
                    m_start_address,
                    m_data_type,
                    m_byte_order,
                    m_word_order,
                    m_use_scale,
                    m_raw_min,
                    m_raw_max,
                    m_eng_min,
                    m_eng_max,
                    transport=m_transport,
                    serial_settings=m_serial,
                )
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)

        if submit_modbus:
            try:
                formatted_state_dict = None
                if m_state_dict.strip():
                    formatted_state_dict = json.dumps(json.loads(m_state_dict))

                new_sensor_id = (
                    modbus_label_to_id.get(m_sensor_label)
                    if m_sensor_label != UNBOUND_LABEL
                    else None
                )
                conflict = None
                if new_sensor_id is not None:
                    binding_map = _load_sensor_binding_map()
                    conflict = _find_binding_conflict(
                        binding_map, new_sensor_id, "modbus_scada", None
                    )

                if conflict:
                    st.error(
                        f"❌ 想綁定的感測器已被其他點位使用：{conflict}，"
                        "未新增。同一個感測器不能同時綁定多個點位。"
                    )
                else:
                    with DatabaseConnector.get_connection() as conn:
                        with conn.cursor() as cur:
                            # 🔧 修正：原本欄位順序跟參數順序沒對齊
                            # （unit 塞錯位置導致 function_code 收到字串），這裡重新對齊，
                            # 並新增 sensor_id 欄位供感測器綁定使用。
                            sql = """
                            INSERT INTO modbus_scada (
                                name, plc_ip, plc_port, slave_id, function_code, start_address, 
                                data_type, byte_order, word_order, raw_min, raw_max, eng_min, eng_max, 
                                state_dictionary, unit, sensor_id, plc_state
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OFFLINE');
                            """
                            if has_v31:
                                sql = sql.replace("sensor_id, plc_state", "sensor_id, plc_state, transport, serial_settings") \
                                         .replace("'OFFLINE');", "'OFFLINE', %s, %s);")
                            params = (
                                    m_name,
                                    m_plc_ip,
                                    int(m_plc_port),
                                    int(m_slave_id),
                                    int(m_function_code),
                                    int(m_start_address),
                                    m_data_type,
                                    m_byte_order,
                                    m_word_order,
                                    m_raw_min if m_use_scale else None,
                                    m_raw_max if m_use_scale else None,
                                    m_eng_min if m_use_scale else None,
                                    m_eng_max if m_use_scale else None,
                                    formatted_state_dict,
                                    m_unit,
                                    new_sensor_id,
                            )
                            if has_v31:
                                params += (m_transport, m_serial if m_transport == "rtu" else None)
                            cur.execute(sql, params)
                            conn.commit()
                    audit_ui("modbus.create", f"modbus:{m_name}")
                    st.success(f"🎉 成功新增 Modbus 點位: {m_name}")
                    st.rerun()
            except json.JSONDecodeError:
                st.error("❌ 狀態字典格式錯誤！請填寫合法的 JSON 格式")
            except Exception as e:
                st.error(f"❌ 新增失敗: {e}")

    _render_delete(df_modbus)


def _render_poll_stats():
    """main.py 回報的各條連線採集統計（需要 sql/012，且 MODBUS_ENABLED=true）。"""
    svc = get_service_status()
    stats = ((svc or {}).get("info") or {}).get("modbus") if svc and svc.get("exists") else None
    if not stats:
        return
    rows = [{
        "連線": label,
        "狀態": {"ONLINE": "🟢 正常", "PARTIAL": "🟡 部分失敗", "OFFLINE": "🔴 離線"}.get(v.get("state"), v.get("state")),
        "點位（成功/總數）": f"{v.get('ok_tags', 0)} / {v.get('tags', 0)}",
        "區塊數": v.get("blocks"),
        "請求數": v.get("requests"),
        "耗時 ms": v.get("last_cycle_ms"),
        "單獨讀取的點位": v.get("isolated_tags"),
        "最後錯誤": v.get("last_error") or "",
    } for label, v in stats.items()]
    with st.expander(f"📈 採集狀況（每條連線，最後回報 {fmt_age(svc['age_seconds'])}）", expanded=False):
        st.caption("同一個閘道後面的多個站號共用一條連線；相近位址合併成一個區塊一次讀取。"
                   "「單獨讀取的點位」是曾經讓整塊讀取失敗、之後改成獨立讀取的點位，建議用線上調適工具確認位址。")
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def _render_delete(df_modbus: pd.DataFrame):
    if df_modbus.empty:
        return
    st.divider()
    with st.expander("🗑️ 刪除點位"):
        labels = {int(r["id"]): f"#{int(r['id'])} {r['name']}（{r['plc_ip']} / 站號 {r['slave_id']} / "
                                f"FC{int(r['function_code']):02d} @ {int(r['start_address'])}）"
                  for _, r in df_modbus.iterrows()}
        chosen = st.multiselect("選擇要刪除的點位", list(labels), format_func=labels.get, key="mb_del")
        st.caption("只會刪除點位設定；已寫入 sensor_readings 的歷史資料會保留（屬於感測器，不屬於點位）。"
                   "只是暫停採集的話，取消表格中的「採集」勾選即可。")
        confirm = st.checkbox(f"我確定要刪除這 {len(chosen)} 個點位", key="mb_del_confirm", disabled=not chosen)
        if st.button("刪除", disabled=not (chosen and confirm), key="mb_del_btn"):
            try:
                n = execute("DELETE FROM modbus_scada WHERE id = ANY(%s);", ([int(i) for i in chosen],))
                audit_ui("modbus.delete", "modbus_scada", {"ids": chosen, "names": [labels[i] for i in chosen]})
                st.success(f"✅ 已刪除 {n} 個點位")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 刪除失敗: {e}")
