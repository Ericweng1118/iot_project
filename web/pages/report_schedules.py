"""
web/pages/report_schedules.py
=============================
⏰ 排程報表（嵌在「報表」頁的分頁，需要 sql/018）

每日 / 每週 / 每月在指定時間，自動把上一期的 Excel 報表寄到信箱或送到 Webhook。
檢視所有人都可以；新增 / 修改 / 刪除 / 立即寄送需要 engineer 以上。
寄送由 main.py 的 ReportScheduler 負責（services/report_scheduler.py）。
"""

from datetime import datetime, time as dtime

import pandas as pd
import streamlit as st

from services.alarm.notifier import Notifier
from services.report_scheduler import (
    FREQUENCY_LABELS,
    WEEKDAY_LABELS,
    build_schedule_report,
    describe,
    latest_due,
    next_run,
    period_label,
    period_for,
    run_schedule,
)
from services.reporting import METRIC_LABELS
from web.common import LOCAL_TZ, audit_ui, can, current_user, execute, fetch_df, now_local, sensor_catalog

GRANULARITY_OPTIONS = {"auto": "自動（每日報表用每小時，每週 / 每月用每日）", "1 hour": "每小時",
                       "1 day": "每日", "1 month": "每月"}


def _schedules() -> pd.DataFrame:
    return fetch_df("SELECT * FROM report_schedules ORDER BY name;")


def _form(row: dict | None, catalog: pd.DataFrame):
    key = f"rs_{row['schedule_id'] if row else 'new'}"
    row = row or {}
    c1, c2, c3 = st.columns([2, 1, 1])
    name = c1.text_input("報表名稱", row.get("name", ""), key=f"{key}_name", placeholder="例如：公用設備每日能耗")
    freq = c2.selectbox("頻率", list(FREQUENCY_LABELS), format_func=FREQUENCY_LABELS.get,
                        index=list(FREQUENCY_LABELS).index(row.get("frequency", "daily")), key=f"{key}_freq")
    send_time = c3.time_input("寄送時間", row.get("send_time") or dtime(7, 0), key=f"{key}_time", step=300)
    weekday, day = int(row.get("weekday") or 0), int(row.get("day_of_month") or 1)
    if freq == "weekly":
        weekday = st.selectbox("星期幾寄", range(7), index=weekday, format_func=lambda i: WEEKDAY_LABELS[i],
                               key=f"{key}_wd")
    elif freq == "monthly":
        day = st.number_input("每月幾號寄（1~28）", 1, 28, day, key=f"{key}_dom")

    devices = sorted(catalog["device_code"].dropna().unique().tolist())
    sensors = dict(zip(catalog["sensor_code"], catalog["label"]))
    c4, c5 = st.columns(2)
    device_codes = c4.multiselect("設備（帶出底下全部感測器）", devices,
                                  default=[d for d in (row.get("device_codes") or []) if d in devices], key=f"{key}_dev")
    sensor_codes = c5.multiselect("個別感測器", list(sensors), format_func=sensors.get,
                                  default=[s for s in (row.get("sensor_codes") or []) if s in sensors],
                                  key=f"{key}_sen")
    c6, c7 = st.columns(2)
    metrics = c6.multiselect("統計值（每個一個工作表）", list(METRIC_LABELS), format_func=METRIC_LABELS.get,
                             default=list(row.get("metrics") or ["avg"]), key=f"{key}_met")
    gran = c7.selectbox("粒度", list(GRANULARITY_OPTIONS), format_func=GRANULARITY_OPTIONS.get,
                        index=list(GRANULARITY_OPTIONS).index(row.get("granularity") or "auto"), key=f"{key}_gran")
    recipients = st.text_input("收件人（逗號分隔，空白 = 用 .env 的 ALARM_EMAIL_TO）",
                               ", ".join(row.get("recipients") or []), key=f"{key}_to")
    c8, c9, c10 = st.columns(3)
    send_email = c8.toggle("寄 Email", row.get("send_email", True), key=f"{key}_email")
    send_webhook = c9.toggle("送 Webhook", row.get("send_webhook", False), key=f"{key}_hook",
                             help="送出 JSON 摘要（ALARM_WEBHOOK_URLS），檔案小於 2 MB 時附 base64 的 Excel")
    enabled = c10.toggle("啟用", row.get("enabled", True), key=f"{key}_en")

    values = dict(name=name.strip(), frequency=freq, send_time=send_time, weekday=int(weekday),
                  day_of_month=int(day), granularity=gran, metrics=metrics, device_codes=device_codes,
                  sensor_codes=sensor_codes,
                  recipients=[r.strip() for r in recipients.split(",") if r.strip()],
                  send_email=send_email, send_webhook=send_webhook, enabled=enabled)
    errors = []
    if not values["name"]:
        errors.append("報表名稱必填")
    if not device_codes and not sensor_codes:
        errors.append("至少選一台設備或一個感測器")
    if not metrics:
        errors.append("至少選一個統計值")
    if not send_email and not send_webhook:
        errors.append("至少選一種寄送方式")
    bad = [r for r in values["recipients"] if "@" not in r]
    if bad:
        errors.append(f"收件人格式不正確：{', '.join(bad)}")
    sample = {**values, "created_at": now_local()}
    st.caption(f"📅 {describe(sample)}｜下一次：{next_run(sample, now_local()):%Y-%m-%d %H:%M}，"
               f"內容期間 {period_label(*period_for(sample, next_run(sample, now_local())))}")
    return values, errors


