"""
web/pages/calculated_points.py
==============================
🧮 計算點（engineer 以上，需要 sql/017）

用運算式把其他感測器的即時值算成新的感測器，例如總功率、單位耗能、運轉判斷、液位百分比。
結果是一個一般的感測器：歷史、趨勢、報表、警報規則都能直接用。

計算由 main.py 的計算引擎（services/calc/engine.py）每 5 秒執行一次；這一頁負責設定、
驗證（引用是否存在、有沒有循環引用）與「試算」（用目前的即時值算一次給你看）。
"""

import pandas as pd
import streamlit as st

from datetime import datetime

from core.config import LOCAL_TZ, env_bool
from data_layer.db_connector import DatabaseConnector
from data_layer.sensor_codes import allocate_sensor_codes, next_code
from services.alarm.rules import format_number
from services.calc.expression import (
    CONSTANTS,
    FUNCTION_DOCS,
    MISSING,
    EvalContext,
    Expression,
    ExpressionError,
    MissingInput,
    evaluation_order,
)
from services.calc.script_runner import TEMPLATE as SCRIPT_TEMPLATE
from services.calc.script_runner import ScriptRunner, check_source, script_refs
from web.common import (
    _load_sensor_binding_map,
    can,
    column_exists,
    audit_ui,
    current_user,
    execute,
    fetch_df,
    fmt_age,
    require_role,
    sensor_catalog,
    table_exists,
)

EXAMPLES = [
    ("總功率", "{PM01_KW} + {PM02_KW} + {PM03_KW}"),
    ("單位耗氣（避免除以零）", "{B03_STEAM} / max({B03_GAS}, 0.001)"),
    ("運轉判斷（1 / 0）", "1 if {P01_CURRENT} > 5 else 0"),
    ("三相電壓不平衡 %", "spread({VA}, {VB}, {VC}) / avg({VA}, {VB}, {VC}) * 100"),
    ("今日用電（生產日 08:00 起算）", "delta({PM01_KWH}, 'day', 8)"),
    ("本月用電", "delta({PM01_KWH}, 'month')"),
    ("功率積分成今日電量（沒有電度表時）", "integral({PM01_KW}, 'day')"),
    ("今日運轉時數", "ontime({P01_CURRENT} > 5, 'day')"),
    ("今日啟動次數", "count({P01_CURRENT} > 5, 'day')"),
    ("時間電價（平日 9 時後尖峰）", "{PM01_KW} * iff(weekday() <= 5 and timeofday() >= 9, 5.2, 2.1)"),
    ("主備援感測器", "coalesce({TT01_A}, {TT01_B})"),
    ("斷線時以 0 計（加總不中斷）", "valueor({PM01_KW}, 0) + valueor({PM02_KW}, 0)"),
    ("PLC 狀態字第 3 位元（故障）", "bit({P01_STATUS}, 3)"),
    ("15 分鐘需量（移動平均）", "movavg({PM01_KW}, 900)"),
    ("去雜訊", "filter({FT01}, 30)"),
]


SCRIPTS_ENABLED = env_bool("CALC_SCRIPTS_ENABLED", False)
_MONO_CSS = "<style>textarea { font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace !important; }</style>"


def scripts_allowed() -> bool:
    """Python 腳本：.env 開關 + 管理員 + sql/021 都要有。"""
    return SCRIPTS_ENABLED and can("admin") and column_exists("calculated_points", "kind")


def _calcs() -> pd.DataFrame:
    extra = "c.kind, c.last_log" if column_exists("calculated_points", "kind") else "'expression' AS kind, NULL AS last_log"
    return fetch_df(f"""
        SELECT c.calc_id, c.sensor_id, s.sensor_code, s.nickname, s.unit, c.expression, c.description,
               c.enabled, c.current_value, c.quality, c.state, c.last_error, {extra},
               EXTRACT(EPOCH FROM now() - c.last_update) AS age
        FROM calculated_points c JOIN sensors s ON s.sensor_id = c.sensor_id
        ORDER BY s.sensor_code;""")


