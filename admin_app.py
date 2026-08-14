import asyncio
import ast
import json
import os
import struct
import pandas as pd
import streamlit as st

# 1. 載入 .env 環境變數
from dotenv import load_dotenv

load_dotenv()

# 取得帳密設定 (若 .env 未設定則給預設值)
ADMIN_USER = os.getenv("ADMIN_USER", "USER")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "PASSWORD")

from data_layer.db_connector import DatabaseConnector
from data_layer.batch_updater import batch_update_opcua_tags

# 2. 相容 Modbus TCP 套件
try:
    from pymodbus.client import ModbusTcpClient
except ImportError:
    from pymodbus.client.sync import ModbusTcpClient  # type: ignore

# 3. Siemens S7 (snap7) 套件引用
try:
    import snap7
    from snap7.util import (
        get_bool,
        get_dint,
        get_dword,
        get_int,
        get_real,
        get_word,
    )

    SNAP7_AVAILABLE = True
except ImportError:
    SNAP7_AVAILABLE = False

# 4. OPC UA (asyncua) 套件引用
try:
    from protocols.opcua_protocol import scan_server, OPCUAConnectionError

    OPCUA_AVAILABLE = True
except ImportError:
    OPCUA_AVAILABLE = False

# ------------------------------------------------------------------
# Helper: 正規化 state_dictionary 欄位（相容合法 JSON 與 Python 字典字面量）
# ------------------------------------------------------------------
def _normalize_state_dict(raw):
    """
    表格編輯器顯示 JSONB 欄位時，可能把它渲染成 Python 字典字面量
    （單引號，例如 {'0': '待機'}），使用者存檔時若沒有改回合法 JSON
    （雙引號），直接丟給 PostgreSQL 會報錯：invalid input syntax for type json。

    這裡統一嘗試三種來源並正規化成合法 JSON 字串：
      1. 已經是 dict 物件（psycopg2 讀出 JSONB 欄位時的預設型態）
      2. 合法 JSON 字串（雙引號）
      3. Python 字典字面量字串（單引號）

    回傳 (json_str_or_None, error_message_or_None)。
    空值（None / 空字串 / 空 dict）視為「不設定」，回傳 (None, None)。
    """
    if raw is None:
        return None, None
    if isinstance(raw, dict):
        if not raw:
            return None, None
        return json.dumps(raw, ensure_ascii=False), None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None, None
        try:
            parsed = json.loads(s)
            return json.dumps(parsed, ensure_ascii=False), None
        except json.JSONDecodeError:
            pass
        try:
            parsed = ast.literal_eval(s)
            if isinstance(parsed, dict):
                return (json.dumps(parsed, ensure_ascii=False), None) if parsed else (None, None)
        except (ValueError, SyntaxError):
            pass
        return None, f"無法解析為合法 JSON 或字典：{s}"
    return None, f"不支援的型態：{type(raw).__name__}"
# ------------------------------------------------------------------
# Helper: 身份驗證機制 (Login Mechanism)
# ------------------------------------------------------------------
def check_password():
    """驗證使用者登入狀態，若未登入則顯示登入表單並終止後續渲染"""
    if "logged_in" not in st.session_state:
        st.session_state["logged_in"] = False

    # 若已登入，傳回 True
    if st.session_state["logged_in"]:
        return True

    # 顯示登入畫面
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.subheader("🔒 IIoT 後台系統登入")
        with st.form("login_form"):
            username_input = st.text_input("帳號 (Username)")
            password_input = st.text_input("密碼 (Password)", type="password")
            submit_login = st.form_submit_button(
                "登入系統", type="primary", use_container_width=True
            )

            if submit_login:
                if (
                    username_input == ADMIN_USER
                    and password_input == ADMIN_PASSWORD
                ):
                    st.session_state["logged_in"] = True
                    st.success("✅ 登入成功！")
                    st.rerun()
                else:
                    st.error("❌ 帳號或密碼錯誤，請重新輸入。")

    return False


# ------------------------------------------------------------------
# Helper 1: Modbus 即時測試讀取函式 (不寫入 DB)
# ------------------------------------------------------------------
def test_modbus_read(
    ip,
    port,
    slave_id,
    function_code,
    start_address,
    data_type,
    byte_order,
    word_order,
    use_scale=False,
    raw_min=0.0,
    raw_max=10000.0,
    eng_min=0.0,
    eng_max=10000.0,
):
    dt = data_type.lower()
    if dt in ["bool", "word", "int", "int16", "uint16"]:
        count = 1
    elif dt in ["dint", "int32", "uint32", "float"]:
        count = 2
    elif dt in ["int64", "uint64", "float64"]:
        count = 4
    else:
        count = 1

    client = ModbusTcpClient(ip, port=int(port), timeout=3)
    if not client.connect():
        return False, f"❌ 連線失敗：無法連線至 {ip}:{port}"

    try:
        kwargs = {}
        try:
            import inspect

            sig = inspect.signature(client.read_holding_registers)
            if "slave" in sig.parameters:
                kwargs["slave"] = int(slave_id)
            else:
                kwargs["unit"] = int(slave_id)
        except Exception:
            kwargs["slave"] = int(slave_id)

        if function_code == 1:
            res = client.read_coils(start_address, count=count, **kwargs)
            if res.isError():
                return False, f"❌ 讀取失敗 (FC1): {res}"
            return True, f"🎉 測試成功！[FC1 Coil] 讀取值: `{res.bits[0]}`"

        elif function_code == 2:
            res = client.read_discrete_inputs(
                start_address, count=count, **kwargs
            )
            if res.isError():
                return False, f"❌ 讀取失敗 (FC2): {res}"
            return True, f"🎉 測試成功！[FC2 Discrete Input] 讀取值: `{res.bits[0]}`"

        elif function_code == 3:
            res = client.read_holding_registers(
                start_address, count=count, **kwargs
            )
            if res.isError():
                return False, f"❌ 讀取失敗 (FC3): {res}"
            registers = res.registers

        elif function_code == 4:
            res = client.read_input_registers(
                start_address, count=count, **kwargs
            )
            if res.isError():
                return False, f"❌ 讀取失敗 (FC4): {res}"
            registers = res.registers
        else:
            return False, f"❌ 不支援的功能碼: {function_code}"

        # -------------------------------------------------------------
        # 1. 處理 Word Order (字組順序: 高字組與低字組順序)
        # -------------------------------------------------------------
        if word_order.upper() == "LITTLE" and len(registers) > 1:
            registers = registers[::-1]

        # -------------------------------------------------------------
        # 2. 處理 Byte Order (位元組順序: 每個 Register 內部 2 個 Byte 的順序)
        # -------------------------------------------------------------
        byte_pack_fmt = ">H" if byte_order.upper() == "BIG" else "<H"
        raw_bytes = b"".join(
            [struct.pack(byte_pack_fmt, r) for r in registers]
        )

        # -------------------------------------------------------------
        # 3. 統一使用 Big-Endian (>) 進行解包，才能正確反映 Byte Order 轉換結果
        # -------------------------------------------------------------
        fmt_map = {
            "word": ">H",
            "int": ">h",
            "uint32": ">I",
            "dint": ">i",
            "float": ">f",
            "uint64": ">Q",
            "int64": ">q",
            "float64": ">d",
        }
        fmt = fmt_map.get(dt, ">H")
        raw_val = struct.unpack(fmt, raw_bytes)[0]

        final_val = raw_val
        scale_info = ""
        if use_scale and (raw_max != raw_min):
            final_val = eng_min + (raw_val - raw_min) * (
                eng_max - eng_min
            ) / (raw_max - raw_min)
            scale_info = (
                f" (Raw 原始值: `{raw_val}`, Scaling 工程值:"
                f" `{round(final_val, 4)}`)"
            )

        val_display = (
            round(final_val, 4) if isinstance(final_val, float) else final_val
        )
        return True, f"🎉 測試成功！讀取結果: `{val_display}`{scale_info}"

    except Exception as e:
        return False, f"❌ 解析失敗: {e}"
    finally:
        client.close()


