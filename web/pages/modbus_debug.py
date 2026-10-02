"""
web/pages/modbus_debug.py
=========================
🔧 Modbus 線上調適（engineer 以上）

現場接新設備時最花時間的就是「試」：位址差 1、FC03 還是 FC04、位元組順序是 ABCD 還是
CDAB、站號是多少。這一頁把 Modbus Poll 之類的桌面工具搬進網頁，直接用跟採集程式完全相同
的連線類別與解碼（protocols/modbus_codec.py），調出來的設定存進去就保證採集得到同樣的值。

分頁：
    📖 暫存器讀取  讀一段暫存器 / 線圈，連續監看並標示變動；同一段資料用四種位元組順序解碼對照，
                  標示「看起來合理」的組合；確認後一鍵建立點位
    🎯 點位驗證    用已建立點位的設定即時讀一次，跟資料庫裡的值比對；位元組順序可能設錯時提示，
                  可直接套用新的順序
    🔍 站號掃描    RS-485 匯流排上有哪些站號有回應（回例外碼也算：代表設備存在）
    ✏️ 寫入測試    FC05 / FC06 / FC16，寫入後立即讀回確認。預設關閉，需在 .env 設定
                  MODBUS_DEBUG_WRITE_ENABLED=true，並勾選確認；每次寫入都記錄在稽核紀錄
    📜 通訊紀錄    這個瀏覽器分頁裡所有請求的時間、結果、回應時間

注意：
    - RTU 序列埠同一時間只能被一個程式開啟。採集服務正在使用該序列埠時，這裡會開不起來；
      請改用序列閘道（TCP），或暫時停用該連線的點位
    - 很多 RS-485 閘道只允許 1~4 條同時連線，調適工具每次請求都會短暫佔用一條
"""

import struct
import time
from datetime import datetime

import pandas as pd
import streamlit as st

from core.config import MODBUS_ENABLED, env_bool
from protocols.modbus_codec import (
    BIT_FUNCTIONS,
    DATA_TYPES,
    FUNCTION_CODES,
    ORDER_PRESETS,
    TYPE_LABELS,
    apply_linear_scaling,
    decode,
    encode,
    interpretations,
    is_plausible,
    modicon_address,
    normalize_type,
    order_name,
    parse_modicon_address,
    register_count,
)
from protocols.modbus_protocol import TRANSPORTS, ModbusConnection
from services.alarm.rules import format_number
from web.common import (
    LOCAL_TZ,
    UNBOUND_LABEL,
    _find_binding_conflict,
    _load_sensor_binding_map,
    _load_sensor_options,
    _sensor_select_options,
    audit_ui,
    column_exists,
    execute,
    fetch_df,
    require_role,
    to_csv_bytes,
)

WRITE_ENABLED = env_bool("MODBUS_DEBUG_WRITE_ENABLED", False)
LOG_KEY = "mbd_log"
MAX_LOG = 300
MULTI_TYPES = ["float32", "int32", "uint32", "float64", "int64", "uint64"]


# ------------------------------------------------------------------
# 通訊
# ------------------------------------------------------------------
def _cfg():
    ss = st.session_state
    return {
        "transport": ss.get("mbd_transport", "tcp"),
        "host": (ss.get("mbd_host") or "").strip(),
        "port": int(ss.get("mbd_port") or 502),
        "serial": ss.get("mbd_serial") or "9600,8,N,1",
        "slave": int(ss.get("mbd_slave") or 1),
        "timeout": float(ss.get("mbd_timeout") or 2.0),
    }


def _connection(cfg, timeout=None, retries=0) -> ModbusConnection:
    return ModbusConnection(cfg["transport"], cfg["host"], cfg["port"],
                            timeout=timeout or cfg["timeout"], serial_settings=cfg["serial"],
                            retries=retries)


def _log(action, slave, fc, address, count, result):
    log = st.session_state.setdefault(LOG_KEY, [])
    log.insert(0, {
        "時間": datetime.now(LOCAL_TZ).strftime("%H:%M:%S.%f")[:-3],
        "動作": action, "站號": slave, "FC": fc, "位址": address, "數量": count,
        "結果": "✅ 成功" if result.ok else ("⚠️ 例外回應" if result.exception_code else "❌ 失敗"),
        "回應 ms": round(result.elapsed_ms, 1),
        "內容 / 錯誤": (str(result.values[:16]) + (" …" if len(result.values) > 16 else "")) if result.ok
        else result.error,
    })
    del log[MAX_LOG:]


