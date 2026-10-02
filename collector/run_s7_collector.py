"""
collector/run_s7_collector.py
=============================
Siemens S7（TIA）採集，由 main.py 每 POLL_INTERVAL 秒呼叫一次 collect_s7_data()。

🆕 v3.3 改寫重點（相較 v2）：
    1. 區域：DB / M / I / Q（sql/019 的 area 欄位）；v2 只能讀 DB
    2. BOOL 支援位元（bit_offset）：v2 一律讀第 0 bit，DB1.DBX10.3 設定不出來
    3. 型態大小正確：v2 只認得 REAL / DINT / INT / BOOL，其他一律當 4 bytes，
       LREAL（8 bytes）排在最後一個時會讀不夠而解析失敗；STRING 也讀錯
    4. 區塊切分：v2 一個 DB 從最小 offset 讀到最大 offset（offset 0 和 5000 就讀 5 KB），
       現在相近的位址才合併（間隔 ≤ S7_MAX_GAP）、單塊上限 S7_MAX_BLOCK bytes
    5. 連線重用、依 PLC（IP + Rack + Slot）分組；Rack / Slot 可設定（S7-300 是 Slot 2）
    6. 讀取失敗時保留最後數值、只更新狀態（v2 會寫入 {"val": 0.0}，總覽頁看起來像真的讀到 0）
    7. CPU 拒絕整塊讀取時改逐點讀取，找出有問題的點位（DB 不存在、超出長度）單獨讀
    8. 錯誤訊息翻成中文說明（PUT/GET 沒開、最佳化區塊存取…）；同一個問題只在發生 / 恢復時記錄

可調參數（.env）：S7_MAX_GAP=32、S7_MAX_BLOCK=400、S7_MAX_WORKERS=8
"""

import logging
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from data_layer.batch_updater import batch_update_tia_data
from data_layer.db_connector import DatabaseConnector
from data_layer.timeseries_writer import sensor_reading_writer
from protocols import s7_codec
from protocols.s7_protocol import S7Connection

logger = logging.getLogger(__name__)


def _env_int(key, default):
    try:
        return int(float(os.getenv(key, str(default)).split("#")[0].strip()))
    except ValueError:
        return default


MAX_GAP = _env_int("S7_MAX_GAP", 32)
MAX_BLOCK = _env_int("S7_MAX_BLOCK", 400)
MAX_WORKERS = _env_int("S7_MAX_WORKERS", 8)
S7_PORT = _env_int("S7_PORT", 102)       # 只有測試 / 透過埠轉發時才需要改

_CONNECTIONS: dict = {}
_ISOLATED: set = set()
_WARNED: dict = {}
_LAST_CONFIGS: list = []
S7_STATS: dict = {}


def _warn(key, message):
    if _WARNED.get(key) != message:
        logger.warning(message)
        _WARNED[key] = message
    else:
        logger.debug(message)


def _recovered(key, message):
    if _WARNED.pop(key, None) is not None:
        logger.info(message)


# 向下相容：v2 曾經提供的工具函式
def get_s7_type_size(data_type):
    try:
        return s7_codec.type_size(data_type)
    except ValueError:
        return 4


# ----------------------------------------------------
# 讀取點位設定（sql/019 之前沒有 area / bit_offset / rack / slot / enabled）
# ----------------------------------------------------
def load_plc_configs():
    queries = (
        'SELECT id, name, plc_ip, db_number, "offset", data_type, sensor_id, '
        "area, bit_offset, rack, slot FROM tia_scada WHERE enabled;",
        'SELECT id, name, plc_ip, db_number, "offset", data_type, sensor_id, '
        "'DB' AS area, 0 AS bit_offset, 0 AS rack, 1 AS slot FROM tia_scada;",
    )
    last_error = None
    for sql in queries:
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cols = [d[0] for d in cur.description]
                    configs = [dict(zip(cols, r)) for r in cur.fetchall()]
            _LAST_CONFIGS[:] = configs
            _recovered("fetch", "✅ tia_scada 恢復讀取")
            return configs
        except Exception as e:
            last_error = e
    # 資料庫暫時連不上：沿用上次的點位設定繼續採集（資料由寫入排程存本機緩存，恢復後補寫）
    if _LAST_CONFIGS:
        _warn("fetch", f"⚠️ 讀取 tia_scada 失敗，沿用上次的 {len(_LAST_CONFIGS)} 個點位設定繼續採集: {last_error}")
        return list(_LAST_CONFIGS)
    logger.error(f"從資料庫讀取 S7 點位配置失敗: {last_error}")
    return []


