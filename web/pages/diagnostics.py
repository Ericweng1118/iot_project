"""
web/pages/diagnostics.py
========================
診斷檢查（v2 admin_app.py 的「🚨 異常監控」分頁）：即時查詢連線異常、數值超出範圍、
資料斷更。v3 起正式的警報改由警報引擎產生（見「警報中心」），這一頁保留作為
不依賴警報引擎的獨立檢查工具。
"""

import streamlit as st

from web.common import MODBUS_ENABLED, TIA_ENABLED, _fetch_df


def render():
    st.subheader("🩺 診斷檢查")
    st.caption(
        "即時查詢三種常見的「點位異常」，不用自己下 SQL 一個一個查。"
        "這裡不依賴警報引擎，main.py 沒在跑時也能用來排查。"
    )

    # ------------------------------------------------------------
    # 1. 連線異常：即時層三張表裡 plc_state 不是 ONLINE 的點位
    # ------------------------------------------------------------
    st.subheader("🔌 連線異常")

    # --- 1-1. Server 層級：OPC UA Server 本身連不上 ---
    # 這一段是必要的：opcua_tags.plc_state 只反映「點位」層級，一台 Server 如果
    # 底下還沒有任何已綁定的點位，斷線時不會有任何點位變成 OFFLINE，只有
    # opcua_servers.conn_state 會記錄到。少了這一段就會整台 Server 斷線卻無人知曉。
    st.markdown("**① OPC UA Server 連線狀態**")
    df_server_offline = _fetch_df(
        """
        SELECT server_name AS Server名稱, ip, port, conn_state AS 連線狀態,
               last_scan::text AS 最後連線時間, last_error AS 最後錯誤訊息
        FROM opcua_servers
        WHERE enabled = TRUE AND conn_state IS DISTINCT FROM 'ONLINE'
        ORDER BY server_name;
        """
    )
    if not df_server_offline.empty:
        st.error(f"⚠️ 有 {len(df_server_offline)} 台已啟用的 OPC UA Server 目前連線異常")
        st.dataframe(df_server_offline, width="stretch")
    else:
        st.success("✅ 所有已啟用的 OPC UA Server 連線正常")

    # --- 1-2. 點位層級 ---
    st.markdown("**② 點位連線狀態（plc_state ≠ ONLINE）**")
    st.caption(
        "代表這個點位上一輪採集時 PLC/Server 連不上，或讀取/解析失敗。"
        "OPC UA 只列出**已綁定感測器**的點位 —— v2 起未綁定的點位不會被訂閱、"
        "數值停留在上次瀏覽的快照，它們的連線狀態沒有參考意義。"
    )

    _offline_queries = []
    if MODBUS_ENABLED:
        _offline_queries.append(
            "SELECT 'Modbus' AS 來源, id, name AS 點位名稱, plc_ip AS 位址, plc_state AS 狀態, "
            "last_update::text AS 最後更新 "
            "FROM modbus_scada WHERE plc_state IS DISTINCT FROM 'ONLINE'"
        )
    if TIA_ENABLED:
        _offline_queries.append(
            "SELECT 'TIA/S7' AS 來源, id, name AS 點位名稱, plc_ip AS 位址, plc_state AS 狀態, last_update::text AS 最後更新 "
            "FROM tia_scada WHERE plc_state IS DISTINCT FROM 'ONLINE'"
        )
    _offline_queries.append(
        "SELECT 'OPC UA' AS 來源, id, node_id AS 點位名稱, server_name AS 位址, plc_state AS 狀態, last_update::text AS 最後更新 "
        "FROM opcua_tags "
        "WHERE plc_state IS DISTINCT FROM 'ONLINE' AND sensor_id IS NOT NULL"
    )

    df_offline = _fetch_df(" UNION ALL ".join(_offline_queries) + " ORDER BY 1, 2;")
    if not df_offline.empty:
        st.error(f"⚠️ 目前有 {len(df_offline)} 個點位連線異常")
        st.dataframe(df_offline, width="stretch")
    else:
        st.success("✅ 目前所有點位連線狀態正常")

    st.divider()

    # ------------------------------------------------------------
    # 2. 數值超出正常範圍（依 sensors.min_threshold / max_threshold）
    # ------------------------------------------------------------
    st.subheader("📈 數值超出正常範圍")
    st.caption(
        "依每個感測器在 sensors 表設定的 min_threshold / max_threshold，"
        "比對 sensor_readings 裡最新一筆數值。僅涵蓋已綁定 sensor_id 的點位。"
    )

    df_out_of_range = _fetch_df(
        """
        SELECT
            s.sensor_code,
            s.nickname,
            s.sensor_type,
            r.value AS 目前數值,
            s.min_threshold AS 下限,
            s.max_threshold AS 上限,
            s.unit,
            r.reading_time AS 讀取時間
        FROM sensors s
        JOIN LATERAL (
            SELECT value, reading_time
            FROM sensor_readings
            WHERE sensor_id = s.sensor_id
            ORDER BY reading_time DESC
            LIMIT 1
        ) r ON true
        WHERE (s.min_threshold IS NOT NULL AND r.value < s.min_threshold)
           OR (s.max_threshold IS NOT NULL AND r.value > s.max_threshold)
        ORDER BY r.reading_time DESC;
        """
    )
    if not df_out_of_range.empty:
        st.error(f"⚠️ 目前有 {len(df_out_of_range)} 個感測器數值超出正常範圍")
        st.dataframe(df_out_of_range, width="stretch")
    else:
        st.success("✅ 目前所有已綁定感測器的數值都在正常範圍內")

    st.divider()

    # ------------------------------------------------------------
    # 3. 資料斷更（超過 N 小時沒有新的 sensor_readings）
    # ------------------------------------------------------------
    st.subheader("⏱️ 資料斷更（疑似離線）")
    stale_hours = st.number_input(
        "判定為「太久沒更新」的時數門檻（小時）", min_value=1, value=2, step=1
    )
    st.caption(
        "正常情況下，就算數值沒變化，系統也會依統一寫入排程的 upload_condition 規則定期補寫一筆進 "
        "sensor_readings。如果一個已綁定的感測器超過這個時數都沒有任何新資料，通常代表該點位已經斷線、"
        "或是採集程式沒有正常執行。"
    )

    df_stale = _fetch_df(
        f"""
        SELECT
            s.sensor_code,
            s.nickname,
            s.sensor_type,
            r.reading_time AS 最後讀取時間,
            r.value AS 最後數值,
            now() - r.reading_time AS 已斷更多久
        FROM sensors s
        JOIN LATERAL (
            SELECT value, reading_time
            FROM sensor_readings
            WHERE sensor_id = s.sensor_id
            ORDER BY reading_time DESC
            LIMIT 1
        ) r ON true
        WHERE r.reading_time < now() - INTERVAL '{int(stale_hours)} hours'
        ORDER BY r.reading_time ASC;
        """
    )
    df_never = _fetch_df(
        """
        SELECT s.sensor_code, s.nickname, s.sensor_type
        FROM sensors s
        -- NOT EXISTS 走 (sensor_id, reading_time) 索引，每個感測器只要找到一筆就停；
        -- 原本 LEFT JOIN 整張 hypertable 會隨資料量線性變慢
        WHERE NOT EXISTS (
            SELECT 1 FROM sensor_readings r WHERE r.sensor_id = s.sensor_id
        );
        """
    )

    if not df_stale.empty:
        st.error(f"⚠️ 有 {len(df_stale)} 個感測器超過 {stale_hours} 小時沒有新資料")
        st.dataframe(df_stale, width="stretch")
    else:
        st.success(f"✅ 目前沒有感測器斷更超過 {stale_hours} 小時")

    if not df_never.empty:
        st.warning(
            f"ℹ️ 另外有 {len(df_never)} 個感測器從建立以來「從未」寫入過 sensor_readings"
            "（可能是尚未綁定對應點位，或該點位一直讀取失敗）："
        )
        st.dataframe(df_never, width="stretch")