def _read(cfg, fc, address, count, slave=None, action="讀取"):
    conn = _connection(cfg)
    try:
        result = conn.read(fc, address, count, cfg["slave"] if slave is None else slave)
    finally:
        conn.close()
    _log(action, cfg["slave"] if slave is None else slave, fc, address, count, result)
    return result


# ------------------------------------------------------------------
# 連線設定面板
# ------------------------------------------------------------------
def _known_connections() -> pd.DataFrame:
    extra = "transport, serial_settings" if column_exists("modbus_scada", "transport") \
        else "'tcp' AS transport, NULL AS serial_settings"
    return fetch_df(
        f"SELECT DISTINCT {extra}, plc_ip, plc_port, slave_id FROM modbus_scada ORDER BY plc_ip, slave_id;",
        show_error=False,
    )


def _apply_known(choice: dict):
    st.session_state.update({
        "mbd_transport": choice["transport"] or "tcp",
        "mbd_host": choice["plc_ip"],
        "mbd_port": int(choice["plc_port"] or 502),
        "mbd_serial": choice["serial_settings"] or "9600,8,N,1",
        "mbd_slave": int(choice["slave_id"] or 1),
    })


_DEFAULTS = {"mbd_transport": "tcp", "mbd_host": "", "mbd_port": 502, "mbd_serial": "9600,8,N,1",
             "mbd_slave": 1, "mbd_timeout": 2.0}


def _connection_panel():
    for key, value in _DEFAULTS.items():
        st.session_state.setdefault(key, value)
    known = _known_connections()
    with st.container(border=True):
        if not known.empty:
            options = [None] + list(range(len(known)))

            def _label(i):
                if i is None:
                    return "（手動輸入）"
                r = known.iloc[i]
                where = r["plc_ip"] if r["transport"] == "rtu" else f"{r['plc_ip']}:{r['plc_port']}"
                return f"{where}｜站號 {r['slave_id']}｜{TRANSPORTS.get(r['transport'], r['transport'])}"

            pick = st.selectbox("帶入已設定的連線", options, format_func=_label, key="mbd_pick")
            if pick is not None and st.session_state.get("_mbd_last_pick") != pick:
                _apply_known(known.iloc[pick].to_dict())
            st.session_state["_mbd_last_pick"] = pick

        c1, c2, c3, c4, c5 = st.columns([1.6, 1.6, 0.8, 0.8, 0.8])
        transport = c1.selectbox("傳輸方式", list(TRANSPORTS), format_func=TRANSPORTS.get, key="mbd_transport")
        if transport == "rtu":
            c2.text_input("序列埠路徑", key="mbd_host", placeholder="/dev/ttyUSB0")
            c3.text_input("序列埠參數", key="mbd_serial", placeholder="9600,8,N,1",
                          help="鮑率,資料位元,同位(N/E/O),停止位元")
        else:
            c2.text_input("IP 位址", key="mbd_host", placeholder="192.168.1.10")
            c3.number_input("Port", min_value=1, max_value=65535, key="mbd_port")
        c4.number_input("站號", min_value=0, max_value=255, key="mbd_slave",
                        help="Unit ID / Slave ID。純 Modbus TCP 設備通常是 1 或 255；閘道後的 RS-485 設備依設備設定")
        c5.number_input("逾時（秒）", min_value=0.2, max_value=10.0, step=0.5, key="mbd_timeout")
        if transport == "rtu":
            st.caption("⚠️ 序列埠同一時間只能被一個程式開啟；採集服務正在用這個序列埠時這裡會連不上。")
    cfg = _cfg()
    if not cfg["host"]:
        st.info("請輸入設備位址，或從「帶入已設定的連線」選擇。")
        return None
    return cfg


