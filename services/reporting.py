"""
services/reporting.py
=====================
報表計算（不依賴 Streamlit）：網頁「報表匯出」與 main.py 的排程報表共用同一套邏輯，
手動匯出跟自動寄送的數字保證一致。

    query_buckets()   sensor_readings 依粒度聚合（time_bucket，以 SCADA_TIMEZONE 對齊當地日 / 月）
    build_report()    依統計值（平均 / 最小 / 最大 / 期末 / 增量）轉成長格式
    make_report()     產出 (樞紐表, 明細) 兩個 DataFrame
    report_excel()    多個統計值各一個工作表 + 明細，回傳 xlsx bytes
"""

import io

import pandas as pd

from core.config import LOCAL_TZ
from data_layer import quality as Q
from data_layer.db_connector import DatabaseConnector

GRANULARITY = {"每小時": "1 hour", "每日": "1 day", "每月": "1 month"}
GRANULARITY_LABELS = {v: k for k, v in GRANULARITY.items()}
METRICS = {"平均": "avg", "最小": "min", "最大": "max", "期末值": "last", "增量（用量）": "delta"}
METRIC_LABELS = {v: k for k, v in METRICS.items()}
_SUMMARY_NAMES = {"delta": "合計", "avg": "平均", "min": "最小", "max": "最大", "last": "期末"}


def build_report(raw: pd.DataFrame, metric: str) -> pd.DataFrame:
    """
    raw 欄位：sensor_id, bucket, avg, min, max, first, last
    回傳長格式：sensor_id, bucket, value（依 metric 計算）
    增量 = 本期期末值 − 上期期末值（第一期用本期期初值），包含兩期之間的跳動。
    """
    if raw.empty:
        return pd.DataFrame(columns=["sensor_id", "bucket", "value"])
    raw = raw.sort_values(["sensor_id", "bucket"])
    if metric == "delta":
        prev_last = raw.groupby("sensor_id")["last"].shift(1)
        base = prev_last.fillna(raw["first"])
        values = raw["last"] - base
    else:
        values = raw[metric]
    return pd.DataFrame({"sensor_id": raw["sensor_id"], "bucket": raw["bucket"], "value": values})


def _has_quality(cur) -> bool:
    cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name='sensor_readings' AND column_name='quality');")
    return bool(cur.fetchone()[0])


def query_buckets(sensor_ids, start, end, bucket) -> pd.DataFrame:
    with DatabaseConnector.get_connection() as conn:
        with conn.cursor() as cur:
            # 通訊中斷 / 品質不良的標記（sql/014）不是量測值，不納入統計
            valid = f"AND {Q.VALID_SQL}" if _has_quality(cur) else ""
            cur.execute(
                f"""
                SELECT sensor_id,
                       time_bucket(%s::interval, reading_time, %s) AS bucket,
                       avg(value)::float8 AS avg, min(value)::float8 AS min, max(value)::float8 AS max,
                       first(value, reading_time)::float8 AS first, last(value, reading_time)::float8 AS last,
                       count(*) AS n
                FROM sensor_readings
                WHERE sensor_id = ANY(%s) AND reading_time >= %s AND reading_time < %s {valid}
                GROUP BY sensor_id, bucket
                ORDER BY sensor_id, bucket;
                """,
                (bucket, str(LOCAL_TZ), list(sensor_ids), start, end),
            )
            cols = [d[0] for d in cur.description]
            raw = pd.DataFrame(cur.fetchall(), columns=cols)
    if not raw.empty:
        raw["bucket"] = pd.to_datetime(raw["bucket"], utc=True)
    return raw


def bucket_label(ts: pd.Timestamp, bucket: str) -> str:
    ts = ts.tz_convert(LOCAL_TZ)
    return {"1 hour": ts.strftime("%Y-%m-%d %H:00"), "1 day": ts.strftime("%Y-%m-%d"),
            "1 month": ts.strftime("%Y-%m")}[bucket]


