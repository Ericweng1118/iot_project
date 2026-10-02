"""
web/pages/s7_debug.py
=====================
🔧 S7 線上調適（engineer 以上）

接 Siemens PLC 時最常卡住的是：PUT/GET 沒開、DB 是「最佳化的區塊存取」、Rack/Slot 不對、
offset 算錯、型態選錯。這一頁用跟採集程式完全相同的連線與解碼（protocols/s7_codec.py）
直接跟 PLC 通訊，現場直接確認。

分頁：
    🖥️ PLC 資訊    CPU 型號、訂貨號、韌體、RUN/STOP、PDU 大小；S7-1200/1500 必要設定檢查表
    📖 記憶體讀取  DB / M / I / Q 任意範圍；直接輸入 TIA 位址（%DB1.DBD0、MW10、I0.1）；
                  連續監看標示變動；每個位元組以 INT / WORD / DINT / DWORD / REAL / LREAL 並列解碼；
                  位元檢視；一鍵建立點位
    🎯 點位驗證    用已建點位的設定即時讀取，比對資料庫目前值
    ✏️ 寫入測試    預設關閉（S7_DEBUG_WRITE_ENABLED=true 才開）；BOOL 只改那一個位元（讀-改-寫），
                  寫入後讀回確認，每次寫入記錄在稽核紀錄
    📜 通訊紀錄
"""

import time
from datetime import datetime

import pandas as pd
import streamlit as st

from core.config import TIA_ENABLED, env_bool
from protocols import s7_codec as C
from protocols.s7_protocol import S7Connection
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

WRITE_ENABLED = env_bool("S7_DEBUG_WRITE_ENABLED", False)
LOG_KEY = "s7d_log"
INTERP_TYPES = ["INT", "WORD", "DINT", "DWORD", "REAL", "LREAL"]
_DEFAULTS = {"s7d_ip": "", "s7d_rack": 0, "s7d_slot": 1, "s7d_port": 102}


def _cfg():
    ss = st.session_state
    return {"ip": (ss.get("s7d_ip") or "").strip(), "rack": int(ss.get("s7d_rack") or 0),
            "slot": int(ss.get("s7d_slot") if ss.get("s7d_slot") is not None else 1),
            "port": int(ss.get("s7d_port") or 102)}


def _conn(cfg) -> S7Connection:
    return S7Connection(cfg["ip"], cfg["rack"], cfg["slot"], cfg["port"])


def _log(action, area, db, start, size, result):
    log = st.session_state.setdefault(LOG_KEY, [])
    where = f"DB{db}" if area == "DB" else area
    log.insert(0, {
        "時間": datetime.now(LOCAL_TZ).strftime("%H:%M:%S.%f")[:-3], "動作": action,
        "區域": where, "起始 byte": start, "長度": size,
        "結果": "✅ 成功" if result.ok else "❌ 失敗", "回應 ms": round(result.elapsed_ms, 1),
        "內容 / 錯誤": result.data[:16].hex(" ").upper() + (" …" if len(result.data) > 16 else "") if result.ok
        else result.error,
    })
    del log[300:]


def _read(cfg, area, db, start, size, action="讀取"):
    conn = _conn(cfg)
    try:
        res = conn.read(area, db, start, size)
    finally:
        conn.close()
    _log(action, area, db, start, size, res)
    return res


