import asyncio
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

tab_modbus, tab_tia, tab_opcua = st.tabs(
    ["📡 Modbus 點位設定", "📡 TIA (S7) 點位設定", "📡 OPC UA 點位設定"]
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
                        state_dictionary, plc_state, current_value,current_data,unit, last_update
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

    st.subheader("📋 Modbus 點位列表（可直接於表格內修改參數）")
    if not df_modbus.empty:
        disabled_cols_modbus = [
            "id",
            "plc_state",
            "current_value",
            "last_update",
        ]

        edited_modbus_df = st.data_editor(
            df_modbus,
            num_rows="dynamic",
            key="modbus_editor",
            disabled=disabled_cols_modbus,
            width="stretch",
        )

        if st.button("💾 儲存 Modbus 修改", type="primary"):
            try:
                with DatabaseConnector.get_connection() as conn:
                    with conn.cursor() as cur:
                        for index, row in edited_modbus_df.iterrows():
                            if pd.notnull(row["id"]):
                                state_dict_val = row["state_dictionary"]
                                if isinstance(state_dict_val, dict):
                                    state_dict_val = json.dumps(state_dict_val)

                                sql = """
                                UPDATE modbus_scada SET 
                                    name=%s, plc_ip=%s, plc_port=%s, slave_id=%s, 
                                    function_code=%s, start_address=%s, data_type=%s,
                                    raw_min=%s, raw_max=%s, eng_min=%s, eng_max=%s,
                                    byte_order=%s, word_order=%s, state_dictionary=%s
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
                                        state_dict_val
                                        if pd.notnull(state_dict_val)
                                        else None,
                                        int(row["id"]),
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

                with DatabaseConnector.get_connection() as conn:
                    with conn.cursor() as cur:
                        sql = """
                        INSERT INTO modbus_scada (
                            name, plc_ip, plc_port, slave_id, function_code, start_address, 
                            data_type, byte_order, word_order, raw_min, raw_max, eng_min, eng_max, 
                            state_dictionary,unit, plc_state
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OFFLINE');
                        """
                        cur.execute(
                            sql,
                            (
                                m_name,
                                m_plc_ip,
                                int(m_plc_port),
                                int(m_slave_id),
                                m_unit,
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
                        plc_state, current_data, last_update 
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

    st.subheader("📋 TIA 點位列表（可直接於表格內修改參數）")
    if not df_tia.empty:
        disabled_cols_tia = ["id", "plc_state", "current_data", "last_update"]

        edited_tia_df = st.data_editor(
            df_tia,
            num_rows="dynamic",
            key="tia_editor",
            disabled=disabled_cols_tia,
            width="stretch",
        )

        if st.button("💾 儲存 TIA 修改", type="primary"):
            try:
                with DatabaseConnector.get_connection() as conn:
                    with conn.cursor() as cur:
                        for index, row in edited_tia_df.iterrows():
                            if pd.notnull(row["id"]):
                                sql = """
                                UPDATE tia_scada SET 
                                    name=%s, plc_name=%s, plc_ip=%s, db_number=%s, 
                                    "offset"=%s, data_type=%s
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
                                        int(row["id"]),
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
                with DatabaseConnector.get_connection() as conn:
                    with conn.cursor() as cur:
                        sql = """
                        INSERT INTO tia_scada (name, plc_name, plc_ip, db_number, "offset", data_type, plc_state)
                        VALUES (%s, %s, %s, %s, %s, %s, 'OFFLINE');
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
                               data_type, current_data, quality, plc_state, last_update
                        FROM opcua_tags WHERE server_id = %s ORDER BY id ASC;
                        """
                        cur.execute(query, (int(server_id),))
                    else:
                        query = """
                        SELECT id, server_name, node_id, browse_name, display_name,
                               data_type, current_data, quality, plc_state, last_update
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
                st.success("✅ OPC UA Server 參數更新成功！")
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
                st.success(f"🎉 成功新增 OPC UA Server: {o_server_name}")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 新增失敗: {e}")

    # ----------------------------------------------------------
    # 手動立即瀏覽（不用等排程週期，寫入資料庫）
    # ----------------------------------------------------------
    st.divider()
    st.subheader("🔍 手動瀏覽已儲存的 Server（立即執行，不用等排程週期）")
    st.caption(
        "如果擔心 main.py 排程週期太長（點位多會拖慢整輪採集），"
        "可以在這裡針對單一 Server 立即瀏覽並直接寫入資料庫，"
        "不影響其他 Server 的排程。"
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

                    st.success(f"✅ 瀏覽完成，寫入 {len(tags)} 筆點位資料")
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
        st.dataframe(df_opcua_tags, width="stretch")
    else:
        st.info("目前尚無任何 OPC UA 點位資料，請先新增 Server 並執行瀏覽。")