def pivot_report(raw: pd.DataFrame, metric: str, labels: dict, sensor_order, bucket: str) -> pd.DataFrame:
    """長格式 → 「時間 × 感測器」樞紐表，最後一列是合計 / 平均 / 最小…。"""
    long = build_report(raw, metric)
    if long.empty:
        return pd.DataFrame(columns=["時間"])
    long["時間"] = long["bucket"].map(lambda t: bucket_label(t, bucket))
    long["感測器"] = long["sensor_id"].map(labels)
    pivot = long.pivot_table(index="時間", columns="感測器", values="value", aggfunc="last")
    pivot = pivot.reindex(columns=[labels[s] for s in sensor_order if labels.get(s) in pivot.columns])
    summary = {
        "delta": pivot.sum(), "avg": pivot.mean(), "min": pivot.min(), "max": pivot.max(),
        "last": pivot.ffill().iloc[-1] if len(pivot) else pivot.sum(),
    }[metric]
    pivot.loc[f"【{_SUMMARY_NAMES[metric]}】"] = summary
    return pivot.reset_index()


def detail_frame(raw: pd.DataFrame, labels: dict, bucket: str) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    return raw.assign(
        時間=raw["bucket"].map(lambda t: bucket_label(t, bucket)),
        感測器=raw["sensor_id"].map(labels),
    )[["時間", "感測器", "avg", "min", "max", "first", "last", "n"]].rename(columns={
        "avg": "平均", "min": "最小", "max": "最大", "first": "期初值", "last": "期末值", "n": "資料筆數",
    })


def sensor_labels(sensor_ids=None, device_codes=None, sensor_codes=None) -> tuple[list, dict]:
    """依 sensor_id / 設備編號 / 感測器編號取得感測器清單與顯示名稱「設備 / 感測器（暱稱）[單位]」。"""
    with DatabaseConnector.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.sensor_id, d.device_code, s.sensor_code, s.nickname, s.unit
                FROM sensors s LEFT JOIN devices d ON d.device_id = s.device_id
                WHERE s.sensor_id = ANY(%s) OR d.device_code = ANY(%s) OR s.sensor_code = ANY(%s)
                ORDER BY d.device_code, s.sensor_code;
                """,
                (list(sensor_ids or []), list(device_codes or []), list(sensor_codes or [])),
            )
            rows = cur.fetchall()
    order, labels = [], {}
    for sid, dev, code, nick, unit in rows:
        label = f"{dev} / {code}" if dev else code
        if nick:
            label += f"（{nick}）"
        if unit:
            label += f" [{unit}]"
        order.append(sid)
        labels[sid] = label
    return order, labels


def report_excel(sheets: dict) -> bytes:
    """{工作表名稱: DataFrame} → xlsx bytes（時間欄位轉當地時間、去掉時區）。"""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            out = frame.copy()
            for col in out.columns:
                if isinstance(out[col].dtype, pd.DatetimeTZDtype):
                    out[col] = out[col].dt.tz_convert(LOCAL_TZ).dt.tz_localize(None)
            out.to_excel(writer, sheet_name=str(name)[:31], index=False)
            ws = writer.sheets[str(name)[:31]]
            for i, col in enumerate(out.columns, start=1):
                width = max([len(str(col))] + [len(str(v)) for v in out[col].head(200)]) + 2
                ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(max(width, 10), 45)
            ws.freeze_panes = "B2"
    return buf.getvalue()


def make_report(sensor_order, labels, start, end, bucket, metrics) -> dict:
    """回傳 {"raw": DataFrame, "sheets": {工作表: DataFrame}}；metrics 為 METRICS 的值，例如 ["avg", "delta"]。"""
    raw = query_buckets(sensor_order, start, end, bucket)
    sheets = {METRIC_LABELS[m].split("（")[0]: pivot_report(raw, m, labels, sensor_order, bucket) for m in metrics}
    sheets["明細"] = detail_frame(raw, labels, bucket)
    return {"raw": raw, "sheets": sheets}
