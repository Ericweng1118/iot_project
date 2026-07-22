import json
import os
import struct
import pandas as pd
import streamlit as st

# 1. 載入 .env 環境變數
from dotenv import load_dotenv

load_dotenv()

# 取得帳密設定 (若 .env 未設定則給預設值)
ADMIN_USER = os.getenv("ADMIN_USER", "eric")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "eric1118")

from data_layer.db_connector import DatabaseConnector

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
    raw_max=4095.0,
    eng_min=0.0,
    eng_max=100.0,
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

        if word_order.upper() == "LITTLE" and len(registers) > 1:
            registers = registers[::-1]

        byte_fmt = ">H" if byte_order.upper() == "BIG" else "<H"
        raw_bytes = b"".join([struct.pack(byte_fmt, r) for r in registers])

        fmt_map = {
            "word": ">H" if byte_order.upper() == "BIG" else "<H",
            "int": ">h" if byte_order.upper() == "BIG" else "<h",
            "uint32": ">I" if byte_order.upper() == "BIG" else "<I",
            "dint": ">i" if byte_order.upper() == "BIG" else "<i",
            "float": ">f" if byte_order.upper() == "BIG" else "<f",
            "uint64": ">Q" if byte_order.upper() == "BIG" else "<Q",
            "int64": ">q" if byte_order.upper() == "BIG" else "<q",
            "float64": ">d" if byte_order.upper() == "BIG" else "<d",
        }
        fmt = fmt_map.get(dt, ">H")
        raw_val = struct.unpack(fmt, raw_bytes)[0]

        final_val = raw_val
        scale_info = ""
        if use_scale and (raw_max != raw_min):
            final_val = eng_min + (raw_val - raw_min) * (eng_max - eng_min) / (
                raw_max - raw_min
            )
            scale_info = f" (Raw 原始值: `{raw_val}`, Scaling 工程值: `{round(final_val, 4)}`)"

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

tab_modbus, tab_tia = st.tabs(["📡 Modbus 點位設定", "📡 TIA (S7) 點位設定"])


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
                        state_dictionary, plc_state, current_value, last_update
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
            m_name = st.text_input("點位名稱 (name)", "T3_Temp1")
            m_plc_ip = st.text_input("PLC IP 地址 (plc_ip)", "192.168.1.100")
            m_plc_port = st.number_input("Modbus 埠號 (plc_port)", value=502)
            m_slave_id = st.number_input(
                "從站 ID (slave_id)", value=1, min_value=1, max_value=247
            )

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
            m_raw_max = st.number_input("原始最大值 (raw_max)", value=4095.0)
            m_eng_min = st.number_input("工程最小值 (eng_min)", value=0.0)
            m_eng_max = st.number_input("工程最大值 (eng_max)", value=100.0)
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
                            state_dictionary, plc_state
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OFFLINE');
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