# ------------------------------------------------------------------
# Helper 2: Siemens TIA S7 即時測試讀取函式 (不寫入 DB)
# ------------------------------------------------------------------
def test_tia_read(ip, db_number, offset, data_type, rack=0, slot=1):
    if not SNAP7_AVAILABLE:
        return (
            False,
            "❌ 未安裝 `python-snap7` 套件！請在 Terminal 執行 `pip install python-snap7`",
        )

    client = snap7.client.Client()
    try:
        client.connect(ip, int(rack), int(slot))
        if not client.get_connected():
            return False, f"❌ 連線失敗：無法透過 S7 連線至 {ip}"

        dt = data_type.upper()
        size = 1 if dt == "BOOL" else (2 if dt in ["INT", "WORD"] else 4)

        db_data = client.db_read(int(db_number), int(offset), size)

        if dt == "BOOL":
            val = get_bool(db_data, 0, 0)
        elif dt == "INT":
            val = get_int(db_data, 0)
        elif dt == "WORD":
            val = get_word(db_data, 0)
        elif dt == "DINT":
            val = get_dint(db_data, 0)
        elif dt == "DWORD":
            val = get_dword(db_data, 0)
        elif dt == "REAL":
            val = round(get_real(db_data, 0), 4)
        else:
            val = db_data.hex()

        return (
            True,
            f"🎉 測試成功！[DB{db_number}.DBX{offset}] 讀取結果: `{val}`",
        )

    except Exception as e:
        return (
            False,
            f"❌ 讀取失敗: {e}\n💡 提示：請確認 TIA Portal 已開啟 PUT/GET 權限且 DB 未啟用「優化區塊存取」。",
        )
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


# ------------------------------------------------------------------
# Helper 3: OPC UA 相關工具函式
# ------------------------------------------------------------------
def run_async(coro):
    """
    在 Streamlit 的同步環境中執行 asyncio coroutine。
    按鈕點下去會同步等待結果回來（畫面轉圈），
    點位數量多或網路慢時可能需要等待數秒到數十秒。
    """
    return asyncio.run(coro)


def _fetch_df(query):
    """
    通用查詢 helper：執行任意 SELECT 並回傳 DataFrame。
    給「感測器階層管理」與「異常監控」兩個分頁共用。
    """
    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                cols = [desc[0] for desc in cur.description]
                rows = cur.fetchall()
                return pd.DataFrame(rows, columns=cols)
    except Exception as e:
        st.error(f"查詢失敗: {e}")
        return pd.DataFrame()


def _load_sensor_options():
    """
    載入所有感測器選項，供 Modbus / TIA / OPC UA 三個分頁的
    sensor_code 綁定下拉選單使用。
    回傳:
        label_to_id: dict，顯示字串 -> sensor_id
        id_to_label: dict，sensor_id -> 顯示字串
    顯示字串格式："設備編號 / 感測器編號 (暱稱)"，沒有暱稱則省略括號部分。
    """
    df = _fetch_df(
        """
        SELECT se.sensor_id, d.device_code, se.sensor_code, se.nickname
        FROM sensors se
        JOIN devices d ON se.device_id = d.device_id
        ORDER BY d.device_code, se.sensor_code;
        """
    )
    label_to_id = {}
    id_to_label = {}
    for _, row in df.iterrows():
        label = f"{row['device_code']} / {row['sensor_code']}"
        if pd.notnull(row["nickname"]) and str(row["nickname"]).strip():
            label += f" ({row['nickname']})"
        sid = int(row["sensor_id"])
        label_to_id[label] = sid
        id_to_label[sid] = label
    return label_to_id, id_to_label


UNBOUND_LABEL = "（未綁定）"


def _load_sensor_binding_map():
    """
    一次撈出目前 Modbus / TIA / OPC UA 三張表裡所有『已綁定感測器』的點位，
    用來做跨協議重複綁定偵測（避免同一個 sensor_id 被兩個不同點位同時綁定）。
    回傳: dict，sensor_id -> [(table_name, point_id, point_display_name), ...]
    """
    mapping = {}
    checks = [
        ("modbus_scada", "id", "name"),
        ("tia_scada", "id", "name"),
        ("opcua_tags", "id", "node_id"),
    ]
    for table, id_col, name_col in checks:
        df = _fetch_df(
            f"SELECT {id_col} AS pid, {name_col} AS pname, sensor_id "
            f"FROM {table} WHERE sensor_id IS NOT NULL;"
        )
        for _, r in df.iterrows():
            sid = int(r["sensor_id"])
            mapping.setdefault(sid, []).append(
                (table, int(r["pid"]), str(r["pname"]))
            )
    return mapping


def _find_binding_conflict(binding_map, sensor_id, table, point_id):
    """
    檢查 sensor_id 是否已被【其他】點位綁定，回傳衝突描述字串；沒有衝突則回傳 None。
    :param table: 目前正在儲存的表名 ('modbus_scada' / 'tia_scada' / 'opcua_tags')
    :param point_id: 目前這筆點位自己的 id（用來排除自己，new 筆傳 None）
    """
    if sensor_id is None:
        return None
    others = [
        f"{t}.{pid}（{name}）"
        for t, pid, name in binding_map.get(sensor_id, [])
        if not (t == table and pid == point_id)
    ]
    return "、".join(others) if others else None


def request_opcua_resubscribe(server_id: int):
    """
    通知常駐的 OPC UA 訂閱服務：該 Server 的點位表已更新，
    請重新瀏覽並更新訂閱內容。
    做法很單純：把 opcua_servers.resubscribe_requested 設為 TRUE，
    main.py 背景執行的訂閱服務會每隔幾秒輪詢這個旗標，
    偵測到後自動重新整理，不需要重啟任何服務。
    """
    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE opcua_servers SET resubscribe_requested = TRUE WHERE id = %s;",
                    (int(server_id),),
                )
    except Exception as e:
        st.warning(f"⚠️ 通知訂閱服務重新整理失敗（不影響本次寫入結果）: {e}")


