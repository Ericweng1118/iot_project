"""
admin_app.py
============
IIoT SCADA 網頁後台入口（Streamlit）。

v3 起這支檔案只負責：頁面設定 → 登入 → 依角色組出導覽選單 → 側邊欄狀態。
各頁面的實作在 web/pages/ 底下，一頁一個模組（見 README「網頁後台」一節）。

啟動：
    python -m streamlit run admin_app.py --server.port 8501
    或 python run_all.py（同時啟動採集主程式）
"""

import streamlit as st

st.set_page_config(
    page_title="IIoT SCADA",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="expanded",
)

from core.auth import has_role  # noqa: E402
from core.config import APP_VERSION, MODBUS_ENABLED, TIA_ENABLED  # noqa: E402
from data_layer.db_connector import DatabaseConnector  # noqa: E402
from web import auth_ui, nav  # noqa: E402
from web.common import fetch_df, fmt_age, get_service_status, table_exists  # noqa: E402
from web.pages import (  # noqa: E402
    alarm_rules,
    alarms,
    audit_log,
    calculated_points,
    config_hierarchy,
    config_opcua,
    device_templates,
    overview,
    reports,
    system,
    trends,
    users,
)

DatabaseConnector.initialize_pool()

# 未登入時先用隱藏選單登記所有頁面路徑，再顯示登入畫面。
# 不這樣做的話，從書籤 / 通知連結打開 /trends?sensors=1 這類深層連結，登入畫面那一輪
# Streamlit 找不到這個頁面，會把網址改回首頁，登入後就不在原本要看的頁面了。
ALL_URL_PATHS = ("overview", "trends", "alarms", "reports", "config-opcua", "config-modbus",
                 "config-tia", "config-hierarchy", "alarm-rules", "modbus-debug", "import-export", "device-templates", "calculated-points", "s7-debug",
                 "system", "users", "audit")
if not st.session_state.get("user"):
    st.navigation(
        [st.Page(auth_ui.login_placeholder, title=path, url_path=path, default=(path == "overview"))
         for path in ALL_URL_PATHS],
        position="hidden",
    )

user = auth_ui.require_login()
auth_ui.enforce_idle_timeout(user)
role = user["role"]

# ------------------------------------------------------------------
# 導覽選單：只顯示目前角色有權限的頁面（頁面內部也會再檢查一次）
# ------------------------------------------------------------------
pages = {
    "監控": [
        st.Page(overview.render, title="即時總覽", icon="🏭", url_path="overview", default=True),
        st.Page(trends.render, title="歷史趨勢", icon="📈", url_path="trends"),
        st.Page(alarms.render, title="警報中心", icon="🚨", url_path="alarms"),
        st.Page(reports.render, title="報表匯出", icon="📑", url_path="reports"),
    ],
}
alarm_page = pages["監控"][2]

if has_role(role, "engineer"):
    config_pages = [st.Page(config_opcua.render, title="OPC UA 點位", icon="📡", url_path="config-opcua")]
    if MODBUS_ENABLED:
        from web.pages import config_modbus
        config_pages.append(st.Page(config_modbus.render, title="Modbus 點位", icon="📡", url_path="config-modbus"))
    if TIA_ENABLED:
        from web.pages import config_tia
        config_pages.append(st.Page(config_tia.render, title="TIA (S7) 點位", icon="📡", url_path="config-tia"))
    config_pages += [
        st.Page(config_hierarchy.render, title="感測器階層", icon="🧬", url_path="config-hierarchy"),
        st.Page(device_templates.render, title="設備範本", icon="🧩", url_path="device-templates"),
        st.Page(calculated_points.render, title="計算點", icon="🧮", url_path="calculated-points"),
        st.Page(alarm_rules.render, title="警報規則", icon="🔔", url_path="alarm-rules"),
    ]
    pages["設定"] = config_pages
    # 工具：Modbus 線上調適不受 MODBUS_ENABLED 限制（新設備接線、評估要不要啟用 Modbus 時就用得到）
    from web.pages import import_export, modbus_debug, s7_debug
    pages["工具"] = [
        st.Page(import_export.render, title="批次匯入匯出", icon="📥", url_path="import-export"),
        st.Page(modbus_debug.render, title="Modbus 線上調適", icon="🔧", url_path="modbus-debug"),
        st.Page(s7_debug.render, title="S7 線上調適", icon="🔧", url_path="s7-debug"),
    ]

system_pages = [st.Page(system.render, title="系統狀態", icon="🩺", url_path="system")]
if has_role(role, "admin"):
    system_pages += [
        st.Page(users.render, title="使用者管理", icon="👥", url_path="users"),
        st.Page(audit_log.render, title="稽核紀錄", icon="📜", url_path="audit"),
    ]
pages["系統"] = system_pages

navigation = st.navigation(pages, expanded=True)   # 頁面多了以後預設會摺疊成「View N more」，這裡全部展開
nav.PAGES.clear()
nav.PAGES.update({p.url_path: p for section in pages.values() for p in section})

# ------------------------------------------------------------------
# 側邊欄：登入者、警報徽章、採集服務狀態
# ------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🏭 IIoT SCADA")

    @st.fragment(run_every=15)
    def sidebar_status():
        if auth_ui.idle_expired():
            st.rerun(scope="app")   # 整頁重跑 → enforce_idle_timeout() 執行登出
        if table_exists("alarm_events"):
            df = fetch_df(
                "SELECT count(*) FILTER (WHERE cleared_at IS NULL) AS active, "
                "count(*) FILTER (WHERE acked_at IS NULL) AS unacked "
                "FROM alarm_events WHERE cleared_at IS NULL OR acked_at IS NULL;",
                show_error=False,
            )
            active = int(df.iloc[0]["active"]) if not df.empty else 0
            unacked = int(df.iloc[0]["unacked"]) if not df.empty else 0
            if active or unacked:
                st.page_link(alarm_page, label=f"警報：發生中 {active}・未確認 {unacked}", icon="🔴")
            else:
                st.caption("🟢 目前沒有警報")
        svc = get_service_status()
        if svc and svc.get("exists"):
            if svc["alive"]:
                st.caption(f"🟢 採集服務運作中（{fmt_age(svc['age_seconds'])}）")
            else:
                st.caption(f"🔴 採集服務無回應（最後心跳 {fmt_age(svc['age_seconds'])}）")
            spool_rows = (svc["info"].get("writer") or {}).get("spool_rows") or 0
            if spool_rows:
                st.caption(f"📦 資料庫寫入異常，本機緩存 {spool_rows:,} 筆待補寫")

    sidebar_status()
    st.divider()
    st.caption(f"👤 {user['display_name']}（{auth_ui.role_label(role)}）")
    if auth_ui.idle_caption(user):
        st.caption(auth_ui.idle_caption(user))
    c1, c2 = st.columns(2)
    if c1.button("🔑 密碼", width="stretch"):
        auth_ui.change_password_dialog()
    if c2.button("🚪 登出", width="stretch"):
        auth_ui.logout()
    disabled = [n for n, on in (("Modbus", MODBUS_ENABLED), ("TIA/S7", TIA_ENABLED)) if not on]
    if disabled:
        st.caption("🔕 已由 .env 停用：" + "、".join(disabled))
    st.caption(f"v{APP_VERSION}")

navigation.run()
