"""
web/pages/alarms.py
===================
🚨 警報中心

分頁：
    目前警報  仍在發生、或已恢復但尚未確認的警報（ISA-18.2 慣例），可確認（ACK）＋備註
    警報歷史  依時間 / 等級 / 關鍵字查詢，附「最常發生的警報」排行（找出需要調整門檻
              或維修的點位）與每日數量統計，可匯出 CSV
    診斷檢查  v2 的「異常監控」即時查詢（不依賴警報引擎）

確認警報需要 operator 以上權限。確認是用 event_id 選取（不是表格列號），
自動更新造成列順序改變時也不會確認到錯的警報。
"""

from datetime import datetime, time as dtime, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from services.alarm.rules import ALARM_TYPE_LABELS, PRIORITY_LABELS
from web.common import (
    LOCAL_TZ,
    audit_ui,
    can,
    current_user,
    execute,
    fetch_df,
    now_local,
    table_exists,
    to_csv_bytes,
)
from web.pages import diagnostics

PRIORITY_ICONS = {1: "🟥", 2: "🟧", 3: "🟨", 4: "🟦"}


def _state_label(r) -> str:
    active = pd.isna(r["cleared_at"])
    acked = pd.notnull(r["acked_at"])
    if active and not acked:
        return "🔴 發生中・未確認"
    if active and acked:
        return "🟠 發生中・已確認"
    if not active and not acked:
        return "🔵 已恢復・未確認"
    return "⚪ 已恢復・已確認"


def _local(series: pd.Series, fmt: str) -> pd.Series:
    """時間欄位轉當地時間字串；整欄都是空值（例如沒有任何已恢復的警報）時也安全。"""
    return pd.to_datetime(series, utc=True).dt.tz_convert(LOCAL_TZ).dt.strftime(fmt).fillna("")


def _prio(p) -> str:
    p = int(p)
    return f"{PRIORITY_ICONS.get(p, '')} {PRIORITY_LABELS.get(p, p)}"


def _load_current() -> pd.DataFrame:
    return fetch_df(
        """
        SELECT event_id, priority, alarm_type, message, trigger_value, setpoint,
               raised_at, cleared_at, acked_at, acked_by, ack_comment, source_type,
               EXTRACT(EPOCH FROM now() - raised_at) AS age_seconds
        FROM alarm_events
        WHERE cleared_at IS NULL OR acked_at IS NULL
        ORDER BY (cleared_at IS NULL) DESC, priority ASC, raised_at DESC;
        """
    )


def _ack(event_ids, comment: str):
    user = current_user()["username"]
    n = execute(
        "UPDATE alarm_events SET acked_at = now(), acked_by = %s, ack_comment = %s "
        "WHERE event_id = ANY(%s) AND acked_at IS NULL;",
        (user, comment or None, [int(e) for e in event_ids]),
    )
    audit_ui("alarm.ack", "alarm_events", {"event_ids": [int(e) for e in event_ids], "comment": comment})
    return n