# ==========================================
# 主頁面配置與登入檢查
# ==========================================
st.set_page_config(
    page_title="IIoT SCADA 點位連線參數管理系統",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 🔐 執行登入檢查：若未登入則停止執行後續程式碼
if not check_password():
    st.stop()

# 側邊欄：顯示登入資訊與登出按鈕
with st.sidebar:
    st.write(f"👤 當前登入：`{ADMIN_USER}`")
    if st.button("🚪 登出系統", use_container_width=True):
        st.session_state["logged_in"] = False
        st.rerun()

st.title("⚙️ IIoT 點位與連線參數管理後台")

if not hasattr(st, "db_inited"):
    DatabaseConnector.initialize_pool()
    st.db_inited = True

tab_modbus, tab_tia, tab_opcua, tab_hierarchy, tab_alerts = st.tabs(
    [
        "📡 Modbus 點位設定",
        "📡 TIA (S7) 點位設定",
        "📡 OPC UA 點位設定",
        "🧬 感測器階層管理",
        "🚨 異常監控",
    ]
)


# ==========================================
# Tab 1: Modbus 點位 CRUD
# ==========================================
with tab_modbus:
    st.header("Modbus TCP 點位配置")

    def load_modbus_tags():
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    query = """
                    SELECT 
                        id, name, plc_ip, plc_port, slave_id, function_code, start_address, 
                        data_type, raw_min, raw_max, eng_min, eng_max, byte_order, word_order,
                        state_dictionary, sensor_id, plc_state, current_value,current_data,unit, last_update
                    FROM modbus_scada 
                    ORDER BY id ASC;
                    """
                    cur.execute(query)
                    cols = [desc[0] for desc in cur.description]
                    rows = cur.fetchall()
                    return pd.DataFrame(rows, columns=cols)
        except Exception as e:
            st.error(f"無法讀取 Modbus 點位資料: {e}")
            return pd.DataFrame()

    df_modbus = load_modbus_tags()
    modbus_label_to_id, modbus_id_to_label = _load_sensor_options()

    st.subheader("📋 Modbus 點位列表（可直接於表格內修改參數）")
    if not df_modbus.empty:
        # 把 sensor_id 轉成人類可讀的 sensor_label 欄位，供下拉選單編輯，
        # 存檔時再反查回 sensor_id。
        df_modbus["sensor_label"] = df_modbus["sensor_id"].apply(
            lambda sid: modbus_id_to_label.get(int(sid), UNBOUND_LABEL)
            if pd.notnull(sid)
            else UNBOUND_LABEL
        )
        df_modbus_display = df_modbus.drop(columns=["sensor_id"])

        disabled_cols_modbus = [
            "id",
            "plc_state",
            "current_value",
            "last_update",
        ]

        edited_modbus_df = st.data_editor(
            df_modbus_display,
            num_rows="dynamic",
            key="modbus_editor",
            disabled=disabled_cols_modbus,
            width="stretch",
            column_config={
                "sensor_label": st.column_config.SelectboxColumn(
                    "綁定感測器 (sensor_code)",
                    options=[UNBOUND_LABEL] + list(modbus_label_to_id.keys()),
                    help="選擇這個點位對應的感測器，數值會同步寫入 sensor_readings 時序表。"
                    "選單內容請先在「感測器階層管理」分頁建立。",
                )
            },
        )

        if st.button("💾 儲存 Modbus 修改", type="primary"):
            binding_map = _load_sensor_binding_map()
            try:
                with DatabaseConnector.get_connection() as conn:
                    with conn.cursor() as cur:
                        for index, row in edited_modbus_df.iterrows():
                            if pd.notnull(row["id"]):
                                point_id = int(row["id"])

                                state_dict_val, state_dict_err = _normalize_state_dict(
                                    row["state_dictionary"]
                                )
                                if state_dict_err:
                                    st.error(
                                        f"❌ id={point_id} 的 "
                                        f"state_dictionary 格式錯誤，該筆未儲存：{state_dict_err}"
                                    )
                                    continue

                                sensor_label = row.get("sensor_label", UNBOUND_LABEL)
                                sensor_id = (
                                    modbus_label_to_id.get(sensor_label)
                                    if sensor_label != UNBOUND_LABEL
                                    else None
                                )
                                conflict = _find_binding_conflict(
                                    binding_map, sensor_id, "modbus_scada", point_id
                                )
                                if conflict:
                                    st.error(
                                        f"❌ id={point_id}（{row['name']}）想綁定的感測器"
                                        f"已被其他點位使用：{conflict}，該筆未儲存。"
                                        "同一個感測器不能同時綁定多個點位。"
                                    )
                                    continue

                                sql = """
                                UPDATE modbus_scada SET 
                                    name=%s, plc_ip=%s, plc_port=%s, slave_id=%s, 
                                    function_code=%s, start_address=%s, data_type=%s,
                                    raw_min=%s, raw_max=%s, eng_min=%s, eng_max=%s,
                                    byte_order=%s, word_order=%s, state_dictionary=%s,
                                    sensor_id=%s
                                WHERE id=%s;
                                """
                                cur.execute(
                                    sql,
                                    (
                                        row["name"],
                                        row["plc_ip"],
                                        int(row["plc_port"]),
                                        int(row["slave_id"]),
                                        int(row["function_code"]),
                                        int(row["start_address"]),
                                        row["data_type"],
                                        float(row["raw_min"])
                                        if pd.notnull(row["raw_min"])
                                        else None,
                                        float(row["raw_max"])
                                        if pd.notnull(row["raw_max"])
                                        else None,
                                        float(row["eng_min"])
                                        if pd.notnull(row["eng_min"])
                                        else None,
                                        float(row["eng_max"])
                                        if pd.notnull(row["eng_max"])
                                        else None,
                                        row["byte_order"],
                                        row["word_order"],
                                        state_dict_val,  # 已由 _normalize_state_dict 正規化為合法 JSON 字串或 None
                                        sensor_id,
                                        point_id,
                                    ),
                                )
                        conn.commit()
                st.success("✅ Modbus 點位參數更新成功！")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 儲存失敗: {e}")

    st.divider()
    st.subheader("➕ 單筆新增 Modbus 點位")
    with st.form("add_modbus_form", clear_on_submit=False):
        col1, col2, col3 = st.columns(3)
        with col1:
            m_name = st.text_input("點位名稱 (name)", "Test")
            m_plc_ip = st.text_input("PLC IP 地址 (plc_ip)", "192.168.1.1")
            m_plc_port = st.number_input("Modbus 埠號 (plc_port)", value=502)
            m_slave_id = st.number_input(
                "從站 ID (slave_id)", value=1, min_value=1, max_value=247
            )
            m_unit = st.text_input("單位 (unit)", "單位")

        with col2:
            fc_mapping = {
                "1 = Read Coils": 1,
                "2 = Read Discrete Inputs": 2,
                "3 = Read Holding Registers": 3,
                "4 = Read Input Registers": 4,
            }
            fc_label = st.selectbox(
                "功能碼 (function_code)", list(fc_mapping.keys()), index=2
            )
            m_function_code = fc_mapping[fc_label]
            m_start_address = st.number_input("起始位址 (start_address)", value=0)
            m_data_type = st.selectbox(
                "資料型態 (data_type)",
                [
                    "bool",
                    "word",
                    "int",
                    "dint",
                    "float",
                    "uint32",
                    "int64",
                    "float64",
                    "uint64",
                ],
                index=4,
            )
            m_byte_order = st.selectbox(
                "位元組順序 (byte_order)", ["BIG", "LITTLE"], index=0
            )
            m_word_order = st.selectbox(
                "字組順序 (word_order)", ["BIG", "LITTLE"], index=0
            )

        with col3:
            m_use_scale = st.checkbox("啟用 Scaling (線性轉換)", value=False)
            m_raw_min = st.number_input("原始最小值 (raw_min)", value=0.0)
            m_raw_max = st.number_input("原始最大值 (raw_max)", value=10000.0)
            m_eng_min = st.number_input("工程最小值 (eng_min)", value=0.0)
            m_eng_max = st.number_input("工程最大值 (eng_max)", value=1000.0)
            m_state_dict = st.text_input(
                "狀態字典 JSON (state_dictionary)",
                value="",
                placeholder='{"1": "待機", "2": "運轉"}',
            )
            m_sensor_label = st.selectbox(
                "綁定感測器 (sensor_code，可留空)",
                [UNBOUND_LABEL] + list(modbus_label_to_id.keys()),
                help="選填，之後也可以在上方表格內再綁定/修改。",
            )

        btn_col1, btn_col2 = st.columns([1, 1])
        with btn_col1:
            submit_modbus = st.form_submit_button(
                "新增 Modbus 點位", type="primary", use_container_width=True
            )
        with btn_col2:
            test_modbus = st.form_submit_button(
                "🧪 測試連線與讀取", use_container_width=True
            )

        if test_modbus:
            with st.spinner("📡 正在連線 PLC 並嘗試讀取數據..."):
                ok, msg = test_modbus_read(
                    m_plc_ip,
                    m_plc_port,
                    m_slave_id,
                    m_function_code,
                    m_start_address,
                    m_data_type,
                    m_byte_order,
                    m_word_order,
                    m_use_scale,
                    m_raw_min,
                    m_raw_max,
                    m_eng_min,
                    m_eng_max,
                )
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)

        if submit_modbus:
            try:
                formatted_state_dict = None
                if m_state_dict.strip():
                    formatted_state_dict = json.dumps(json.loads(m_state_dict))

                new_sensor_id = (
                    modbus_label_to_id.get(m_sensor_label)
                    if m_sensor_label != UNBOUND_LABEL
                    else None
                )
                conflict = None
                if new_sensor_id is not None:
                    binding_map = _load_sensor_binding_map()
                    conflict = _find_binding_conflict(
                        binding_map, new_sensor_id, "modbus_scada", None
                    )

                if conflict:
                    st.error(
                        f"❌ 想綁定的感測器已被其他點位使用：{conflict}，"
                        "未新增。同一個感測器不能同時綁定多個點位。"
                    )
                else:
                    with DatabaseConnector.get_connection() as conn:
                        with conn.cursor() as cur:
                            # 🔧 修正：原本欄位順序跟參數順序沒對齊
                            # （unit 塞錯位置導致 function_code 收到字串），這裡重新對齊，
                            # 並新增 sensor_id 欄位供感測器綁定使用。
                            sql = """
                            INSERT INTO modbus_scada (
                                name, plc_ip, plc_port, slave_id, function_code, start_address, 
                                data_type, byte_order, word_order, raw_min, raw_max, eng_min, eng_max, 
                                state_dictionary, unit, sensor_id, plc_state
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OFFLINE');
                            """
                            cur.execute(
                                sql,
                                (
                                    m_name,
                                    m_plc_ip,
                                    int(m_plc_port),
                                    int(m_slave_id),
                                    int(m_function_code),
                                    int(m_start_address),
                                    m_data_type,
                                    m_byte_order,
                                    m_word_order,
                                    m_raw_min if m_use_scale else None,
                                    m_raw_max if m_use_scale else None,
                                    m_eng_min if m_use_scale else None,
                                    m_eng_max if m_use_scale else None,
                                    formatted_state_dict,
                                    m_unit,
                                    new_sensor_id,
                                ),
                            )
                            conn.commit()
                    st.success(f"🎉 成功新增 Modbus 點位: {m_name}")
                    st.rerun()
            except json.JSONDecodeError:
                st.error("❌ 狀態字典格式錯誤！請填寫合法的 JSON 格式")
            except Exception as e:
                st.error(f"❌ 新增失敗: {e}")


