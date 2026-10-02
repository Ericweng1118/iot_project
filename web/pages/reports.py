"""
web/pages/reports.py
====================
📑 報表

分頁：
    即時產生   選範圍 / 期間 / 粒度 / 統計值，畫面預覽並下載 Excel / CSV
    排程寄送   每日 / 每週 / 每月自動產生 Excel 寄到信箱或 Webhook（web/pages/report_schedules.py）

- 粒度：每小時 / 每日 / 每月；以 SCADA_TIMEZONE（預設台灣時間）對齊，
  「每日」是當地 00:00 ~ 24:00，不是 UTC 的一天
- 統計值：平均 / 最小 / 最大 / 期末值 / 增量
    增量 = 本期期末值 − 上期期末值（第一期用本期期初值），適合累計型電表、流量計
    算「這段期間用了多少」。單調遞增的計數器才有意義，一般類比量請看平均。
- 通訊中斷 / 品質不良的標記（sql/014 的 quality ≥ 3）不納入統計

計算邏輯在 services/reporting.py，跟排程報表共用。
"""

from datetime import date, datetime, time as dtime, timedelta

import pandas as pd
import streamlit as st

from services.reporting import (
    GRANULARITY,
    METRICS,
    build_report,  # noqa: F401  （tests 從這裡 import，保留相容）
    detail_frame,
    pivot_report,
    query_buckets,
)
from web.common import LOCAL_TZ, now_local, sensor_catalog, table_exists, to_csv_bytes, to_excel_bytes


def render():
    st.title("📑 報表")
    if table_exists("report_schedules"):
        tab_now, tab_sched = st.tabs(["⚡ 即時產生", "⏰ 排程寄送"])
        with tab_sched:
            from web.pages import report_schedules
            report_schedules.render()
        with tab_now:
            _render_now()
    else:
        _render_now()


def _render_now():
    catalog = sensor_catalog()
    if catalog.empty:
        st.info("目前沒有任何感測器。")
        return
    labels = dict(zip(catalog["sensor_id"], catalog["label"]))

    with st.container(border=True):
        mode = st.segmented_control("範圍", ["依設備", "依感測器"], default="依設備", key="rp_mode")
        if mode == "依感測器":
            sensor_ids = st.multiselect("感測器", list(labels), format_func=labels.get, key="rp_sensors",
                                        placeholder="輸入關鍵字搜尋…")
        else:
            devs = catalog.dropna(subset=["device_code"])
            dev_labels = {
                code: f"{code}（{name}）" if pd.notnull(name) and name else code
                for code, name in devs[["device_code", "device_name"]].drop_duplicates().itertuples(index=False)
            }
            chosen = st.multiselect("設備", list(dev_labels), format_func=dev_labels.get, key="rp_devices",
                                    placeholder="選擇一或多台設備，會帶出底下全部感測器")
            sensor_ids = catalog[catalog["device_code"].isin(chosen)]["sensor_id"].tolist()

        c1, c2, c3, c4 = st.columns(4)
        today = now_local().date()
        gran = c1.selectbox("粒度", list(GRANULARITY), index=1, key="rp_gran")
        metric_label = c2.selectbox("統計值", list(METRICS), key="rp_metric",
                                    help="增量 = 本期期末值 − 上期期末值，適合累計電表 / 流量計算用量")
        default_start = today.replace(day=1) if gran != "每月" else date(today.year, 1, 1)
        sd = c3.date_input("開始日期", default_start, key="rp_sd")
        ed = c4.date_input("結束日期（含）", today, key="rp_ed")

    if not sensor_ids:
        st.info("請選擇設備或感測器。")
        return
    if sd > ed:
        st.error("開始日期不能晚於結束日期")
        return
    if gran == "每小時" and (ed - sd).days > 62:
        st.warning("每小時報表最多 62 天，請縮短期間或改用每日。")
        return

    start = datetime.combine(sd, dtime(0, 0), tzinfo=LOCAL_TZ)
    end = datetime.combine(ed + timedelta(days=1), dtime(0, 0), tzinfo=LOCAL_TZ)
    bucket = GRANULARITY[gran]
    with st.spinner("統計中…"):
        raw = query_buckets(sensor_ids, start, end, bucket)
    if raw.empty:
        st.warning("這段期間沒有資料。")
        return

    metric = METRICS[metric_label]
    pivot = pivot_report(raw, metric, labels, sensor_ids, bucket)

    st.caption(f"{sd} ～ {ed}｜{gran}｜{metric_label}｜{len(sensor_ids)} 個感測器｜時區 {LOCAL_TZ}")
    st.dataframe(
        pivot, hide_index=True, width="stretch",
        column_config={c: st.column_config.NumberColumn(format="%.4g") for c in pivot.columns if c != "時間"},
    )

    detail = detail_frame(raw, labels, bucket)

    fname = f"report_{gran}_{metric_label.split('（')[0]}_{sd:%Y%m%d}_{ed:%Y%m%d}"
    c1, c2, c3 = st.columns(3)
    xlsx = to_excel_bytes({"報表": pivot, "明細": detail})
    if xlsx:
        c1.download_button("⬇️ 下載 Excel", xlsx, file_name=f"{fname}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           type="primary")
    else:
        c1.caption("（未安裝 openpyxl，無法匯出 Excel）")
    c2.download_button("⬇️ 下載 CSV（報表）", to_csv_bytes(pivot), file_name=f"{fname}.csv", mime="text/csv")
    c3.download_button("⬇️ 下載 CSV（明細）", to_csv_bytes(detail), file_name=f"{fname}_detail.csv", mime="text/csv")

    with st.expander("明細（每期的平均 / 最小 / 最大 / 期初 / 期末 / 筆數）"):
        st.dataframe(detail, hide_index=True, width="stretch")