def _render_current():
    c1, c2, c3 = st.columns([2.2, 1, 1])
    only_active = c2.toggle("只看發生中", key="al_only_active",
                            help="隱藏「已恢復但尚未確認」的警報")
    refresh = c3.selectbox("自動更新", ["關閉", "10 秒", "30 秒"], key="al_refresh")
    run_every = None if refresh == "關閉" else int(refresh.split()[0])

    @st.fragment(run_every=run_every)
    def table():
        df = _load_current()
        if df.empty:
            st.success("✅ 目前沒有任何警報。")
            return
        if only_active:
            shown = df[df["cleared_at"].isna()]
        else:
            shown = df
        active = int(df["cleared_at"].isna().sum())
        unacked = int(df["acked_at"].isna().sum())
        m1, m2, m3 = st.columns(3)
        m1.metric("發生中", active, border=True)
        m2.metric("未確認", unacked, border=True)
        m3.metric("最高等級", _prio(df["priority"].min()), border=True)

        if shown.empty:
            st.success("✅ 目前沒有發生中的警報（有已恢復、待確認的警報，關掉「只看發生中」即可看到）。")
            return
        show = pd.DataFrame({
            "#": shown["event_id"],
            "狀態": shown.apply(_state_label, axis=1),
            "等級": shown["priority"].map(_prio),
            "類型": shown["alarm_type"].map(lambda t: ALARM_TYPE_LABELS.get(t, t)),
            "訊息": shown["message"],
            "發生時間": _local(shown["raised_at"], "%m-%d %H:%M:%S"),
            "恢復時間": _local(shown["cleared_at"], "%m-%d %H:%M:%S"),
            "確認者": shown["acked_by"].fillna(""),
        })
        st.dataframe(show, hide_index=True, width="stretch",
                     height=min(38 * (len(show) + 1), 560))

    with c1:
        st.caption("清單包含「仍在發生」與「已恢復但還沒有人確認」的警報；確認且恢復後就會移到警報歷史。")
    table()

    if not can("operator"):
        st.info("🔒 確認警報需要「操作員」以上的權限。")
        return

    unacked = fetch_df(
        "SELECT event_id, priority, message, raised_at FROM alarm_events "
        "WHERE acked_at IS NULL ORDER BY priority, raised_at DESC;", show_error=False,
    )
    if unacked.empty:
        return
    labels = {
        int(r["event_id"]): f"#{int(r['event_id'])} {_prio(r['priority'])} {r['message'][:80]}"
        for _, r in unacked.iterrows()
    }
    with st.form("ack_form", clear_on_submit=True):
        st.markdown("**✔️ 確認警報**")
        chosen = st.multiselect("選擇要確認的警報", list(labels), format_func=labels.get,
                                placeholder="不選擇 + 按「全部確認」= 確認全部未確認警報")
        comment = st.text_input("備註（選填）", placeholder="例如：已通知現場人員處理")
        b1, b2 = st.columns(2)
        ack_selected = b1.form_submit_button("確認所選", type="primary", width="stretch")
        ack_all = b2.form_submit_button(f"全部確認（{len(labels)} 筆）", width="stretch")
    if ack_selected and chosen:
        n = _ack(chosen, comment)
        st.toast(f"✅ 已確認 {n} 筆警報")
        st.rerun()
    elif ack_selected:
        st.warning("請先選擇要確認的警報")
    elif ack_all:
        n = _ack(list(labels), comment)
        st.toast(f"✅ 已確認 {n} 筆警報")
        st.rerun()