# ==========================================
# Tab 2: TIA (S7) 點位 CRUD
# ==========================================
with tab_tia:
    st.header("Siemens TIA S7 點位配置")

    def load_tia_tags():
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    query = """
                    SELECT 
                        id, name, plc_name, plc_ip, db_number, "offset", data_type, 
                        sensor_id, plc_state, current_data, last_update 
                    FROM tia_scada 
                    ORDER BY id ASC;
                    """
                    cur.execute(query)
                    cols = [desc[0] for desc in cur.description]
                    rows = cur.fetchall()
                    return pd.DataFrame(rows, columns=cols)
        except Exception as e:
            st.error(f"無法讀取 TIA 點位資料: {e}")
            return pd.DataFrame()

    df_tia = load_tia_tags()
    tia_label_to_id, tia_id_to_label = _load_sensor_options()

    st.subheader("📋 TIA 點位列表（可直接於表格內修改參數）")
    if not df_tia.empty:
        df_tia["sensor_label"] = df_tia["sensor_id"].apply(
            lambda sid: tia_id_to_label.get(int(sid), UNBOUND_LABEL)
            if pd.notnull(sid)
            else UNBOUND_LABEL
        )
        df_tia_display = df_tia.drop(columns=["sensor_id"])

        disabled_cols_tia = ["id", "plc_state", "current_data", "last_update"]

        edited_tia_df = st.data_editor(
            df_tia_display,
            num_rows="dynamic",
            key="tia_editor",
            disabled=disabled_cols_tia,
            width="stretch",
            column_config={
                "sensor_label": st.column_config.SelectboxColumn(
                    "綁定感測器 (sensor_code)",
                    options=[UNBOUND_LABEL] + list(tia_label_to_id.keys()),
                    help="選擇這個點位對應的感測器，數值會同步寫入 sensor_readings 時序表。"
                    "選單內容請先在「感測器階層管理」分頁建立。",
                )
            },
        )

        if st.button("💾 儲存 TIA 修改", type="primary"):
            binding_map = _load_sensor_binding_map()
            try:
                with DatabaseConnector.get_connection() as conn:
                    with conn.cursor() as cur:
                        for index, row in edited_tia_df.iterrows():
                            if pd.notnull(row["id"]):
                                point_id = int(row["id"])

                                sensor_label = row.get("sensor_label", UNBOUND_LABEL)
                                sensor_id = (
                                    tia_label_to_id.get(sensor_label)
                                    if sensor_label != UNBOUND_LABEL
                                    else None
                                )
                                conflict = _find_binding_conflict(
                                    binding_map, sensor_id, "tia_scada", point_id
                                )
                                if conflict:
                                    st.error(
                                        f"❌ id={point_id}（{row['name']}）想綁定的感測器"
                                        f"已被其他點位使用：{conflict}，該筆未儲存。"
                                        "同一個感測器不能同時綁定多個點位。"
                                    )
                                    continue

                                sql = """
                                UPDATE tia_scada SET 
                                    name=%s, plc_name=%s, plc_ip=%s, db_number=%s, 
                                    "offset"=%s, data_type=%s, sensor_id=%s
                                WHERE id=%s;
                                """
                                cur.execute(
                                    sql,
                                    (
                                        row["name"],
                                        row["plc_name"],
                                        row["plc_ip"],
                                        int(row["db_number"]),
                                        int(row["offset"]),
                                        row["data_type"],
                                        sensor_id,
                                        point_id,
                                    ),
                                )
                        conn.commit()
                st.success("✅ TIA 點位參數更新成功！")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 儲存失敗: {e}")

    st.divider()
    st.subheader("➕ 單筆新增 TIA S7 點位")
    with st.form("add_tia_form", clear_on_submit=False):
        col1, col2 = st.columns(2)
        with col1:
            t_name = st.text_input("點位名稱 (name)", "T3_PM:kW")
            t_plc_name = st.text_input("PLC 設備名稱 (plc_name)", "PLC_MAIN")
            t_plc_ip = st.text_input("PLC IP 地址 (plc_ip)", "192.168.1.200")

        with col2:
            t_db_number = st.number_input(
                "DB 區塊號碼 (db_number)", value=1, min_value=1
            )
            t_offset = st.number_input(
                "記憶體偏移量 (offset)", value=0, min_value=0
            )
            t_data_type = st.selectbox(
                "資料型態 (data_type)",
                ["BOOL", "INT", "DINT", "REAL", "WORD", "DWORD"],
                index=3,
            )
            t_sensor_label = st.selectbox(
                "綁定感測器 (sensor_code，可留空)",
                [UNBOUND_LABEL] + list(tia_label_to_id.keys()),
                help="選填，之後也可以在上方表格內再綁定/修改。",
            )

        btn_col1, btn_col2 = st.columns([1, 1])
        with btn_col1:
            submit_tia = st.form_submit_button(
                "新增 TIA 點位", type="primary", use_container_width=True
            )
        with btn_col2:
            test_tia = st.form_submit_button(
                "🧪 測試連線與讀取", use_container_width=True
            )

        if test_tia:
            with st.spinner("📡 正在連線 Siemens PLC 並嘗試讀取 DB 數據..."):
                ok, msg = test_tia_read(
                    t_plc_ip, t_db_number, t_offset, t_data_type
                )
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)

        if submit_tia:
            try:
                new_sensor_id = (
                    tia_label_to_id.get(t_sensor_label)
                    if t_sensor_label != UNBOUND_LABEL
                    else None
                )
                conflict = None
                if new_sensor_id is not None:
                    binding_map = _load_sensor_binding_map()
                    conflict = _find_binding_conflict(
                        binding_map, new_sensor_id, "tia_scada", None
                    )

                if conflict:
                    st.error(
                        f"❌ 想綁定的感測器已被其他點位使用：{conflict}，"
                        "未新增。同一個感測器不能同時綁定多個點位。"
                    )
                else:
                    with DatabaseConnector.get_connection() as conn:
                        with conn.cursor() as cur:
                            sql = """
                            INSERT INTO tia_scada (name, plc_name, plc_ip, db_number, "offset", data_type, sensor_id, plc_state)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, 'OFFLINE');
                            """
                            cur.execute(
                                sql,
                                (
                                    t_name,
                                    t_plc_name,
                                    t_plc_ip,
                                    int(t_db_number),
                                    int(t_offset),
                                    t_data_type,
                                    new_sensor_id,
                                ),
                            )
                            conn.commit()
                    st.success(f"🎉 成功新增 TIA 點位: {t_name}")
                    st.rerun()
            except Exception as e:
                st.error(f"❌ 新增失敗: {e}")