# ------------------------------------------------------------------
# 📖 暫存器讀取
# ------------------------------------------------------------------
def _address_input(prefix: str, default_fc: int = 3):
    c1, c2, c3 = st.columns([1.6, 1.2, 1])
    fc = c1.selectbox("功能碼", list(FUNCTION_CODES), index=list(FUNCTION_CODES).index(default_fc),
                      format_func=FUNCTION_CODES.get, key=f"{prefix}_fc")
    modicon = c3.toggle("Modicon 位址", key=f"{prefix}_modicon",
                        help="開啟後可直接輸入設備手冊上的 40001 / 30001 表示法，自動換算協議位址與功能碼")
    raw = c2.text_input("起始位址", value="0", key=f"{prefix}_addr",
                        help="協議位址從 0 開始；手冊寫 40001 的話，協議位址是 0（或開啟 Modicon 位址）")
    if modicon:
        parsed_fc, addr = parse_modicon_address(raw)
        if addr is None or parsed_fc is None:
            st.error("Modicon 位址格式應為 40001 / 30001 / 10001 / 00001（5 或 6 位數）")
            return fc, None
        if parsed_fc != fc:
            st.caption(f"↳ {raw} 代表 {FUNCTION_CODES[parsed_fc]}，已自動改用這個功能碼")
        fc = parsed_fc
        st.caption(f"↳ 協議位址 {addr}")
        return fc, addr
    if not raw.strip().isdigit():
        st.error("位址請輸入數字")
        return fc, None
    return fc, int(raw)


def _register_table(fc, start, values, previous):
    rows = []
    for i, v in enumerate(values):
        addr = start + i
        changed = previous is not None and i < len(previous) and previous[i] != v
        if fc in BIT_FUNCTIONS:
            rows.append({"位址": addr, "Modicon": modicon_address(fc, addr),
                         "狀態": "🟢 ON (1)" if v else "⚪ OFF (0)", "變動": "●" if changed else ""})
        else:
            v = int(v)
            rows.append({
                "位址": addr, "Modicon": modicon_address(fc, addr),
                "HEX": f"0x{v:04X}", "UINT16": v, "INT16": v - 65536 if v >= 32768 else v,
                "二進位": f"{v >> 8:08b} {v & 0xFF:08b}",
                "ASCII": "".join(chr(b) if 32 <= b < 127 else "·" for b in (v >> 8, v & 0xFF)),
                "變動": "●" if changed else "",
            })
    df = pd.DataFrame(rows)

    def _highlight(row):
        return ["background-color: rgba(255, 197, 61, 0.35)" if row["變動"] else "" for _ in row]

    return df.style.apply(_highlight, axis=1)


def _decode_table(start, values, data_type):
    rows = interpretations(values, data_type)
    if not rows:
        return None
    out = []
    for r in rows:
        item = {"起始位址": start + r["offset"], "Modicon": modicon_address(3, start + r["offset"])}
        for name in ORDER_PRESETS:
            v = r[name]
            item[name] = (format_number(v) + ("" if is_plausible(v) else " ✗")) if v is not None else "—"
        out.append(item)
    df = pd.DataFrame(out)

    def _style(col):
        if col.name not in ORDER_PRESETS:
            return [""] * len(col)
        return ["color: rgba(128,128,128,0.55)" if str(v).endswith("✗") else "font-weight: 600" for v in col]

    return df.style.apply(_style, axis=0)


