"""
web/pages/config_hierarchy.py
=============================
感測器階層管理：廠區 → 產線 → 設備 → 感測器（v2 admin_app.py 的「🧬 感測器階層管理」分頁）。
需要 engineer 以上權限。
"""

import json

import pandas as pd
import streamlit as st

from data_layer.db_connector import DatabaseConnector
from data_layer.sensor_codes import allocate_sensor_codes, peek_next_sensor_code
from web.common import (
    _fetch_df,
    _normalize_state_dict,
    audit_ui,
    frame_changes,
    require_role,
)


def render():
    require_role("engineer")
    st.header("感測器階層管理")
    st.caption(
        "在這裡建立 廠區 → 產線 → 設備 → 感測器 的階層資料。"
        "建立好 sensor 之後（sensor_code 會自動編號），回到「Modbus/TIA/OPC UA 點位設定」分頁"
        "把點位綁定到對應的感測器，採集程式就會自動把數值寫進 sensor_readings 時序表。"
    )

    # ------------------------------------------------------------
    # 1. 廠區 (sites)
    # ------------------------------------------------------------
    st.subheader("🏭 廠區 (sites)")
    st.caption(
        "此表格為編輯既有廠區用；新增請使用下方表單。"
        "刪除目前沒有網頁入口（廠區底下若還有產線，資料庫的外鍵也會擋下刪除）。"
    )
    df_sites = _fetch_df(
        "SELECT site_id, site_name, location FROM sites ORDER BY site_id ASC;"
    )
    if not df_sites.empty:
        edited_sites = st.data_editor(
            df_sites,
            # ⚠️ 同上：儲存只做 UPDATE，新增請用下方「新增廠區」表單
            num_rows="fixed",
            key="sites_editor",
            disabled=["site_id"], width="stretch",
        )
        if st.button("💾 儲存廠區修改", key="save_sites"):
            try:
                with DatabaseConnector.get_connection() as conn:
                    with conn.cursor() as cur:
                        for _, row in edited_sites.iterrows():
                            if pd.notnull(row["site_id"]):
                                cur.execute(
                                    "UPDATE sites SET site_name=%s, location=%s WHERE site_id=%s;",
                                    (row["site_name"], row["location"], int(row["site_id"])),
                                )
                        conn.commit()
                audit_ui("site.update", "sites", frame_changes(df_sites, edited_sites, "site_id"))
                st.success("✅ 廠區資料更新成功！")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 儲存失敗: {e}")

    with st.form("add_site_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            new_site_name = st.text_input("廠區名稱 (site_name)", "")
        with col2:
            new_site_location = st.text_input("位置 (location)", "")
        if st.form_submit_button("➕ 新增廠區", type="primary"):
            if new_site_name.strip():
                try:
                    with DatabaseConnector.get_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "INSERT INTO sites (site_name, location) VALUES (%s, %s);",
                                (new_site_name, new_site_location or None),
                            )
                            conn.commit()
                    audit_ui("site.create", f"site:{new_site_name}")
                    st.success(f"🎉 成功新增廠區: {new_site_name}")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 新增失敗: {e}")
            else:
                st.warning("⚠️ 請輸入廠區名稱")

    st.divider()

    # ------------------------------------------------------------
    # 2. 產線 (production_lines)
    # ------------------------------------------------------------
    st.subheader("🏗️ 產線 (production_lines)")
    df_lines = _fetch_df(
        """
        SELECT pl.line_id, s.site_name, pl.line_name, pl.site_id
        FROM production_lines pl
        LEFT JOIN sites s ON pl.site_id = s.site_id
        ORDER BY pl.line_id ASC;
        """
    )
    site_options = {row["site_name"]: row["site_id"] for _, row in df_sites.iterrows()} if not df_sites.empty else {}

    if not df_lines.empty:
        st.dataframe(df_lines[["line_id", "site_name", "line_name"]], width="stretch")

    with st.form("add_line_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            if site_options:
                new_line_site = st.selectbox("所屬廠區 (site_name)", list(site_options.keys()))
            else:
                new_line_site = None
                st.warning("⚠️ 請先新增廠區")
        with col2:
            new_line_name = st.text_input("產線名稱 (line_name)", "")
        if st.form_submit_button("➕ 新增產線", type="primary"):
            if new_line_name.strip() and new_line_site:
                try:
                    with DatabaseConnector.get_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "INSERT INTO production_lines (site_id, line_name) VALUES (%s, %s);",
                                (site_options[new_line_site], new_line_name),
                            )
                            conn.commit()
                    audit_ui("line.create", f"line:{new_line_name}")
                    st.success(f"🎉 成功新增產線: {new_line_name}")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 新增失敗: {e}")
            else:
                st.warning("⚠️ 請輸入產線名稱並選擇所屬廠區")

    st.divider()

    # ------------------------------------------------------------
    # 3. 設備 (devices)
    # ------------------------------------------------------------
    st.subheader("⚙️ 設備 (devices)")
    df_devices = _fetch_df(
        """
        SELECT d.device_id, s.site_name, pl.line_name, d.device_code, d.device_name,
               d.device_type, d.manufacturer, d.install_date, d.status, d.line_id
        FROM devices d
        LEFT JOIN production_lines pl ON d.line_id = pl.line_id
        LEFT JOIN sites s ON pl.site_id = s.site_id
        ORDER BY d.device_id ASC;
        """
    )
    line_options = {
        f"{row['site_name']} / {row['line_name']}": row["line_id"]
        for _, row in df_lines.iterrows()
    } if not df_lines.empty else {}

    if not df_devices.empty:
        st.dataframe(
            df_devices[[
                "device_id", "site_name", "line_name", "device_code",
                "device_name", "device_type", "manufacturer", "install_date", "status",
            ]],
            width="stretch",
        )

    with st.form("add_device_form", clear_on_submit=True):
        col1, col2, col3 = st.columns(3)
        with col1:
            if line_options:
                new_device_line = st.selectbox("所屬產線", list(line_options.keys()))
            else:
                new_device_line = None
                st.warning("⚠️ 請先新增產線")
            new_device_code = st.text_input("設備編號 (device_code，唯一)", "")
        with col2:
            new_device_name = st.text_input("設備名稱 (device_name)", "")
            new_device_type = st.text_input("設備類型 (device_type)", "")
        with col3:
            new_device_manufacturer = st.text_input("製造商 (manufacturer)", "")
            new_device_status = st.selectbox("狀態 (status)", ["active", "maintenance", "offline"])
        if st.form_submit_button("➕ 新增設備", type="primary"):
            if new_device_code.strip() and new_device_line:
                try:
                    with DatabaseConnector.get_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                INSERT INTO devices (line_id, device_code, device_name, device_type, manufacturer, status)
                                VALUES (%s, %s, %s, %s, %s, %s);
                                """,
                                (
                                    line_options[new_device_line],
                                    new_device_code,
                                    new_device_name or None,
                                    new_device_type or None,
                                    new_device_manufacturer or None,
                                    new_device_status,
                                ),
                            )
                            conn.commit()
                    audit_ui("device.create", f"device:{new_device_code}")
                    st.success(f"🎉 成功新增設備: {new_device_code}")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 新增失敗（device_code 需為唯一值）: {e}")
            else:
                st.warning("⚠️ 請輸入設備編號並選擇所屬產線")

    st.divider()

    # ------------------------------------------------------------
    # 4. 感測器 (sensors)
    # ------------------------------------------------------------
    st.subheader("🌡️ 感測器 (sensors)")
    df_sensors_full = _fetch_df(
        """
        SELECT se.sensor_id, d.device_code, se.sensor_code, se.nickname, se.sensor_type,
               se.unit, se.min_threshold, se.max_threshold, se.state_dictionary,
               se.opcua_sampling_interval_ms, se.opcua_deadband_type, se.opcua_deadband_value,
               se.upload_condition, se.upload_threshold,
               se.device_id
        FROM sensors se
        LEFT JOIN devices d ON se.device_id = d.device_id
        ORDER BY se.sensor_id ASC;
        """
    )
    device_options = {
        row["device_code"]: row["device_id"]
        for _, row in df_devices.iterrows()
    } if not df_devices.empty else {}

    st.caption(
        "可直接在下方表格編輯「nickname」等欄位，方便日後對照識別（例如中文說明、位置等）。"
        "「state_dictionary」是選填的狀態字典（JSON 格式，例如 {\"1\": \"待機\", \"2\": \"運轉\"}），"
        "設定後查詢 sensor_readings_translated 這個 view 就會自動把數字翻譯成文字。"
    )
    st.caption(
        "🆕 「OPC UA 取樣頻率(ms)」留空代表沿用 Server 層級預設頻率；"
        "「上傳條件」決定 sensor_readings 統一週期性寫入時，這個感測器要不要真的被寫入，四選一："
        "always=不判斷、每輪都寫｜on_change=數值有變化就寫｜"
        "threshold_percent=變化百分比達到「上傳門檻」才寫（門檻填百分比數字，例如 1 代表 1%，"
        "以上次實際寫入的值為基準）｜threshold_absolute=變化絕對值達到「上傳門檻」才寫。"
        "全部感測器預設為 threshold_percent、門檻 1%（與上次寫入值相比變化不到 1% 就不寫入）。"
        "調整後最慢在下一個統一寫入週期（.env 的 SENSOR_READING_FLUSH_INTERVAL）內生效，不需要重啟服務。"
    )
    st.caption(
        "🆕 「OPC UA Deadband」是另一層、更早的過濾：伺服器端就決定要不要把這筆變化透過網路送過來，"
        "跟上面的「上傳條件」（決定收到之後要不要寫進資料庫）是不同階段。三選一："
        "none=不設定、伺服器只要有變化就送｜percent=變化百分比達到「Deadband 門檻」才送"
        "（⚠️ 依節點是否有設定 EURange 而定，不確定設備有沒有配置時建議用 absolute）｜"
        "absolute=變化絕對值達到「Deadband 門檻」才送。適合網路連線不穩、想直接減少流量的場域。"
    )
    if not df_sensors_full.empty:
        df_sensors_full["state_dictionary"] = df_sensors_full["state_dictionary"].apply(
            lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v
        )

        edited_sensors_df = st.data_editor(
            df_sensors_full[[
                "sensor_id", "device_code", "sensor_code", "nickname", "sensor_type",
                "unit", "min_threshold", "max_threshold", "state_dictionary",
                "opcua_sampling_interval_ms", "opcua_deadband_type", "opcua_deadband_value",
                "upload_condition", "upload_threshold",
            ]],
            num_rows="fixed",
            key="sensors_editor",
            disabled=["sensor_id", "device_code", "sensor_code"],
            width="stretch",
            column_config={
                "min_threshold": st.column_config.NumberColumn(
                    "警報下限", help="低於此值產生 L 警報；清空 = 不設下限",
                ),
                "max_threshold": st.column_config.NumberColumn(
                    "警報上限", help="高於此值產生 H 警報；清空 = 不設上限（累計型計數器不要設）",
                ),
                "state_dictionary": st.column_config.TextColumn(
                    "state_dictionary（選填，JSON 格式）",
                    help='例如：{"1": "待機", "2": "運轉"}',
                ),
                "opcua_sampling_interval_ms": st.column_config.NumberColumn(
                    "OPC UA 取樣頻率(ms)",
                    help="留空 = 沿用 Server 層級預設訂閱頻率。相同頻率的點位會被歸進同一組訂閱。"
                    "⚠️ 有些設備的 OPC UA Server 內部有自己固定的更新周期，會忽略/覆寫這個請求值"
                    "（連線 log 出現 'RevisedPublishingInterval' 就是這種情況），此時這個設定對該設備無效。",
                    min_value=50,
                    step=50,
                ),
                "opcua_deadband_type": st.column_config.SelectboxColumn(
                    "OPC UA Deadband (伺服器端過濾)",
                    options=["none", "percent", "absolute"],
                    help="none=不設定｜percent=變化百分比達門檻才送（需節點有 EURange，不確定就用 absolute）｜"
                    "absolute=變化絕對值達門檻才送。這是在伺服器端就過濾，直接減少網路流量。",
                ),
                "opcua_deadband_value": st.column_config.NumberColumn(
                    "Deadband 門檻",
                    help="opcua_deadband_type=percent 時填百分比數字；=absolute 時填絕對值；=none 時不生效。",
                ),
                "upload_condition": st.column_config.SelectboxColumn(
                    "上傳條件 (upload_condition)",
                    options=["always", "on_change", "threshold_percent", "threshold_absolute"],
                    help="always=不判斷每輪都寫｜on_change=數值變化就寫｜"
                    "threshold_percent=變化百分比達門檻才寫｜threshold_absolute=變化絕對值達門檻才寫",
                ),
                "upload_threshold": st.column_config.NumberColumn(
                    "上傳門檻",
                    help="threshold_percent 時填百分比數字（例如 1 = 1%）；"
                    "threshold_absolute 時填絕對值；always / on_change 時不生效。",
                ),
            },
        )

        if st.button("💾 儲存感測器修改", key="save_sensors"):
            try:
                with DatabaseConnector.get_connection() as conn:
                    with conn.cursor() as cur:
                        for _, row in edited_sensors_df.iterrows():
                            state_dict_val, state_dict_err = _normalize_state_dict(
                                row["state_dictionary"]
                            )
                            if state_dict_err:
                                st.error(
                                    f"❌ sensor_id={int(row['sensor_id'])} 的 "
                                    f"state_dictionary 格式錯誤，該筆未儲存：{state_dict_err}"
                                )
                                continue

                            upload_condition = row.get("upload_condition") or "threshold_percent"
                            if upload_condition not in (
                                "always", "on_change", "threshold_percent", "threshold_absolute"
                            ):
                                upload_condition = "threshold_percent"

                            deadband_type = row.get("opcua_deadband_type") or "none"
                            if deadband_type not in ("none", "percent", "absolute"):
                                deadband_type = "none"

                            cur.execute(
                                """
                                UPDATE sensors SET
                                    nickname=%s, sensor_type=%s, unit=%s,
                                    min_threshold=%s, max_threshold=%s, state_dictionary=%s,
                                    opcua_sampling_interval_ms=%s,
                                    opcua_deadband_type=%s, opcua_deadband_value=%s,
                                    upload_condition=%s, upload_threshold=%s
                                WHERE sensor_id=%s;
                                """,
                                (
                                    row["nickname"].strip()
                                    if pd.notnull(row["nickname"]) and str(row["nickname"]).strip()
                                    else None,
                                    row["sensor_type"],
                                    row["unit"],
                                    float(row["min_threshold"]) if pd.notnull(row["min_threshold"]) else None,
                                    float(row["max_threshold"]) if pd.notnull(row["max_threshold"]) else None,
                                    state_dict_val,
                                    int(row["opcua_sampling_interval_ms"])
                                    if pd.notnull(row["opcua_sampling_interval_ms"])
                                    else None,
                                    deadband_type,
                                    float(row["opcua_deadband_value"])
                                    if pd.notnull(row["opcua_deadband_value"])
                                    else None,
                                    upload_condition,
                                    float(row["upload_threshold"])
                                    if pd.notnull(row["upload_threshold"])
                                    else None,
                                    int(row["sensor_id"]),
                                ),
                            )
                        conn.commit()
                audit_ui("sensor.update", "sensors", frame_changes(df_sensors_full, edited_sensors_df, "sensor_id"))
                st.success("✅ 感測器資料更新成功！")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 儲存失敗: {e}")
    else:
        st.info("目前尚無任何感測器，請在下方新增。新增後即可回到點位設定分頁進行綁定。")

    # 常見感測器類型（可選「其他（自訂）」自行輸入）
    SENSOR_TYPE_OPTIONS = [
        "temperature", "pressure", "vibration", "current", "voltage",
        "power", "energy", "flow", "level", "humidity", "speed",
        "torque", "position", "ph", "conductivity", "weight", "count", "status",
        "gas", "liquid", "solid",
        "其他（自訂）",
    ]

    # 常用工程單位（依 UNECE Recommendation 20 / OPC UA Part 8 Engineering Units 慣例整理）
    OPCUA_UNIT_OPTIONS = [
        "°C", "°F", "K",
        "Pa", "kPa", "bar", "mbar", "psi",
        "m/s", "mm/s", "m/s²", "rpm", "Hz",
        "V", "mV", "A", "mA",
        "W", "kW", "Wh", "kWh", "VA", "var",
        "%", "%RH",
        "L", "L/min", "m³", "m³/h",
        "mm", "cm", "m",
        "g", "kg", "t",
        "N", "Nm",
        "pH", "μS/cm",
        "count",
        "其他（自訂）",
    ]

    flash = st.session_state.pop("hier_sensor_flash", None)
    if flash:
        st.success(flash)
    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                next_code = peek_next_sensor_code(cur)
    except Exception:
        next_code = "?"

    with st.form("add_sensor_form", clear_on_submit=True):
        col1, col2, col3 = st.columns(3)
        with col1:
            if device_options:
                new_sensor_device = st.selectbox("所屬設備 (device_code)", list(device_options.keys()))
            else:
                new_sensor_device = None
                st.warning("⚠️ 請先新增設備")
            st.text_input(
                "感測器編號 (sensor_code)", value=f"自動編號（下一個是 {next_code}）", disabled=True,
                help="sensor_code 由系統依流水號自動產生，不需要手動輸入。"
                "要辨識用途請填下方的「暱稱」；需要指定編號的大量建立請用「批次匯入匯出」。",
            )
            new_sensor_nickname = st.text_input(
                "暱稱 (nickname，選填)", "", placeholder="例如：B03 蒸氣流量計"
            )
        with col2:
            new_sensor_type_selected = st.selectbox(
                "感測器類型 (sensor_type)", SENSOR_TYPE_OPTIONS, index=0
            )
            new_sensor_type_custom = st.text_input(
                "↳ 選「其他（自訂）」時請在此輸入",
                "",
                placeholder="例如：oil_pressure",
                key="new_sensor_type_custom",
            )
            new_sensor_unit_selected = st.selectbox(
                "單位 (unit，依 OPC UA 工程單位慣例)", OPCUA_UNIT_OPTIONS, index=0
            )
            new_sensor_unit_custom = st.text_input(
                "↳ 選「其他（自訂）」時請在此輸入",
                "",
                placeholder="例如：mmHg",
                key="new_sensor_unit_custom",
            )
        with col3:
            # 預設空白：上下限會自動變成 L / H 警報，填了預設值（舊版 0 / 100）等於替每個感測器都開了警報
            new_sensor_min = st.number_input(
                "警報下限 (min_threshold，選填)", value=None, placeholder="留空 = 不設下限",
                help="低於此值會產生 L 警報（等級「中」），趨勢圖與總覽也會標示。"
                "需要延遲、遲滯、其他等級時，改到「警報規則」建立正式規則。",
            )
            new_sensor_max = st.number_input(
                "警報上限 (max_threshold，選填)", value=None, placeholder="留空 = 不設上限",
                help="高於此值會產生 H 警報（等級「中」）。累計型計數器（kWh、m³ 等）不要設上限。",
            )
            new_sensor_state_dict = st.text_input(
                "狀態字典 JSON (state_dictionary，選填)",
                value="",
                placeholder='{"1": "待機", "2": "運轉"}',
            )

        st.markdown("**🆕 OPC UA 訂閱頻率與統一寫入條件**")
        col4, col5, col6 = st.columns(3)
        with col4:
            new_sensor_use_custom_interval = st.checkbox(
                "使用自訂 OPC UA 取樣頻率", value=False
            )
            new_sensor_sampling_interval = st.number_input(
                "取樣頻率 (ms)", value=1000, min_value=50, step=50,
                help="留空（不勾選左方選項）代表沿用 Server 層級預設頻率。",
            )
        with col5:
            new_sensor_upload_condition = st.selectbox(
                "上傳條件 (upload_condition)",
                ["threshold_percent", "threshold_absolute", "on_change", "always"],
                help="threshold_percent=變化百分比達門檻才寫（預設，1% 起跳）｜"
                "threshold_absolute=變化絕對值達門檻才寫｜on_change=數值變化就寫｜"
                "always=不判斷，每輪都寫",
            )
        with col6:
            new_sensor_upload_threshold = st.number_input(
                "上傳門檻",
                value=1.0,
                help="threshold_percent 時填百分比數字（例如 1 = 1%）；"
                "threshold_absolute 時填絕對值；其餘條件下不生效。",
            )

        st.markdown("**🆕 OPC UA Deadband（伺服器端過濾，減少網路流量）**")
        col7, col8 = st.columns(2)
        with col7:
            new_sensor_deadband_type = st.selectbox(
                "Deadband 類型 (opcua_deadband_type)",
                ["none", "percent", "absolute"],
                help="none=不設定（預設，伺服器只要有變化就送）｜"
                "percent=變化百分比達門檻才送（需節點有 EURange，不確定就用 absolute）｜"
                "absolute=變化絕對值達門檻才送。",
            )
        with col8:
            new_sensor_deadband_value = st.number_input(
                "Deadband 門檻",
                value=0.0,
                help="opcua_deadband_type=percent 時填百分比數字；=absolute 時填絕對值；=none 時不生效。",
            )

        if st.form_submit_button("➕ 新增感測器", type="primary"):
            final_sensor_type = (
                new_sensor_type_custom.strip()
                if new_sensor_type_selected == "其他（自訂）"
                else new_sensor_type_selected
            )
            final_sensor_unit = (
                new_sensor_unit_custom.strip()
                if new_sensor_unit_selected == "其他（自訂）"
                else new_sensor_unit_selected
            )
            formatted_state_dict = None
            state_dict_error = False
            if new_sensor_state_dict.strip():
                try:
                    formatted_state_dict = json.dumps(
                        json.loads(new_sensor_state_dict), ensure_ascii=False
                    )
                except json.JSONDecodeError:
                    state_dict_error = True
                    st.error("❌ 狀態字典格式錯誤！請填寫合法的 JSON 格式")

            if state_dict_error:
                pass
            elif new_sensor_min is not None and new_sensor_max is not None and new_sensor_min > new_sensor_max:
                st.error("❌ 警報下限大於警報上限")
            elif new_sensor_device and final_sensor_type:
                try:
                    with DatabaseConnector.get_connection() as conn:
                        with conn.cursor() as cur:
                            new_sensor_code = allocate_sensor_codes(cur)[0]
                            cur.execute(
                                """
                                INSERT INTO sensors
                                    (device_id, sensor_code, nickname, sensor_type, unit,
                                     min_threshold, max_threshold, state_dictionary,
                                     opcua_sampling_interval_ms,
                                     opcua_deadband_type, opcua_deadband_value,
                                     upload_condition, upload_threshold)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                                """,
                                (
                                    device_options[new_sensor_device],
                                    new_sensor_code.strip(),
                                    new_sensor_nickname.strip() or None,
                                    final_sensor_type,
                                    final_sensor_unit or None,
                                    new_sensor_min,
                                    new_sensor_max,
                                    formatted_state_dict,
                                    int(new_sensor_sampling_interval)
                                    if new_sensor_use_custom_interval
                                    else None,
                                    new_sensor_deadband_type,
                                    new_sensor_deadband_value
                                    if new_sensor_deadband_type in ("percent", "absolute")
                                    else None,
                                    new_sensor_upload_condition,
                                    new_sensor_upload_threshold
                                    if new_sensor_upload_condition
                                    in ("threshold_percent", "threshold_absolute")
                                    else None,
                                ),
                            )
                            conn.commit()
                    audit_ui("sensor.create", f"sensor:{new_sensor_code}")
                    nick = new_sensor_nickname.strip()
                    st.session_state["hier_sensor_flash"] = (
                        f"🎉 成功新增感測器：編號 {new_sensor_code}" + (f"（{nick}）" if nick else "")
                    )
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 新增失敗: {e}")
            else:
                st.warning("⚠️ 請選擇所屬設備與感測器類型")