def _refs_of(kind, text):
    if kind == "python":
        return script_refs(text)
    return Expression(text).refs


def validate(text: str, target_code: str, calcs: pd.DataFrame, codes: set, calc_id=None, kind="expression") -> tuple:
    """
    回傳 (Expression / 腳本原始碼 / None, 錯誤清單)。
    檢查語法、引用存在、不能引用自己、不能造成循環（運算式與腳本可以互相引用）。
    """
    errors = []
    if kind == "python":
        errors = check_source(text)
        if errors and any("語法" in e for e in errors):
            return None, errors
        expr, refs = text, script_refs(text)
    else:
        try:
            expr = Expression(text)
        except ExpressionError as e:
            return None, [str(e)]
        refs = expr.refs
        if not refs:
            errors.append("運算式至少要引用一個感測器（固定值請直接設定在警報規則或上下限）")
    missing = [r for r in refs if r not in codes]
    if missing:
        errors.append(f"引用的感測器不存在：{', '.join(missing)}")
    if target_code in refs:
        errors.append("不能引用結果感測器自己")
    deps = {}
    for _, r in calcs.iterrows():
        if calc_id is not None and int(r["calc_id"]) == int(calc_id):
            continue
        try:
            deps[r["sensor_code"]] = set(_refs_of(r.get("kind") or "expression", r["expression"]))
        except ExpressionError:
            continue
    deps[target_code] = set(refs)
    _, cyclic = evaluation_order(deps)
    if target_code in cyclic and target_code not in refs:
        errors.append(f"循環引用：{target_code} 透過其他計算點間接引用到自己")
    return expr, errors


def _live_values() -> dict:
    """目前即時值（即時層 + 計算點），試算用。"""
    from web.pages.overview import load_live
    df = load_live()
    if df.empty:
        return {}
    out = {}
    for _, r in df.iterrows():
        try:
            out[r["sensor_code"]] = (float(r["val"]), r["status"], r["age_seconds"])
        except (TypeError, ValueError):
            continue
    return out


def _trial_baseline(code, start):
    """試算 delta() 用：查週期開始時的實際歷史值，讓「今日用量」試算出真的數字。"""
    df = fetch_df(
        """(SELECT value::float8 AS v FROM sensor_readings r JOIN sensors s USING (sensor_id)
            WHERE s.sensor_code = %s AND reading_time <= %s ORDER BY reading_time DESC LIMIT 1)
           UNION ALL
           (SELECT value::float8 FROM sensor_readings r JOIN sensors s USING (sensor_id)
            WHERE s.sensor_code = %s AND reading_time > %s ORDER BY reading_time LIMIT 1) LIMIT 1;""",
        (code, start, code, start), show_error=False)
    return None if df.empty else float(df.iloc[0]["v"])


def _trial(expr: Expression):
    live = _live_values()
    rows, values = [], {}
    for ref in expr.refs:
        if ref in live:
            value, status, age = live[ref]
            values[ref] = value
            rows.append({"感測器": ref, "目前值": format_number(value), "狀態": status, "更新": fmt_age(age)})
        else:
            values[ref] = MISSING
            rows.append({"感測器": ref, "目前值": "—", "狀態": "無即時值", "更新": ""})
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    ctx = EvalContext(now=datetime.now(LOCAL_TZ), baseline=_trial_baseline, trial=True)
    try:
        st.success(f"🧮 試算結果：**{format_number(expr.evaluate(values, ctx))}**")
    except MissingInput as e:
        st.warning(f"{e}：計算引擎遇到這種情況會暫停計算並標記斷線"
                   "（要容許斷線，可用 valueor / coalesce / isgood 處理）")
    except ExpressionError as e:
        st.error(f"計算錯誤：{e}")
    if expr.stateful:
        st.caption("ℹ️ 這個運算式有累計 / 動態函式：試算沒有上一輪的資料，integral / ontime / count 從 0 開始、"
                   "prev / derivative 等於目前值；delta 會用歷史資料當基準，試算結果就是實際的期間用量。"
                   "儲存後由計算引擎每 5 秒持續累計，重啟後接續（需要 sql/020）。")