def _tab_read(cfg):
    fc, start = _address_input("mbr", 3)
    c1, c2, c3, c4 = st.columns([1, 1, 1, 1.2])
    max_count = 2000 if fc in BIT_FUNCTIONS else 125
    count = c1.number_input("數量", min_value=1, max_value=max_count, value=10, key="mbr_count")
    read_once = c2.button("📖 讀取一次", type="primary", disabled=start is None, width="stretch")
    monitor = c3.toggle("🔁 連續監看", key="mbr_monitor", disabled=start is None)
    interval = c4.selectbox("監看間隔", [1, 2, 5, 10], index=1, format_func=lambda s: f"每 {s} 秒",
                            key="mbr_interval", disabled=not monitor)
    if read_once:
        st.session_state["mbr_read_now"] = True

    @st.fragment(run_every=interval if monitor and start is not None else None)
    def live():
        if start is not None and (monitor or st.session_state.pop("mbr_read_now", False)):
            res = _read(cfg, fc, start, int(count))
            last = st.session_state.get("mbr_last")
            st.session_state["mbr_prev"] = last["values"] if last and last["fc"] == fc and last["start"] == start else None
            st.session_state["mbr_last"] = {
                "fc": fc, "start": start, "ok": res.ok, "values": res.values, "error": res.error,
                "ms": res.elapsed_ms, "at": datetime.now(LOCAL_TZ).strftime("%H:%M:%S"),
                "slave": cfg["slave"],
            }
        last = st.session_state.get("mbr_last")
        if not last:
            st.caption("按「讀取一次」或開啟「連續監看」開始。")
            return
        if not last["ok"]:
            st.error(f"❌ {last['at']} 讀取失敗（{last['ms']:.0f} ms）：{last['error']}")
            return
        st.success(
            f"✅ {last['at']}｜站號 {last['slave']}｜{FUNCTION_CODES[last['fc']]}｜"
            f"位址 {last['start']} 起 {len(last['values'])} 筆｜回應 {last['ms']:.0f} ms"
        )
        left, right = st.columns([1.15, 1])
        with left:
            st.markdown("**原始資料**（黃底 = 與上一次讀取不同）")
            st.dataframe(_register_table(last["fc"], last["start"], last["values"], st.session_state.get("mbr_prev")),
                         hide_index=True, width="stretch", height=min(36 * (len(last["values"]) + 1), 460))
        with right:
            if last["fc"] in BIT_FUNCTIONS:
                st.caption("線圈 / 離散輸入是位元資料，不需要位元組順序。")
            else:
                dt = st.selectbox("多暫存器解碼", MULTI_TYPES, format_func=TYPE_LABELS.get, key="mbr_dt")
                st.markdown("**四種位元組順序對照**（粗體 = 數值合理，灰色 ✗ = 不像量測值）")
                table = _decode_table(last["start"], last["values"], dt)
                if table is None:
                    st.caption(f"資料不足：{dt} 需要 {register_count(dt)} 個暫存器")
                else:
                    st.dataframe(table, hide_index=True, width="stretch", height=min(36 * (len(last["values"]) + 1), 460))
                    st.caption("ABCD = 標準（BIG/BIG）｜CDAB = word swap（BIG/LITTLE，台灣電表最常見）｜"
                               "BADC = byte swap｜DCBA = 全反轉。對照設備面板或手冊上的數值找出正確的那一欄。")

    live()
    _create_tag_form(cfg)