_COLS = ["name", "frequency", "send_time", "weekday", "day_of_month", "granularity", "metrics", "device_codes",
         "sensor_codes", "recipients", "send_email", "send_webhook", "enabled"]


def _save(values, schedule_id=None):
    params = [values[c] for c in _COLS]
    if schedule_id is None:
        execute(f"INSERT INTO report_schedules ({', '.join(_COLS)}, created_by) "
                f"VALUES ({', '.join(['%s'] * len(_COLS))}, %s);", params + [current_user()["username"]])
        audit_ui("report_schedule.create", f"report:{values['name']}", {"frequency": values["frequency"]})
    else:
        sets = ", ".join(f"{c}=%s" for c in _COLS)
        execute(f"UPDATE report_schedules SET {sets}, updated_at=now() WHERE schedule_id=%s;", params + [schedule_id])
        audit_ui("report_schedule.update", f"report:{values['name']}", {"frequency": values["frequency"]})


def render():
    st.caption("每日 / 每週 / 每月自動把上一期的 Excel 報表寄到信箱或 Webhook。"
               "由採集服務（main.py）每分鐘檢查一次；服務停機錯過的排程，恢復後只補寄最近一期。")
    notifier = Notifier()
    if not notifier.smtp_host and not notifier.webhook_urls:
        st.warning("尚未設定寄送方式：請在 .env 設定 SMTP_HOST（Email）或 ALARM_WEBHOOK_URLS（Webhook）。")

    catalog = sensor_catalog()
    df = _schedules()
    now = now_local()
    if df.empty:
        st.info("還沒有任何排程報表。")
    else:
        rows = []
        for _, r in df.iterrows():
            s = r.to_dict()
            nxt = next_run(s, now)
            pending = latest_due(s, s["last_run_at"] if pd.notnull(s["last_run_at"]) else None, s["created_at"], now)
            rows.append({
                "名稱": s["name"], "排程": describe(s), "啟用": "✅" if s["enabled"] else "⏸️",
                "範圍": "、".join(list(s["device_codes"] or []) + list(s["sensor_codes"] or []))[:60],
                "內容": "、".join(METRIC_LABELS.get(m, m) for m in s["metrics"]),
                "收件人": "、".join(s["recipients"] or []) or "（ALARM_EMAIL_TO）",
                "下一次": "待寄送" if pending else f"{nxt:%m-%d %H:%M}",
                "上一期": f"{s['last_period'] or '—'} {({'OK': '✅', 'ERROR': '❌', 'RETRYING': '🔁'}).get(s['last_status'], '')}",
                "錯誤": s["last_error"] or "",
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    if not can("engineer"):
        st.caption("🔒 新增 / 修改排程需要「工程師」以上的權限。")
        return

    if not df.empty:
        labels = {int(r["schedule_id"]): r["name"] for _, r in df.iterrows()}
        sid = st.selectbox("管理排程", list(labels), format_func=labels.get, index=None, key="rs_pick",
                           placeholder="選擇要修改 / 測試 / 刪除的排程…")
        if sid is not None:
            row = df[df["schedule_id"] == sid].iloc[0].to_dict()
            with st.container(border=True):
                values, errors = _form(row, catalog)
                for e in errors:
                    st.error(e)
                b1, b2, b3, b4 = st.columns(4)
                if b1.button("💾 儲存", type="primary", disabled=bool(errors), key=f"rs_save_{sid}"):
                    _save(values, int(sid))
                    st.success("✅ 已儲存")
                    st.rerun()
                if b2.button("📥 下載最近一期", key=f"rs_prev_{sid}", disabled=bool(errors)):
                    try:
                        fname, data, label, *_ = build_schedule_report({**row, **values}, datetime.now(LOCAL_TZ))
                        st.session_state[f"rs_file_{sid}"] = (fname, data)
                    except Exception as e:
                        st.error(f"產生失敗：{e}")
                if f"rs_file_{sid}" in st.session_state:
                    fname, data = st.session_state[f"rs_file_{sid}"]
                    b2.download_button(f"⬇️ {fname}", data, file_name=fname, key=f"rs_dl_{sid}",
                                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                if b3.button("📧 立即寄送測試", key=f"rs_send_{sid}", disabled=bool(errors)):
                    with st.spinner("產生並寄送中…"):
                        try:
                            ok, msg, label = run_schedule({**row, **values}, datetime.now(LOCAL_TZ), notifier)
                        except Exception as e:
                            ok, msg, label = False, str(e), ""
                    audit_ui("report_schedule.send_test", f"report:{values['name']}", {"ok": ok, "message": msg})
                    (st.success if ok else st.error)(f"{'✅ 已寄出' if ok else '❌ 寄送失敗'}（{label}）：{msg}")
                with b4.popover("🗑️ 刪除"):
                    if st.button("確定刪除這個排程", key=f"rs_del_{sid}"):
                        execute("DELETE FROM report_schedules WHERE schedule_id=%s;", (int(sid),))
                        audit_ui("report_schedule.delete", f"report:{row['name']}")
                        st.rerun()

    with st.expander("➕ 新增排程報表", expanded=df.empty):
        values, errors = _form(None, catalog)
        for e in errors[1:] if not values["name"] else errors:
            st.caption(f"⚠️ {e}")
        if st.button("建立排程", type="primary", disabled=bool(errors), key="rs_create"):
            _save(values)
            st.success(f"✅ 已建立「{values['name']}」")
            st.rerun()