def _trial_script(source: str, calc_id=0):
    live = _live_values()
    values = {code: v for code, (v, status, _age) in live.items() if status not in ("離線", "品質不良", "未綁定")}
    qualities = {code: "GOOD" for code in values}
    refs = script_refs(source)
    if refs:
        st.dataframe(pd.DataFrame([{"感測器": r, "目前值": format_number(values[r]) if r in values else "—（無有效值）"}
                                   for r in refs]), hide_index=True, width="stretch")
    runner = ScriptRunner()
    try:
        res = runner.run(calc_id, source, values, qualities, {}, datetime.now(LOCAL_TZ))
    finally:
        runner.stop()
    if res.ok:
        if res.value is None:
            st.info(f"🐍 執行成功（{res.elapsed_ms:.0f} ms），result = None：這一輪不會更新")
        else:
            st.success(f"🐍 試算結果：**{format_number(res.value)}**（執行 {res.elapsed_ms:.0f} ms）")
    else:
        st.error(f"🐍 {res.error}")
    if res.logs:
        st.code("\n".join(res.logs), language="text")
    if res.ok and res.state:
        st.caption("state（之後每一輪會保存並接續）：")
        st.json(res.state)
    st.caption("試算時 state 從空的開始、可以讀到所有目前有效的感測器；實際執行由計算引擎每 5 秒一次。")


def _insert_script_ref(text_key: str, picker_key: str):
    code = st.session_state.get(picker_key)
    if code:
        current = st.session_state.get(text_key, "") or ""
        st.session_state[text_key] = current.rstrip("\n") + ("\n" if current else "") + f'x = value("{code}")\n'


def _load_template(text_key: str):
    st.session_state[text_key] = SCRIPT_TEMPLATE


def _script_editor(prefix: str, catalog: pd.DataFrame, default: str = ""):
    text_key, picker_key = f"{prefix}_py", f"{prefix}_pypick"
    st.session_state.setdefault(text_key, default)
    st.markdown(_MONO_CSS, unsafe_allow_html=True)
    c1, c2, c3 = st.columns([3, 1, 1])
    labels = dict(zip(catalog["sensor_code"], catalog["label"]))
    c1.selectbox("插入感測器", list(labels), format_func=labels.get, index=None, key=picker_key,
                 placeholder="搜尋感測器…（插入 value(\"代碼\")）")
    c2.button("➕ 插入", key=f"{prefix}_pyins", on_click=_insert_script_ref, args=(text_key, picker_key),
              width="stretch")
    c3.button("📄 載入範本", key=f"{prefix}_pytpl", on_click=_load_template, args=(text_key,), width="stretch")
    return st.text_area("Python 腳本", key=text_key, height=320,
                        help="可用 value(\"代碼\", 預設)、tags、quality()、isgood()、now、state、log()；最後設定 result")


def _insert_ref(text_key: str, picker_key: str):
    code = st.session_state.get(picker_key)
    if code:
        current = st.session_state.get(text_key, "") or ""
        st.session_state[text_key] = (current + (" " if current and not current.endswith(" ") else "") + f"{{{code}}}")


def _expression_editor(prefix: str, catalog: pd.DataFrame, default: str = ""):
    text_key, picker_key = f"{prefix}_expr", f"{prefix}_pick"
    st.session_state.setdefault(text_key, default)
    c1, c2 = st.columns([3, 1])
    labels = dict(zip(catalog["sensor_code"], catalog["label"]))
    c1.selectbox("插入感測器", list(labels), format_func=labels.get, index=None, key=picker_key,
                 placeholder="搜尋感測器…")
    c2.button("➕ 插入", key=f"{prefix}_ins", on_click=_insert_ref, args=(text_key, picker_key), width="stretch")
    return st.text_area("運算式", key=text_key, height=90,
                        placeholder="例如 {PM01_KW} + {PM02_KW}，感測器編號用大括號包起來")