def _create_tag_form(cfg):
    last = st.session_state.get("mbr_last")
    if not last or not last["ok"]:
        return
    with st.expander("📌 用這次讀到的資料建立點位", expanded=False):
        fc, start, values = last["fc"], last["start"], last["values"]
        c1, c2, c3 = st.columns(3)
        addr = c1.number_input("點位位址", min_value=start, max_value=start + len(values) - 1, value=start,
                               key="mbc_addr")
        if fc in BIT_FUNCTIONS:
            dt, order = "bool", "ABCD"
        else:
            dt = c2.selectbox("資料型態", list(DATA_TYPES), index=list(DATA_TYPES).index("float32"),
                              format_func=TYPE_LABELS.get, key="mbc_dt")
            order = c3.radio("位元組順序", list(ORDER_PRESETS), horizontal=True, key="mbc_order")
        offset = int(addr) - start
        span = 1 if fc in BIT_FUNCTIONS else register_count(dt)
        bo, wo = ORDER_PRESETS[order]
        chunk = values[offset:offset + span]
        preview = decode(chunk, dt, bo, wo) if len(chunk) == span else None

        c4, c5, c6 = st.columns(3)
        name = c4.text_input("點位名稱", key="mbc_name", placeholder="例如 B03_電表_有效功率")
        unit = c5.text_input("單位", key="mbc_unit", placeholder="kW")
        label_to_id, _ = _load_sensor_options()
        binding_map = _load_sensor_binding_map()
        options, _ = _sensor_select_options(label_to_id, binding_map)
        sensor_label = c6.selectbox("綁定感測器（選填）", options, key="mbc_sensor")
        use_scale = st.checkbox("線性換算（原始值 → 工程值）", key="mbc_scale")
        raw_min = raw_max = eng_min = eng_max = None
        if use_scale:
            s1, s2, s3, s4 = st.columns(4)
            raw_min = s1.number_input("原始最小", value=0.0, key="mbc_rmin")
            raw_max = s2.number_input("原始最大", value=10000.0, key="mbc_rmax")
            eng_min = s3.number_input("工程最小", value=0.0, key="mbc_emin")
            eng_max = s4.number_input("工程最大", value=1000.0, key="mbc_emax")
        final = apply_linear_scaling(preview, raw_min, raw_max, eng_min, eng_max) if use_scale else preview
        if preview is None:
            st.warning("所選位址之後的暫存器不夠解碼這個型態，請多讀幾筆或換起始位址。")
        else:
            st.info(f"預覽：位址 {addr}（{modicon_address(fc, int(addr))}）{dt} {order} → "
                    f"**{format_number(final)} {unit}**")
        if not MODBUS_ENABLED:
            st.caption("ℹ️ .env 目前 MODBUS_ENABLED=false，點位會存進資料庫，但要啟用後採集服務才會開始讀取。")
        if st.button("建立點位", type="primary", disabled=not name.strip() or preview is None, key="mbc_create"):
            sensor_id = label_to_id.get(sensor_label) if sensor_label != UNBOUND_LABEL else None
            conflict = _find_binding_conflict(_load_sensor_binding_map(), sensor_id, "modbus_scada", None)
            if conflict:
                st.error(f"想綁定的感測器已被其他點位使用：{conflict}")
                return
            cols = ["name", "plc_ip", "plc_port", "slave_id", "function_code", "start_address", "data_type",
                    "byte_order", "word_order", "raw_min", "raw_max", "eng_min", "eng_max", "unit",
                    "sensor_id", "plc_state"]
            params = [name.strip(), cfg["host"], cfg["port"], cfg["slave"], fc, int(addr), dt, bo, wo,
                      raw_min, raw_max, eng_min, eng_max, unit.strip() or None, sensor_id, "OFFLINE"]
            if column_exists("modbus_scada", "transport"):
                cols += ["transport", "serial_settings"]
                params += [cfg["transport"], cfg["serial"] if cfg["transport"] == "rtu" else None]
            try:
                execute(
                    f"INSERT INTO modbus_scada ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))});",
                    params,
                )
                audit_ui("modbus.create", f"modbus:{name.strip()}", {
                    "source": "線上調適", "address": int(addr), "fc": fc, "data_type": dt, "order": order,
                    "host": cfg["host"], "slave": cfg["slave"], "preview": final,
                })
                st.success(f"✅ 已建立點位「{name.strip()}」")
            except Exception as e:
                st.error(f"❌ 建立失敗: {e}")


