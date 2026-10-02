"""
services/calc/engine.py
=======================
計算引擎：main.py 內的背景執行緒，每 CALC_INTERVAL 秒（預設 5）計算一次所有計算點。

    輸入   SensorReadingWriter 的記憶體最新值（跨 OPC UA / Modbus / TIA，不必等寫入資料庫）
    輸出   writer.update_latest(結果感測器, 值) —— 之後的寫入、警報、趨勢都跟實體感測器一樣
    狀態   回寫 calculated_points 的 current_value / state / last_error，給網頁顯示

規則：
    - 任何一個輸入沒有資料、通訊中斷、品質不良、或超過 SENSOR_ALIVE_WINDOW 沒更新 →
      這一輪不算，結果感測器標記為「來源斷線」（寫一筆通訊中斷標記），不會用舊值硬算
      （例外：運算式用 isgood / valueor / coalesce 自己處理斷線的輸入）
    - 🆕 v3.4 Python 腳本計算點（kind = python，sql/021）：在獨立子程序執行（services/calc/script_runner.py），
      有逾時與記憶體上限；需要 CALC_SCRIPTS_ENABLED=true
    - 🆕 v3.3 累計 / 動態函式（integral、delta、ontime、count…）的狀態存進
      calculated_points.calc_state（sql/020），重啟後接續；delta 的週期基準值從 sensor_readings 查
    - 輸入有「不確定」品質時，結果也標記為不確定
    - 計算點可以引用其他計算點，依相依順序計算；循環引用的計算點標記 ERROR
    - 運算式錯誤（除以零等）只影響該計算點
"""

import json
import logging
import threading
import time
from datetime import datetime, timedelta

from core.config import LOCAL_TZ, env_bool, env_float
from data_layer import quality as Q
from data_layer.db_connector import DatabaseConnector
from data_layer.timeseries_writer import sensor_reading_writer
from services.calc.expression import (
    MISSING,
    EvalContext,
    Expression,
    ExpressionError,
    MissingInput,
    evaluation_order,
    persistable_state,
)

logger = logging.getLogger("calc_engine")

CALC_INTERVAL = env_float("CALC_INTERVAL", 5.0)
SCRIPTS_ENABLED = env_bool("CALC_SCRIPTS_ENABLED", False)


class PyScript:
    """Python 腳本計算點（程式碼存在 calculated_points.expression）。"""

    def __init__(self, source: str):
        import hashlib
        from services.calc.script_runner import script_refs
        self.source = source
        self.refs = script_refs(source)
        self.signature = hashlib.sha1(source.encode()).hexdigest()[:12]
REFRESH_INTERVAL = env_float("CALC_REFRESH_INTERVAL", 15.0)