def _render_history():
    now = now_local()
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([1, 1, 1.2, 1.6])
        sd = c1.date_input("開始日期", now.date() - timedelta(days=7), key="ah_sd")
        ed = c2.date_input("結束日期", now.date(), key="ah_ed")
        prios = c3.multiselect("等級", [1, 2, 3, 4], format_func=_prio, key="ah_prio",
                               placeholder="全部")
        keyword = c4.text_input("關鍵字", placeholder="訊息內容（設備 / 感測器 / Server 名稱）", key="ah_kw")

    start = datetime.combine(sd, dtime(0, 0), tzinfo=LOCAL_TZ)
    end = datetime.combine(ed + timedelta(days=1), dtime(0, 0), tzinfo=LOCAL_TZ)
    df = fetch_df(
        """
        SELECT event_id, priority, alarm_type, message, trigger_value, setpoint, alarm_key,
               raised_at, cleared_at, acked_at, acked_by, ack_comment,
               EXTRACT(EPOCH FROM COALESCE(cleared_at, now()) - raised_at) AS duration_seconds
        FROM alarm_events
        WHERE raised_at >= %s AND raised_at < %s
          AND (%s::smallint[] IS NULL OR priority = ANY(%s::smallint[]))
          AND (%s = '' OR message ILIKE '%%' || %s || '%%')
        ORDER BY raised_at DESC
        LIMIT 20000;
        """,
        (start, end, prios or None, prios or None, keyword.strip(), keyword.strip()),
    )
    if df.empty:
        st.info("這段期間沒有警報紀錄。")
        return

    m1, m2, m3, m4 = st.columns(4)
    days = max((end - start).days, 1)
    m1.metric("警報總數", f"{len(df):,}", border=True)
    m2.metric("平均每日", f"{len(df) / days:.1f}", border=True,
              help="ISA-18.2 建議：每位操作員每天約 150 則以下屬於可管理範圍")
    m3.metric("平均持續", _fmt_duration(df["duration_seconds"].mean()), border=True)
    m4.metric("未確認", int(df["acked_at"].isna().sum()), border=True)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**最常發生的警報（前 10 名）**")
        top = (df.groupby("alarm_key")
                 .agg(次數=("event_id", "count"), 訊息=("message", "first"))
                 .sort_values("次數", ascending=False).head(10).reset_index())
        top["名稱"] = top["訊息"].str.split("：").str[0].str.slice(0, 40)
        st.altair_chart(
            alt.Chart(top).mark_bar().encode(
                x=alt.X("次數:Q", title="次數"),
                y=alt.Y("名稱:N", sort="-x", title=None),
                tooltip=["訊息", "次數"],
            ).properties(height=280),
            width="stretch",
        )
    with c2:
        st.markdown("**每日警報數**")
        daily = df.assign(日期=pd.to_datetime(df["raised_at"], utc=True).dt.tz_convert(LOCAL_TZ).dt.date,
                          等級=df["priority"].map(lambda p: PRIORITY_LABELS.get(int(p))))
        daily = daily.groupby(["日期", "等級"]).size().reset_index(name="數量")
        st.altair_chart(
            alt.Chart(daily).mark_bar().encode(
                x=alt.X("日期:T", title=None, axis=alt.Axis(format="%m/%d")),
                y=alt.Y("數量:Q", stack=True),
                color=alt.Color("等級:N", sort=["緊急", "高", "中", "低"],
                                scale=alt.Scale(domain=["緊急", "高", "中", "低"],
                                                range=["#e5484d", "#f76b15", "#ffc53d", "#3e63dd"])),
                tooltip=["日期:T", "等級", "數量"],
            ).properties(height=280),
            width="stretch",
        )

    table = pd.DataFrame({
        "#": df["event_id"],
        "等級": df["priority"].map(_prio),
        "類型": df["alarm_type"].map(lambda t: ALARM_TYPE_LABELS.get(t, t)),
        "訊息": df["message"],
        "發生時間": pd.to_datetime(df["raised_at"], utc=True),
        "恢復時間": pd.to_datetime(df["cleared_at"], utc=True),
        "持續": df["duration_seconds"].map(_fmt_duration),
        "確認者": df["acked_by"].fillna(""),
        "確認時間": pd.to_datetime(df["acked_at"], utc=True),
        "備註": df["ack_comment"].fillna(""),
    })
    st.dataframe(
        table, hide_index=True, width="stretch", height=420,
        column_config={c: st.column_config.DatetimeColumn(c, format="YYYY-MM-DD HH:mm:ss", timezone=str(LOCAL_TZ))
                       for c in ("發生時間", "恢復時間", "確認時間")},
    )
    st.download_button("⬇️ 下載 CSV", to_csv_bytes(table),
                       file_name=f"alarms_{sd:%Y%m%d}_{ed:%Y%m%d}.csv", mime="text/csv")


def _fmt_duration(seconds) -> str:
    if seconds is None or pd.isna(seconds):
        return "—"
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小時"
    return f"{seconds / 86400:.1f} 天"


def render():
    st.title("🚨 警報中心")
    if not table_exists("alarm_events"):
        st.warning(
            "尚未建立警報資料表。請用資料表擁有者執行 `sql/011_alarm_management.sql`，"
            "並重新啟動 main.py 讓警報引擎開始運作。下方「診斷檢查」仍可使用。"
        )
        diagnostics.render()
        return

    tab_now, tab_hist, tab_diag = st.tabs(["目前警報", "警報歷史", "診斷檢查"])
    with tab_now:
        _render_current()
    with tab_hist:
        _render_history()
    with tab_diag:
        diagnostics.render()
