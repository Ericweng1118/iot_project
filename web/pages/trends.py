"""
web/pages/trends.py
===================
📈 歷史趨勢：查詢 sensor_readings 畫成趨勢圖。

- 可同時選多個感測器；不同工程單位自動分成不同圖表（同一張圖的 Y 軸單位一致）
- 時間範圍快選（1 小時 ~ 30 天）或自訂
- 自動降採樣：區間長時用 TimescaleDB time_bucket 聚合（平均 / 最小 / 最大 / 最後值），
  每個感測器控制在約 1500 點以內，查 30 天也不會把瀏覽器卡死
- 預設「階梯線」：sensor_readings 是「有變化才寫」，兩筆之間的值其實是維持前一筆，
  用斜線連起來會讓人誤以為數值是漸變的
- 只選一個感測器時，畫出上下限與警報設定值的參考線
- 統計表（最小 / 最大 / 平均 / 首筆 / 末筆 / 區間變化量）＋ CSV 下載

- 🆕 品質（sql/014）：通訊中斷 / 品質不良的時間點以紅色 ▼ 標示，線條在那裡斷開，
  不會把斷線前後的值直接連起來；統計值排除這些標記

網址支援 ?sensors=1,2,3，總覽頁的「查看趨勢」就是用這個帶過來。
"""

from datetime import datetime, time as dtime, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from data_layer import quality as Q
from web.common import (
    LOCAL_TZ,
    column_exists,
    fetch_df,
    now_local,
    sensor_catalog,
    table_exists,
    to_csv_bytes,
)

PRESETS = {
    "最近 1 小時": timedelta(hours=1),
    "最近 6 小時": timedelta(hours=6),
    "最近 24 小時": timedelta(hours=24),
    "最近 3 天": timedelta(days=3),
    "最近 7 天": timedelta(days=7),
    "最近 30 天": timedelta(days=30),
    "自訂": None,
}

BUCKETS = {
    "原始資料": None,
    "1 分鐘": "1 minute",
    "5 分鐘": "5 minutes",
    "15 分鐘": "15 minutes",
    "1 小時": "1 hour",
    "1 天": "1 day",
}
_BUCKET_SECONDS = {None: 0, "1 minute": 60, "5 minutes": 300, "15 minutes": 900,
                   "1 hour": 3600, "1 day": 86400}

TARGET_POINTS = 1500
RAW_ROW_LIMIT = 200_000


def auto_bucket(span: timedelta, sensor_count: int, raw_estimate: int | None = None) -> str | None:
    """依時間跨度挑聚合粒度，讓每個感測器大約 TARGET_POINTS 點以內。"""
    if raw_estimate is not None and raw_estimate <= TARGET_POINTS * max(sensor_count, 1):
        return None
    seconds = span.total_seconds()
    for bucket in ("1 minute", "5 minutes", "15 minutes", "1 hour", "1 day"):
        if seconds / _BUCKET_SECONDS[bucket] <= TARGET_POINTS:
            return bucket
    return "1 day"


def _query(sensor_ids, start, end, bucket, has_quality: bool) -> pd.DataFrame:
    q_col = "COALESCE(quality, 0)" if has_quality else "0"
    valid = f"AND {Q.VALID_SQL}" if has_quality else ""
    if bucket is None:
        return fetch_df(
            f"""
            SELECT sensor_id, reading_time AS t, value::float8 AS value,
                   value::float8 AS vmin, value::float8 AS vmax, {q_col} AS q
            FROM sensor_readings
            WHERE sensor_id = ANY(%s) AND reading_time >= %s AND reading_time < %s
            ORDER BY reading_time
            LIMIT %s;
            """,
            (list(sensor_ids), start, end, RAW_ROW_LIMIT),
        )
    return fetch_df(
        f"""
        SELECT sensor_id, time_bucket(%s::interval, reading_time, %s) AS t,
               avg(value)::float8 AS value, min(value)::float8 AS vmin, max(value)::float8 AS vmax,
               last(value, reading_time)::float8 AS vlast, count(*) AS n
        FROM sensor_readings
        WHERE sensor_id = ANY(%s) AND reading_time >= %s AND reading_time < %s {valid}
        GROUP BY sensor_id, t
        ORDER BY t;
        """,
        (bucket, str(LOCAL_TZ), list(sensor_ids), start, end),
    )


def _events(sensor_ids, start, end) -> pd.DataFrame:
    """通訊中斷 / 品質不良的標記點（聚合模式另外查，原始模式從原始資料裡挑出來）。"""
    return fetch_df(
        f"""
        SELECT sensor_id, reading_time AS t, value::float8 AS value, quality AS q
        FROM sensor_readings
        WHERE sensor_id = ANY(%s) AND reading_time >= %s AND reading_time < %s
          AND quality >= {Q.BAD}
        ORDER BY reading_time LIMIT 5000;
        """,
        (list(sensor_ids), start, end), show_error=False,
    )