def _render_list(calcs: pd.DataFrame, catalog: pd.DataFrame, codes: set):
    if calcs.empty:
        st.info("還沒有任何計算點。")
        return
    icon = {"ONLINE": "🟢", "OFFLINE": "⚫", "ERROR": "🔴"}
    show = pd.DataFrame({
        "結果感測器": calcs["sensor_code"] + calcs["nickname"].map(lambda n: f"（{n}）" if n else ""),
        "類型": calcs["kind"].map(lambda k: "🐍 Python" if k == "python" else "運算式"),
        "運算式": [(e.strip().splitlines()[0][:60] + " …") if k == "python" else e
                  for e, k in zip(calcs["expression"], calcs["kind"])],
        "目前值": [f"{format_number(v)} {u or ''}".strip() if pd.notnull(v) else "—"
                  for v, u in zip(calcs["current_value"], calcs["unit"])],
        "狀態": [("⏸️ 停用" if not en else f"{icon.get(s, '⚪')} {s or '尚未計算'}")
               for s, en in zip(calcs["state"], calcs["enabled"])],
        "訊息": calcs["last_error"].fillna(""),
        "最後計算": calcs["age"].map(fmt_age),
    })
    st.dataframe(show, hide_index=True, width="stretch")
    st.caption("狀態：🟢 正常｜⚫ 輸入感測器無資料或斷線（暫停計算）｜🔴 運算式錯誤。計算引擎每 5 秒計算一次，設定變更約 15 秒內套用。")

    labels = {int(r["calc_id"]): r["sensor_code"] for _, r in calcs.iterrows()}
    cid = st.selectbox("編輯計算點", list(labels), format_func=labels.get, index=None, key="cp_edit_id",
                       placeholder="選擇要編輯的計算點…")
    if cid is None:
        return
    row = calcs[calcs["calc_id"] == cid].iloc[0]
    kind = row.get("kind") or "expression"
    with st.container(border=True):
        if kind == "python":
            if row.get("last_log"):
                st.caption("最後一輪 log：")
                st.code(row["last_log"], language="text")
            if not scripts_allowed():
                st.code(row["expression"], language="python")
                st.info("🔒 Python 腳本只有「管理員」能修改，而且需要 .env 設定 CALC_SCRIPTS_ENABLED=true。")
                return
            text = _script_editor(f"cp_e{cid}", catalog, row["expression"])
        else:
            text = _expression_editor(f"cp_e{cid}", catalog, row["expression"])
        c1, c2 = st.columns([3, 1])
        desc = c1.text_input("說明", row["description"] or "", key=f"cp_e{cid}_desc")
        enabled = c2.toggle("啟用", bool(row["enabled"]), key=f"cp_e{cid}_en")
        expr, errors = validate(text, row["sensor_code"], calcs, codes, cid, kind)
        for e in errors:
            st.error(e)
        b1, b2, b3 = st.columns(3)
        if b1.button("🧪 試算", key=f"cp_e{cid}_try", disabled=bool(errors)):
            _trial_script(text, int(cid)) if kind == "python" else _trial(expr)
        if b2.button("💾 儲存", type="primary", key=f"cp_e{cid}_save", disabled=bool(errors)):
            execute("UPDATE calculated_points SET expression=%s, description=%s, enabled=%s, updated_at=now() "
                    "WHERE calc_id=%s;", (text.strip(), desc.strip() or None, enabled, int(cid)))
            audit_ui("calc.update", f"calc:{row['sensor_code']}",
                     {"kind": kind, "expression": [row["expression"], text.strip()], "enabled": enabled})
            st.success("✅ 已儲存，約 15 秒內套用")
            st.rerun()
        with b3.popover("🗑️ 刪除"):
            st.caption("只刪除計算規則；結果感測器與它的歷史資料會保留。")
            if st.button("確定刪除", key=f"cp_e{cid}_del"):
                execute("DELETE FROM calculated_points WHERE calc_id=%s;", (int(cid),))
                audit_ui("calc.delete", f"calc:{row['sensor_code']}", {"expression": row["expression"]})
                st.rerun()