# ----------------------------------------------------
# 區塊規劃（純函式，tests/test_s7.py）
# ----------------------------------------------------
def tag_span(tag) -> int:
    dt = s7_codec.normalize_type(tag["data_type"])
    return 1 if dt == "BOOL" else s7_codec.type_size(tag["data_type"])


def plan_s7_blocks(tags, max_gap=MAX_GAP, max_block=MAX_BLOCK, isolate=frozenset()):
    """
    同區域、同 DB、位址相近的點位合併成一次讀取。
    回傳 [{"area", "db", "start", "size", "items": [(tag, 區塊內位移)]}]。
    isolate 的點位單獨一塊，而且其他區塊不會跨過它。
    """
    groups = defaultdict(list)
    for t in tags:
        area = (t.get("area") or "DB").upper()
        groups[(area, int(t["db_number"] or 0) if area == "DB" else 0)].append(t)
    blocks = []
    for (area, db), group in sorted(groups.items()):
        current = None
        for t in sorted(group, key=lambda x: (int(x["offset"]), x.get("id", 0))):
            start, span = int(t["offset"]), tag_span(t)
            if t.get("id") in isolate:
                if current:
                    blocks.append(current)
                    current = None
                blocks.append({"area": area, "db": db, "start": start, "size": span, "items": [(t, 0)]})
                continue
            if current:
                end = current["start"] + current["size"]
                new_end = max(end, start + span)
                if start - end <= max_gap and new_end - current["start"] <= max_block:
                    current["size"] = new_end - current["start"]
                    current["items"].append((t, start - current["start"]))
                    continue
                blocks.append(current)
            current = {"area": area, "db": db, "start": start, "size": span, "items": [(t, 0)]}
        if current:
            blocks.append(current)
    return blocks


# ----------------------------------------------------
# 單一 PLC 的採集
# ----------------------------------------------------
def _value(tag, data, rel):
    v = s7_codec.decode(data, rel, tag["data_type"], int(tag.get("bit_offset") or 0))
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        return round(v, 4)
    return v


def _ok(tag, value, now):
    sensor_reading_writer.update_latest(tag.get("sensor_id"), value, now)
    return (tag["id"], {"val": value}, "ONLINE")


def _fail(tags, state):
    sensor_reading_writer.mark_unavailable(t.get("sensor_id") for t in tags)
    return [(t["id"], None, state) for t in tags]


