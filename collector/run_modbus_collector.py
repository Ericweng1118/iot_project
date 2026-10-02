"""
collector/run_modbus_collector.py
=================================
Modbus 採集（由 main.py 每 POLL_INTERVAL 秒呼叫一次 main()）。

🆕 v3.1 改寫重點（相較 v2）：
    1. 批次讀取：同站號、同功能碼、位址相近的點位合併成一次請求（collector/modbus_blocks.py），
       50 個點從 50 次請求降到 1~3 次
    2. 連線重用：連線跨輪次保留，不再每輪 connect / disconnect
    3. 依「實體連線」分組：同一個閘道（IP:Port）後面的多個站號共用一條連線、依序讀取。
       v2 是每個 (IP, Port, 站號) 各開一條連線同時連，很多 RS-485 閘道只允許 1~4 條連線，
       站號一多就互相搶、隨機失敗
    4. 傳輸方式：Modbus TCP / RTU over TCP / RTU 序列埠（sql/015 的 transport 欄位）
    5. 自動避開壞點位：整塊讀取被設備拒絕（例如某個位址不存在）時改逐點讀取，
       找出有問題的點位後讓它單獨讀，不再拖累其他點位；若逐點都成功，代表是空隙裡的
       未定義暫存器造成，該組改成只合併連續位址
    6. 一個站號逾時，同一條連線上的其他站號照樣讀；連線本身斷掉才整條標記離線
    7. 品質：讀不到的點位通知寫入排程「來源斷線」（通訊中斷標記、停止心跳補寫）
    8. 不再自行 flush sensor_readings（交給 main.py 的統一寫入排程）；每個點位的 log 改成 DEBUG，
       每輪只印一行摘要
    9. 讀取失敗時保留最後一次的數值，只更新連線狀態（與 OPC UA 行為一致）

可調參數（.env）：
    MODBUS_TIMEOUT=3             單次請求逾時（秒）
    MODBUS_RETRIES=1             逾時重試次數（離線設備越多次越拖慢整輪）
    MODBUS_MAX_BLOCK_REGISTERS=100   一次讀取的暫存器上限（部分設備只接受 32 / 64，可調小）
    MODBUS_MAX_GAP=10            兩個點位相隔多少暫存器以內才合併
    MODBUS_MAX_WORKERS=8         同時採集的實體連線數上限
"""

import json
import logging
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from psycopg2.extras import execute_values

from collector.modbus_blocks import plan_blocks
from data_layer.db_connector import DatabaseConnector
from data_layer.timeseries_writer import sensor_reading_writer
from protocols.modbus_codec import BIT_FUNCTIONS, apply_linear_scaling, decode, register_count
from protocols.modbus_protocol import ModbusConnection

logger = logging.getLogger(__name__)


def _env_num(key, default):
    try:
        return float(os.getenv(key, str(default)).split("#")[0].strip())
    except ValueError:
        return default


TIMEOUT = _env_num("MODBUS_TIMEOUT", 3.0)
RETRIES = int(_env_num("MODBUS_RETRIES", 1))
MAX_BLOCK_REGISTERS = int(_env_num("MODBUS_MAX_BLOCK_REGISTERS", 100))
MAX_GAP = int(_env_num("MODBUS_MAX_GAP", 10))
MAX_WORKERS = int(_env_num("MODBUS_MAX_WORKERS", 8))

# 跨輪次保留的狀態（main.py 每輪呼叫 main()，模組常駐在記憶體裡）
_CONNECTIONS: dict = {}            # 連線 key -> ModbusConnection
_ISOLATED_TAGS: set = set()        # 需要單獨讀取的點位 id
_STRICT_GROUPS: dict = defaultdict(set)   # 連線 key -> {(slave, fc)} 只合併連續位址
MODBUS_STATS: dict = {}            # 連線標籤 -> 統計（services/status_reporter.py 回報）
_WARNED: dict = {}                 # 問題 key -> 訊息：同一個問題只在第一次發生 / 內容改變時印 WARNING