def split_segments(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    原始資料拆成（線條資料, 標記點）。標記點（quality >= BAD）不畫在線上，
    而且之後的資料換一個 segment，讓線條在斷線處斷開。
    """
    if "q" not in df or df.empty:
        out = df.assign(seg=df["sensor_id"].astype(str) if not df.empty else [])
        return out, df.iloc[0:0]
    df = df.sort_values(["sensor_id", "t"]).copy()
    is_mark = df["q"] >= Q.BAD
    df["seg"] = df["sensor_id"].astype(str) + "-" + is_mark.groupby(df["sensor_id"]).cumsum().astype(str)
    return df[~is_mark], df[is_mark]


def _count_estimate(sensor_ids, start, end) -> int:
    df = fetch_df(
        "SELECT count(*) AS n FROM sensor_readings "
        "WHERE sensor_id = ANY(%s) AND reading_time >= %s AND reading_time < %s;",
        (list(sensor_ids), start, end), show_error=False,
    )
    return int(df.iloc[0]["n"]) if not df.empty else 0


def _reference_lines(sensor_id: int, catalog_row) -> pd.DataFrame:
    refs = []
    if pd.notnull(catalog_row.get("max_threshold")):
        refs.append(("上限", float(catalog_row["max_threshold"])))
    if pd.notnull(catalog_row.get("min_threshold")):
        refs.append(("下限", float(catalog_row["min_threshold"])))
    if table_exists("alarm_rules"):
        rules = fetch_df(
            "SELECT alarm_type, setpoint::float8 AS sp FROM alarm_rules "
            "WHERE sensor_id = %s AND enabled AND alarm_type IN ('HH','H','L','LL');",
            (int(sensor_id),), show_error=False,
        )
        for _, r in rules.iterrows():
            refs.append((r["alarm_type"], float(r["sp"])))
    return pd.DataFrame(refs, columns=["name", "y"])


def _chart(data: pd.DataFrame, unit: str, interpolate: str, show_band: bool, refs: pd.DataFrame | None,
           events: pd.DataFrame | None = None):
    base = alt.Chart(data).encode(
        x=alt.X("t:T", title=None, axis=alt.Axis(format="%m/%d %H:%M", labelOverlap=True)),
    )
    color = alt.Color("label:N", title=None, legend=alt.Legend(orient="bottom", columns=2, labelLimit=420))
    tooltip = [
        alt.Tooltip("label:N", title="感測器"),
        alt.Tooltip("t:T", title="時間", format="%Y-%m-%d %H:%M:%S"),
        alt.Tooltip("value:Q", title="數值", format=",.4~f"),
    ]
    if "vmin" in data and show_band:
        tooltip += [alt.Tooltip("vmin:Q", title="最小", format=",.4~f"),
                    alt.Tooltip("vmax:Q", title="最大", format=",.4~f")]

    hover = alt.selection_point(fields=["t"], nearest=True, on="pointerover", empty=False, clear="pointerout")
    detail = alt.Detail("seg:N") if "seg" in data else alt.Detail("label:N")
    line = base.mark_line(interpolate=interpolate, strokeWidth=1.6).encode(
        y=alt.Y("value:Q", title=unit or "數值", scale=alt.Scale(zero=False),
                axis=alt.Axis(format=",~f", labelLimit=120)),
        color=color, detail=detail,
    )
    layers = []
    if show_band:
        layers.append(base.mark_area(opacity=0.15, interpolate=interpolate).encode(
            y="vmin:Q", y2="vmax:Q", color=color, detail=detail,
        ))
    layers.append(line)
    layers.append(base.mark_point(size=40, filled=True).encode(
        y="value:Q", color=color, tooltip=tooltip,
        opacity=alt.condition(hover, alt.value(1), alt.value(0)),
    ).add_params(hover))
    layers.append(base.mark_rule(color="gray", strokeDash=[2, 2]).encode(
        opacity=alt.condition(hover, alt.value(0.6), alt.value(0)),
    ).transform_filter(hover))
    if events is not None and not events.empty:
        layers.append(alt.Chart(events).mark_point(
            shape="triangle-down", size=90, filled=True, color="#e5484d",
        ).encode(
            x="t:T", y="value:Q",
            tooltip=[alt.Tooltip("label:N", title="感測器"), alt.Tooltip("狀態:N"),
                     alt.Tooltip("t:T", title="時間", format="%Y-%m-%d %H:%M:%S")],
        ))
    if refs is not None and not refs.empty:
        layers.append(alt.Chart(refs).mark_rule(strokeDash=[6, 4], color="#e5484d", opacity=0.7).encode(
            y="y:Q", tooltip=[alt.Tooltip("name:N", title="設定"), alt.Tooltip("y:Q", title="值")],
        ))
        layers.append(alt.Chart(refs).mark_text(align="left", dx=4, dy=-6, color="#e5484d", fontSize=11).encode(
            y="y:Q", x=alt.value(0), text="name:N",
        ))
    # contains="padding"：讓軸標籤算進圖表寬度內，不然 Y 軸的百萬級數字會被切掉
    return (alt.layer(*layers)
            .properties(height=340, autosize=alt.AutoSizeParams(type="fit-x", contains="padding"))
            .interactive(bind_y=False))


def _stats(df: pd.DataFrame, labels: dict, events: pd.DataFrame | None = None) -> pd.DataFrame:
    rows = []
    event_counts = events["sensor_id"].value_counts() if events is not None and not events.empty else {}
    for sid, g in df.groupby("sensor_id"):
        g = g.sort_values("t")
        last_col = "vlast" if "vlast" in g else "value"
        rows.append({
            "感測器": labels.get(sid, sid),
            "資料點": int(g["n"].sum()) if "n" in g else len(g),
            "最小": g["vmin"].min(),
            "最大": g["vmax"].max(),
            "平均": g["value"].mean(),
            "首筆": g["value"].iloc[0],
            "末筆": g[last_col].iloc[-1],
            "區間變化量": g[last_col].iloc[-1] - g["value"].iloc[0],
            "中斷/不良次數": int(event_counts.get(sid, 0)),
            "最後時間": g["t"].iloc[-1],
        })
    return pd.DataFrame(rows)


def render():
    st.title("📈 歷史趨勢")

    catalog = sensor_catalog()
    if catalog.empty:
        st.info("目前沒有任何感測器。")
        return
    labels = dict(zip(catalog["sensor_id"], catalog["label"]))

    # 網址參數 ?sensors=1,2 → 預設選取
    default_ids = []
    raw = st.query_params.get("sensors")
    if raw:
        for part in str(raw).split(","):
            if part.strip().isdigit() and int(part) in labels:
                default_ids.append(int(part))
    if "trend_sensors" not in st.session_state:
        st.session_state["trend_sensors"] = default_ids
    elif default_ids and st.session_state.get("_trend_qp") != raw:
        st.session_state["trend_sensors"] = default_ids
    st.session_state["_trend_qp"] = raw

    with st.container(border=True):
        c1, c2 = st.columns([1, 3])
        devices = ["全部設備"] + sorted(catalog["device_code"].dropna().unique().tolist())
        device = c1.selectbox("設備篩選", devices, key="trend_device")
        pool = catalog if device == "全部設備" else catalog[catalog["device_code"] == device]
        options = list(dict.fromkeys(pool["sensor_id"].tolist() + st.session_state["trend_sensors"]))
        selected = c2.multiselect(
            "感測器（可多選，最多 8 個）", options, key="trend_sensors", max_selections=8,
            format_func=lambda sid: labels.get(sid, str(sid)), placeholder="輸入關鍵字搜尋…",
        )

        c3, c4, c5, c6 = st.columns([1.3, 1, 1, 1])
        preset = c3.selectbox("時間範圍", list(PRESETS), index=2, key="trend_preset")
        bucket_choice = c4.selectbox("聚合", ["自動"] + list(BUCKETS), key="trend_bucket",
                                     help="自動：依時間跨度挑選，每個感測器約 1500 點以內")
        style = c5.selectbox("線型", ["階梯（建議）", "折線", "平滑"], key="trend_style",
                             help="sensor_readings 是有變化才寫入，兩筆之間數值維持不變，所以階梯線最貼近實際")
        split = c6.toggle("依單位分圖", value=True, key="trend_split")

        now = now_local()
        if PRESETS[preset] is None:
            d1, d2, d3, d4 = st.columns(4)
            sd = d1.date_input("開始日期", now.date() - timedelta(days=1), key="trend_sd")
            stime = d2.time_input("開始時間", dtime(0, 0), key="trend_st")
            ed = d3.date_input("結束日期", now.date(), key="trend_ed")
            etime = d4.time_input("結束時間", now.time().replace(second=0, microsecond=0), key="trend_et")
            start = datetime.combine(sd, stime, tzinfo=LOCAL_TZ)
            end = datetime.combine(ed, etime, tzinfo=LOCAL_TZ)
        else:
            end = now
            start = now - PRESETS[preset]

    if not selected:
        st.info("請先選擇感測器。也可以從「即時總覽」點感測器名稱直接開啟。")
        return
    if start >= end:
        st.error("開始時間必須早於結束時間")
        return

    if bucket_choice == "自動":
        estimate = _count_estimate(selected, start, end) if (end - start) <= timedelta(days=3) else None
        bucket = auto_bucket(end - start, len(selected), estimate)
    else:
        bucket = BUCKETS[bucket_choice]

    has_quality = column_exists("sensor_readings", "quality")
    with st.spinner("查詢中…"):
        df = _query(selected, start, end, bucket, has_quality)

    if df.empty:
        st.warning("這段時間內沒有資料。")
        return
    if bucket is None and len(df) >= RAW_ROW_LIMIT:
        st.warning(f"原始資料超過 {RAW_ROW_LIMIT:,} 筆，只顯示前段；請縮短時間範圍或改用聚合。")

    df["t"] = pd.to_datetime(df["t"])
    unit_map = dict(zip(catalog["sensor_id"], catalog["unit"].fillna("")))
    raw_df = df
    if bucket is None:
        df, events = split_segments(df)
    else:
        events = _events(selected, start, end) if has_quality else pd.DataFrame()
        if not events.empty:
            events["t"] = pd.to_datetime(events["t"])
    df = df.assign(label=df["sensor_id"].map(labels), unit=df["sensor_id"].map(unit_map))
    if not events.empty:
        events = events.assign(
            label=events["sensor_id"].map(labels), unit=events["sensor_id"].map(unit_map),
            狀態=events["q"].map(lambda q: Q.LABELS.get(int(q), q)),
        )
    if df.empty:
        st.warning("這段時間內只有通訊中斷 / 品質不良的紀錄，沒有有效數值。")
        return

    interpolate = {"階梯（建議）": "step-after", "折線": "linear", "平滑": "monotone"}[style]
    show_band = bucket is not None
    st.caption(
        f"{start:%Y-%m-%d %H:%M} ～ {end:%Y-%m-%d %H:%M}｜"
        + (f"每 {bucket} 聚合（線 = 平均，色帶 = 最小～最大）" if bucket else "原始資料")
        + f"｜共 {len(df):,} 點"
        + (f"｜🔻 通訊中斷 / 品質不良 {len(events)} 次" if not events.empty else "")
    )

    refs = None
    if len(selected) == 1:
        row = catalog[catalog["sensor_id"] == selected[0]].iloc[0].to_dict()
        refs = _reference_lines(selected[0], row)

    groups = df.groupby("unit", sort=False) if split else [("", df)]
    for unit, part in groups:
        if split and len(df["unit"].unique()) > 1:
            st.markdown(f"**{unit or '（無單位）'}**")
        part_events = events[events["unit"] == unit] if split and not events.empty else events
        st.altair_chart(_chart(part, unit, interpolate, show_band, refs, part_events), width="stretch")

    st.subheader("統計")
    stats = _stats(df, labels, events)
    st.dataframe(
        stats, hide_index=True, width="stretch",
        column_config={c: st.column_config.NumberColumn(format="%.4g")
                       for c in ["最小", "最大", "平均", "首筆", "末筆", "區間變化量"]},
    )

    if bucket is None:
        export = raw_df.assign(
            感測器=raw_df["sensor_id"].map(labels),
            品質=raw_df["q"].map(lambda q: Q.LABELS.get(int(q), q)),
        ).rename(columns={"t": "時間", "value": "數值"})[["感測器", "時間", "數值", "品質"]]
    else:
        export = df[["label", "t", "value", "vmin", "vmax"]].rename(
            columns={"label": "感測器", "t": "時間", "value": "平均值", "vmin": "最小", "vmax": "最大"}
        )
    c1, c2 = st.columns(2)
    c1.download_button(
        "⬇️ 下載 CSV（長格式）", to_csv_bytes(export),
        file_name=f"trend_{start:%Y%m%d%H%M}_{end:%Y%m%d%H%M}.csv", mime="text/csv",
    )
    wide_src = export[export["品質"].isin([Q.LABELS[Q.GOOD], Q.LABELS[Q.HELD], Q.LABELS[Q.UNCERTAIN]])] \
        if "品質" in export else export
    wide = wide_src.pivot_table(index="時間", columns="感測器",
                              values="平均值" if bucket else "數值", aggfunc="last").reset_index()
    c2.download_button(
        "⬇️ 下載 CSV（寬格式，一欄一個感測器）", to_csv_bytes(wide),
        file_name=f"trend_wide_{start:%Y%m%d%H%M}_{end:%Y%m%d%H%M}.csv", mime="text/csv",
    )
