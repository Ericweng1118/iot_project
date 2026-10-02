"""
services/report_scheduler.py
============================
排程報表（sql/018）：main.py 內的背景執行緒，每分鐘檢查一次，到時間就產生上一期的 Excel
報表寄出（Email / Webhook，設定沿用警報通知的 SMTP / ALARM_WEBHOOK_URLS）。

時間規則（全部以 SCADA_TIMEZONE 當地時間計算）：
    daily    每天 send_time 寄「前一天 00:00 ~ 24:00」
    weekly   每週 weekday 的 send_time 寄「上週一 00:00 ~ 本週一 00:00」
    monthly  每月 day_of_month 的 send_time 寄「上個月 1 日 ~ 本月 1 日」

錯過的排程（採集服務停機）：恢復後只補寄最近一期，不會把錯過的每一期都補寄。
寄送失敗：每 10 分鐘重試，最多 6 次，之後放棄這一期並把錯誤記在 last_error。

時間計算是純函式（next_run / latest_due / period_for），單元測試見 tests/test_report_scheduler.py。
"""

import logging
import threading
from datetime import datetime, time as dtime, timedelta

from core.config import LOCAL_TZ, env_bool
from data_layer.db_connector import DatabaseConnector

logger = logging.getLogger("report_scheduler")

CHECK_INTERVAL = 60
RETRY_INTERVAL = timedelta(minutes=10)
MAX_ATTEMPTS = 6
FREQUENCY_LABELS = {"daily": "每日", "weekly": "每週", "monthly": "每月"}
WEEKDAY_LABELS = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


# ------------------------------------------------------------------
# 時間計算（純函式）
# ------------------------------------------------------------------
def _send_time(schedule) -> dtime:
    t = schedule["send_time"]
    if isinstance(t, str):
        h, m = t.split(":")[:2]
        return dtime(int(h), int(m))
    return t


def _occurrence_on(day, schedule) -> datetime | None:
    """某一天是否是排程日；是的話回傳那天的執行時間。"""
    freq = schedule["frequency"]
    if freq == "weekly" and day.weekday() != int(schedule.get("weekday") or 0):
        return None
    if freq == "monthly" and day.day != int(schedule.get("day_of_month") or 1):
        return None
    return datetime.combine(day, _send_time(schedule), tzinfo=LOCAL_TZ)


def next_run(schedule, after: datetime) -> datetime:
    """嚴格晚於 after 的下一次執行時間。"""
    day = after.astimezone(LOCAL_TZ).date()
    for _ in range(400):
        occ = _occurrence_on(day, schedule)
        if occ is not None and occ > after:
            return occ
        day += timedelta(days=1)
    raise ValueError("找不到下一次執行時間（排程設定有誤？）")


def latest_due(schedule, last_run: datetime | None, created_at: datetime, now: datetime) -> datetime | None:
    """
    最近一次「應該執行、但還沒執行」的時間；沒有則回傳 None。
    建立之前的時間點不算（新建的每日報表不會立刻寄出昨天的報表）。
    """
    anchor = last_run or created_at
    due = None
    t = next_run(schedule, anchor)
    while t <= now:
        due = t
        t = next_run(schedule, t)
    return due


def period_for(schedule, run_at: datetime) -> tuple[datetime, datetime]:
    """這次執行要報告的期間 [start, end)。"""
    run_day = run_at.astimezone(LOCAL_TZ).date()
    midnight = datetime.combine(run_day, dtime(0, 0), tzinfo=LOCAL_TZ)
    freq = schedule["frequency"]
    if freq == "daily":
        return midnight - timedelta(days=1), midnight
    if freq == "weekly":
        this_monday = midnight - timedelta(days=run_day.weekday())
        return this_monday - timedelta(days=7), this_monday
    first = midnight.replace(day=1)
    prev_first = (first - timedelta(days=1)).replace(day=1)
    return prev_first, first


def bucket_for(schedule) -> str:
    gran = schedule.get("granularity") or "auto"
    if gran != "auto":
        return gran
    return "1 hour" if schedule["frequency"] == "daily" else "1 day"


def period_label(start: datetime, end: datetime) -> str:
    last_day = (end - timedelta(days=1)).date()
    if start.date() == last_day:
        return f"{start:%Y-%m-%d}"
    return f"{start:%Y-%m-%d} ~ {last_day:%Y-%m-%d}"


def describe(schedule) -> str:
    t = _send_time(schedule).strftime("%H:%M")
    freq = schedule["frequency"]
    if freq == "daily":
        return f"每天 {t} 寄前一天"
    if freq == "weekly":
        return f"每{WEEKDAY_LABELS[int(schedule.get('weekday') or 0)]} {t} 寄上週"
    return f"每月 {int(schedule.get('day_of_month') or 1)} 日 {t} 寄上個月"


# ------------------------------------------------------------------
# 產生與寄送
# ------------------------------------------------------------------
def build_schedule_report(schedule, run_at: datetime):
    """回傳 (檔名, xlsx bytes, 期間文字, 摘要 dict, {工作表: DataFrame})；沒有任何感測器時丟 ValueError。"""
    from services.reporting import make_report, report_excel, sensor_labels

    start, end = period_for(schedule, run_at)
    order, labels = sensor_labels(device_codes=schedule.get("device_codes"),
                                  sensor_codes=schedule.get("sensor_codes"))
    if not order:
        raise ValueError("報表範圍內沒有任何感測器（設備或感測器已被刪除？）")
    bucket = bucket_for(schedule)
    metrics = list(schedule.get("metrics") or ["avg"])
    result = make_report(order, labels, start, end, bucket, metrics)
    label = period_label(start, end)
    filename = f"{schedule['name']}_{label.replace(' ~ ', '_')}.xlsx"
    summary = {"name": schedule["name"], "period": label, "sensors": len(order),
               "rows": int(len(result["raw"])), "metrics": metrics}
    return filename, report_excel(result["sheets"]), label, summary, result["sheets"]


