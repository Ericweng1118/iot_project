"""排程報表的時間計算（services/report_scheduler.py）。"""

from datetime import datetime, time as dtime

from core.config import LOCAL_TZ
from services.report_scheduler import bucket_for, describe, latest_due, next_run, period_for, period_label


def at(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=LOCAL_TZ)


DAILY = {"frequency": "daily", "send_time": dtime(7, 0)}
WEEKLY = {"frequency": "weekly", "send_time": dtime(8, 30), "weekday": 0}      # 週一
MONTHLY = {"frequency": "monthly", "send_time": "06:00", "day_of_month": 1}


def test_next_run():
    assert next_run(DAILY, at(2026, 10, 1, 6, 59)) == at(2026, 10, 1, 7)
    assert next_run(DAILY, at(2026, 10, 1, 7)) == at(2026, 10, 2, 7)          # 嚴格晚於
    assert next_run(WEEKLY, at(2026, 10, 1)) == at(2026, 10, 5, 8, 30)        # 2026-10-01 是週四
    assert next_run(MONTHLY, at(2026, 10, 1, 7)) == at(2026, 11, 1, 6)


def test_periods():
    assert period_for(DAILY, at(2026, 10, 1, 7)) == (at(2026, 9, 30), at(2026, 10, 1))
    assert period_for(WEEKLY, at(2026, 10, 5, 8, 30)) == (at(2026, 9, 28), at(2026, 10, 5))
    assert period_for(MONTHLY, at(2026, 10, 1, 6)) == (at(2026, 9, 1), at(2026, 10, 1))
    assert period_for(MONTHLY, at(2026, 1, 1, 6)) == (at(2025, 12, 1), at(2026, 1, 1))     # 跨年
    assert period_label(at(2026, 9, 30), at(2026, 10, 1)) == "2026-09-30"
    assert period_label(at(2026, 9, 1), at(2026, 10, 1)) == "2026-09-01 ~ 2026-09-30"


def test_latest_due_only_latest_missed_and_not_before_creation():
    created = at(2026, 9, 28, 12)
    # 建立後還沒到第一次執行時間
    assert latest_due(DAILY, None, created, at(2026, 9, 29, 6)) is None
    # 停機錯過 9/29、9/30、10/1 三次：只回傳最近一次
    assert latest_due(DAILY, None, created, at(2026, 10, 1, 9)) == at(2026, 10, 1, 7)
    # 已經寄過 10/1 的就不再寄
    assert latest_due(DAILY, at(2026, 10, 1, 7), created, at(2026, 10, 1, 9)) is None


def test_bucket_and_describe():
    assert bucket_for(DAILY) == "1 hour" and bucket_for(WEEKLY) == "1 day"
    assert bucket_for({**MONTHLY, "granularity": "1 month"}) == "1 month"
    assert describe(WEEKLY) == "每週一 08:30 寄上週"