def _collect_plc(key, tags):
    ip, rack, slot = key
    conn = _CONNECTIONS.get(key)
    if conn is None:
        conn = _CONNECTIONS[key] = S7Connection(ip, rack, slot, S7_PORT)
    started = time.perf_counter()
    stats = {"label": conn.label, "tags": len(tags), "requests": 0, "errors": 0, "last_error": None}
    rows = []
    blocks = plan_s7_blocks(tags, isolate=_ISOLATED)
    stats["blocks"] = len(blocks)
    down = False
    for b in blocks:
        block_tags = [t for t, _ in b["items"]]
        if down:
            rows += _fail(block_tags, "OFFLINE")
            continue
        res = conn.read(b["area"], b["db"], b["start"], b["size"])
        stats["requests"] += 1
        now = datetime.now().astimezone()
        if res.ok:
            _recovered(("plc", key), f"✅ S7 {conn.label} 連線恢復")
            for t, rel in b["items"]:
                try:
                    rows.append(_ok(t, _value(t, res.data, rel), now))
                    _recovered(("tag", t["id"]), f"✅ S7 點位 [{t['name']}] 恢復正常")
                except (ValueError, TypeError) as e:
                    _warn(("tag", t["id"]), f"⚠️ S7 點位 [{t['name']}] 解析失敗：{e}")
                    rows += _fail([t], "PARSE_ERROR")
            continue
        stats["errors"] += 1
        stats["last_error"] = res.error
        if res.fatal:
            _warn(("plc", key), f"❌ S7 {conn.label} 連線失敗：{res.error}")
            down = True
            rows += _fail(block_tags, "OFFLINE")
            continue
        if len(block_tags) == 1:
            _ISOLATED.add(block_tags[0]["id"])
            _warn(("tag", block_tags[0]["id"]), f"⚠️ S7 點位 [{block_tags[0]['name']}] 讀取被拒絕：{res.error}")
            rows += _fail(block_tags, "ERROR")
            continue
        _warn(("block", key, b["area"], b["db"], b["start"]),
              f"⚠️ S7 {conn.label} {b['area']}{b['db'] if b['area'] == 'DB' else ''} "
              f"byte {b['start']}~{b['start'] + b['size'] - 1} 整塊讀取被拒絕（{res.error}），改逐點讀取")
        for t in block_tags:
            single = conn.read(b["area"], b["db"], int(t["offset"]), tag_span(t))
            stats["requests"] += 1
            now = datetime.now().astimezone()
            if single.ok:
                try:
                    rows.append(_ok(t, _value(t, single.data, 0), now))
                    continue
                except (ValueError, TypeError) as e:
                    single.error = str(e)
            _ISOLATED.add(t["id"])
            _warn(("tag", t["id"]), f"   └─ S7 點位 [{t['name']}] 讀取失敗：{single.error}，之後單獨讀取")
            rows += _fail([t], "ERROR")
    ok = sum(1 for r in rows if r[2] == "ONLINE")
    stats.update(ok_tags=ok, state="ONLINE" if ok == len(tags) else ("OFFLINE" if ok == 0 else "PARTIAL"),
                 last_cycle_ms=round((time.perf_counter() - started) * 1000, 1),
                 isolated_tags=len([t for t in tags if t["id"] in _ISOLATED]),
                 last_poll_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    S7_STATS[conn.label] = stats
    return rows


def collect_s7_data():
    """核心採集流程（多台 PLC 併發）。"""
    configs = load_plc_configs()
    if not configs:
        logger.info("tia_scada 沒有啟用中的點位。")
        return
    grouped = defaultdict(list)
    for c in configs:
        slot = c.get("slot")
        grouped[(c["plc_ip"], int(c.get("rack") or 0), 1 if slot is None else int(slot))].append(c)
    for key in list(_CONNECTIONS):
        if key not in grouped:
            removed = _CONNECTIONS.pop(key)
            S7_STATS.pop(removed.label, None)
            removed.close()

    started = time.perf_counter()
    rows = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(grouped)) or 1, thread_name_prefix="s7") as ex:
        futures = {ex.submit(_collect_plc, key, group): key for key, group in grouped.items()}
        for f in as_completed(futures):
            try:
                rows.extend(f.result())
            except Exception as e:
                logger.error(f"S7 PLC {futures[f]} 採集執行緒發生未預期例外: {e}", exc_info=True)

    batch_update_tia_data(rows)
    ok = sum(1 for r in rows if r[2] == "ONLINE")
    labels = {_CONNECTIONS[k].label for k in grouped}
    requests = sum(s["requests"] for label, s in S7_STATS.items() if label in labels)
    logger.info(f"🏁 S7 本輪：{len(grouped)} 台 PLC、{len(configs)} 個點位（成功 {ok}）、{requests} 次請求，"
                f"耗時 {(time.perf_counter() - started) * 1000:.0f} ms")
    # v3.1 起 sensor_readings 一律由 main.py 的統一寫入排程寫入，這裡不再自行 flush


def get_stats() -> dict:
    return {label: dict(s) for label, s in S7_STATS.items()}


def shutdown():
    for conn in _CONNECTIONS.values():
        conn.close()
    _CONNECTIONS.clear()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    DatabaseConnector.initialize_pool()
    sensor_reading_writer.load_initial_cache()
    collect_s7_data()
    sensor_reading_writer.flush()
    shutdown()
    DatabaseConnector.close_pool()