# ------------------------------------------------------------------
# 連線面板
# ------------------------------------------------------------------
def _connection_panel():
    for k, v in _DEFAULTS.items():
        st.session_state.setdefault(k, v)
    v33 = column_exists("tia_scada", "rack")
    known = fetch_df(
        f"SELECT DISTINCT plc_ip, plc_name, {'rack, slot' if v33 else '0 AS rack, 1 AS slot'} "
        f"FROM tia_scada ORDER BY plc_ip;", show_error=False)
    with st.container(border=True):
        if not known.empty:
            options = [None] + list(range(len(known)))
            pick = st.selectbox(
                "帶入已設定的 PLC", options, key="s7d_pick",
                format_func=lambda i: "（手動輸入）" if i is None else
                f"{known.iloc[i]['plc_ip']}｜{known.iloc[i]['plc_name']}｜Rack {known.iloc[i]['rack']} / Slot {known.iloc[i]['slot']}")
            if pick is not None and st.session_state.get("_s7d_last_pick") != pick:
                r = known.iloc[pick]
                st.session_state.update(s7d_ip=r["plc_ip"], s7d_rack=int(r["rack"]), s7d_slot=int(r["slot"]))
            st.session_state["_s7d_last_pick"] = pick
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        c1.text_input("PLC IP", key="s7d_ip", placeholder="192.168.0.1")
        c2.number_input("Rack", 0, 7, key="s7d_rack")
        c3.number_input("Slot", 0, 31, key="s7d_slot", help="S7-1200/1500 = 1，S7-300 = 2，S7-400 依 CPU 所在槽位")
        c4.number_input("Port", 1, 65535, key="s7d_port", help="S7 固定是 102，只有經過埠轉發時才需要改")
    cfg = _cfg()
    if not cfg["ip"]:
        st.info("請輸入 PLC IP，或從「帶入已設定的 PLC」選擇。")
        return None
    return cfg


# ------------------------------------------------------------------
# 🖥️ PLC 資訊
# ------------------------------------------------------------------
def _tab_info(cfg):
    if st.button("🖥️ 讀取 CPU 資訊", type="primary", key="s7d_info_btn"):
        conn = _conn(cfg)
        started = time.perf_counter()
        try:
            st.session_state["s7d_info"] = (conn.cpu_info(), (time.perf_counter() - started) * 1000)
        finally:
            conn.close()
    info = st.session_state.get("s7d_info")
    if info:
        data, ms = info
        if data.get("error"):
            st.error(f"❌ {data['error']}")
        else:
            state = data.get("state", "?")
            (st.success if state == "RUN" else st.warning)(
                f"{'🟢' if state == 'RUN' else '🟠'} CPU 狀態：{state}｜回應 {ms:.0f} ms")
            c1, c2, c3 = st.columns(3)
            c1.metric("型號", data.get("ModuleTypeName") or "—", border=True)
            c2.metric("訂貨號", data.get("order_code") or "—", border=True)
            c3.metric("韌體", data.get("firmware") or "—", border=True)
            c4, c5, c6 = st.columns(3)
            c4.metric("站名", data.get("ASName") or "—", border=True)
            c5.metric("序號", data.get("SerialNumber") or "—", border=True)
            c6.metric("PDU", data.get("pdu") or "—", border=True,
                      help="單次請求的最大封包大小；S7-1200 通常 240、S7-1500 960，越大一次能讀越多")
            for k, v in data.items():
                if k.endswith("_error"):
                    st.caption(f"⚠️ {k.replace('_error', '')}：{v}")
    with st.expander("✅ S7-1200 / S7-1500 必要設定（連不上或讀不到時先檢查這裡）", expanded=not info):
        st.markdown(
            "1. **允許 PUT/GET**：TIA Portal → 裝置組態 → CPU 屬性 → 防護與安全 → 連線機制 → "
            "勾選「允許來自遠端物件的 PUT/GET 通訊存取」\n"
            "2. **取消最佳化的區塊存取**：要讀的每個 DB → 右鍵屬性 → 屬性 → 取消勾選「最佳化的區塊存取」，"
            "編譯後 DB 裡才看得到每個變數的 offset\n"
            "3. **存取等級**：防護等級不要設成「完全防護」\n"
            "4. **下載**：以上修改都要「下載到裝置」才會生效（DB 改成非最佳化會重新初始化數值）\n"
            "5. **Rack / Slot**：S7-1200/1500 = 0 / 1，S7-300 = 0 / 2"
        )


# ------------------------------------------------------------------
# 📖 記憶體讀取
# ------------------------------------------------------------------
def _apply_quick_address():
    try:
        a = C.parse_address(st.session_state.get("s7r_quick", ""))
    except ValueError as e:
        st.session_state["s7r_quick_err"] = str(e)
        return
    st.session_state.pop("s7r_quick_err", None)
    st.session_state.update(s7r_area=a["area"], s7r_db=a["db"] or st.session_state.get("s7r_db", 1),
                            s7r_start=a["byte"])
    st.session_state["s7r_hint"] = a