class CalcEngine:
    def __init__(self, writer=None):
        self.writer = writer or sensor_reading_writer
        self._stop = threading.Event()
        self._thread = None
        self._defs = []              # [(calc_id, target_sensor_id, target_code, Expression | ExpressionError)]
        self._order = []
        self._cyclic = set()
        self._code_to_id = {}
        self._last_refresh = 0.0
        self._available = None
        self._warned = set()
        self._states = {}            # calc_id -> 跨輪狀態（累計函式用）
        self._has_state_column = False
        self._baseline_cache = {}
        self._has_kind_column = False
        self._runner = None
        self._logs = {}               # calc_id -> 最後一輪腳本 log（None = 不是腳本）
        self._cycle_inputs = None     # 一輪內共用的腳本輸入（所有有效感測器）
        self._backoff = {}            # calc_id -> (下次可重試的 monotonic 時間, 目前退避秒數)
        self.stats = {"points": 0, "ok": 0, "offline": 0, "error": 0, "last_cycle_at": None, "last_error": None}

    # ------------------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="calc-engine", daemon=True)
        self._thread.start()
        logger.info(f"🧮 計算引擎已啟動，週期 {CALC_INTERVAL} 秒")

    def stop(self, timeout=10):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        if self._runner is not None:
            self._runner.stop()

    def get_stats(self):
        return dict(self.stats)

    def _loop(self):
        while not self._stop.is_set():
            try:
                if time.monotonic() - self._last_refresh >= REFRESH_INTERVAL or self._available is None:
                    self._refresh()
                if self._available:
                    self.run_once()
            except Exception as e:
                self.stats["last_error"] = str(e)[:300]
                logger.error(f"❌ 計算引擎執行例外: {e}", exc_info=True)
            self._stop.wait(CALC_INTERVAL)

    # ------------------------------------------------------------------
    def _refresh(self):
        self._last_refresh = time.monotonic()
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('public.calculated_points') IS NOT NULL;")
                    if not cur.fetchone()[0]:
                        if self._available is None:
                            logger.info("ℹ️ 尚未執行 sql/017，計算引擎待命中。")
                        self._available = False
                        return
                    cur.execute("SELECT sensor_code, sensor_id FROM sensors;")
                    code_to_id = dict(cur.fetchall())
                    cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                                "WHERE table_name='calculated_points' AND column_name='calc_state');")
                    self._has_state_column = bool(cur.fetchone()[0])
                    state_col = "c.calc_state" if self._has_state_column else "NULL"
                    cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                                "WHERE table_name='calculated_points' AND column_name='kind');")
                    self._has_kind_column = bool(cur.fetchone()[0])
                    kind_col = "c.kind" if self._has_kind_column else "'expression'"
                    cur.execute(f"""
                        SELECT c.calc_id, c.sensor_id, s.sensor_code, c.expression, {state_col}, {kind_col}
                        FROM calculated_points c JOIN sensors s ON s.sensor_id = c.sensor_id
                        WHERE c.enabled ORDER BY c.calc_id;""")
                    rows = cur.fetchall()
        except Exception as e:
            logger.debug(f"讀取計算點設定失敗，沿用上一輪: {e}")
            return
        defs = []
        for calc_id, sensor_id, code, text, saved_state, kind in rows:
            # 累計狀態只在第一次載入時從資料庫讀回（之後以記憶體為準，每輪寫回）
            if calc_id not in self._states:
                if isinstance(saved_state, str):
                    saved_state = json.loads(saved_state)
                self._states[calc_id] = dict(saved_state or {})
            if kind == "python":
                if not SCRIPTS_ENABLED:
                    expr = ExpressionError("Python 腳本未啟用：請在 .env 設定 CALC_SCRIPTS_ENABLED=true 後重新啟動")
                else:
                    expr = PyScript(text)
                    missing = [r for r in expr.refs if r not in code_to_id]
                    if missing:
                        expr = ExpressionError(f"腳本引用的感測器不存在：{', '.join(missing)}")
                    elif code in expr.refs:
                        expr = ExpressionError("不能引用自己")
                defs.append((calc_id, sensor_id, code, expr))
                continue
            try:
                expr = Expression(text)
                missing = [r for r in expr.refs if r not in code_to_id]
                if missing:
                    expr = ExpressionError(f"引用的感測器不存在：{', '.join(missing)}")
                elif code in expr.refs:
                    expr = ExpressionError("不能引用自己")
            except ExpressionError as e:
                expr = e
            defs.append((calc_id, sensor_id, code, expr))
        deps = {code: set(expr.refs) for _, _, code, expr in defs if isinstance(expr, (Expression, PyScript))}
        self._order, self._cyclic = evaluation_order(deps)
        self._defs = defs
        self._code_to_id = code_to_id
        self._available = True
        self.stats["points"] = len(defs)

    def run_once(self):
        latest = self.writer.snapshot_latest()
        self._cycle_inputs = None
        wall_now = datetime.now().astimezone()
        alive_window = self.writer.alive_window
        by_code = {code: (calc_id, sid, expr) for calc_id, sid, code, expr in self._defs}
        computed = {}           # 這一輪已算出的計算點：code -> (value, quality)
        results = []            # 回寫 calculated_points 的狀態
        ok = offline = error = 0

        # 先算有相依順序的，再處理有錯 / 循環的
        for code in self._order + [c for c in by_code if c not in self._order]:
            calc_id, sid, expr = by_code[code]
            if code in self._cyclic:
                results.append((calc_id, None, None, "ERROR", "循環引用（直接或間接引用到自己）", wall_now))
                error += 1
                continue
            if isinstance(expr, ExpressionError):
                results.append((calc_id, None, None, "ERROR", str(expr), wall_now))
                error += 1
                continue
            if isinstance(expr, PyScript):
                outcome = self._run_script(calc_id, sid, code, expr, latest, computed, wall_now, alive_window)
                results.append(outcome)
                state = outcome[3]
                ok += state == "ONLINE"
                offline += state == "OFFLINE"
                error += state == "ERROR"
                continue

            values, quality_of, reasons = {}, {}, {}
            for ref in expr.refs:
                if ref in computed:
                    value, q = computed[ref]
                else:
                    sample = latest.get(self._code_to_id[ref])
                    if sample is None:
                        values[ref], reasons[ref] = MISSING, f"{ref} 尚無資料"
                        continue
                    value, _ts, alive_at, q = sample
                    if q >= Q.BAD:
                        values[ref], reasons[ref] = MISSING, f"{ref} {Q.LABELS.get(q, '品質不良')}"
                        continue
                    if alive_at is None or (wall_now - alive_at).total_seconds() > alive_window:
                        values[ref], reasons[ref] = MISSING, f"{ref} 資料過時（來源未確認存活）"
                        continue
                values[ref] = value
                quality_of[ref] = q
            ctx = EvalContext(now=datetime.now(LOCAL_TZ), state=self._states.setdefault(calc_id, {}),
                              baseline=self._baseline)
            try:
                value = expr.evaluate(values, ctx)
            except MissingInput as e:
                self.writer.mark_unavailable([sid])
                missing = reasons.get(e.ref) or "、".join(reasons.values()) or str(e)
                results.append((calc_id, None, None, "OFFLINE", missing, wall_now))
                offline += 1
                continue
            except ExpressionError as e:
                self.writer.mark_unavailable([sid])
                results.append((calc_id, None, None, "ERROR", str(e), wall_now))
                self._warn_once(calc_id, f"⚠️ 計算點 {code} 計算失敗：{e}")
                error += 1
                continue
            # 品質：實際用到的輸入裡最差的那個（被 valueor / coalesce 略過的斷線輸入不算）
            worst = Q.UNCERTAIN if any(quality_of.get(r) == Q.UNCERTAIN for r in expr.accessed) else Q.GOOD
            self._warned.discard(calc_id)
            self.writer.update_latest(sid, value, quality=worst)
            computed[code] = (value, worst)
            results.append((calc_id, value, "UNCERTAIN" if worst == Q.UNCERTAIN else "GOOD", "ONLINE", None, wall_now))
            ok += 1

        self.stats.update(ok=ok, offline=offline, error=error, last_cycle_at=wall_now.isoformat(timespec="seconds"))
        self._write_status(results)

    def _script_inputs(self, latest, computed, wall_now, alive_window):
        """腳本的輸入：所有有效、來源存活的感測器 + 這一輪已算出的計算點。"""
        id_to_code = {v: k for k, v in self._code_to_id.items()}
        values, qualities, reasons = {}, {}, {}
        for sid, (value, _ts, alive_at, q) in latest.items():
            code = id_to_code.get(sid)
            if code is None:
                continue
            if q >= Q.BAD:
                reasons[code] = f"{code} {Q.LABELS.get(q, '品質不良')}"
                continue
            if alive_at is None or (wall_now - alive_at).total_seconds() > alive_window:
                reasons[code] = f"{code} 資料過時（來源未確認存活）"
                continue
            values[code] = value
            qualities[code] = "UNCERTAIN" if q == Q.UNCERTAIN else "GOOD"
        for code, (value, q) in computed.items():
            values[code] = value
            qualities[code] = "UNCERTAIN" if q == Q.UNCERTAIN else "GOOD"
        return values, qualities, reasons

    def _run_script(self, calc_id, sid, code, script, latest, computed, wall_now, alive_window):
        from services.calc.script_runner import ScriptRunner

        # 逾時過的腳本退避：每次逾時都會卡住計算引擎 CALC_SCRIPT_TIMEOUT 秒並重啟子程序，
        # 不退避的話一個無窮迴圈的腳本會讓每一輪都變慢（30 秒起、每次加倍、最多 10 分鐘）
        retry_at, delay = self._backoff.get(calc_id, (0.0, 0.0))
        if time.monotonic() < retry_at:
            self.writer.mark_unavailable([sid])
            return (calc_id, None, None, "ERROR",
                    f"上次執行逾時，暫停 {delay:.0f} 秒後重試（{retry_at - time.monotonic():.0f} 秒後）", wall_now)
        if self._runner is None:
            self._runner = ScriptRunner()
        if self._cycle_inputs is None:
            self._cycle_inputs = self._script_inputs(latest, computed, wall_now, alive_window)
        values, qualities, reasons = self._cycle_inputs
        # 這一輪較早算出的計算點也要給腳本用（輸入快取建立之後才算出的那些）
        values = {**values, **{c: v for c, (v, _q) in computed.items()}}
        qualities = {**qualities, **{c: "UNCERTAIN" if q == Q.UNCERTAIN else "GOOD" for c, (_v, q) in computed.items()}}
        holder = self._states.setdefault(calc_id, {})
        if holder.get("__sig") != script.signature:
            holder.clear()
            holder["__sig"] = script.signature
        res = self._runner.run(calc_id, script.source, values, qualities, dict(holder.get("py", {})),
                               datetime.now(LOCAL_TZ))
        self._logs[calc_id] = "\n".join(res.logs) if res.logs else ""
        if res.timeout:
            delay = min(max(delay * 2, 30.0), 600.0)
            self._backoff[calc_id] = (time.monotonic() + delay, delay)
        else:
            self._backoff.pop(calc_id, None)
        self.stats["script_restarts"] = self._runner.restarts
        if res.ok:
            holder["py"] = res.state or {}
            self._warned.discard(calc_id)
            if res.value is None:
                return (calc_id, None, None, "ONLINE", "本輪 result = None，不更新", wall_now)
            uncertain = any(qualities.get(r) == "UNCERTAIN" for r in script.refs)
            q = Q.UNCERTAIN if uncertain else Q.GOOD
            self.writer.update_latest(sid, res.value, quality=q)
            computed[code] = (res.value, q)
            return (calc_id, res.value, "UNCERTAIN" if uncertain else "GOOD", "ONLINE",
                    f"執行 {res.elapsed_ms:.0f} ms", wall_now)
        self.writer.mark_unavailable([sid])
        missing = [reasons.get(r) or f"{r} 尚無資料" for r in script.refs if r not in values]
        if missing and not res.timeout:
            return (calc_id, None, None, "OFFLINE", "、".join(missing) + f"（{res.error}）", wall_now)
        self._warn_once(calc_id, f"⚠️ 計算點 {code}（Python）執行失敗：{res.error}")
        return (calc_id, None, None, "ERROR", res.error, wall_now)

    def _baseline(self, code, start):
        """delta() 用：週期開始時的值 = 該時間點（含）之前最後一筆有效資料，沒有就取之後第一筆。"""
        key = (code, start.isoformat())
        if key in self._baseline_cache:
            return self._baseline_cache[key]
        sid = self._code_to_id.get(code)
        value = None
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                                "WHERE table_name='sensor_readings' AND column_name='quality');")
                    valid = "AND (quality IS NULL OR quality < 3)" if cur.fetchone()[0] else ""
                    cur.execute(f"""
                        (SELECT value::float8 FROM sensor_readings
                         WHERE sensor_id = %s AND reading_time <= %s AND reading_time > %s {valid}
                         ORDER BY reading_time DESC LIMIT 1)
                        UNION ALL
                        (SELECT value::float8 FROM sensor_readings
                         WHERE sensor_id = %s AND reading_time > %s {valid}
                         ORDER BY reading_time ASC LIMIT 1)
                        LIMIT 1;""", (sid, start, start - timedelta(days=7), sid, start))
                    row = cur.fetchone()
                    value = row[0] if row else None
        except Exception as e:
            logger.debug(f"查詢 {code} 的週期基準值失敗: {e}")
            return None            # 不快取，下次再查
        if len(self._baseline_cache) > 5000:
            self._baseline_cache.clear()
        self._baseline_cache[key] = value
        return value

    def _warn_once(self, key, message):
        if key not in self._warned:
            logger.warning(message)
            self._warned.add(key)

    def _write_status(self, results):
        if not results:
            return
        from psycopg2.extras import execute_values
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    if self._has_state_column:
                        states = [(cid, json.dumps(persistable_state(st), default=str))
                                  for cid, st in self._states.items() if len(st) > 1]
                        if states:
                            execute_values(cur, """
                                UPDATE calculated_points AS c SET calc_state = v.st::jsonb
                                FROM (VALUES %s) AS v(id, st) WHERE c.calc_id = v.id;""", states)
                    logs = [(cid, text) for cid, text in self._logs.items()]
                    if self._has_kind_column and logs:
                        execute_values(cur, """
                            UPDATE calculated_points AS c SET last_log = v.log
                            FROM (VALUES %s) AS v(id, log) WHERE c.calc_id = v.id;""", logs)
                    execute_values(cur, """
                        UPDATE calculated_points AS c
                        SET current_value = COALESCE(v.val, c.current_value), quality = v.q, state = v.st,
                            last_error = v.err,
                            last_update = CASE WHEN v.st = 'ONLINE' THEN v.ts ELSE c.last_update END
                        FROM (VALUES %s) AS v(id, val, q, st, err, ts)
                        WHERE c.calc_id = v.id;""", results,
                        template="(%s, %s::float8, %s, %s, %s, %s::timestamptz)")
        except Exception as e:
            logger.debug(f"回寫計算點狀態失敗: {e}")
