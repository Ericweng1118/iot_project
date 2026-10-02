"""
web/pages/overview.py
=====================
🏭 即時總覽：一眼看出整個廠區的狀態。

- 頂部 KPI：採集服務是否活著、OPC UA Server 連線數、感測器狀態分布、目前警報數
- 依 廠區 / 產線 / 設備 篩選，或用關鍵字搜尋感測器
- 「卡片」檢視：每台設備一張卡片，感測器以顏色標示 正常 / 警報 / 離線 / 品質不良
- 「表格」檢視：全部感測器一張表，可排序，適合點位多的時候
- 自動更新（st.fragment 局部重繪，不會整頁閃爍、也不會清掉篩選條件）

即時值來源是即時層三張表（opcua_tags / modbus_scada / tia_scada）透過 sensor_id
對應到 sensors，跟採集主程式是否在同一台機器無關。
"""

import html
import json

import pandas as pd
import streamlit as st

from services.alarm.rules import PRIORITY_LABELS, format_value
from web import nav
from web.common import (
    MODBUS_ENABLED,
    OPCUA_ENABLED,
    TIA_ENABLED,
    fetch_df,
    fmt_age,
    get_service_status,
    table_exists,
)

STATUS_ORDER = ["警報", "離線", "品質不良", "超限", "正常", "未綁定"]
STATUS_STYLE = {
    "警報": ("🔴", "#e5484d"),
    "離線": ("⚫", "#8b8d98"),
    "品質不良": ("🟠", "#f76b15"),
    "超限": ("🟡", "#ffc53d"),
    "正常": ("🟢", "#30a46c"),
    "未綁定": ("⚪", "#b9bbc6"),
}

_CSS = """
<style>
.ov-table { width: 100%; border-collapse: collapse; font-size: 0.88rem; border: none !important; margin: 0; }
.ov-table tr, .ov-table td { border: none !important; background: transparent !important; }
.ov-table td { padding: 3px 4px; border-bottom: 1px solid rgba(128,128,128,0.15) !important; vertical-align: middle; }
.ov-table td.val { text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; white-space: nowrap; }
.ov-table td.age { text-align: right; color: rgba(128,128,128,0.9); font-size: 0.75rem; white-space: nowrap; }
.ov-dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; }
.ov-sub { color: rgba(128,128,128,0.95); font-size: 0.78rem; }
</style>
"""


def _live_points_sql() -> str:
    parts = []
    # 每一段都要明確命名欄位：UNION 的欄位名稱取自第一段，OPC UA 停用時第一段會變成 Modbus
    if OPCUA_ENABLED:
        parts.append(
            "SELECT sensor_id, current_data->>'val' AS val, plc_state, quality, last_update, "
            "'OPC UA' AS source FROM opcua_tags WHERE sensor_id IS NOT NULL"
        )
    if MODBUS_ENABLED:
        parts.append(
            "SELECT sensor_id, current_data->>'val' AS val, plc_state, NULL::text AS quality, "
            "last_update, 'Modbus' AS source FROM modbus_scada WHERE sensor_id IS NOT NULL"
        )
    if TIA_ENABLED:
        parts.append(
            "SELECT sensor_id, current_data->>'val' AS val, plc_state, NULL::text AS quality, "
            "last_update, 'TIA/S7' AS source FROM tia_scada WHERE sensor_id IS NOT NULL"
        )
    if table_exists("calculated_points"):
        parts.append(
            "SELECT sensor_id, current_value::text AS val, state AS plc_state, quality, last_update, "
            "'計算點' AS source FROM calculated_points WHERE enabled"
        )
    if not parts:
        return ("SELECT NULL::int AS sensor_id, NULL::text AS val, NULL::text AS plc_state, "
                "NULL::text AS quality, NULL::timestamptz AS last_update, NULL::text AS source WHERE false")
    return " UNION ALL ".join(parts)