# ------------------------------------------------------------------
# 🎯 點位驗證
# ------------------------------------------------------------------
def _tab_verify():
    extra = ", transport, serial_settings" if column_exists("modbus_scada", "transport") \
        else ", 'tcp' AS transport, NULL AS serial_settings"
    tags = fetch_df(
        f"""SELECT id, name, plc_ip, plc_port, slave_id, function_code, start_address, data_type,
                   byte_order, word_order, raw_min, raw_max, eng_min, eng_max, unit,
                   current_value, plc_state {extra}
            FROM modbus_scada ORDER BY plc_ip, slave_id, function_code, start_address;""",
        show_error=False,
    )
    if tags.empty:
        st.info("目前沒有任何 Modbus 點位。可以在「暫存器讀取」找到資料後直接建立。")
        return
    tags["conn"] = tags.apply(
        lambda r: r["plc_ip"] if r["transport"] == "rtu" else f"{r['plc_ip']}:{r['plc_port']}", axis=1)
    c1, c2 = st.columns([1, 2])
    conn_label = c1.selectbox("連線", sorted(tags["conn"].unique()), key="mbv_conn")
    pool = tags[tags["conn"] == conn_label]
    labels = {int(r["id"]): f"#{int(r['id'])} {r['name']}（站號 {r['slave_id']}，FC{int(r['function_code']):02d} @ {r['start_address']}）"
              for _, r in pool.iterrows()}
    chosen = c2.multiselect("點位（不選 = 這條連線全部，最多 50 個）", list(labels), format_func=labels.get,
                            key="mbv_tags")
    target = pool[pool["id"].isin(chosen)] if chosen else pool.head(50)

    if st.button("🎯 立即讀取驗證", type="primary"):
        first = target.iloc[0]
        conn = ModbusConnection(first["transport"] or "tcp", first["plc_ip"], int(first["plc_port"] or 502),
                                timeout=2.0, serial_settings=first["serial_settings"], retries=0)
        rows = []
        try:
            for _, t in target.iterrows():
                fc = int(t["function_code"])
                span = 1 if fc in BIT_FUNCTIONS else register_count(t["data_type"])
                res = conn.read(fc, int(t["start_address"]), span, int(t["slave_id"]))
                _log("點位驗證", int(t["slave_id"]), fc, int(t["start_address"]), span, res)
                row = {"id": int(t["id"]), "點位": t["name"], "位址": modicon_address(fc, int(t["start_address"])),
                       "型態": normalize_type(t["data_type"]),
                       "目前順序": order_name(t["byte_order"], t["word_order"]),
                       "資料庫目前值": format_number(t["current_value"]) if pd.notnull(t["current_value"]) else "—"}
                if not res.ok:
                    row.update({"狀態": "❌ " + (res.error or "")[:60]})
                    rows.append(row)
                    continue
                raw = decode(res.values, t["data_type"], t["byte_order"], t["word_order"])
                eng = apply_linear_scaling(raw, t["raw_min"], t["raw_max"], t["eng_min"], t["eng_max"])
                row.update({
                    "原始暫存器": " ".join(f"{int(v):04X}" for v in res.values) if fc not in BIT_FUNCTIONS
                    else str(res.values),
                    "解碼值": format_number(raw) if raw is not None else "—",
                    "工程值": (f"{format_number(eng)} {t['unit'] if pd.notnull(t['unit']) else ''}".strip()
                              if eng is not None else "—"),
                })
                suggestion = ""
                if span >= 2:
                    alts = {n: decode(res.values, t["data_type"], bo, wo) for n, (bo, wo) in ORDER_PRESETS.items()}
                    for n, v in alts.items():
                        row[n] = format_number(v) if v is not None else "—"
                    if not is_plausible(raw):
                        good = [n for n, v in alts.items() if is_plausible(v)]
                        if good:
                            suggestion = f"⚠️ 目前順序解出的值不合理，可能是 {' / '.join(good)}"
                row["狀態"] = suggestion or "✅"
                rows.append(row)
        finally:
            conn.close()
        st.session_state["mbv_result"] = rows

    rows = st.session_state.get("mbv_result")
    if not rows:
        st.caption("用每個點位自己的設定（位址 / 型態 / 位元組順序 / 換算）即時讀一次，跟採集服務讀到的完全一樣。")
        return
    st.dataframe(pd.DataFrame(rows).fillna(""), hide_index=True, width="stretch")

    with st.form("mbv_apply"):
        st.markdown("**套用新的位元組順序**")
        c1, c2, c3 = st.columns([2, 1, 1])
        ids = [r["id"] for r in rows]
        tag_id = c1.selectbox("點位", ids, format_func=lambda i: next(r["點位"] for r in rows if r["id"] == i))
        order = c2.selectbox("位元組順序", list(ORDER_PRESETS))
        if c3.form_submit_button("套用", type="primary"):
            bo, wo = ORDER_PRESETS[order]
            old = next(r["目前順序"] for r in rows if r["id"] == tag_id)
            execute("UPDATE modbus_scada SET byte_order=%s, word_order=%s WHERE id=%s;", (bo, wo, int(tag_id)))
            audit_ui("modbus.update", f"modbus:{tag_id}", {"byte/word order": [old, order], "source": "線上調適"})
            st.success(f"✅ 已將點位 #{tag_id} 改為 {order}（{bo}/{wo}），下一輪採集生效。")
            st.session_state.pop("mbv_result", None)


