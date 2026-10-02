"""報表與趨勢的計算邏輯。"""

from datetime import timedelta

import pandas as pd

from web.pages.reports import build_report
from web.pages.trends import auto_bucket


def test_delta_uses_previous_period_last_value():
    raw = pd.DataFrame({
        "sensor_id": [1, 1, 1, 2],
        "bucket": pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-01"]),
        "avg": [0, 0, 0, 0], "min": [0, 0, 0, 0], "max": [0, 0, 0, 0],
        "first": [100.0, 112.0, 130.0, 5.0],
        "last": [110.0, 125.0, 131.0, 9.0],
    })
    out = build_report(raw, "delta")
    s1 = out[out["sensor_id"] == 1]["value"].tolist()
    # 第一期：期末 - 期初；之後：本期期末 - 上期期末（含兩期之間的跳動）
    assert s1 == [10.0, 15.0, 6.0]
    assert out[out["sensor_id"] == 2]["value"].tolist() == [4.0]


def test_plain_metric_passthrough():
    raw = pd.DataFrame({"sensor_id": [1], "bucket": [pd.Timestamp("2026-09-01")],
                        "avg": [3.5], "min": [1.0], "max": [6.0], "first": [1.0], "last": [6.0]})
    assert build_report(raw, "avg")["value"].tolist() == [3.5]
    assert build_report(raw, "max")["value"].tolist() == [6.0]


def test_auto_bucket():
    assert auto_bucket(timedelta(hours=1), 1, raw_estimate=500) is None
    assert auto_bucket(timedelta(hours=24), 1) == "1 minute"
    assert auto_bucket(timedelta(days=7), 1) == "15 minutes"
    assert auto_bucket(timedelta(days=30), 1) == "1 hour"
    assert auto_bucket(timedelta(days=3650), 1) == "1 day"


def test_split_segments_breaks_line_at_markers():
    from web.pages.trends import split_segments
    df = pd.DataFrame({
        "sensor_id": [1, 1, 1, 1, 2],
        "t": pd.to_datetime(["2026-09-01 00:00", "2026-09-01 00:01", "2026-09-01 00:02",
                             "2026-09-01 00:03", "2026-09-01 00:00"]),
        "value": [1.0, 2.0, 2.0, 3.0, 9.0],
        "q": [0, 0, 4, 0, 1],
    })
    line, marks = split_segments(df)
    assert len(marks) == 1 and marks.iloc[0]["q"] == 4
    segs = line[line["sensor_id"] == 1]["seg"].tolist()
    assert segs[0] == segs[1] != segs[2]       # 中斷之後換新的線段
    assert line[line["sensor_id"] == 2]["seg"].nunique() == 1
