"""
services/alarm/notifier.py
==========================
警報通知：把警報發生 / 恢復送到外部頻道。支援三種，全部由 .env 設定，沒設定就不啟用：

    Webhook   ALARM_WEBHOOK_URLS（逗號分隔可多個）
              POST JSON：{"event", "text", "content", "alarm": {...}}
              text / content 是給 Slack / Mattermost / Discord / n8n 直接顯示用的純文字
    Email     SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_STARTTLS
              ALARM_EMAIL_FROM / ALARM_EMAIL_TO（逗號分隔）
    Telegram  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID

只通知優先等級 <= ALARM_NOTIFY_MIN_PRIORITY 的警報（預設 3：緊急/高/中，「低」不通知）。
ALARM_NOTIFY_ON_CLEAR=true（預設）時，警報恢復也會通知一次。

發送在背景執行緒進行（佇列），網路慢或對方掛掉不會拖慢警報引擎；每則訊息失敗會
重試 3 次（間隔 2、4 秒），仍失敗就記 log 放棄。
"""

import json
import logging
import queue
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage

import requests

from core.config import env_bool, env_int, env_list, env_str
from services.alarm.rules import PRIORITY_LABELS

logger = logging.getLogger(__name__)

_EVENT_LABELS = {"raised": "🚨 警報發生", "cleared": "✅ 警報恢復", "test": "🧪 測試通知"}


