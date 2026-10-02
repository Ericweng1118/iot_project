"""
services/status_reporter.py
===========================
採集主程式（main.py）的心跳回報：每 STATUS_REPORT_INTERVAL 秒（預設 10）把
「我還活著 + 各子系統的執行統計」UPSERT 進 service_status（sql/012）。

為什麼需要：網頁後台和採集主程式是兩個獨立的程序，原本網頁完全看不出 main.py 是否
還在跑 —— main.py 掛掉時，即時層資料表停在最後的值，畫面看起來一切正常。
網頁「總覽」與「系統狀態」會用 last_heartbeat 判斷：超過 60 秒沒更新就顯示「停止」。

info 欄位格式（JSONB）：
    {
      "protocols": {"opcua": true, "modbus": false, "tia": false, "mqtt": false},
      "writer":    SensorReadingWriter.get_stats(),
      "opcua":     {server_id: 訂閱服務各 Server 統計},
      "alarm":     AlarmEngine.get_stats(),
      "main_loop": {"cycle": 123, "last_cycle_seconds": 0.8}
    }
"""

import json
import logging
import os
import socket
import threading
from datetime import datetime

from core.config import APP_VERSION, env_float
from data_layer.db_connector import DatabaseConnector

logger = logging.getLogger(__name__)

REPORT_INTERVAL = env_float("STATUS_REPORT_INTERVAL", 10.0)


class StatusReporter:
    def __init__(self, service_name: str = "collector"):
        self.service_name = service_name
        self.started_at = datetime.now().astimezone()
        self._providers = {}
        self._stop = threading.Event()
        self._thread = None
        self._warned = False

    def register(self, name: str, provider):
        """provider：無參數、回傳 JSON 可序列化 dict 的函式。"""
        self._providers[name] = provider

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="status-reporter", daemon=True)
        self._thread.start()

    def stop(self, timeout=5):
        """停止回報，並註記「正常關閉」，讓網頁能分辨正常停機與異常中斷。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE service_status SET info = COALESCE(info, '{}'::jsonb) || "
                        "jsonb_build_object('stopped_at', now()) WHERE service_name = %s;",
                        (self.service_name,),
                    )
        except Exception:
            pass

    def collect(self) -> dict:
        info = {}
        for name, provider in self._providers.items():
            try:
                info[name] = provider()
            except Exception as e:
                info[name] = {"error": str(e)}
        return info

    def report_once(self):
        info = json.dumps(self.collect(), ensure_ascii=False, default=str)
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO service_status
                            (service_name, host, pid, version, started_at, last_heartbeat, info)
                        VALUES (%s, %s, %s, %s, %s, now(), %s)
                        ON CONFLICT (service_name) DO UPDATE SET
                            host = EXCLUDED.host, pid = EXCLUDED.pid, version = EXCLUDED.version,
                            started_at = EXCLUDED.started_at, last_heartbeat = now(),
                            info = EXCLUDED.info;
                        """,
                        (self.service_name, socket.gethostname(), os.getpid(), APP_VERSION,
                         self.started_at, info),
                    )
            self._warned = False
        except Exception as e:
            if not self._warned:
                logger.warning(f"⚠️ 寫入 service_status 失敗（尚未執行 sql/012？）: {e}")
                self._warned = True

    def _loop(self):
        while not self._stop.is_set():
            self.report_once()
            self._stop.wait(REPORT_INTERVAL)
