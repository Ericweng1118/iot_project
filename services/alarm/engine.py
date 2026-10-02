"""
services/alarm/engine.py
========================
警報引擎：跑在 main.py 的背景執行緒，每 ALARM_EVAL_INTERVAL 秒（預設 5）判斷一次。

資料來源：
    數值警報  直接讀 SensorReadingWriter 的記憶體最新值（snapshot_latest），
              不必等 sensor_readings 的統一寫入週期（預設 60 秒），反應快很多
    通訊警報  opcua_servers.conn_state（OPC UA Server 斷線）、
              modbus_scada / tia_scada 依 plc_ip 分組、整台 PLC 全部點位都不是
              ONLINE 才算一筆（一台 PLC 斷線只發一筆，不會洗出幾十筆點位警報）

警報規則：
    alarm_rules 表（sql/011）+ sensors.min_threshold / max_threshold 的隱含 L / H 規則
    （ALARM_USE_SENSOR_LIMITS=false 可關閉隱含規則）

狀態保存：
    alarm_events 表，「cleared_at IS NULL」= 仍在發生。啟動時把仍在發生的事件讀回來，
    重啟不會重複發出同一個警報。確認（ACK）由網頁直接更新 alarm_events，引擎不需要知道。

刻意不做的事：
    - 來源已經斷線（數值不是最新）的感測器不做數值判斷，維持原狀態，交給通訊警報處理，
      避免斷線時用最後一個舊值反覆觸發 / 恢復
    - 規則讀取失敗的那一輪不做「規則被刪除 → 自動恢復」的清理，避免 DB 抖一下就把所有
      警報誤判成恢復
"""

import json
import logging
import threading
import time
from datetime import datetime

from core.config import LOCAL_TZ, MODBUS_ENABLED, OPCUA_ENABLED, TIA_ENABLED, env_float, env_int, env_bool
from data_layer import quality as Q
from data_layer.db_connector import DatabaseConnector
from data_layer.timeseries_writer import sensor_reading_writer
from services.alarm.notifier import Notifier
from services.alarm.rules import (
    AlarmRule,
    OnDelayTracker,
    build_message,
    condition_active,
    format_value,
    implicit_limit_rules,
)

logger = logging.getLogger("alarm_engine")

EVAL_INTERVAL = env_float("ALARM_EVAL_INTERVAL", 5.0)
RULE_REFRESH_INTERVAL = env_float("ALARM_RULE_REFRESH_INTERVAL", 15.0)
USE_SENSOR_LIMITS = env_bool("ALARM_USE_SENSOR_LIMITS", True)
LIMIT_PRIORITY = env_int("ALARM_LIMIT_PRIORITY", 3)
COMM_PRIORITY = env_int("ALARM_COMM_PRIORITY", 2)
COMM_DELAY_SEC = env_float("ALARM_COMM_DELAY_SEC", 60.0)


def _iso(dt):
    """通知訊息裡的時間一律轉成 SCADA_TIMEZONE，不受資料庫 session 時區影響。"""
    if isinstance(dt, datetime):
        return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return dt