def run_schedule(schedule, run_at: datetime, notifier=None) -> tuple[bool, str, str]:
    """產生並寄送一次；回傳 (成功與否, 訊息, 期間文字)。網頁「立即寄送」也呼叫這支。"""
    from services.alarm.notifier import Notifier

    notifier = notifier or Notifier()
    filename, data, label, summary, sheets = build_schedule_report(schedule, run_at)
    subject = f"[{notifier.site_name}] 報表：{schedule['name']}（{label}）"
    body = (f"{schedule['name']}\n期間：{label}\n感測器：{summary['sensors']} 個\n"
            f"內容：{'、'.join(summary['metrics'])}\n\n此信由 IIoT SCADA 排程報表自動寄出。")
    results = notifier.send_report(
        subject, body, filename, data,
        recipients=list(schedule.get("recipients") or []),
        email=bool(schedule.get("send_email", True)),
        webhook=bool(schedule.get("send_webhook")),
        summary={**summary, "sheets": {k: v.head(500).to_dict("records") for k, v in sheets.items() if k != "明細"}},
    )
    if not results:
        return False, "沒有可用的寄送方式（請設定 SMTP / 收件人或 Webhook）", label
    failed = [f"{ch}：{err}" for ch, ok, err in results if not ok]
    if failed:
        return False, "；".join(failed), label
    return True, "、".join(ch for ch, _, _ in results), label


class ReportScheduler:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
        self._attempts = {}        # (schedule_id, due) -> (次數, 下次重試時間)
        self._available = None
        self.stats = {"schedules": 0, "sent_total": 0, "failed_total": 0, "last_check_at": None, "last_error": None}

    def start(self):
        if not env_bool("REPORT_SCHEDULER_ENABLED", True):
            logger.info("🔕 REPORT_SCHEDULER_ENABLED=false，排程報表停用。")
            return
        self._thread = threading.Thread(target=self._loop, name="report-scheduler", daemon=True)
        self._thread.start()
        logger.info("📅 排程報表已啟動（每分鐘檢查一次）")

    def stop(self, timeout=5):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def get_stats(self):
        return dict(self.stats)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as e:
                self.stats["last_error"] = str(e)[:300]
                logger.error(f"❌ 排程報表檢查失敗: {e}", exc_info=True)
            self._stop.wait(CHECK_INTERVAL)

    def _load(self):
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('public.report_schedules') IS NOT NULL;")
                    if not cur.fetchone()[0]:
                        return None
                    cur.execute("SELECT * FROM report_schedules WHERE enabled;")
                    cols = [d[0] for d in cur.description]
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
        except Exception as e:
            logger.debug(f"讀取排程報表失敗: {e}")
            return []

    def check_once(self, now: datetime | None = None):
        schedules = self._load()
        if schedules is None:
            if self._available is None:
                logger.info("ℹ️ 尚未執行 sql/018，排程報表待命中。")
            self._available = False
            return
        self._available = True
        now = now or datetime.now(LOCAL_TZ)
        self.stats.update(schedules=len(schedules), last_check_at=now.isoformat(timespec="seconds"))
        for s in schedules:
            due = latest_due(s, s.get("last_run_at"), s["created_at"], now)
            if due is None:
                continue
            key = (s["schedule_id"], due)
            attempts, retry_at = self._attempts.get(key, (0, None))
            if retry_at and now < retry_at:
                continue
            try:
                ok, message, label = run_schedule(s, due)
            except Exception as e:
                ok, message, label = False, str(e), period_label(*period_for(s, due))
            attempts += 1
            if ok:
                self.stats["sent_total"] += 1
                logger.info(f"📧 排程報表「{s['name']}」（{label}）已寄出：{message}")
                self._mark(s["schedule_id"], due, label, "OK", None)
                self._attempts.pop(key, None)
            elif attempts >= MAX_ATTEMPTS:
                self.stats["failed_total"] += 1
                logger.error(f"❌ 排程報表「{s['name']}」（{label}）重試 {attempts} 次仍失敗，放棄這一期：{message}")
                self._mark(s["schedule_id"], due, label, "ERROR", message)
                self._attempts.pop(key, None)
            else:
                logger.warning(f"⚠️ 排程報表「{s['name']}」（{label}）寄送失敗（第 {attempts} 次），10 分鐘後重試：{message}")
                self._attempts[key] = (attempts, now + RETRY_INTERVAL)
                self._mark_error_only(s["schedule_id"], message)

    def _mark(self, schedule_id, due, label, status, error):
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE report_schedules SET last_run_at=%s, last_period=%s, last_status=%s, "
                            "last_error=%s WHERE schedule_id=%s;", (due, label, status, error, schedule_id))

    def _mark_error_only(self, schedule_id, error):
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE report_schedules SET last_status='RETRYING', last_error=%s "
                                "WHERE schedule_id=%s;", (str(error)[:500], schedule_id))
        except Exception:
            pass