# ------------------------------------------------------------------
# 🔍 站號掃描
# ------------------------------------------------------------------
def _tab_scan(cfg):
    st.caption("對每個站號送一個讀取請求：有回資料或回例外碼都代表該站號有設備；逾時代表沒有。")
    c1, c2, c3, c4, c5 = st.columns(5)
    first = c1.number_input("起始站號", 1, 247, 1, key="mbs_from")
    last = c2.number_input("結束站號", 1, 247, 10, key="mbs_to")
    fc = c3.selectbox("功能碼", [3, 4, 1, 2], key="mbs_fc", format_func=lambda f: f"FC{f:02d}")
    addr = c4.number_input("位址", 0, 65535, 0, key="mbs_addr")
    timeout = c5.number_input("每站逾時（秒）", 0.1, 5.0, 0.3, 0.1, key="mbs_timeout")
    total = max(int(last) - int(first) + 1, 0)
    st.caption(f"最多約需 {total * timeout:.0f} 秒（全部沒回應時）。")
    if st.button("🔍 開始掃描", type="primary", disabled=total == 0):
        conn = _connection(cfg, timeout=timeout, retries=0)
        found = []
        bar = st.progress(0.0, text="掃描中…")
        try:
            for i, slave in enumerate(range(int(first), int(last) + 1)):
                res = conn.read(fc, int(addr), 1, slave)
                _log("站號掃描", slave, fc, int(addr), 1, res)
                if res.device_responded:
                    found.append({"站號": slave,
                                  "結果": "✅ 有回應" if res.ok else f"⚠️ 回應例外碼（設備存在）：{res.error}",
                                  "回應 ms": round(res.elapsed_ms, 1),
                                  "讀到的值": str(res.values) if res.ok else ""})
                if res.fatal and not res.device_responded:
                    st.error(f"連線失敗，停止掃描：{res.error}")
                    break
                bar.progress((i + 1) / total, text=f"掃描中… 站號 {slave}（已找到 {len(found)} 台）")
        finally:
            conn.close()
            bar.empty()
        st.session_state["mbs_found"] = found
        st.session_state["mbs_done_at"] = datetime.now(LOCAL_TZ).strftime("%H:%M:%S")

    if "mbs_found" in st.session_state:
        found = st.session_state["mbs_found"]
        if found:
            st.success(f"{st.session_state['mbs_done_at']} 掃描完成，找到 {len(found)} 個站號")
            st.dataframe(pd.DataFrame(found), hide_index=True, width="stretch")
        else:
            st.warning("沒有任何站號回應。請確認連線方式、鮑率 / 同位設定、RS-485 A/B 線是否接反。")