def _warn(key, message):
    """同一個問題每輪都會再發生一次（例如設備離線），只在狀態改變時印 WARNING，避免洗版。"""
    if _WARNED.get(key) != message:
        logger.warning(message)
        _WARNED[key] = message
    else:
        logger.debug(message)


def _recovered(key, message):
    if _WARNED.pop(key, None) is not None:
        logger.info(message)

# 向下相容：v2 曾經從這裡 import 這幾個工具函式
get_register_count = register_count
parse_registers = decode


# ----------------------------------------------------
# 讀取點位設定（sql/015 之前沒有 transport / serial_settings / enabled 欄位）
# ----------------------------------------------------
_BASE_COLUMNS = """id, name, plc_ip, plc_port, slave_id, function_code,
                   start_address, data_type, raw_min, raw_max, eng_min, eng_max,
                   byte_order, word_order, state_dictionary, sensor_id"""


def fetch_scada_tags():
    queries = (
        f"SELECT {_BASE_COLUMNS}, transport, serial_settings FROM modbus_scada WHERE enabled;",
        f"SELECT {_BASE_COLUMNS}, 'tcp' AS transport, NULL AS serial_settings FROM modbus_scada;",
    )
    last_error = None
    for sql in queries:
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(sql)
                    columns = [desc[0] for desc in cursor.description]
                    tags = [dict(zip(columns, row)) for row in cursor.fetchall()]
            _LAST_TAGS[:] = tags
            _recovered("fetch", "✅ modbus_scada 恢復讀取")
            return tags
        except Exception as e:
            last_error = e
    # 資料庫暫時連不上：沿用上一次成功讀到的點位設定繼續採集，採到的資料由寫入排程
    # 存進本機緩存，資料庫恢復後補寫（不這樣做的話，斷線期間 Modbus 資料會全部遺失）
    if _LAST_TAGS:
        _warn("fetch", f"⚠️ 讀取 modbus_scada 失敗，沿用上次的 {len(_LAST_TAGS)} 個點位設定繼續採集: {last_error}")
        return list(_LAST_TAGS)
    logger.error(f"從 modbus_scada 資料表讀取點位失敗: {last_error}")
    return []


_LAST_TAGS: list = []


def connection_key(tag) -> tuple:
    transport = (tag.get("transport") or "tcp").lower()
    if transport == "rtu":
        return ("rtu", tag["plc_ip"], 0, tag.get("serial_settings") or "9600,8,N,1")
    return (transport, tag["plc_ip"], int(tag.get("plc_port") or 502), None)


def _get_connection(key) -> ModbusConnection:
    conn = _CONNECTIONS.get(key)
    if conn is None:
        transport, host, port, serial = key
        conn = ModbusConnection(transport, host, port, timeout=TIMEOUT, serial_settings=serial,
                                retries=RETRIES)
        _CONNECTIONS[key] = conn
    return conn


# ----------------------------------------------------
# 數值處理
# ----------------------------------------------------
def _to_engineering(tag, registers):
    raw = decode(registers, tag["data_type"], tag.get("byte_order") or "BIG", tag.get("word_order") or "BIG")
    return apply_linear_scaling(raw, tag["raw_min"], tag["raw_max"], tag["eng_min"], tag["eng_max"])


def _payload(tag, value):
    """狀態字典對應成功時，current_data 放文字（例如「大火燃燒」），current_value 仍是數字。"""
    state_dict = tag.get("state_dictionary")
    if isinstance(state_dict, str):
        try:
            state_dict = json.loads(state_dict)
        except ValueError:
            state_dict = None
    if state_dict and isinstance(state_dict, dict):
        key = str(int(value)) if float(value).is_integer() else str(value)
        if key in state_dict:
            return {"val": state_dict[key]}
    return {"val": value}


def _ok_row(tag, value, now):
    rounded = round(value, 4)
    sensor_reading_writer.update_latest(tag.get("sensor_id"), rounded, now)
    logger.debug(f"   └─ 📊 [{tag['name']}] (ID:{tag['id']}) = {rounded}")
    return (rounded, json.dumps(_payload(tag, rounded), ensure_ascii=False), "ONLINE", now, tag["id"])


