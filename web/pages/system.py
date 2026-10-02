"""
web/pages/system.py
===================
🩺 系統狀態：採集服務本身健不健康、資料庫長多大、migration 跑齊了沒。

資料來源：
    service_status   main.py 每 10 秒回報的心跳與統計（sql/012，services/status_reporter.py）
    opcua_servers    各 Server 的連線狀態
    TimescaleDB      hypertable 大小、chunk 數、壓縮 / 保留政策
    information_schema  各 migration 建立的表 / 欄位是否存在
"""

import pandas as pd
import streamlit as st

from core.config import APP_VERSION, env_str
from web.common import (
    LOCAL_TZ,
    MODBUS_ENABLED,
    OPCUA_ENABLED,
    TIA_ENABLED,
    fetch_df,
    fmt_age,
    get_service_status,
)

# (migration, 說明, 檢查用的 SQL：回傳 true 表示已套用)
MIGRATION_CHECKS = [
    ("000", "即時層四張表", "SELECT to_regclass('public.opcua_tags') IS NOT NULL"),
    ("001", "階層表 + sensor_readings", "SELECT to_regclass('public.sensor_readings') IS NOT NULL"),
    ("006", "上傳條件 / 取樣頻率", "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='sensors' AND column_name='upload_condition')"),
    ("007", "OPC UA deadband", "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='sensors' AND column_name='opcua_deadband_type')"),
    ("008", "補齊程式使用欄位", "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='sensors' AND column_name='nickname')"),
    ("009", "Server 訂閱頻率", "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='opcua_servers' AND column_name='publish_interval_ms')"),
    ("011", "警報管理", "SELECT to_regclass('public.alarm_events') IS NOT NULL"),
    ("012", "使用者 / 稽核 / 服務狀態", "SELECT to_regclass('public.service_status') IS NOT NULL"),
    ("014", "歷史資料品質欄位", "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='sensor_readings' AND column_name='quality')"),
    ("015", "Modbus 傳輸方式 / 點位啟用", "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='modbus_scada' AND column_name='transport')"),
    ("016", "設備範本", "SELECT to_regclass('public.device_templates') IS NOT NULL"),
    ("017", "計算點", "SELECT to_regclass('public.calculated_points') IS NOT NULL"),
    ("018", "排程報表", "SELECT to_regclass('public.report_schedules') IS NOT NULL"),
    ("019", "S7 區域 / 位元 / Rack-Slot", "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='tia_scada' AND column_name='area')"),
    ("020", "計算點狀態保存", "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='calculated_points' AND column_name='calc_state')"),
    ("021", "Python 腳本計算點", "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='calculated_points' AND column_name='kind')"),
    ("013", "時序資料壓縮（選用）", "SELECT EXISTS (SELECT 1 FROM timescaledb_information.jobs "
            "WHERE hypertable_name='sensor_readings' AND proc_name='policy_compression')"),
]


def _scalar(sql: str):
    df = fetch_df(sql, show_error=False)
    return None if df.empty else df.iloc[0, 0]


def _render_service():
    st.subheader("⚙️ 採集服務（main.py）")
    svc = get_service_status()
    if svc is None:
        st.warning("尚未執行 `sql/012_users_audit_status.sql`，無法得知採集服務狀態。")
        return None
    if not svc.get("exists"):
        st.warning("採集服務還沒有回報過心跳：main.py 尚未以 v3 版本啟動，或無法連線資料庫。")
        return None

    if svc["alive"]:
        st.success(f"🟢 運作中｜最後心跳 {fmt_age(svc['age_seconds'])}")
    elif svc["stopped_normally"]:
        st.info(f"⚪ 已正常停止｜最後心跳 {fmt_age(svc['age_seconds'])}")
    else:
        st.error(
            f"🔴 無回應｜最後心跳 {fmt_age(svc['age_seconds'])}。main.py 可能已當機或與資料庫斷線，"
            "即時值與警報都不會再更新。請檢查容器 / 程序狀態與 log。"
        )

    info = svc["info"]
    proto = info.get("protocols", {})
    started = pd.Timestamp(svc["started_at"]).tz_convert(LOCAL_TZ) if svc["started_at"] is not None else None
    uptime = (pd.Timestamp.now(tz=LOCAL_TZ) - started) if started is not None else None
    enabled = [n for n, k in (("OPC UA", "opcua"), ("Modbus", "modbus"), ("TIA/S7", "tia"),
                              ("MQTT", "mqtt"), ("警報引擎", "alarm")) if proto.get(k)]
    st.markdown(
        f"主機 **{svc['host']}**｜PID **{svc['pid']}**｜版本 **{svc['version'] or '—'}**｜"
        f"啟動於 **{started.strftime('%Y-%m-%d %H:%M') if started is not None else '—'}**"
        + (f"（已運作 {uptime.days} 天 {uptime.seconds // 3600} 小時）" if uptime is not None else "")
    )
    st.markdown("啟用模組：" + " ".join(f":blue-badge[{n}]" for n in enabled) if enabled else "啟用模組：—")
    return info


