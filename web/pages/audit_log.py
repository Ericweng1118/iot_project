"""
web/pages/audit_log.py
======================
📜 稽核紀錄（admin）：誰在什麼時候做了什麼。

記錄的動作包含登入 / 登出 / 登入失敗、所有設定頁的儲存與新增、警報確認、
警報規則變更、使用者管理。detail 欄位記錄「改了哪些欄位、舊值 → 新值」。
"""

import json
from datetime import datetime, time as dtime, timedelta

import pandas as pd
import streamlit as st

from web.common import LOCAL_TZ, fetch_df, now_local, require_role, table_exists, to_csv_bytes


def render():
    require_role("admin")
    st.title("📜 稽核紀錄")
    if not table_exists("audit_log"):
        st.warning("尚未建立稽核資料表，請用資料表擁有者執行 `sql/012_users_audit_status.sql`。")
        return

    today = now_local().date()
    c1, c2, c3, c4 = st.columns(4)
    sd = c1.date_input("開始日期", today - timedelta(days=7))
    ed = c2.date_input("結束日期", today)
    users = fetch_df("SELECT DISTINCT username FROM audit_log ORDER BY 1;", show_error=False)
    user = c3.selectbox("使用者", ["全部"] + users["username"].tolist() if not users.empty else ["全部"])
    action = c4.text_input("動作包含", placeholder="例如 sensor、alarm、login")

    start = datetime.combine(sd, dtime(0, 0), tzinfo=LOCAL_TZ)
    end = datetime.combine(ed + timedelta(days=1), dtime(0, 0), tzinfo=LOCAL_TZ)
    df = fetch_df(
        """
        SELECT ts, username, action, target, detail
        FROM audit_log
        WHERE ts >= %s AND ts < %s
          AND (%s = '全部' OR username = %s)
          AND (%s = '' OR action ILIKE '%%' || %s || '%%')
        ORDER BY ts DESC LIMIT 5000;
        """,
        (start, end, user, user, action.strip(), action.strip()),
    )
    if df.empty:
        st.info("沒有符合條件的紀錄。")
        return

    df["detail"] = df["detail"].map(
        lambda d: "" if d is None or (isinstance(d, float) and pd.isna(d))
        else json.dumps(d, ensure_ascii=False) if not isinstance(d, str) else d
    )
    df.columns = ["時間", "使用者", "動作", "對象", "內容"]
    st.caption(f"共 {len(df)} 筆（最多顯示 5000 筆）")
    st.dataframe(
        df, hide_index=True, width="stretch", height=560,
        column_config={
            "時間": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm:ss", timezone=str(LOCAL_TZ)),
            "內容": st.column_config.TextColumn(width="large"),
        },
    )
    st.download_button("⬇️ 下載 CSV", to_csv_bytes(df), file_name=f"audit_{sd:%Y%m%d}_{ed:%Y%m%d}.csv",
                       mime="text/csv")