def _render_add(calcs: pd.DataFrame, catalog: pd.DataFrame, codes: set):
    st.subheader("➕ 新增計算點")
    mode = st.segmented_control("結果存到", ["建立新感測器", "既有感測器"], default="建立新感測器", key="cp_mode")
    target_code, new_sensor, auto = None, None, False
    if mode == "既有感測器":
        used = set(_load_sensor_binding_map())
        free = catalog[~catalog["sensor_id"].isin(used)]
        labels = dict(zip(free["sensor_code"], free["label"]))
        target_code = st.selectbox("結果感測器（只列出沒有綁定點位的感測器）", list(labels), format_func=labels.get,
                                   index=None, key="cp_target")
    else:
        devices = fetch_df("SELECT device_id, device_code, device_name FROM devices ORDER BY device_code;")
        if devices.empty:
            st.warning("請先建立設備。")
            return
        dev_labels = {int(r["device_id"]): f"{r['device_code']} {r['device_name'] or ''}" for _, r in devices.iterrows()}
        c1, c2, c3 = st.columns(3)
        device_id = c1.selectbox("所屬設備", list(dev_labels), format_func=dev_labels.get, key="cp_new_dev")
        auto_code = str(next_code(codes))
        target_code = c2.text_input(
            "感測器編號（留空 = 自動編號）", key="cp_new_code", placeholder=f"自動編號：{auto_code}",
            help="留空時依流水號自動產生；想在運算式裡用好記的名稱引用它（例如 PLANT_TOTAL_KW）才需要自己填。",
        ).strip() or None
        auto = target_code is None
        if auto:
            target_code = auto_code
        sensor_type = c3.text_input("感測器類型", "calculated", key="cp_new_type")
        c4, c5, c6 = st.columns(3)
        nickname = c4.text_input("暱稱", key="cp_new_nick", placeholder="全廠總功率")
        unit = c5.text_input("單位", key="cp_new_unit", placeholder="kW")
        condition = c6.selectbox("上傳條件", ["on_change", "always", "threshold_absolute", "threshold_percent"],
                                 key="cp_new_cond")
        threshold = None
        if condition.startswith("threshold"):
            threshold = st.number_input("上傳門檻", value=1.0, key="cp_new_th")
        if not auto and target_code in codes:
            st.error(f"感測器編號 {target_code} 已存在，請改用「既有感測器」或換一個編號")
            target_code = None
        new_sensor = dict(device_id=device_id, sensor_type=sensor_type.strip() or "calculated",
                          nickname=nickname.strip() or None, unit=unit.strip() or None,
                          upload_condition=condition, upload_threshold=threshold)

    kind = "expression"
    if scripts_allowed():
        kind = "python" if st.segmented_control("計算方式", ["運算式", "🐍 Python 腳本"], default="運算式",
                                                key="cp_new_kind") == "🐍 Python 腳本" else "expression"
    elif can("admin"):
        st.caption("🐍 Python 腳本目前未啟用：在 .env 設定 CALC_SCRIPTS_ENABLED=true 並重新啟動服務後可使用（需執行 sql/021）。")
    if kind == "python":
        st.caption("腳本在獨立子程序執行，每輪最多 CALC_SCRIPT_TIMEOUT 秒（預設 2）、記憶體上限 CALC_SCRIPT_MEMORY_MB；"
                   "超時會被強制終止，不會拖垮採集服務。⚠️ 這不是安全沙箱，請只放可信任的程式碼。")
        text = _script_editor("cp_new", catalog)
    else:
        text = _expression_editor("cp_new", catalog)
    desc = st.text_input("說明（選填）", key="cp_new_desc")
    if not text.strip() or not target_code:
        return
    expr, errors = validate(text, target_code, calcs, codes | ({target_code} if new_sensor else set()), kind=kind)
    for e in errors:
        st.error(e)
    b1, b2 = st.columns(2)
    if b1.button("🧪 試算", key="cp_new_try", disabled=bool(errors)):
        _trial_script(text) if kind == "python" else _trial(expr)
    if b2.button("💾 建立計算點", type="primary", key="cp_new_save", disabled=bool(errors)):
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    if new_sensor:
                        if auto:
                            target_code = allocate_sensor_codes(cur)[0]
                        cur.execute("""INSERT INTO sensors (device_id, sensor_code, sensor_type, nickname, unit,
                                                            upload_condition, upload_threshold)
                                       VALUES (%(device_id)s, %(code)s, %(sensor_type)s, %(nickname)s, %(unit)s,
                                               %(upload_condition)s, %(upload_threshold)s) RETURNING sensor_id;""",
                                    {**new_sensor, "code": target_code})
                    else:
                        cur.execute("SELECT sensor_id FROM sensors WHERE sensor_code=%s;", (target_code,))
                    sensor_id = cur.fetchone()[0]
                    if column_exists("calculated_points", "kind"):
                        cur.execute("INSERT INTO calculated_points (sensor_id, expression, description, created_by, kind) "
                                    "VALUES (%s, %s, %s, %s, %s);",
                                    (sensor_id, text.strip(), desc.strip() or None, current_user()["username"], kind))
                    else:
                        cur.execute("INSERT INTO calculated_points (sensor_id, expression, description, created_by) "
                                    "VALUES (%s, %s, %s, %s);",
                                    (sensor_id, text.strip(), desc.strip() or None, current_user()["username"]))
        except Exception as e:
            st.error(f"❌ 建立失敗：{e}")
            return
        audit_ui("calc.create", f"calc:{target_code}",
                 {"kind": kind, "expression": text.strip(), "new_sensor": bool(new_sensor)})
        sensor_catalog.clear()
        for k in ("cp_new_expr", "cp_new_py", "cp_new_code", "cp_new_desc"):
            st.session_state.pop(k, None)
        st.success(f"✅ 已建立計算點 {target_code}，約 15 秒內開始計算")
        st.rerun()