def _render_writer(info: dict):
    w = info.get("writer") or {}
    if not w:
        return
    st.subheader("💾 時序資料寫入排程")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("最新值快取", f"{w.get('latest_count', 0)} 個感測器", border=True)
    c2.metric("上一輪寫入", f"{w.get('last_flush_rows', 0)} 筆", border=True,
              help=f"寫入週期 {w.get('flush_interval')} 秒，心跳補寫 {w.get('heartbeat_interval')} 秒")
    c3.metric("本次啟動累計", f"{w.get('total_rows', 0):,} 筆", border=True)
    c4.metric("最後寫入", str(w.get("last_flush_at") or "—")[11:19], border=True)
    spool_rows = w.get("spool_rows") or 0
    if spool_rows:
        st.warning(
            f"📦 本機緩存中有 **{spool_rows:,}** 筆待補寫的資料（最早 {str(w.get('spool_oldest'))[:19]}）。"
            "資料庫無法寫入期間的資料都暫存在這裡，恢復後會自動依序補寫，不需要處理；"
            "若數量持續增加，代表資料庫一直寫不進去。"
        )
    else:
        st.caption(
            f"📦 本機緩存：空（本次啟動累計緩存 {w.get('spooled_total', 0):,} 筆、"
            f"已補寫 {w.get('drained_total', 0):,} 筆）"
        )
    if w.get("spool_dropped"):
        st.error(f"本機緩存曾因超過上限丟棄 {w['spool_dropped']:,} 筆最舊的資料（SPOOL_MAX_ROWS）。")
    if w.get("quality_column") is False:
        st.info("sensor_readings 尚無 quality 欄位，歷史資料不會記錄品質。請執行 `sql/014_reading_quality.sql`。")
    if w.get("last_error"):
        err_at, ok_at = str(w.get("last_error_at") or ""), str(w.get("last_success_at") or "")
        text = f"最近一次寫入錯誤（{err_at[:19]}）：{w['last_error']}"
        if ok_at and ok_at > err_at:
            st.caption(f"✅ 已恢復（最後成功寫入 {ok_at[11:19]}）。{text}")
        else:
            st.error(text)