def _fail_rows(tags, state, now):
    sensor_reading_writer.mark_unavailable(t.get("sensor_id") for t in tags)
    return [(None, None, state, now, t["id"]) for t in tags]


# ----------------------------------------------------
# 單一實體連線的採集（在獨立執行緒中執行）
# ----------------------------------------------------
def _collect_connection(key, tags):
    conn = _get_connection(key)
    started = time.perf_counter()
    stats = {"label": conn.label, "transport": key[0], "tags": len(tags), "requests": 0,
             "errors": 0, "blocks": 0, "last_error": None}
    results = []
    blocks = plan_blocks(tags, max_registers=MAX_BLOCK_REGISTERS, max_gap=MAX_GAP,
                         isolate=_ISOLATED_TAGS, strict_groups=_STRICT_GROUPS[key])
    stats["blocks"] = len(blocks)
    silent_slaves = set()
    connection_down = False

    for block in blocks:
        block_tags = [t for t, _ in block.items]
        now = datetime.now().astimezone()
        if connection_down:
            results += _fail_rows(block_tags, "OFFLINE", now)
            continue
        if block.slave in silent_slaves:
            results += _fail_rows(block_tags, "OFFLINE", now)
            continue

        res = conn.read(block.function_code, block.start, block.count, block.slave)
        stats["requests"] += 1
        now = datetime.now().astimezone()

        if res.ok:
            _recovered(("conn", key), f"✅ Modbus {conn.label} 連線恢復")
            _recovered(("slave", key, block.slave), f"✅ Modbus {conn.label} 站號 {block.slave} 恢復回應")
            for tag, _ in block.items:
                _recovered(("tag", tag["id"]), f"✅ Modbus 點位 [{tag['name']}] 恢復正常")
            for tag, offset in block.items:
                span = 1 if block.function_code in BIT_FUNCTIONS else register_count(tag["data_type"])
                value = _to_engineering(tag, res.values[offset:offset + span])
                results.append(_ok_row(tag, value, now) if value is not None
                               else _fail_rows([tag], "ERROR", now)[0])
            continue

        stats["errors"] += 1
        stats["last_error"] = res.error
        if res.fatal:
            _warn(("conn", key), f"❌ Modbus {conn.label} 連線失敗：{res.error}")
            connection_down = True
            results += _fail_rows(block_tags, "OFFLINE", now)
            continue
        if res.exception_code is None:
            # 逾時：這個站號沒回應，同一條連線上的其他站號照樣讀
            _warn(("slave", key, block.slave), f"⚠️ Modbus {conn.label} 站號 {block.slave} 無回應：{res.error}")
            silent_slaves.add(block.slave)
            results += _fail_rows(block_tags, "OFFLINE", now)
            continue

        # 設備有回應但拒絕這個請求（例如位址不存在）
        if len(block.items) == 1:
            tag = block_tags[0]
            _ISOLATED_TAGS.add(tag["id"])
            _warn(("tag", tag["id"]), f"⚠️ Modbus 點位 [{tag['name']}] 讀取被拒絕：{res.error}")
            results += _fail_rows([tag], "ERROR", now)
            continue

        logger.warning(
            f"⚠️ Modbus {conn.label} 站號 {block.slave} FC{block.function_code:02d} "
            f"位址 {block.start}~{block.start + block.count - 1} 整塊讀取被拒絕（{res.error}），改逐點讀取"
        )
        all_ok = True
        for tag in block_tags:
            span = 1 if block.function_code in BIT_FUNCTIONS else register_count(tag["data_type"])
            single = conn.read(block.function_code, int(tag["start_address"]), span, block.slave)
            stats["requests"] += 1
            now = datetime.now().astimezone()
            value = _to_engineering(tag, single.values) if single.ok else None
            if value is not None:
                results.append(_ok_row(tag, value, now))
            else:
                all_ok = False
                _ISOLATED_TAGS.add(tag["id"])
                logger.warning(f"   └─ 點位 [{tag['name']}] 讀取失敗：{single.error}，之後單獨讀取")
                results += _fail_rows([tag], "ERROR", now)
        if all_ok:
            _STRICT_GROUPS[key].add((block.slave, block.function_code))
            logger.warning(
                f"   └─ 逐點讀取全部成功，代表是點位之間未定義的暫存器造成；"
                f"站號 {block.slave} FC{block.function_code:02d} 之後只合併連續位址"
            )

    ok_count = sum(1 for r in results if r[2] == "ONLINE")
    stats.update(
        ok_tags=ok_count,
        state="ONLINE" if ok_count == len(tags) else ("OFFLINE" if ok_count == 0 else "PARTIAL"),
        last_cycle_ms=round((time.perf_counter() - started) * 1000, 1),
        isolated_tags=len([t for t in tags if t["id"] in _ISOLATED_TAGS]),
        strict_groups=sorted(_STRICT_GROUPS[key]),
        last_poll_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    MODBUS_STATS[conn.label] = stats
    return results


# ----------------------------------------------------
# 資料庫寫入：將採集結果寫回 modbus_scada
# ----------------------------------------------------
def update_scada_results(results):
    if not results:
        return
    sql = """
        UPDATE modbus_scada AS m
        SET current_value = COALESCE(v.cv, m.current_value),
            current_data  = COALESCE(v.cd, m.current_data),
            plc_state     = v.st,
            last_update   = CASE WHEN v.st = 'ONLINE' THEN v.ts ELSE m.last_update END
        FROM (VALUES %s) AS v(cv, cd, st, ts, id)
        WHERE m.id = v.id;
    """
    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cursor:
                execute_values(cursor, sql, results,
                               template="(%s::real, %s::jsonb, %s, %s::timestamptz, %s::bigint)")
        _recovered("update", "✅ modbus_scada 恢復寫入")
    except Exception as e:
        _warn("update", f"⚠️ 寫入 modbus_scada 即時值失敗（資料庫恢復後自動更新）: {e}")


# ----------------------------------------------------
# 一輪採集（main.py 每 POLL_INTERVAL 秒呼叫一次）
# ----------------------------------------------------
def main():
    if not DatabaseConnector.initialize_pool() and not _LAST_TAGS:
        logger.error("PostgreSQL 連線池初始化失敗，本輪 Modbus 採集略過。")
        return

    tags = fetch_scada_tags()
    if not tags:
        logger.info("modbus_scada 沒有啟用中的點位。")
        return

    grouped = defaultdict(list)
    for tag in tags:
        grouped[connection_key(tag)].append(tag)

    # 設定被刪除 / 改位址的連線：關閉並移除
    for key in list(_CONNECTIONS):
        if key not in grouped:
            removed = _CONNECTIONS.pop(key)
            MODBUS_STATS.pop(removed.label, None)
            removed.close()

    started = time.perf_counter()
    results = []
    worker_count = min(MAX_WORKERS, len(grouped)) or 1
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="modbus") as executor:
        futures = {executor.submit(_collect_connection, key, group): key for key, group in grouped.items()}
        for future in as_completed(futures):
            try:
                results.extend(future.result())
            except Exception as e:
                logger.error(f"Modbus 連線 {futures[future]} 採集執行緒發生未預期例外: {e}", exc_info=True)

    update_scada_results(results)
    ok = sum(1 for r in results if r[2] == "ONLINE")
    labels = {_CONNECTIONS[k].label for k in grouped}
    requests = sum(s["requests"] for label, s in MODBUS_STATS.items() if label in labels)
    logger.info(
        f"🏁 Modbus 本輪：{len(grouped)} 條連線、{len(tags)} 個點位（成功 {ok}）、"
        f"{requests} 次請求，耗時 {(time.perf_counter() - started) * 1000:.0f} ms"
    )


def get_stats() -> dict:
    return {label: dict(s) for label, s in MODBUS_STATS.items()}


def shutdown():
    for conn in _CONNECTIONS.values():
        conn.close()
    _CONNECTIONS.clear()


if __name__ == "__main__":
    # 獨立執行本檔案（不透過 main.py）：跑一輪並立即寫入 sensor_readings
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    sensor_reading_writer.load_initial_cache()
    main()
    sensor_reading_writer.flush()
    shutdown()