class AlarmEngine:
    def __init__(self, notifier: Notifier | None = None):
        self.notifier = notifier or Notifier()
        self._stop = threading.Event()
        self._thread = None
        self._delay = OnDelayTracker()

        self._rules: list[AlarmRule] = []
        self._sensor_info: dict = {}
        self._rules_loaded_ok = False
        self._last_rule_refresh = 0.0
        self._active_keys: set = set()
        self._available = None   # None=尚未檢查；False=alarm 表不存在
        self._last_availability_check = 0.0

        self.stats = {
            "active_count": 0,
            "rules_count": 0,
            "last_eval_at": None,
            "raised_total": 0,
            "cleared_total": 0,
            "last_error": None,
        }

    # ------------------------------------------------------------------
    # 生命週期
    # ------------------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.notifier.start()
        self._thread = threading.Thread(target=self._loop, name="alarm-engine", daemon=True)
        self._thread.start()
        logger.info(
            f"🚨 警報引擎已啟動：判斷週期 {EVAL_INTERVAL} 秒，"
            f"隱含上下限規則 {'啟用' if USE_SENSOR_LIMITS else '停用'}，"
            f"通訊中斷延遲 {COMM_DELAY_SEC:.0f} 秒"
        )

    def stop(self, timeout=10):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self.notifier.stop()
        logger.info("👋 警報引擎已停止。")

    def get_stats(self):
        stats = dict(self.stats)
        stats["notifier"] = dict(self.notifier.stats)
        stats["notify_channels"] = [name for name, _ in self.notifier.channels()]
        return stats

    def _loop(self):
        while not self._stop.is_set():
            try:
                if self._ensure_available():
                    self.evaluate_once()
            except Exception as e:
                self.stats["last_error"] = str(e)[:300]
                logger.error(f"❌ 警報引擎執行例外: {e}", exc_info=True)
            self._stop.wait(EVAL_INTERVAL)

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def _ensure_available(self) -> bool:
        """alarm 表不存在（sql/011 沒跑）時每 60 秒重試一次，不讓錯誤洗版。"""
        if self._available:
            return True
        if self._available is False and time.monotonic() - self._last_availability_check < 60:
            return False
        self._last_availability_check = time.monotonic()
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT alarm_key FROM alarm_events WHERE cleared_at IS NULL;")
                    self._active_keys = {row[0] for row in cur.fetchall()}
            self._available = True
            logger.info(f"📥 警報引擎載入 {len(self._active_keys)} 筆仍在發生的警報。")
            return True
        except Exception as e:
            if self._available is None:
                logger.warning(f"⚠️ 警報引擎暫停：讀取 alarm_events 失敗（尚未執行 sql/011？）: {e}")
            self._available = False
            return False

    # ------------------------------------------------------------------
    # 規則
    # ------------------------------------------------------------------
    def _refresh_rules(self):
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT s.sensor_id, s.sensor_code, s.nickname, s.unit,
                               s.min_threshold, s.max_threshold, s.state_dictionary,
                               d.device_code
                        FROM sensors s
                        LEFT JOIN devices d ON d.device_id = s.device_id;
                        """
                    )
                    sensor_rows = cur.fetchall()
                    cur.execute(
                        """
                        SELECT rule_id, sensor_id, alarm_type, setpoint, deadband,
                               on_delay_sec, priority, message
                        FROM alarm_rules WHERE enabled;
                        """
                    )
                    rule_rows = cur.fetchall()
        except Exception as e:
            self.stats["last_error"] = f"讀取警報規則失敗: {e}"[:300]
            if self._rules_loaded_ok:
                logger.error(f"❌ 讀取警報規則失敗，沿用上一輪的規則（恢復前不再重複提示）: {e}")
            self._rules_loaded_ok = False
            return
        if not self._rules_loaded_ok and self._rules:
            logger.info("✅ 警報規則恢復讀取")

        sensor_info = {}
        rules = []
        for sid, code, nickname, unit, min_th, max_th, state_dict, device_code in sensor_rows:
            label = f"{device_code} / {code}" if device_code else code
            if nickname:
                label += f"（{nickname}）"
            if isinstance(state_dict, str):
                try:
                    state_dict = json.loads(state_dict)
                except ValueError:
                    state_dict = None
            sensor_info[sid] = {"label": label, "unit": unit, "state_dictionary": state_dict}
            if USE_SENSOR_LIMITS:
                rules.extend(implicit_limit_rules(sid, min_th, max_th, LIMIT_PRIORITY))

        for rule_id, sid, alarm_type, setpoint, deadband, delay, priority, message in rule_rows:
            rules.append(AlarmRule(
                key=f"rule:{rule_id}", source_type="sensor_rule", sensor_id=sid,
                alarm_type=alarm_type, setpoint=float(setpoint),
                deadband=float(deadband or 0), on_delay_sec=float(delay or 0),
                priority=int(priority), message=message, rule_id=rule_id,
            ))

        self._sensor_info = sensor_info
        self._rules = rules
        self._rules_loaded_ok = True
        self.stats["rules_count"] = len(rules)

    # ------------------------------------------------------------------
    # 一輪判斷
    # ------------------------------------------------------------------
    def evaluate_once(self):
        mono_now = time.monotonic()
        if mono_now - self._last_rule_refresh >= RULE_REFRESH_INTERVAL or not self._rules_loaded_ok:
            self._refresh_rules()
            self._last_rule_refresh = mono_now

        wall_now = datetime.now().astimezone()
        latest = sensor_reading_writer.snapshot_latest()
        alive_window = sensor_reading_writer.alive_window
        expected_keys = set()

        # ---- 1. 數值警報 ----
        for rule in self._rules:
            expected_keys.add(rule.key)
            sample = latest.get(rule.sensor_id)
            if sample is None:
                continue
            value, _sample_time, alive_at, quality = sample
            if quality >= Q.BAD:
                continue  # 品質不良 / 通訊中斷的值不可信，維持原狀態
            if alive_at is None or (wall_now - alive_at).total_seconds() > alive_window:
                continue  # 來源已斷線，數值不是最新的，交給通訊警報

            active = rule.key in self._active_keys
            cond = condition_active(rule.alarm_type, rule.setpoint, rule.deadband, value, active)
            info = self._sensor_info.get(rule.sensor_id, {"label": f"sensor {rule.sensor_id}"})
            if active:
                if not cond:
                    self._clear(rule.key, value)
            elif self._delay.update(rule.key, cond, rule.on_delay_sec, mono_now):
                message = build_message(
                    rule, info["label"], value, info.get("unit"), info.get("state_dictionary")
                )
                self._raise({
                    "alarm_key": rule.key,
                    "source_type": rule.source_type,
                    "sensor_id": rule.sensor_id,
                    "server_id": None,
                    "rule_id": rule.rule_id,
                    "alarm_type": rule.alarm_type,
                    "priority": rule.priority,
                    "message": message,
                    "trigger_value": value,
                    "setpoint": rule.setpoint,
                })

        # ---- 2. 通訊警報 ----
        comm_ok, comm_conditions = self._load_comm_conditions()
        for cond in comm_conditions:
            key = cond["alarm_key"]
            expected_keys.add(key)
            active = key in self._active_keys
            if active:
                if not cond["offline"]:
                    self._clear(key, None)
            elif self._delay.update(key, cond["offline"], COMM_DELAY_SEC, mono_now):
                self._raise(cond["event"])

        # ---- 3. 規則已刪除 / 停用、Server 已移除的警報：自動恢復 ----
        if self._rules_loaded_ok and comm_ok:
            for key in list(self._active_keys - expected_keys):
                logger.info(f"ℹ️ 警報 {key} 的規則已移除或停用，自動恢復")
                self._clear(key, None)

        self.stats["active_count"] = len(self._active_keys)
        self.stats["last_eval_at"] = _iso(wall_now)
        if self._rules_loaded_ok and comm_ok:
            self.stats["last_error"] = None   # 已恢復：網頁不要一直顯示過時的錯誤

    def _load_comm_conditions(self):
        """回傳 (是否成功讀取, [ {alarm_key, offline, event} ])"""
        conditions = []
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    if OPCUA_ENABLED:
                        cur.execute(
                            "SELECT id, server_name, ip, port, conn_state, last_error "
                            "FROM opcua_servers WHERE enabled = TRUE;"
                        )
                        for sid, name, ip, port, state, last_error in cur.fetchall():
                            offline = state in ("OFFLINE", "ERROR")
                            msg = f"OPC UA Server [{name}]（{ip}:{port}）通訊中斷"
                            if last_error:
                                msg += f"：{str(last_error)[:150]}"
                            conditions.append({
                                "alarm_key": f"server:{sid}:offline",
                                "offline": offline,
                                "event": {
                                    "alarm_key": f"server:{sid}:offline",
                                    "source_type": "server_offline",
                                    "sensor_id": None, "server_id": sid, "rule_id": None,
                                    "alarm_type": "OFFLINE", "priority": COMM_PRIORITY,
                                    "message": msg, "trigger_value": None, "setpoint": None,
                                },
                            })
                    for enabled, table, label in (
                        (MODBUS_ENABLED, "modbus_scada", "Modbus"),
                        (TIA_ENABLED, "tia_scada", "TIA/S7"),
                    ):
                        if not enabled:
                            continue
                        cur.execute(
                            f"SELECT plc_ip, bool_or(plc_state = 'ONLINE'), count(*) "
                            f"FROM {table} GROUP BY plc_ip;"
                        )
                        for ip, any_online, n in cur.fetchall():
                            key = f"plc:{table}:{ip}:offline"
                            conditions.append({
                                "alarm_key": key,
                                "offline": not any_online,
                                "event": {
                                    "alarm_key": key, "source_type": "device_offline",
                                    "sensor_id": None, "server_id": None, "rule_id": None,
                                    "alarm_type": "OFFLINE", "priority": COMM_PRIORITY,
                                    "message": f"{label} 設備 {ip} 通訊中斷（{n} 個點位皆無法讀取）",
                                    "trigger_value": None, "setpoint": None,
                                },
                            })
            return True, conditions
        except Exception as e:
            logger.debug(f"讀取通訊狀態失敗: {e}")
            return False, conditions

    # ------------------------------------------------------------------
    # 寫入 alarm_events + 通知
    # ------------------------------------------------------------------
    def _raise(self, event: dict):
        key = event["alarm_key"]
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO alarm_events
                            (alarm_key, source_type, sensor_id, server_id, rule_id, alarm_type,
                             priority, message, trigger_value, setpoint)
                        VALUES (%(alarm_key)s, %(source_type)s, %(sensor_id)s, %(server_id)s,
                                %(rule_id)s, %(alarm_type)s, %(priority)s, %(message)s,
                                %(trigger_value)s, %(setpoint)s)
                        ON CONFLICT (alarm_key) WHERE cleared_at IS NULL DO NOTHING
                        RETURNING event_id, raised_at;
                        """,
                        event,
                    )
                    row = cur.fetchone()
        except Exception as e:
            self.stats["last_error"] = f"寫入警報失敗: {e}"[:300]
            logger.error(f"❌ 寫入警報事件失敗 ({key}): {e}")
            return

        self._active_keys.add(key)
        self._delay.reset(key)
        if row is None:
            return  # 已經有一筆仍在發生的同 key 事件（例如另一個實例寫入的）
        self.stats["raised_total"] += 1
        logger.warning(f"🚨 [警報發生] {event['message']}")
        self.notifier.notify("raised", {**event, "event_id": row[0], "raised_at": _iso(row[1])})

    def _clear(self, key: str, value):
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE alarm_events
                        SET cleared_at = now(), clear_value = %s
                        WHERE alarm_key = %s AND cleared_at IS NULL
                        RETURNING event_id, priority, message, raised_at, cleared_at, sensor_id;
                        """,
                        (value, key),
                    )
                    rows = cur.fetchall()
        except Exception as e:
            self.stats["last_error"] = f"更新警報恢復失敗: {e}"[:300]
            logger.error(f"❌ 更新警報恢復失敗 ({key}): {e}")
            return

        self._active_keys.discard(key)
        self._delay.reset(key)
        for event_id, priority, message, raised_at, cleared_at, sensor_id in rows:
            self.stats["cleared_total"] += 1
            logger.info(f"✅ [警報恢復] {message}")
            clear_text = None
            if value is not None:
                info = self._sensor_info.get(sensor_id, {})
                clear_text = format_value(value, info.get("unit"), info.get("state_dictionary"))
            self.notifier.notify("cleared", {
                "alarm_key": key, "event_id": event_id, "priority": priority,
                "message": message, "raised_at": _iso(raised_at), "cleared_at": _iso(cleared_at),
                "clear_value": value, "clear_value_text": clear_text,
            })