class Notifier:
    def __init__(self):
        self.webhook_urls = env_list("ALARM_WEBHOOK_URLS")
        self.smtp_host = env_str("SMTP_HOST")
        self.smtp_port = env_int("SMTP_PORT", 587)
        self.smtp_user = env_str("SMTP_USER")
        self.smtp_password = env_str("SMTP_PASSWORD")
        self.smtp_starttls = env_bool("SMTP_STARTTLS", True)
        self.email_from = env_str("ALARM_EMAIL_FROM") or self.smtp_user
        self.email_to = env_list("ALARM_EMAIL_TO")
        self.telegram_token = env_str("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = env_str("TELEGRAM_CHAT_ID")
        self.min_priority = env_int("ALARM_NOTIFY_MIN_PRIORITY", 3)
        self.notify_on_clear = env_bool("ALARM_NOTIFY_ON_CLEAR", True)
        self.site_name = env_str("SCADA_SITE_NAME", "IIoT SCADA")

        self._queue: queue.Queue = queue.Queue(maxsize=1000)
        self._thread = None
        self._stop = threading.Event()
        self.stats = {"sent": 0, "failed": 0, "dropped": 0, "last_error": None}

    # ------------------------------------------------------------------
    # 頻道
    # ------------------------------------------------------------------
    def channels(self):
        result = []
        if self.webhook_urls:
            result.append(("webhook", self._send_webhook))
        if self.smtp_host and self.email_to:
            result.append(("email", self._send_email))
        if self.telegram_token and self.telegram_chat_id:
            result.append(("telegram", self._send_telegram))
        return result

    def describe_channels(self):
        """給網頁顯示目前啟用了哪些頻道（不含任何密碼）。"""
        return {
            "webhook": f"{len(self.webhook_urls)} 個 URL" if self.webhook_urls else None,
            "email": (
                f"{self.smtp_host}:{self.smtp_port} → {', '.join(self.email_to)}"
                if self.smtp_host and self.email_to else None
            ),
            "telegram": f"chat_id={self.telegram_chat_id}"
            if self.telegram_token and self.telegram_chat_id else None,
            "min_priority": f"{self.min_priority}（{PRIORITY_LABELS.get(self.min_priority, '?')}）以上",
            "notify_on_clear": self.notify_on_clear,
        }

    @property
    def enabled(self) -> bool:
        return bool(self.channels())

    # ------------------------------------------------------------------
    # 對外介面
    # ------------------------------------------------------------------
    def notify(self, event: str, alarm: dict) -> None:
        """event: raised / cleared。alarm 至少要有 priority、message。"""
        if not self.enabled:
            return
        if event == "cleared" and not self.notify_on_clear:
            return
        if int(alarm.get("priority") or 4) > self.min_priority:
            return
        try:
            self._queue.put_nowait((event, alarm))
        except queue.Full:
            self.stats["dropped"] += 1
            logger.error("❌ 警報通知佇列已滿，丟棄一則通知（通知頻道可能長時間無法連線）")

    def send_test(self, username: str = "") -> list:
        """同步發送一則測試訊息到所有頻道，回傳 [(頻道, 成功與否, 錯誤訊息)]，給網頁按鈕用。"""
        alarm = {
            "priority": 4,
            "message": f"這是一則測試通知（由 {username or '系統'} 從網頁發送），收到代表通知設定正確。",
        }
        results = []
        for name, send in self.channels():
            subject, text = self._format("test", alarm)
            try:
                send(subject, text, "test", alarm)
                results.append((name, True, None))
            except Exception as e:
                results.append((name, False, str(e)))
        return results

    def send_report(self, subject, body, filename, data: bytes, recipients=None, email=True,
                    webhook=False, summary=None) -> list:
        """
        排程報表用（services/report_scheduler.py）：同步寄送，回傳 [(頻道, 成功與否, 錯誤)]。
        Email 夾帶 Excel；Webhook 送 JSON 摘要（樞紐表前 500 列），檔案小於 2 MB 時一併附上 base64。
        收件人空白時用 ALARM_EMAIL_TO。
        """
        import base64

        results = []
        to = [r for r in (recipients or []) if r] or self.email_to
        if email and self.smtp_host and to:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = self.email_from or self.smtp_user
            msg["To"] = ", ".join(to)
            msg.set_content(body)
            msg.add_attachment(data, maintype="application",
                               subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=filename)
            try:
                self._smtp_send(msg)
                results.append(("email", True, None))
            except Exception as e:
                results.append(("email", False, str(e)))
        elif email and not webhook:
            results.append(("email", False, "未設定 SMTP_HOST 或收件人"))
        if webhook and self.webhook_urls:
            payload = {"event": "report", "text": f"{subject}\n{body}", "content": f"{subject}\n{body}",
                       "report": summary or {}, "filename": filename}
            if len(data) < 2 * 1024 * 1024:
                payload["xlsx_base64"] = base64.b64encode(data).decode("ascii")
            try:
                raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                for url in self.webhook_urls:
                    requests.post(url, data=raw, timeout=30,
                                  headers={"Content-Type": "application/json; charset=utf-8"}).raise_for_status()
                results.append(("webhook", True, None))
            except Exception as e:
                results.append(("webhook", False, str(e)))
        elif webhook:
            results.append(("webhook", False, "未設定 ALARM_WEBHOOK_URLS"))
        return results

    def start(self):
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="alarm-notifier", daemon=True)
        self._thread.start()
        logger.info(f"📣 警報通知已啟用，頻道：{', '.join(n for n, _ in self.channels())}")

    def stop(self, timeout=5):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    # ------------------------------------------------------------------
    # 內部
    # ------------------------------------------------------------------
    def _format(self, event: str, alarm: dict):
        priority = int(alarm.get("priority") or 4)
        label = _EVENT_LABELS.get(event, event)
        subject = f"[{self.site_name}] {label}【{PRIORITY_LABELS.get(priority, priority)}】"
        message = alarm.get("message", "")
        lines = [subject, f"原警報：{message}" if event == "cleared" else message]
        if event == "cleared" and alarm.get("clear_value_text"):
            lines.append(f"恢復時數值：{alarm['clear_value_text']}")
        if alarm.get("raised_at"):
            lines.append(f"發生時間：{alarm['raised_at']}")
        if event == "cleared" and alarm.get("cleared_at"):
            lines.append(f"恢復時間：{alarm['cleared_at']}")
        return subject, "\n".join(line for line in lines if line)

    def _loop(self):
        while not self._stop.is_set():
            try:
                event, alarm = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            subject, text = self._format(event, alarm)
            for name, send in self.channels():
                for attempt in range(3):
                    try:
                        send(subject, text, event, alarm)
                        self.stats["sent"] += 1
                        break
                    except Exception as e:
                        if attempt == 2:
                            self.stats["failed"] += 1
                            self.stats["last_error"] = f"{name}: {e}"
                            logger.error(f"❌ 警報通知發送失敗（{name}，已重試 3 次）: {e}")
                        else:
                            time.sleep(2 * (attempt + 1))

    def _send_webhook(self, subject, text, event, alarm):
        payload = {
            "event": event,
            "text": text,       # Slack / Mattermost / n8n
            "content": text,    # Discord
            "alarm": alarm,
        }
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        for url in self.webhook_urls:
            resp = requests.post(
                url, data=body, timeout=10,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            resp.raise_for_status()

    def _send_email(self, subject, text, event, alarm):
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.email_from
        msg["To"] = ", ".join(self.email_to)
        msg.set_content(text)
        self._smtp_send(msg)

    def _smtp_send(self, msg):
        if self.smtp_port == 465:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=15,
                                  context=ssl.create_default_context()) as smtp:
                if self.smtp_user:
                    smtp.login(self.smtp_user, self.smtp_password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as smtp:
                if self.smtp_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if self.smtp_user:
                    smtp.login(self.smtp_user, self.smtp_password)
                smtp.send_message(msg)

    def _send_telegram(self, subject, text, event, alarm):
        resp = requests.post(
            f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
            json={"chat_id": self.telegram_chat_id, "text": text},
            timeout=10,
        )
        resp.raise_for_status()