# ------------------------------------------------------------------
# ✏️ 寫入測試
# ------------------------------------------------------------------
def _tab_write(cfg):
    if not WRITE_ENABLED:
        st.info(
            "🔒 寫入功能預設關閉。寫入會直接改變現場設備的狀態或設定值（例如啟停、設定溫度），"
            "需要時請在 .env 設定 `MODBUS_DEBUG_WRITE_ENABLED=true` 並重新啟動網頁服務。"
            "啟用後只有工程師以上可以使用，每次寫入都會記錄在稽核紀錄。"
        )
        return
    st.warning("⚠️ 寫入會直接改變現場設備。請確認位址與數值，並確定現場人員知情。")
    kind = st.radio("寫入對象", ["保持暫存器（FC06 / FC16）", "線圈（FC05）"], horizontal=True, key="mbw_kind")
    is_coil = kind.startswith("線圈")
    c1, c2 = st.columns(2)
    addr_raw = c1.text_input("位址（協議位址，0 起算）", "0", key="mbw_addr")
    if not addr_raw.strip().isdigit():
        st.error("位址請輸入數字")
        return
    addr = int(addr_raw)
    if is_coil:
        value = c2.radio("值", ["ON (1)", "OFF (0)"], horizontal=True, key="mbw_coil") == "ON (1)"
        registers = None
    else:
        dt = c2.selectbox("資料型態", [t for t in DATA_TYPES if t != "bool"], index=0,
                          format_func=TYPE_LABELS.get, key="mbw_dt")
        c3, c4 = st.columns(2)
        order = c3.radio("位元組順序", list(ORDER_PRESETS), horizontal=True, key="mbw_order")
        value = c4.number_input("值", value=0.0, format="%g", key="mbw_value")
        try:
            registers = encode(value, dt, *ORDER_PRESETS[order])
        except (ValueError, OverflowError, struct.error) as e:
            st.error(f"數值無法編碼成 {dt}（超出範圍？）：{e}")
            return
        st.caption(f"將寫入 {len(registers)} 個暫存器：{' '.join(f'{r:04X}' for r in registers)}"
                   f"（{'FC06' if len(registers) == 1 else 'FC16'}）")

    if st.button("讀回目前值", key="mbw_peek"):
        fc = 1 if is_coil else 3
        res = _read(cfg, fc, addr, 1 if is_coil else len(registers), action="寫入前讀取")
        st.session_state["mbw_peek_result"] = res
    peek = st.session_state.get("mbw_peek_result")
    if peek is not None:
        if peek.ok:
            current = peek.values[0] if is_coil else decode(peek.values, dt, *ORDER_PRESETS[order])
            st.caption(f"目前值：{current}（原始 {peek.values}）")
        else:
            st.caption(f"讀取失敗：{peek.error}")

    confirm = st.checkbox(f"我確認要寫入站號 {cfg['slave']}、位址 {addr}，並了解這會改變現場設備", key="mbw_confirm")
    if st.button("✏️ 寫入", type="primary", disabled=not confirm, key="mbw_go"):
        conn = _connection(cfg)
        try:
            if is_coil:
                res = conn.write_coil(addr, value, cfg["slave"])
                _log("寫入 FC05", cfg["slave"], 5, addr, 1, res)
                back = conn.read(1, addr, 1, cfg["slave"]) if res.ok else None
            else:
                res = conn.write_registers(addr, registers, cfg["slave"])
                _log("寫入 FC06" if len(registers) == 1 else "寫入 FC16", cfg["slave"],
                     6 if len(registers) == 1 else 16, addr, len(registers), res)
                back = conn.read(3, addr, len(registers), cfg["slave"]) if res.ok else None
        finally:
            conn.close()
        audit_ui("modbus.write", f"modbus:{cfg['host']}/{cfg['slave']}/{addr}", {
            "value": value, "registers": registers, "ok": res.ok, "error": res.error,
            "transport": cfg["transport"],
        })
        if not res.ok:
            st.error(f"❌ 寫入失敗：{res.error}")
        elif back is not None and back.ok:
            matched = (back.values[0] == value) if is_coil else (back.values == registers)
            (st.success if matched else st.warning)(
                f"{'✅ 寫入成功，讀回一致' if matched else '⚠️ 寫入成功，但讀回的值不同（設備可能有限制或會自行變更）'}：{back.values}")
        else:
            st.success("✅ 寫入成功（讀回失敗，請手動確認）")


# ------------------------------------------------------------------
def _tab_log():
    log = st.session_state.get(LOG_KEY, [])
    if not log:
        st.caption("還沒有任何通訊紀錄。")
        return
    df = pd.DataFrame(log)
    ok = (df["結果"] == "✅ 成功").sum()
    c1, c2, c3 = st.columns(3)
    c1.metric("請求數", len(df), border=True)
    c2.metric("成功率", f"{ok / len(df) * 100:.0f}%", border=True)
    c3.metric("平均回應", f"{df['回應 ms'].mean():.0f} ms", border=True)
    st.dataframe(df, hide_index=True, width="stretch", height=420)
    b1, b2 = st.columns(2)
    b1.download_button("⬇️ 下載 CSV", to_csv_bytes(df), file_name=f"modbus_log_{time.strftime('%Y%m%d_%H%M%S')}.csv",
                       mime="text/csv")
    if b2.button("清除紀錄"):
        st.session_state[LOG_KEY] = []
        st.rerun()


def render():
    require_role("engineer")
    st.title("🔧 Modbus 線上調適")
    st.caption("用跟採集服務完全相同的連線與解碼方式直接跟設備通訊：找位址、確認位元組順序、掃描站號、驗證點位設定。")
    cfg = _connection_panel()
    tabs = st.tabs(["📖 暫存器讀取", "🎯 點位驗證", "🔍 站號掃描", "✏️ 寫入測試", "📜 通訊紀錄"])
    with tabs[1]:
        _tab_verify()
    with tabs[4]:
        _tab_log()
    if cfg is None:
        return
    with tabs[0]:
        _tab_read(cfg)
    with tabs[2]:
        _tab_scan(cfg)
    with tabs[3]:
        _tab_write(cfg)