def load_live() -> pd.DataFrame:
    has_alarm = table_exists("alarm_events")
    alarm_join = (
        "LEFT JOIN (SELECT sensor_id, min(priority) AS alarm_priority, count(*) AS alarm_count "
        "           FROM alarm_events WHERE cleared_at IS NULL AND sensor_id IS NOT NULL "
        "           GROUP BY sensor_id) a ON a.sensor_id = s.sensor_id"
        if has_alarm else
        "LEFT JOIN (SELECT NULL::int AS sensor_id, NULL::int AS alarm_priority, NULL::int AS alarm_count) a ON false"
    )
    df = fetch_df(
        f"""
        WITH pts AS ({_live_points_sql()})
        SELECT si.site_name, pl.line_name, d.device_id, d.device_code, d.device_name,
               s.sensor_id, s.sensor_code, s.nickname, s.sensor_type, s.unit,
               s.min_threshold, s.max_threshold, s.state_dictionary,
               p.val, p.plc_state, p.quality, p.last_update, p.source,
               EXTRACT(EPOCH FROM now() - p.last_update) AS age_seconds,
               a.alarm_priority, a.alarm_count
        FROM sensors s
        LEFT JOIN devices d           ON d.device_id = s.device_id
        LEFT JOIN production_lines pl ON pl.line_id = d.line_id
        LEFT JOIN sites si            ON si.site_id = pl.site_id
        LEFT JOIN pts p               ON p.sensor_id = s.sensor_id
        {alarm_join}
        ORDER BY si.site_name NULLS LAST, pl.line_name NULLS LAST, d.device_code NULLS LAST, s.sensor_code;
        """
    )
    if df.empty:
        return df
    df["status"] = df.apply(_status, axis=1)
    df["display_value"] = df.apply(_display_value, axis=1)
    df["name"] = df.apply(
        lambda r: r["nickname"] if pd.notnull(r["nickname"]) and str(r["nickname"]).strip() else r["sensor_code"],
        axis=1,
    )
    return df


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _status(r) -> str:
    if pd.isna(r["source"]):
        return "未綁定"
    if r["plc_state"] != "ONLINE":
        return "離線"
    if pd.notnull(r["quality"]) and r["quality"] != "GOOD":
        return "品質不良"
    if pd.notnull(r["alarm_priority"]):
        return "警報"
    num = _num(r["val"])
    if num is not None:
        if pd.notnull(r["max_threshold"]) and num > float(r["max_threshold"]):
            return "超限"
        if pd.notnull(r["min_threshold"]) and num < float(r["min_threshold"]):
            return "超限"
    return "正常"


def _display_value(r) -> str:
    if pd.isna(r["val"]):
        return "—"
    state_dict = r["state_dictionary"]
    if isinstance(state_dict, str):
        try:
            state_dict = json.loads(state_dict)
        except ValueError:
            state_dict = None
    unit = r["unit"] if pd.notnull(r["unit"]) else None
    num = _num(r["val"])
    if num is None:
        return str(r["val"])  # Modbus 狀態字典已翻譯成文字
    return format_value(num, unit, state_dict if isinstance(state_dict, dict) else None)