def _byte_table(start, data, previous, area, db):
    rows = []
    for i, b in enumerate(data):
        changed = previous is not None and i < len(previous) and previous[i] != b
        addr = f"DB{db}.DBB{start + i}" if area == "DB" else f"{area}B{start + i}"
        rows.append({"byte": start + i, "TIA 位址": addr, "HEX": f"{b:02X}", "十進位": b,
                     "位元 7 6 5 4 3 2 1 0": " ".join("1" if b >> k & 1 else "·" for k in range(7, -1, -1)),
                     "變動": "●" if changed else ""})
    df = pd.DataFrame(rows)
    return df.style.apply(lambda r: ["background-color: rgba(255,197,61,.35)" if r["變動"] else "" for _ in r], axis=1)


def _interp_table(start, data, area, db):
    rows = []
    for r in C.interpretations(data, start):
        item = {"byte": r["位址"], "雙字位址": C.format_address(area, db, r["位址"], "REAL")}
        for dt in INTERP_TYPES:
            v = r[dt]
            item[dt] = "—" if v is None else format_number(v)
        rows.append(item)
    return pd.DataFrame(rows)


def _tab_read(cfg):
    st.session_state.setdefault("s7r_area", "DB")
    st.session_state.setdefault("s7r_db", 1)
    st.session_state.setdefault("s7r_start", 0)
    st.session_state.setdefault("s7r_len", 20)
    q1, q2 = st.columns([3, 1])
    q1.text_input("TIA 位址快速輸入", key="s7r_quick", placeholder="%DB1.DBD0、DB1.DBX10.3、MW20、IB0…",
                  on_change=_apply_quick_address, help="輸入後按 Enter，自動填入下方的區域 / DB / 起始 byte")
    if st.session_state.get("s7r_quick_err"):
        st.error(st.session_state["s7r_quick_err"])
    c1, c2, c3, c4 = st.columns(4)
    area = c1.selectbox("區域", list(C.AREAS), format_func=C.AREAS.get, key="s7r_area")
    db = c2.number_input("DB 編號", 1, 65535, key="s7r_db", disabled=area != "DB")
    start = c3.number_input("起始 byte", 0, 65535, key="s7r_start")
    size = c4.number_input("長度（bytes）", 1, 480, key="s7r_len")
    b1, b2, b3 = st.columns([1, 1, 1.2])
    if b1.button("📖 讀取一次", type="primary", width="stretch", key="s7r_once"):
        st.session_state["s7r_now"] = True
    monitor = b2.toggle("🔁 連續監看", key="s7r_monitor")
    interval = b3.selectbox("監看間隔", [1, 2, 5, 10], index=1, format_func=lambda s: f"每 {s} 秒",
                            key="s7r_interval", disabled=not monitor)

    @st.fragment(run_every=interval if monitor else None)
    def live():
        if monitor or st.session_state.pop("s7r_now", False):
            res = _read(cfg, area, int(db), int(start), int(size))
            last = st.session_state.get("s7r_last")
            same = last and (last["area"], last["db"], last["start"]) == (area, int(db), int(start))
            st.session_state["s7r_prev"] = last["data"] if same and last["ok"] else None
            st.session_state["s7r_last"] = {"area": area, "db": int(db), "start": int(start), "ok": res.ok,
                                            "data": res.data, "error": res.error, "ms": res.elapsed_ms,
                                            "at": datetime.now(LOCAL_TZ).strftime("%H:%M:%S")}
        last = st.session_state.get("s7r_last")
        if not last:
            st.caption("按「讀取一次」或開啟「連續監看」開始。")
            return
        if not last["ok"]:
            st.error(f"❌ {last['at']} 讀取失敗（{last['ms']:.0f} ms）：{last['error']}")
            return
        where = f"DB{last['db']}" if last["area"] == "DB" else C.AREAS[last["area"]]
        st.success(f"✅ {last['at']}｜{where} byte {last['start']} 起 {len(last['data'])} bytes｜回應 {last['ms']:.0f} ms")
        hint = st.session_state.get("s7r_hint")
        if hint and hint["byte"] >= last["start"] and hint["byte"] < last["start"] + len(last["data"]):
            rel = hint["byte"] - last["start"]
            dt = C.suggest_type(hint["width"])
            try:
                v = C.decode(last["data"], rel, dt, hint["bit"])
                shown = format_number(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
                st.info(f"📍 {st.session_state.get('s7r_quick')} = **{shown}**（以 {dt} 解碼；其他型態看下表）")
            except ValueError:
                pass
        left, right = st.columns([1, 1.4])
        with left:
            st.markdown("**位元組**（黃底 = 與上一次不同）")
            st.dataframe(_byte_table(last["start"], last["data"], st.session_state.get("s7r_prev"),
                                     last["area"], last["db"]),
                         hide_index=True, width="stretch", height=min(36 * (len(last["data"]) + 1), 460))
        with right:
            st.markdown("**以各型態解碼**（S7 一律 Big-Endian；從該 byte 開始讀）")
            st.dataframe(_interp_table(last["start"], last["data"], last["area"], last["db"]),
                         hide_index=True, width="stretch", height=min(36 * (len(last["data"]) + 1), 460))
            st.caption("在 TIA Portal 打開 DB（取消最佳化後）看「偏移」欄，就是這裡的 byte。")

    live()
    _create_tag(cfg)


def _create_tag(cfg):
    last = st.session_state.get("s7r_last")
    if not last or not last["ok"]:
        return
    with st.expander("📌 用這次讀到的資料建立點位"):
        c1, c2, c3 = st.columns(3)
        byte = c1.number_input("byte", last["start"], last["start"] + len(last["data"]) - 1, last["start"], key="s7c_byte")
        dt = c2.selectbox("型態", C.NUMERIC_TYPES + ["STRING[20]"], index=C.NUMERIC_TYPES.index("REAL"), key="s7c_dt")
        bit = c3.number_input("bit（BOOL 用）", 0, 7, 0, key="s7c_bit", disabled=dt != "BOOL")
        rel = int(byte) - last["start"]
        try:
            preview = C.decode(last["data"], rel, dt, int(bit))
        except ValueError as e:
            preview, err = None, str(e)
        address = C.format_address(last["area"], last["db"], int(byte), dt, int(bit))
        if preview is None:
            st.warning(f"{err}：請多讀幾個 bytes")
        else:
            st.info(f"預覽：{address} = **{preview}**")
        c4, c5, c6 = st.columns(3)
        name = c4.text_input("點位名稱", key="s7c_name", placeholder="例如 鍋爐溫度")
        plc_name = c5.text_input("PLC 名稱", key="s7c_plc", value="PLC_" + cfg["ip"].split(".")[-1])
        label_to_id, _ = _load_sensor_options()
        options, _ = _sensor_select_options(label_to_id, _load_sensor_binding_map())
        sensor_label = c6.selectbox("綁定感測器（選填）", options, key="s7c_sensor")
        v33 = column_exists("tia_scada", "area")
        if last["area"] != "DB" and not v33:
            st.warning("尚未執行 sql/019，只能建立 DB 區域的點位。")
        if not TIA_ENABLED:
            st.caption("ℹ️ .env 目前 TIA_ENABLED=false，點位會存進資料庫，但要啟用後採集服務才會開始讀取。")
        disabled = preview is None or not name.strip() or (last["area"] != "DB" and not v33) \
            or (dt == "BOOL" and int(bit) != 0 and not v33)
        if st.button("建立點位", type="primary", disabled=disabled, key="s7c_go"):
            sensor_id = label_to_id.get(sensor_label) if sensor_label != UNBOUND_LABEL else None
            conflict = _find_binding_conflict(_load_sensor_binding_map(), sensor_id, "tia_scada", None)
            if conflict:
                st.error(f"想綁定的感測器已被其他點位使用：{conflict}")
                return
            cols = ["name", "plc_name", "plc_ip", "db_number", '"offset"', "data_type", "sensor_id", "plc_state"]
            vals = [name.strip(), plc_name.strip(), cfg["ip"], last["db"] if last["area"] == "DB" else 0,
                    int(byte), dt, sensor_id, "OFFLINE"]
            if v33:
                cols += ["area", "bit_offset", "rack", "slot"]
                vals += [last["area"], int(bit), cfg["rack"], cfg["slot"]]
            execute(f"INSERT INTO tia_scada ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))});", vals)
            audit_ui("tia.create", f"tia:{name.strip()}", {"source": "S7 線上調適", "address": address,
                                                           "plc": cfg["ip"], "preview": str(preview)})
            st.success(f"✅ 已建立點位「{name.strip()}」（{address}）")


# ------------------------------------------------------------------
# 🎯 點位驗證
# ------------------------------------------------------------------
def _tab_verify(cfg):
    v33 = column_exists("tia_scada", "area")
    extra = "area, bit_offset, rack, slot" if v33 else "'DB' AS area, 0 AS bit_offset, 0 AS rack, 1 AS slot"
    tags = fetch_df(f'SELECT id, name, plc_ip, db_number, "offset", data_type, current_data, plc_state, {extra} '
                    f"FROM tia_scada WHERE plc_ip = %s ORDER BY area, db_number, \"offset\";", (cfg["ip"],),
                    show_error=False)
    if tags.empty:
        st.info(f"{cfg['ip']} 還沒有任何點位。")
        return
    if st.button(f"🎯 讀取 {len(tags)} 個點位驗證", type="primary", key="s7v_go"):
        conn = _conn(cfg)
        rows = []
        try:
            for _, t in tags.iterrows():
                size = 1 if C.normalize_type(t["data_type"]) == "BOOL" else C.type_size(t["data_type"])
                res = conn.read(t["area"], int(t["db_number"] or 0), int(t["offset"]), size)
                _log("點位驗證", t["area"], int(t["db_number"] or 0), int(t["offset"]), size, res)
                db_val = (t["current_data"] or {}).get("val") if isinstance(t["current_data"], dict) else None
                row = {"點位": t["name"],
                       "TIA 位址": C.format_address(t["area"], int(t["db_number"] or 0), int(t["offset"]),
                                                    t["data_type"], int(t["bit_offset"] or 0)),
                       "型態": t["data_type"], "資料庫目前值": "—" if db_val is None else str(db_val)}
                if res.ok:
                    try:
                        v = C.decode(res.data, 0, t["data_type"], int(t["bit_offset"] or 0))
                        row.update({"即時讀取": format_number(v) if isinstance(v, (int, float)) and not isinstance(v, bool)
                                    else str(v), "原始 HEX": res.data.hex(" ").upper()[:47], "狀態": "✅"})
                    except ValueError as e:
                        row.update({"即時讀取": "—", "狀態": f"❌ {e}"})
                else:
                    row.update({"即時讀取": "—", "狀態": f"❌ {res.error}"[:120]})
                rows.append(row)
        finally:
            conn.close()
        st.session_state["s7v_rows"] = rows
    if st.session_state.get("s7v_rows"):
        st.dataframe(pd.DataFrame(st.session_state["s7v_rows"]).fillna(""), hide_index=True, width="stretch")
        st.caption("即時讀取與 TIA Portal 監看表（Watch table）的值不同時，通常是 offset 算錯或型態選錯，"
                   "用「記憶體讀取」把附近的 bytes 讀出來對照。")


# ------------------------------------------------------------------
# ✏️ 寫入測試
# ------------------------------------------------------------------
def _tab_write(cfg):
    if not WRITE_ENABLED:
        st.info("🔒 寫入功能預設關閉。寫入會直接改變 PLC 的變數（可能造成設備動作），需要時在 .env 設定 "
                "`S7_DEBUG_WRITE_ENABLED=true` 並重新啟動網頁服務；啟用後只有工程師以上可用，每次寫入都記錄在稽核紀錄。")
        return
    st.warning("⚠️ 寫入會直接改變 PLC 的變數。輸出（Q）在 CPU RUN 時會被程式覆寫；請確認現場人員知情。")
    c1, c2, c3 = st.columns([2, 1, 1])
    addr_text = c1.text_input("TIA 位址", key="s7w_addr", placeholder="DB1.DBD0、DB1.DBX18.3、MW20")
    try:
        a = C.parse_address(addr_text) if addr_text.strip() else None
    except ValueError as e:
        st.error(str(e))
        return
    if a is None:
        return
    is_bool = a["width"] == 0
    if is_bool:
        dt = "BOOL"
        value = c2.radio("值", ["TRUE (1)", "FALSE (0)"], key="s7w_bool") == "TRUE (1)"
    else:
        choices = [t for t in C.NUMERIC_TYPES if t != "BOOL" and (C.TYPES[t][0] == a["width"] or a["width"] == 0)]
        dt = c2.selectbox("型態", choices, key="s7w_dt")
        value = c3.number_input("值", value=0.0, format="%g", key="s7w_val")
    size = 1 if is_bool else C.type_size(dt)
    if st.button("讀回目前值", key="s7w_peek"):
        res = _read(cfg, a["area"], a["db"], a["byte"], size, "寫入前讀取")
        st.session_state["s7w_peek"] = (addr_text, res)
    peek = st.session_state.get("s7w_peek")
    if peek and peek[0] == addr_text:
        res = peek[1]
        st.caption(f"目前值：{C.decode(res.data, 0, dt, a['bit'])}" if res.ok else f"讀取失敗：{res.error}")
    confirm = st.checkbox(f"我確認要寫入 {cfg['ip']} 的 {addr_text}，並了解這會改變 PLC 狀態", key="s7w_ok")
    if st.button("✏️ 寫入", type="primary", disabled=not confirm, key="s7w_go"):
        conn = _conn(cfg)
        try:
            if is_bool:
                cur = conn.read(a["area"], a["db"], a["byte"], 1)
                if not cur.ok:
                    st.error(f"讀取原本的 byte 失敗，未寫入：{cur.error}")
                    return
                payload = C.encode_bool(cur.data[0], a["bit"], value)
            else:
                payload = C.encode(value, dt)
            res = conn.write(a["area"], a["db"], a["byte"], payload)
            _log("寫入", a["area"], a["db"], a["byte"], len(payload), res)
            back = conn.read(a["area"], a["db"], a["byte"], size) if res.ok else None
        except (ValueError, OverflowError) as e:
            st.error(f"數值無法編碼成 {dt}：{e}")
            return
        finally:
            conn.close()
        audit_ui("s7.write", f"s7:{cfg['ip']}/{addr_text}",
                 {"value": value, "type": dt, "ok": res.ok, "error": res.error})
        if not res.ok:
            st.error(f"❌ 寫入失敗：{res.error}")
        elif back is not None and back.ok:
            got = C.decode(back.data, 0, dt, a["bit"])
            matched = (got == value) if is_bool else abs(float(got) - float(value)) < 1e-6 * max(1, abs(float(value)))
            (st.success if matched else st.warning)(
                f"{'✅ 寫入成功，讀回一致' if matched else '⚠️ 寫入成功，但讀回的值不同（PLC 程式可能立即覆寫）'}：{got}")


def _tab_log():
    log = st.session_state.get(LOG_KEY, [])
    if not log:
        st.caption("還沒有任何通訊紀錄。")
        return
    df = pd.DataFrame(log)
    c1, c2, c3 = st.columns(3)
    c1.metric("請求數", len(df), border=True)
    c2.metric("成功率", f"{(df['結果'] == '✅ 成功').mean() * 100:.0f}%", border=True)
    c3.metric("平均回應", f"{df['回應 ms'].mean():.0f} ms", border=True)
    st.dataframe(df, hide_index=True, width="stretch", height=420)
    b1, b2 = st.columns(2)
    b1.download_button("⬇️ 下載 CSV", to_csv_bytes(df), file_name=f"s7_log_{time.strftime('%Y%m%d_%H%M%S')}.csv",
                       mime="text/csv")
    if b2.button("清除紀錄", key="s7l_clear"):
        st.session_state[LOG_KEY] = []
        st.rerun()


def render():
    require_role("engineer")
    st.title("🔧 S7 線上調適")
    st.caption("用跟採集服務完全相同的連線與解碼直接跟 Siemens PLC 通訊：確認 CPU 設定、找 offset、確認型態、驗證點位。")
    cfg = _connection_panel()
    tabs = st.tabs(["🖥️ PLC 資訊", "📖 記憶體讀取", "🎯 點位驗證", "✏️ 寫入測試", "📜 通訊紀錄"])
    with tabs[4]:
        _tab_log()
    if cfg is None:
        return
    with tabs[0]:
        _tab_info(cfg)
    with tabs[1]:
        _tab_read(cfg)
    with tabs[2]:
        _tab_verify(cfg)
    with tabs[3]:
        _tab_write(cfg)