# ==========================================
# Tab 3: OPC UA Server / 點位管理
# ==========================================
with tab_opcua:
    st.header("OPC UA 點位配置")

    if not OPCUA_AVAILABLE:
        st.error(
            "❌ 未安裝 `asyncua` 套件！請在 Terminal 執行 `pip install asyncua`，"
            "並確認 `protocols/opcua_protocol.py` 已放入專案中。"
        )
        st.stop()

    st.caption(
        "💡 OPC UA 點位數值現在由主程式的常駐訂閱服務即時推播更新，"
        "不需要等排程輪詢。若在下方新增/修改點位或重新瀏覽，"
        "系統會自動通知訂閱服務在數秒內套用最新的點位表。"
    )

    # ----------------------------------------------------------
    # 讀取 opcua_servers 清單
    # ----------------------------------------------------------
    def load_opcua_servers():
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    query = """
                    SELECT id, server_name, ip, port, username, password,
                           security_policy, security_mode, root_node_id, browse_depth,
                           enabled, conn_state, last_scan, last_error
                    FROM opcua_servers
                    ORDER BY id ASC;
                    """
                    cur.execute(query)
                    cols = [desc[0] for desc in cur.description]
                    rows = cur.fetchall()
                    return pd.DataFrame(rows, columns=cols)
        except Exception as e:
            st.error(f"無法讀取 OPC UA Server 清單: {e}")
            return pd.DataFrame()

    # ----------------------------------------------------------
    # 讀取 opcua_tags（已採集的點位資料）
    # ----------------------------------------------------------
    def load_opcua_tags(server_id=None):
        try:
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    if server_id:
                        query = """
                        SELECT id, server_name, node_id, browse_name, display_name,
                               data_type, sensor_id, current_data, quality, plc_state, last_update
                        FROM opcua_tags WHERE server_id = %s ORDER BY id ASC;
                        """
                        cur.execute(query, (int(server_id),))
                    else:
                        query = """
                        SELECT id, server_name, node_id, browse_name, display_name,
                               data_type, sensor_id, current_data, quality, plc_state, last_update
                        FROM opcua_tags ORDER BY id ASC;
                        """
                        cur.execute(query)
                    cols = [desc[0] for desc in cur.description]
                    rows = cur.fetchall()
                    return pd.DataFrame(rows, columns=cols)
        except Exception as e:
            st.error(f"無法讀取 OPC UA 點位資料: {e}")
            return pd.DataFrame()

    df_opcua_servers = load_opcua_servers()

    # ----------------------------------------------------------
    # Server 清單（可直接編輯）
    # ----------------------------------------------------------
    st.subheader("📋 OPC UA Server 清單（可直接於表格內修改參數）")
    if not df_opcua_servers.empty:
        disabled_cols_opcua = ["id", "conn_state", "last_scan", "last_error"]

        edited_opcua_df = st.data_editor(
            df_opcua_servers,
            num_rows="dynamic",
            key="opcua_editor",
            disabled=disabled_cols_opcua,
            width="stretch",
        )

        if st.button("💾 儲存 OPC UA Server 修改", type="primary"):
            try:
                with DatabaseConnector.get_connection() as conn:
                    with conn.cursor() as cur:
                        for index, row in edited_opcua_df.iterrows():
                            if pd.notnull(row["id"]):
                                sql = """
                                UPDATE opcua_servers SET
                                    server_name=%s, ip=%s, port=%s, username=%s, password=%s,
                                    security_policy=%s, security_mode=%s, root_node_id=%s,
                                    browse_depth=%s, enabled=%s
                                WHERE id=%s;
                                """
                                cur.execute(
                                    sql,
                                    (
                                        row["server_name"],
                                        row["ip"],
                                        int(row["port"]),
                                        row["username"] if pd.notnull(row["username"]) else None,
                                        row["password"] if pd.notnull(row["password"]) else None,
                                        row["security_policy"],
                                        row["security_mode"],
                                        row["root_node_id"],
                                        int(row["browse_depth"]),
                                        bool(row["enabled"]),
                                        int(row["id"]),
                                    ),
                                )
                        conn.commit()
                st.success(
                    "✅ OPC UA Server 參數更新成功！"
                    "訂閱服務會在 15 秒內自動偵測到連線參數變更並重新連線套用，不需要重啟 main.py。"
                )
                st.rerun()
            except Exception as e:
                st.error(f"❌ 儲存失敗: {e}")
    else:
        st.info("目前尚無任何 OPC UA Server，請先在下方新增。")

    # ----------------------------------------------------------
    # 新增 Server 表單（含測試連線與瀏覽）
    # ----------------------------------------------------------
    st.divider()
    st.subheader("➕ 新增 OPC UA Server")
    with st.form("add_opcua_form", clear_on_submit=False):
        col1, col2 = st.columns(2)
        with col1:
            o_server_name = st.text_input("Server 名稱 (server_name)", "OPCUA_Line_A")
            o_ip = st.text_input("IP 位址 (ip)", "192.168.1.100")
            o_port = st.number_input("Port", value=4840)
            o_username = st.text_input("帳號 (username，留空=匿名連線)", "")
            o_password = st.text_input("密碼 (password)", "", type="password")

        with col2:
            o_security_policy = st.selectbox(
                "Security Policy", ["None", "Basic256Sha256"], index=0
            )
            o_security_mode = st.selectbox(
                "Security Mode", ["None", "Sign", "SignAndEncrypt"], index=0
            )
            o_root_node_id = st.text_input(
                "瀏覽起始節點 (root_node_id)",
                "i=85",
                help="預設 i=85 為 Objects 資料夾，可指定更精確的節點以縮小瀏覽範圍",
            )
            o_browse_depth = st.number_input(
                "瀏覽深度上限 (browse_depth)", value=5, min_value=1, max_value=20
            )

        btn_col1, btn_col2 = st.columns([1, 1])
        with btn_col1:
            submit_opcua = st.form_submit_button(
                "新增 Server", type="primary", use_container_width=True
            )
        with btn_col2:
            test_opcua = st.form_submit_button(
                "🧪 測試連線與瀏覽", use_container_width=True
            )

        if test_opcua:
            with st.spinner(
                "📡 正在連線並瀏覽 Address Space（點位多時可能需要一些時間）..."
            ):
                try:
                    server_cfg = {
                        "server_name": o_server_name,
                        "ip": o_ip,
                        "port": int(o_port),
                        "username": o_username or None,
                        "password": o_password or None,
                        "security_policy": o_security_policy,
                        "security_mode": o_security_mode,
                        "root_node_id": o_root_node_id,
                        "browse_depth": int(o_browse_depth),
                    }
                    tags = run_async(scan_server(server_cfg))
                    st.success(f"🎉 測試成功！共瀏覽到 {len(tags)} 個點位（此次測試不會寫入資料庫）")
                    if tags:
                        st.dataframe(pd.DataFrame(tags), width="stretch")
                except OPCUAConnectionError as e:
                    st.error(f"❌ 連線失敗: {e}")
                except Exception as e:
                    st.error(f"❌ 瀏覽失敗: {e}")

        if submit_opcua:
            try:
                with DatabaseConnector.get_connection() as conn:
                    with conn.cursor() as cur:
                        sql = """
                        INSERT INTO opcua_servers
                            (server_name, ip, port, username, password, security_policy,
                             security_mode, root_node_id, browse_depth, enabled, conn_state)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, 'UNKNOWN');
                        """
                        cur.execute(
                            sql,
                            (
                                o_server_name,
                                o_ip,
                                int(o_port),
                                o_username or None,
                                o_password or None,
                                o_security_policy,
                                o_security_mode,
                                o_root_node_id,
                                int(o_browse_depth),
                            ),
                        )
                        conn.commit()
                st.success(
                    f"🎉 成功新增 OPC UA Server: {o_server_name}\n\n"
                    "訂閱服務會在 15 秒內自動偵測到這台新 Server 並開始監控，不需要重啟 main.py。"
                )
                st.rerun()
            except Exception as e:
                st.error(f"❌ 新增失敗: {e}")

    # ----------------------------------------------------------
    # 手動立即瀏覽（不用等排程週期，寫入資料庫）
    # ----------------------------------------------------------
    st.divider()
    st.subheader("🔍 手動瀏覽已儲存的 Server（立即執行，不用等排程週期）")
    st.caption(
        "針對單一 Server 立即重新瀏覽並寫入資料庫。"
        "寫入完成後會自動通知常駐的訂閱服務重新整理訂閱內容"
        "（新增的點位會開始被監控、移除的點位會停止監控），"
        "通常數秒內即可生效，不需要重啟任何服務。"
    )

    if df_opcua_servers.empty:
        st.info("尚無 Server 可供瀏覽，請先新增。")
    else:
        server_options = {
            f"{row['server_name']} ({row['ip']}:{row['port']})": row["id"]
            for _, row in df_opcua_servers.iterrows()
        }
        selected_label = st.selectbox("選擇要瀏覽的 Server", list(server_options.keys()))
        selected_id = server_options[selected_label]

        if st.button("🚀 立即瀏覽並寫入資料庫", type="primary"):
            row = df_opcua_servers[df_opcua_servers["id"] == selected_id].iloc[0]
            server_cfg = {
                "server_name": row["server_name"],
                "ip": row["ip"],
                "port": int(row["port"]),
                "username": row["username"] if pd.notnull(row["username"]) else None,
                "password": row["password"] if pd.notnull(row["password"]) else None,
                "security_policy": row["security_policy"],
                "security_mode": row["security_mode"],
                "root_node_id": row["root_node_id"],
                "browse_depth": int(row["browse_depth"]),
            }

            with st.spinner(
                f"正在瀏覽 {row['server_name']}，依點位數量可能需要幾秒到幾分鐘，請耐心等候..."
            ):
                try:
                    tags = run_async(scan_server(server_cfg))
                    batch_update_opcua_tags(int(selected_id), row["server_name"], tags)

                    with DatabaseConnector.get_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE opcua_servers
                                SET conn_state='ONLINE', last_scan=CURRENT_TIMESTAMP, last_error=NULL
                                WHERE id=%s
                                """,
                                (int(selected_id),),
                            )
                            conn.commit()

                    # 🔥 通知常駐訂閱服務：點位表已更新，請重新整理訂閱內容
                    request_opcua_resubscribe(int(selected_id))

                    st.success(
                        f"✅ 瀏覽完成，寫入 {len(tags)} 筆點位資料。"
                        "已通知訂閱服務重新整理，數秒內會套用最新點位表。"
                    )
                    st.rerun()
                except Exception as e:
                    try:
                        with DatabaseConnector.get_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    """
                                    UPDATE opcua_servers
                                    SET conn_state='ERROR', last_scan=CURRENT_TIMESTAMP, last_error=%s
                                    WHERE id=%s
                                    """,
                                    (str(e), int(selected_id)),
                                )
                                conn.commit()
                    except Exception:
                        pass
                    st.error(f"❌ 瀏覽失敗: {e}")

    # ----------------------------------------------------------
    # 已採集的點位資料檢視
    # ----------------------------------------------------------
    st.divider()
    st.subheader("📊 已採集的 OPC UA 點位資料")

    if not df_opcua_servers.empty:
        filter_options = ["全部 Server"] + [
            f"{row['server_name']} ({row['ip']}:{row['port']})"
            for _, row in df_opcua_servers.iterrows()
        ]
        filter_label = st.selectbox("篩選 Server", filter_options, key="opcua_tag_filter")

        if filter_label == "全部 Server":
            df_opcua_tags = load_opcua_tags()
        else:
            filter_id = server_options[filter_label]
            df_opcua_tags = load_opcua_tags(server_id=filter_id)
    else:
        df_opcua_tags = load_opcua_tags()

    if not df_opcua_tags.empty:
        opcua_label_to_id, opcua_id_to_label = _load_sensor_options()

        df_opcua_tags["sensor_label"] = df_opcua_tags["sensor_id"].apply(
            lambda sid: opcua_id_to_label.get(int(sid), UNBOUND_LABEL)
            if pd.notnull(sid)
            else UNBOUND_LABEL
        )
        df_opcua_tags_display = df_opcua_tags.drop(columns=["sensor_id"])

        st.caption("💡 可直接在下方表格的「綁定感測器」欄位選擇對應感測器後按儲存。")

        disabled_cols_opcua_tags = [
            "id", "server_name", "node_id", "browse_name", "display_name",
            "data_type", "current_data", "quality", "plc_state", "last_update",
        ]

        edited_opcua_tags_df = st.data_editor(
            df_opcua_tags_display,
            key="opcua_tags_editor",
            disabled=disabled_cols_opcua_tags,
            width="stretch",
            column_config={
                "sensor_label": st.column_config.SelectboxColumn(
                    "綁定感測器 (sensor_code)",
                    options=[UNBOUND_LABEL] + list(opcua_label_to_id.keys()),
                    help="選擇這個點位對應的感測器，數值會同步寫入 sensor_readings 時序表。"
                    "選單內容請先在「感測器階層管理」分頁建立。",
                )
            },
        )

        if st.button("💾 儲存 OPC UA 點位綁定", type="primary"):
            binding_map = _load_sensor_binding_map()
            try:
                with DatabaseConnector.get_connection() as conn:
                    with conn.cursor() as cur:
                        for index, row in edited_opcua_tags_df.iterrows():
                            point_id = int(row["id"])

                            sensor_label = row.get("sensor_label", UNBOUND_LABEL)
                            sensor_id = (
                                opcua_label_to_id.get(sensor_label)
                                if sensor_label != UNBOUND_LABEL
                                else None
                            )
                            conflict = _find_binding_conflict(
                                binding_map, sensor_id, "opcua_tags", point_id
                            )
                            if conflict:
                                st.error(
                                    f"❌ id={point_id}（{row['node_id']}）想綁定的感測器"
                                    f"已被其他點位使用：{conflict}，該筆未儲存。"
                                    "同一個感測器不能同時綁定多個點位。"
                                )
                                continue

                            cur.execute(
                                "UPDATE opcua_tags SET sensor_id = %s WHERE id = %s;",
                                (sensor_id, point_id),
                            )
                        conn.commit()
                st.success("✅ OPC UA 點位綁定更新成功！")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 儲存失敗: {e}")
    else:
        st.info("目前尚無任何 OPC UA 點位資料，請先新增 Server 並執行瀏覽。")


#######複製貼上的##############################
# ==========================================
# Tab 4: 感測器階層管理 (sites -> production_lines -> devices -> sensors)
# ==========================================
with tab_hierarchy:
    st.header("感測器階層管理")
    st.caption(
        "在這裡建立 廠區 → 產線 → 設備 → 感測器 的階層資料。"
        "建立好 sensor 之後，回到「Modbus/TIA/OPC UA 點位設定」分頁，"
        "把每個點位的 sensor_code 欄位選成對應的感測器，"
        "採集程式就會自動把數值寫進 sensor_readings 時序表。"
    )

    # ------------------------------------------------------------
    # 1. 廠區 (sites)
    # ------------------------------------------------------------
    st.subheader("🏭 廠區 (sites)")
    df_sites = _fetch_df(
        "SELECT site_id, site_name, location FROM sites ORDER BY site_id ASC;"
    )
    if not df_sites.empty:
        edited_sites = st.data_editor(
            df_sites, num_rows="dynamic", key="sites_editor",
            disabled=["site_id"], width="stretch",
        )
        if st.button("💾 儲存廠區修改", key="save_sites"):
            try:
                with DatabaseConnector.get_connection() as conn:
                    with conn.cursor() as cur:
                        for _, row in edited_sites.iterrows():
                            if pd.notnull(row["site_id"]):
                                cur.execute(
                                    "UPDATE sites SET site_name=%s, location=%s WHERE site_id=%s;",
                                    (row["site_name"], row["location"], int(row["site_id"])),
                                )
                        conn.commit()
                st.success("✅ 廠區資料更新成功！")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 儲存失敗: {e}")

    with st.form("add_site_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            new_site_name = st.text_input("廠區名稱 (site_name)", "")
        with col2:
            new_site_location = st.text_input("位置 (location)", "")
        if st.form_submit_button("➕ 新增廠區", type="primary"):
            if new_site_name.strip():
                try:
                    with DatabaseConnector.get_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "INSERT INTO sites (site_name, location) VALUES (%s, %s);",
                                (new_site_name, new_site_location or None),
                            )
                            conn.commit()
                    st.success(f"🎉 成功新增廠區: {new_site_name}")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 新增失敗: {e}")
            else:
                st.warning("⚠️ 請輸入廠區名稱")

    st.divider()

    # ------------------------------------------------------------
    # 2. 產線 (production_lines)
    # ------------------------------------------------------------
    st.subheader("🏗️ 產線 (production_lines)")
    df_lines = _fetch_df(
        """
        SELECT pl.line_id, s.site_name, pl.line_name, pl.site_id
        FROM production_lines pl
        LEFT JOIN sites s ON pl.site_id = s.site_id
        ORDER BY pl.line_id ASC;
        """
    )
    site_options = {row["site_name"]: row["site_id"] for _, row in df_sites.iterrows()} if not df_sites.empty else {}

    if not df_lines.empty:
        st.dataframe(df_lines[["line_id", "site_name", "line_name"]], width="stretch")

    with st.form("add_line_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            if site_options:
                new_line_site = st.selectbox("所屬廠區 (site_name)", list(site_options.keys()))
            else:
                new_line_site = None
                st.warning("⚠️ 請先新增廠區")
        with col2:
            new_line_name = st.text_input("產線名稱 (line_name)", "")
        if st.form_submit_button("➕ 新增產線", type="primary"):
            if new_line_name.strip() and new_line_site:
                try:
                    with DatabaseConnector.get_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "INSERT INTO production_lines (site_id, line_name) VALUES (%s, %s);",
                                (site_options[new_line_site], new_line_name),
                            )
                            conn.commit()
                    st.success(f"🎉 成功新增產線: {new_line_name}")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 新增失敗: {e}")
            else:
                st.warning("⚠️ 請輸入產線名稱並選擇所屬廠區")

    st.divider()

    # ------------------------------------------------------------
    # 3. 設備 (devices)
    # ------------------------------------------------------------
    st.subheader("⚙️ 設備 (devices)")
    df_devices = _fetch_df(
        """
        SELECT d.device_id, s.site_name, pl.line_name, d.device_code, d.device_name,
               d.device_type, d.manufacturer, d.install_date, d.status, d.line_id
        FROM devices d
        LEFT JOIN production_lines pl ON d.line_id = pl.line_id
        LEFT JOIN sites s ON pl.site_id = s.site_id
        ORDER BY d.device_id ASC;
        """
    )
    line_options = {
        f"{row['site_name']} / {row['line_name']}": row["line_id"]
        for _, row in df_lines.iterrows()
    } if not df_lines.empty else {}

    if not df_devices.empty:
        st.dataframe(
            df_devices[[
                "device_id", "site_name", "line_name", "device_code",
                "device_name", "device_type", "manufacturer", "install_date", "status",
            ]],
            width="stretch",
        )

    with st.form("add_device_form", clear_on_submit=True):
        col1, col2, col3 = st.columns(3)
        with col1:
            if line_options:
                new_device_line = st.selectbox("所屬產線", list(line_options.keys()))
            else:
                new_device_line = None
                st.warning("⚠️ 請先新增產線")
            new_device_code = st.text_input("設備編號 (device_code，唯一)", "")
        with col2:
            new_device_name = st.text_input("設備名稱 (device_name)", "")
            new_device_type = st.text_input("設備類型 (device_type)", "")
        with col3:
            new_device_manufacturer = st.text_input("製造商 (manufacturer)", "")
            new_device_status = st.selectbox("狀態 (status)", ["active", "maintenance", "offline"])
        if st.form_submit_button("➕ 新增設備", type="primary"):
            if new_device_code.strip() and new_device_line:
                try:
                    with DatabaseConnector.get_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                INSERT INTO devices (line_id, device_code, device_name, device_type, manufacturer, status)
                                VALUES (%s, %s, %s, %s, %s, %s);
                                """,
                                (
                                    line_options[new_device_line],
                                    new_device_code,
                                    new_device_name or None,
                                    new_device_type or None,
                                    new_device_manufacturer or None,
                                    new_device_status,
                                ),
                            )
                            conn.commit()
                    st.success(f"🎉 成功新增設備: {new_device_code}")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 新增失敗（device_code 需為唯一值）: {e}")
            else:
                st.warning("⚠️ 請輸入設備編號並選擇所屬產線")

    st.divider()

    # ------------------------------------------------------------
    # 4. 感測器 (sensors)
    # ------------------------------------------------------------
    st.subheader("🌡️ 感測器 (sensors)")
    df_sensors_full = _fetch_df(
        """
        SELECT se.sensor_id, d.device_code, se.sensor_code, se.nickname, se.sensor_type,
               se.unit, se.min_threshold, se.max_threshold, se.state_dictionary, se.device_id
        FROM sensors se
        LEFT JOIN devices d ON se.device_id = d.device_id
        ORDER BY se.sensor_id ASC;
        """
    )
    device_options = {
        row["device_code"]: row["device_id"]
        for _, row in df_devices.iterrows()
    } if not df_devices.empty else {}

    st.caption(
        "可直接在下方表格編輯「nickname」等欄位，方便日後對照識別（例如中文說明、位置等）。"
        "「state_dictionary」是選填的狀態字典（JSON 格式，例如 {\"1\": \"待機\", \"2\": \"運轉\"}），"
        "設定後查詢 sensor_readings_translated 這個 view 就會自動把數字翻譯成文字。"
    )
    if not df_sensors_full.empty:
        df_sensors_full["state_dictionary"] = df_sensors_full["state_dictionary"].apply(
            lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v
        )

        edited_sensors_df = st.data_editor(
            df_sensors_full[[
                "sensor_id", "device_code", "sensor_code", "nickname", "sensor_type",
                "unit", "min_threshold", "max_threshold", "state_dictionary",
            ]],
            num_rows="fixed",
            key="sensors_editor",
            disabled=["sensor_id", "device_code", "sensor_code"],
            width="stretch",
            column_config={
                "state_dictionary": st.column_config.TextColumn(
                    "state_dictionary（選填，JSON 格式）",
                    help='例如：{"1": "待機", "2": "運轉"}',
                )
            },
        )

        if st.button("💾 儲存感測器修改", key="save_sensors"):
            try:
                with DatabaseConnector.get_connection() as conn:
                    with conn.cursor() as cur:
                        for _, row in edited_sensors_df.iterrows():
                            state_dict_val, state_dict_err = _normalize_state_dict(
                                row["state_dictionary"]
                            )
                            if state_dict_err:
                                st.error(
                                    f"❌ sensor_id={int(row['sensor_id'])} 的 "
                                    f"state_dictionary 格式錯誤，該筆未儲存：{state_dict_err}"
                                )
                                continue

                            cur.execute(
                                """
                                UPDATE sensors SET
                                    nickname=%s, sensor_type=%s, unit=%s,
                                    min_threshold=%s, max_threshold=%s, state_dictionary=%s
                                WHERE sensor_id=%s;
                                """,
                                (
                                    row["nickname"].strip()
                                    if pd.notnull(row["nickname"]) and str(row["nickname"]).strip()
                                    else None,
                                    row["sensor_type"],
                                    row["unit"],
                                    float(row["min_threshold"]) if pd.notnull(row["min_threshold"]) else None,
                                    float(row["max_threshold"]) if pd.notnull(row["max_threshold"]) else None,
                                    state_dict_val,
                                    int(row["sensor_id"]),
                                ),
                            )
                        conn.commit()
                st.success("✅ 感測器資料更新成功！")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 儲存失敗: {e}")
    else:
        st.info("目前尚無任何感測器，請在下方新增。新增後即可回到點位設定分頁進行綁定。")

    # 常見感測器類型（可選「其他（自訂）」自行輸入）
    SENSOR_TYPE_OPTIONS = [
        "temperature", "pressure", "vibration", "current", "voltage",
        "power", "energy", "flow", "level", "humidity", "speed",
        "torque", "position", "ph", "conductivity", "weight", "count", "status",
        "gas", "liquid", "solid",
        "其他（自訂）",
    ]

    # 常用工程單位（依 UNECE Recommendation 20 / OPC UA Part 8 Engineering Units 慣例整理）
    OPCUA_UNIT_OPTIONS = [
        "°C", "°F", "K",
        "Pa", "kPa", "bar", "mbar", "psi",
        "m/s", "mm/s", "m/s²", "rpm", "Hz",
        "V", "mV", "A", "mA",
        "W", "kW", "Wh", "kWh", "VA", "var",
        "%", "%RH",
        "L", "L/min", "m³", "m³/h",
        "mm", "cm", "m",
        "g", "kg", "t",
        "N", "Nm",
        "pH", "μS/cm",
        "count",
        "其他（自訂）",
    ]

    with st.form("add_sensor_form", clear_on_submit=True):
        col1, col2, col3 = st.columns(3)
        with col1:
            if device_options:
                new_sensor_device = st.selectbox("所屬設備 (device_code)", list(device_options.keys()))
            else:
                new_sensor_device = None
                st.warning("⚠️ 請先新增設備")
            new_sensor_code = st.text_input("感測器編號 (sensor_code，唯一)", "")
            new_sensor_nickname = st.text_input(
                "暱稱 (nickname，選填)", "", placeholder="例如：B03 蒸氣流量計"
            )
        with col2:
            new_sensor_type_selected = st.selectbox(
                "感測器類型 (sensor_type)", SENSOR_TYPE_OPTIONS, index=0
            )
            new_sensor_type_custom = st.text_input(
                "↳ 選「其他（自訂）」時請在此輸入",
                "",
                placeholder="例如：oil_pressure",
                key="new_sensor_type_custom",
            )
            new_sensor_unit_selected = st.selectbox(
                "單位 (unit，依 OPC UA 工程單位慣例)", OPCUA_UNIT_OPTIONS, index=0
            )
            new_sensor_unit_custom = st.text_input(
                "↳ 選「其他（自訂）」時請在此輸入",
                "",
                placeholder="例如：mmHg",
                key="new_sensor_unit_custom",
            )
        with col3:
            new_sensor_min = st.number_input("正常值下限 (min_threshold)", value=0.0)
            new_sensor_max = st.number_input("正常值上限 (max_threshold)", value=100.0)
            new_sensor_state_dict = st.text_input(
                "狀態字典 JSON (state_dictionary，選填)",
                value="",
                placeholder='{"1": "待機", "2": "運轉"}',
            )
        if st.form_submit_button("➕ 新增感測器", type="primary"):
            final_sensor_type = (
                new_sensor_type_custom.strip()
                if new_sensor_type_selected == "其他（自訂）"
                else new_sensor_type_selected
            )
            final_sensor_unit = (
                new_sensor_unit_custom.strip()
                if new_sensor_unit_selected == "其他（自訂）"
                else new_sensor_unit_selected
            )
            formatted_state_dict = None
            state_dict_error = False
            if new_sensor_state_dict.strip():
                try:
                    formatted_state_dict = json.dumps(
                        json.loads(new_sensor_state_dict), ensure_ascii=False
                    )
                except json.JSONDecodeError:
                    state_dict_error = True
                    st.error("❌ 狀態字典格式錯誤！請填寫合法的 JSON 格式")

            if state_dict_error:
                pass
            elif new_sensor_code.strip() and new_sensor_device and final_sensor_type:
                try:
                    with DatabaseConnector.get_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                INSERT INTO sensors
                                    (device_id, sensor_code, nickname, sensor_type, unit,
                                     min_threshold, max_threshold, state_dictionary)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                                """,
                                (
                                    device_options[new_sensor_device],
                                    new_sensor_code.strip(),
                                    new_sensor_nickname.strip() or None,
                                    final_sensor_type,
                                    final_sensor_unit or None,
                                    new_sensor_min,
                                    new_sensor_max,
                                    formatted_state_dict,
                                ),
                            )
                            conn.commit()
                    st.success(f"🎉 成功新增感測器: {new_sensor_code}")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 新增失敗（sensor_code 需為唯一值）: {e}")
            else:
                st.warning("⚠️ 請輸入感測器編號並選擇所屬設備")


# ==========================================
# Tab 5: 異常監控（連線異常 + 閾值超標 + 資料斷更）
# ==========================================
with tab_alerts:
    st.header("🚨 異常監控")
    st.caption(
        "這個分頁彙整三種常見的「點位異常」，不用自己下 SQL 一個一個查。"
    )

    # ------------------------------------------------------------
    # 1. 連線異常：即時層三張表裡 plc_state 不是 ONLINE 的點位
    # ------------------------------------------------------------
    st.subheader("🔌 連線異常（plc_state ≠ ONLINE）")
    st.caption("代表這個點位上一輪採集時 PLC/Server 連不上，或讀取/解析失敗。")

    df_offline = _fetch_df(
        """
        SELECT 'Modbus' AS 來源, id, name AS 點位名稱, plc_ip, plc_state, last_update::text AS last_update
        FROM modbus_scada WHERE plc_state IS DISTINCT FROM 'ONLINE'
        UNION ALL
        SELECT 'TIA/S7', id, name, plc_ip, plc_state, last_update::text
        FROM tia_scada WHERE plc_state IS DISTINCT FROM 'ONLINE'
        UNION ALL
        SELECT 'OPC UA', id, node_id, server_name, plc_state, last_update::text
        FROM opcua_tags WHERE plc_state IS DISTINCT FROM 'ONLINE'
        ORDER BY 來源, id;
        """
    )
    if not df_offline.empty:
        st.error(f"⚠️ 目前有 {len(df_offline)} 個點位連線異常")
        st.dataframe(df_offline, width="stretch")
    else:
        st.success("✅ 目前所有點位連線狀態正常")

    st.divider()

    # ------------------------------------------------------------
    # 2. 數值超出正常範圍（依 sensors.min_threshold / max_threshold）
    # ------------------------------------------------------------
    st.subheader("📈 數值超出正常範圍")
    st.caption(
        "依每個感測器在 sensors 表設定的 min_threshold / max_threshold，"
        "比對 sensor_readings 裡最新一筆數值。僅涵蓋已綁定 sensor_id 的點位。"
    )

    df_out_of_range = _fetch_df(
        """
        SELECT
            s.sensor_code,
            s.nickname,
            s.sensor_type,
            r.value AS 目前數值,
            s.min_threshold AS 下限,
            s.max_threshold AS 上限,
            s.unit,
            r.reading_time AS 讀取時間
        FROM sensors s
        JOIN LATERAL (
            SELECT value, reading_time
            FROM sensor_readings
            WHERE sensor_id = s.sensor_id
            ORDER BY reading_time DESC
            LIMIT 1
        ) r ON true
        WHERE (s.min_threshold IS NOT NULL AND r.value < s.min_threshold)
           OR (s.max_threshold IS NOT NULL AND r.value > s.max_threshold)
        ORDER BY r.reading_time DESC;
        """
    )
    if not df_out_of_range.empty:
        st.error(f"⚠️ 目前有 {len(df_out_of_range)} 個感測器數值超出正常範圍")
        st.dataframe(df_out_of_range, width="stretch")
    else:
        st.success("✅ 目前所有已綁定感測器的數值都在正常範圍內")

    st.divider()

    # ------------------------------------------------------------
    # 3. 資料斷更（超過 N 小時沒有新的 sensor_readings）
    # ------------------------------------------------------------
    st.subheader("⏱️ 資料斷更（疑似離線）")
    stale_hours = st.number_input(
        "判定為「太久沒更新」的時數門檻（小時）", min_value=1, value=2, step=1
    )
    st.caption(
        "正常情況下，就算數值沒變化，系統也會依心跳週期定期補寫一筆進 sensor_readings。"
        "如果一個已綁定的感測器超過這個時數都沒有任何新資料，通常代表該點位已經斷線、"
        "或是採集程式沒有正常執行。"
    )

    df_stale = _fetch_df(
        f"""
        SELECT
            s.sensor_code,
            s.nickname,
            s.sensor_type,
            r.reading_time AS 最後讀取時間,
            r.value AS 最後數值,
            now() - r.reading_time AS 已斷更多久
        FROM sensors s
        JOIN LATERAL (
            SELECT value, reading_time
            FROM sensor_readings
            WHERE sensor_id = s.sensor_id
            ORDER BY reading_time DESC
            LIMIT 1
        ) r ON true
        WHERE r.reading_time < now() - INTERVAL '{int(stale_hours)} hours'
        ORDER BY r.reading_time ASC;
        """
    )
    df_never = _fetch_df(
        """
        SELECT s.sensor_code, s.nickname, s.sensor_type
        FROM sensors s
        LEFT JOIN sensor_readings r ON r.sensor_id = s.sensor_id
        WHERE r.sensor_id IS NULL;
        """
    )

    if not df_stale.empty:
        st.error(f"⚠️ 有 {len(df_stale)} 個感測器超過 {stale_hours} 小時沒有新資料")
        st.dataframe(df_stale, width="stretch")
    else:
        st.success(f"✅ 目前沒有感測器斷更超過 {stale_hours} 小時")

    if not df_never.empty:
        st.warning(
            f"ℹ️ 另外有 {len(df_never)} 個感測器從建立以來「從未」寫入過 sensor_readings"
            "（可能是尚未綁定對應點位，或該點位一直讀取失敗）："
        )
        st.dataframe(df_never, width="stretch")