# ------------------------------------------------------------------
# KPI
# ------------------------------------------------------------------
def _render_kpis(df: pd.DataFrame):
    cols = st.columns(5)

    svc = get_service_status()
    with cols[0]:
        if svc is None:
            st.metric("採集服務", "未知", help="尚未執行 sql/012，無法得知 main.py 是否運作中", border=True)
        elif not svc.get("exists"):
            st.metric("採集服務", "未回報", help="main.py 尚未啟動過 v3 版本", border=True)
        elif svc["alive"]:
            st.metric("採集服務", "🟢 運作中", f"心跳 {fmt_age(svc['age_seconds'])}",
                      delta_color="off", border=True)
        else:
            label = "⚪ 已停止" if svc["stopped_normally"] else "🔴 無回應"
            st.metric("採集服務", label, f"最後心跳 {fmt_age(svc['age_seconds'])}",
                      delta_color="inverse", border=True)

    with cols[1]:
        if OPCUA_ENABLED:
            srv = fetch_df(
                "SELECT count(*) FILTER (WHERE conn_state = 'ONLINE') AS online, count(*) AS total "
                "FROM opcua_servers WHERE enabled;", show_error=False,
            )
            online = int(srv.iloc[0]["online"]) if not srv.empty else 0
            total = int(srv.iloc[0]["total"]) if not srv.empty else 0
            st.metric("OPC UA Server", f"{online} / {total}",
                      "全部連線" if online == total else f"{total - online} 台斷線",
                      delta_color="normal" if online == total else "inverse", border=True)
        else:
            st.metric("OPC UA Server", "停用", border=True)

    bound = df[df["status"] != "未綁定"] if not df.empty else df
    normal = int((bound["status"] == "正常").sum()) if not bound.empty else 0
    with cols[2]:
        st.metric("感測器正常", f"{normal} / {len(bound)}",
                  help=f"已綁定點位的感測器；另有 {len(df) - len(bound)} 個未綁定", border=True)

    with cols[3]:
        offline = int((bound["status"] == "離線").sum()) if not bound.empty else 0
        bad = int((bound["status"] == "品質不良").sum()) if not bound.empty else 0
        st.metric("離線 / 品質不良", f"{offline} / {bad}", border=True)

    with cols[4]:
        if table_exists("alarm_events"):
            al = fetch_df(
                "SELECT count(*) FILTER (WHERE cleared_at IS NULL) AS active, "
                "count(*) FILTER (WHERE acked_at IS NULL) AS unacked, "
                "min(priority) FILTER (WHERE cleared_at IS NULL) AS top "
                "FROM alarm_events WHERE cleared_at IS NULL OR acked_at IS NULL;",
                show_error=False,
            )
            active = int(al.iloc[0]["active"]) if not al.empty else 0
            unacked = int(al.iloc[0]["unacked"]) if not al.empty else 0
            top = al.iloc[0]["top"] if not al.empty else None
            label = f"未確認 {unacked}"
            if pd.notnull(top):
                label += f"・最高 {PRIORITY_LABELS.get(int(top))}"
            st.metric("發生中警報", active, label,
                      delta_color="inverse" if active or unacked else "off", border=True)
        else:
            st.metric("目前警報", "—", help="尚未執行 sql/011", border=True)


# ------------------------------------------------------------------
# 卡片 / 表格
# ------------------------------------------------------------------
def _sensor_rows_html(group: pd.DataFrame) -> str:
    rows = []
    for _, r in group.iterrows():
        _, color = STATUS_STYLE[r["status"]]
        name = html.escape(str(r["name"]))
        title = html.escape(f"{r['sensor_code']}｜{r['status']}｜{r['source'] if pd.notnull(r['source']) else '未綁定'}")
        value = html.escape(r["display_value"])
        age = fmt_age(r["age_seconds"]) if pd.notnull(r["age_seconds"]) else ""
        rows.append(
            f"<tr title='{title}'>"
            f"<td><span class='ov-dot' style='background:{color}'></span>{name}</td>"
            f"<td class='val'>{value}</td><td class='age'>{age}</td></tr>"
        )
    return "<table class='ov-table'>" + "".join(rows) + "</table>"


def _render_cards(df: pd.DataFrame):
    st.markdown(_CSS, unsafe_allow_html=True)
    devices = list(df.groupby(["device_code", "device_name", "site_name", "line_name"], dropna=False, sort=False))
    per_row = 3
    for start in range(0, len(devices), per_row):
        cols = st.columns(per_row)
        for col, ((code, name, site, line), group) in zip(cols, devices[start:start + per_row]):
            counts = group["status"].value_counts()
            worst = next((s for s in STATUS_ORDER if counts.get(s)), "正常")
            icon, _ = STATUS_STYLE[worst]
            with col.container(border=True):
                title = f"{icon} **{code if pd.notnull(code) else '（未指定設備）'}**"
                if pd.notnull(name) and name:
                    title += f"　{name}"
                st.markdown(title)
                where = " / ".join(str(x) for x in (site, line) if pd.notnull(x))
                summary = "　".join(f"{STATUS_STYLE[s][0]} {int(counts[s])}" for s in STATUS_ORDER if counts.get(s))
                st.markdown(f"<div class='ov-sub'>{html.escape(where)}　{summary}</div>", unsafe_allow_html=True)
                st.markdown(_sensor_rows_html(group), unsafe_allow_html=True)
                trends_page = nav.get("trends")
                bound_ids = group[group["status"] != "未綁定"]["sensor_id"].astype(int).tolist()[:8]
                if trends_page and bound_ids:
                    st.page_link(trends_page, label="查看趨勢", icon="📈",
                                 query_params={"sensors": ",".join(map(str, bound_ids))})