def render():
    require_role("engineer")
    st.title("🧮 計算點")
    if not table_exists("calculated_points"):
        st.warning("尚未建立計算點資料表，請用資料表擁有者執行 `sql/017_calculated_points.sql`。")
        return
    st.caption("用運算式把其他感測器的即時值算成新的感測器（總功率、單位耗能、運轉判斷…）。"
               "結果跟一般感測器一樣，可以看趨勢、出報表、設警報。")
    with st.expander("📖 運算式語法"):
        st.markdown("感測器編號用**大括號**包起來：`{PM01_KW}`。支援 `+ - * / % ** //`、比較 `> >= < <= == !=`、"
                    "`and or not`（結果 1 / 0）、條件式 `a if 條件 else b`、常數 " + "、".join(f"`{c}`" for c in CONSTANTS) + "。")
        st.dataframe(pd.DataFrame(FUNCTION_DOCS, columns=["分類", "函式", "說明"]),
                     hide_index=True, width="stretch", height=560)
        st.caption("週期：'hour' / 'day' / 'week' / 'month' / 'never'（不歸零）；第三個參數是起始小時，"
                   "例如 8 = 生產日從 08:00 開始。時間函式依 SCADA_TIMEZONE（預設台灣時間）。")
        st.markdown("**範例**")
        st.dataframe(pd.DataFrame(EXAMPLES, columns=["用途", "運算式"]), hide_index=True, width="stretch")
        st.caption("任何一個輸入感測器沒有資料、斷線或品質不良時，計算點會暫停計算並標記為斷線，不會用舊值硬算；"
                   "要容許斷線請用 valueor / coalesce / isgood。")

    catalog = sensor_catalog()
    codes = set(catalog["sensor_code"]) if not catalog.empty else set()
    calcs = _calcs()
    _render_list(calcs, catalog, codes)
    st.divider()
    _render_add(calcs, catalog, codes)