def _render_opcua(info: dict):
    if not OPCUA_ENABLED:
        return
    st.subheader("📡 OPC UA Server")
    servers = fetch_df(
        "SELECT id, server_name, ip, port, enabled, conn_state, last_scan, last_error, "
        "(SELECT count(*) FROM opcua_tags t WHERE t.server_id = s.id) AS tags, "
        "(SELECT count(*) FROM opcua_tags t WHERE t.server_id = s.id AND t.sensor_id IS NOT NULL) AS bound "
        "FROM opcua_servers s ORDER BY server_name;"
    )
    if servers.empty:
        st.info("尚未設定任何 OPC UA Server。")
        return
    stats = {int(k): v for k, v in (info.get("opcua") or {}).items()}
    rows = []
    for _, s in servers.iterrows():
        st_ = stats.get(int(s["id"]), {})
        state = s["conn_state"] if s["enabled"] else "停用"
        icon = {"ONLINE": "🟢", "OFFLINE": "🔴", "ERROR": "🔴"}.get(state, "⚪")
        rows.append({
            "Server": s["server_name"],
            "位址": f"{s['ip']}:{s['port']}",
            "狀態": f"{icon} {state}",
            "點位 / 已綁定": f"{s['tags']} / {s['bound']}",
            "監控中": st_.get("monitored_items", "—"),
            "訂閱組數": st_.get("subscriptions", "—"),
            "品質不良": st_.get("bad_quality_items", "—"),
            "最後收到資料": str(st_.get("last_data_at") or "—")[:19].replace("T", " "),
            "最後心跳成功": str(st_.get("last_heartbeat_ok") or "—")[:19].replace("T", " "),
            "重連次數": st_.get("reconnect_count", "—"),
            "最後錯誤": s["last_error"] or st_.get("last_error") or "",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def _render_alarm(info: dict):
    a = info.get("alarm")
    if not a:
        return
    st.subheader("🚨 警報引擎")
    n = a.get("notifier") or {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("規則數", a.get("rules_count", 0), border=True, help="含感測器上下限產生的隱含規則")
    c2.metric("發生中", a.get("active_count", 0), border=True)
    c3.metric("本次啟動 發生 / 恢復", f"{a.get('raised_total', 0)} / {a.get('cleared_total', 0)}", border=True)
    c4.metric("通知 成功 / 失敗", f"{n.get('sent', 0)} / {n.get('failed', 0)}", border=True,
              help="頻道：" + ("、".join(a.get("notify_channels") or []) or "未設定"))
    if a.get("last_error"):
        st.warning(f"警報引擎最近錯誤：{a['last_error']}")
    if n.get("last_error"):
        st.warning(f"通知最近錯誤：{n['last_error']}")


def _render_calc_reports(info: dict):
    calc, rep = info.get("calc"), info.get("reports")
    if not calc and not rep:
        return
    st.subheader("🧮 計算點 / 📅 排程報表")
    c1, c2, c3, c4 = st.columns(4)
    if calc:
        c1.metric("計算點", calc.get("points", 0), border=True)
        c2.metric("正常 / 斷線 / 錯誤", f"{calc.get('ok', 0)} / {calc.get('offline', 0)} / {calc.get('error', 0)}",
                  border=True, help="斷線 = 輸入感測器無資料或斷線；錯誤 = 運算式錯誤（除以零、循環引用…）")
    if rep:
        c3.metric("排程報表", rep.get("schedules", 0), border=True)
        c4.metric("本次啟動 寄出 / 失敗", f"{rep.get('sent_total', 0)} / {rep.get('failed_total', 0)}", border=True)


def _render_database():
    st.subheader("🗄️ 資料庫")
    size = _scalar("SELECT pg_size_pretty(hypertable_size('sensor_readings'))")
    rows = _scalar("SELECT approximate_row_count('sensor_readings')")
    chunks = _scalar("SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_name = 'sensor_readings'")
    oldest = _scalar("SELECT min(range_start) FROM timescaledb_information.chunks WHERE hypertable_name = 'sensor_readings'")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("sensor_readings 大小", size or "—", border=True)
    c2.metric("約略筆數", f"{int(rows):,}" if rows is not None else "—", border=True)
    c3.metric("Chunk 數", chunks if chunks is not None else "—", border=True)
    c4.metric("最早資料", str(oldest)[:10] if oldest is not None else "—", border=True)

    jobs = fetch_df(
        "SELECT proc_name, schedule_interval::text, config::text FROM timescaledb_information.jobs "
        "WHERE hypertable_name = 'sensor_readings';",
        show_error=False,
    )
    has_compress = not jobs.empty and (jobs["proc_name"] == "policy_compression").any()
    has_retention = not jobs.empty and (jobs["proc_name"] == "policy_retention").any()
    if not has_compress:
        st.warning(
            "⚠️ sensor_readings 尚未啟用壓縮政策，磁碟用量會持續成長。"
            "可參考 `sql/013_timeseries_policy.sql`（先在測試環境驗證）。"
        )
    if not has_retention:
        st.caption("ℹ️ 未設定資料保留政策（歷史資料永久保存）。是否要自動刪除舊資料屬於營運決策，請見 todo.md。")
    if not jobs.empty:
        st.dataframe(jobs.rename(columns={"proc_name": "政策", "schedule_interval": "執行週期", "config": "設定"}),
                     hide_index=True, width="stretch")


def _render_migrations():
    st.subheader("🧩 資料庫 Migration")
    rows = []
    for mid, desc, sql in MIGRATION_CHECKS:
        ok = _scalar(sql)
        rows.append({"Migration": mid, "內容": desc, "狀態": "✅ 已套用" if ok else "⬜ 未套用"})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def _render_config():
    st.subheader("🔧 目前設定（.env，不含密碼）")
    keys = [
        "POLL_INTERVAL", "SENSOR_READING_FLUSH_INTERVAL", "SENSOR_HEARTBEAT_INTERVAL", "SENSOR_ALIVE_WINDOW",
        "OPCUA_PUBLISH_INTERVAL_MS", "OPCUA_CLIENT_TIMEOUT", "OPCUA_HEARTBEAT_INTERVAL_SEC",
        "OPCUA_HEARTBEAT_MAX_FAILURES", "MQTT_ENABLED", "MQTT_INCLUDE_OPCUA", "ALARM_ENABLED",
        "ALARM_EVAL_INTERVAL", "ALARM_USE_SENSOR_LIMITS", "ALARM_COMM_DELAY_SEC",
        "ALARM_NOTIFY_MIN_PRIORITY", "SCADA_TIMEZONE", "DB_HOST", "DB_NAME",
    ]
    data = [{"項目": k, "值": env_str(k) or "（預設）"} for k in keys]
    data.insert(0, {"項目": "協議", "值": f"OPC UA={OPCUA_ENABLED}｜Modbus={MODBUS_ENABLED}｜TIA={TIA_ENABLED}"})
    data.insert(0, {"項目": "網頁版本", "值": APP_VERSION})
    st.caption("網頁後台讀到的是自己這個程序的 .env；main.py 實際使用的值以容器 / 程序啟動時為準。")
    st.dataframe(pd.DataFrame(data), hide_index=True, width="stretch")


def render():
    st.title("🩺 系統狀態")
    info = _render_service()
    if info:
        _render_writer(info)
        _render_opcua(info)
        _render_alarm(info)
        _render_calc_reports(info)
    elif OPCUA_ENABLED:
        _render_opcua({})
    _render_database()
    _render_migrations()
    with st.expander("設定值"):
        _render_config()