def _render_table(df: pd.DataFrame):
    df = df.reset_index(drop=True)
    out = df.assign(
        狀態=df["status"].map(lambda s: f"{STATUS_STYLE[s][0]} {s}"),
        更新=df["age_seconds"].map(fmt_age),
    )[["狀態", "site_name", "line_name", "device_code", "sensor_code", "name",
       "display_value", "source", "更新"]]
    out.columns = ["狀態", "廠區", "產線", "設備", "感測器編號", "名稱", "即時值", "來源", "更新"]
    event = st.dataframe(
        out, hide_index=True, width="stretch", height=min(38 * (len(out) + 1), 720),
        on_select="rerun", selection_mode="multi-row", key="ov_table",
    )
    rows = [i for i in event.selection.rows if i < len(df)][:8]
    trends_page = nav.get("trends")
    if trends_page and st.button(f"📈 開啟所選感測器的趨勢（{len(rows)}）", disabled=not rows):
        st.switch_page(trends_page, query_params={
            "sensors": ",".join(str(int(df.loc[i, "sensor_id"])) for i in rows)
        })
    st.caption("勾選表格左側的列（最多 8 個）後按上方按鈕開啟趨勢。")


def render():
    st.title("🏭 即時總覽")

    df_all = load_live()
    if df_all.empty:
        st.info("目前沒有任何感測器。請先到「感測器階層管理」建立 廠區 → 產線 → 設備 → 感測器，"
                "再到點位設定頁把點位綁定到感測器。")
        return

    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([1.2, 1.2, 1.6, 1])
        sites = sorted(df_all["site_name"].dropna().unique().tolist())
        site = c1.selectbox("廠區", ["全部"] + sites, key="ov_site")
        scoped = df_all if site == "全部" else df_all[df_all["site_name"] == site]
        lines = sorted(scoped["line_name"].dropna().unique().tolist())
        line = c2.selectbox("產線", ["全部"] + lines, key="ov_line")
        keyword = c3.text_input("搜尋", placeholder="設備 / 感測器編號 / 暱稱", key="ov_kw")
        refresh = c4.selectbox("自動更新", ["10 秒", "30 秒", "60 秒", "關閉"], key="ov_refresh")
        c5, c6 = st.columns([3, 1])
        status_filter = c5.pills(
            "狀態", STATUS_ORDER, selection_mode="multi", key="ov_status",
            help="不選 = 全部顯示",
        )
        view = c6.segmented_control("檢視", ["卡片", "表格"], default="卡片", key="ov_view")
        hide_unbound = st.toggle("隱藏未綁定點位的感測器", value=True, key="ov_hide_unbound")

    run_every = None if refresh == "關閉" else int(refresh.split()[0])

    @st.fragment(run_every=run_every)
    def live():
        df = load_live()
        if df.empty:
            return
        _render_kpis(df)

        if site != "全部":
            df = df[df["site_name"] == site]
        if line != "全部":
            df = df[df["line_name"] == line]
        if keyword:
            kw = keyword.strip().lower()
            hay = (df["device_code"].fillna("") + " " + df["device_name"].fillna("") + " "
                   + df["sensor_code"].fillna("") + " " + df["nickname"].fillna("")).str.lower()
            df = df[hay.str.contains(kw, regex=False)]
        if hide_unbound:
            df = df[df["status"] != "未綁定"]
        if status_filter:
            df = df[df["status"].isin(status_filter)]

        st.caption(f"顯示 {len(df)} 個感測器。")
        if df.empty:
            st.info("沒有符合條件的感測器。")
        elif view == "表格":
            _render_table(df)
        else:
            _render_cards(df)

    live()
