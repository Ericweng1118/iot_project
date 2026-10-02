"""
web/common.py
=============
網頁後台各分頁共用的工具函式：資料庫查詢、感測器下拉選單、點位綁定衝突檢查、
目前登入者 / 權限檢查、操作稽核。

原本全部寫在 admin_app.py 裡（2266 行單檔），v3 拆成 web/ 底下的模組，
admin_app.py 只剩頁面導覽與登入。舊分頁沿用的函式名稱（_fetch_df 等）維持不變。
"""

import asyncio
import ast
import json
from datetime import datetime

import pandas as pd
import streamlit as st

from core.audit import audit
from core.auth import ROLE_LABELS, has_role
from core.config import LOCAL_TZ, MODBUS_ENABLED, OPCUA_ENABLED, TIA_ENABLED, now_local  # noqa: F401  (給各分頁 import)
from data_layer.db_connector import DatabaseConnector

# 採集主程式心跳超過這個秒數沒更新，就視為停止（main.py 預設每 10 秒回報一次）
SERVICE_STALE_SECONDS = 60


# ------------------------------------------------------------------
# 目前登入者 / 權限 / 稽核
# ------------------------------------------------------------------
def current_user() -> dict:
    return st.session_state.get("user") or {
        "username": "unknown", "display_name": "unknown", "role": "viewer", "source": "none",
    }


def can(role: str) -> bool:
    """目前登入者是否具備 role 以上的權限。"""
    return has_role(current_user()["role"], role)


def require_role(role: str) -> None:
    """權限不足時顯示訊息並停止渲染這一頁。"""
    if not can(role):
        st.error(f"🔒 這個頁面需要「{ROLE_LABELS.get(role, role)}」以上的權限。")
        st.stop()


def audit_ui(action: str, target: str | None = None, detail=None) -> None:
    """以目前登入者的身分寫一筆稽核紀錄（失敗不影響原本操作）。"""
    audit(current_user()["username"], action, target, detail)


# ------------------------------------------------------------------
# 查詢工具
# ------------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner=False)
def table_exists(table: str) -> bool:
    """用來判斷某支 migration 跑過沒有，沒跑的話新功能顯示提示而不是整頁報錯。"""
    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s) IS NOT NULL;", (f"public.{table}",))
                return bool(cur.fetchone()[0])
    except Exception:
        return False


@st.cache_data(ttl=300, show_spinner=False)
def column_exists(table: str, column: str) -> bool:
    """判斷某支 migration 加的欄位是否存在（例如 sql/014 的 sensor_readings.quality）。"""
    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = %s AND column_name = %s);",
                    (table, column),
                )
                return bool(cur.fetchone()[0])
    except Exception:
        return False


def fetch_df(query: str, params=None, show_error: bool = True) -> pd.DataFrame:
    """執行 SELECT 回傳 DataFrame；支援 %s 參數（新程式碼請一律用參數，不要字串拼接）。"""
    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                cols = [desc[0] for desc in cur.description]
                return pd.DataFrame(cur.fetchall(), columns=cols)
    except Exception as e:
        if show_error:
            st.error(f"查詢失敗: {e}")
        return pd.DataFrame()


def execute(query: str, params=None) -> int:
    """執行 INSERT / UPDATE / DELETE，回傳影響筆數；失敗時丟出例外由呼叫端顯示。"""
    with DatabaseConnector.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.rowcount


def get_service_status(service_name: str = "collector") -> dict | None:
    """讀取 service_status，附加 age_seconds / alive / stopped 判斷。表不存在時回傳 None。"""
    if not table_exists("service_status"):
        return None
    df = fetch_df(
        "SELECT *, EXTRACT(EPOCH FROM now() - last_heartbeat) AS age_seconds "
        "FROM service_status WHERE service_name = %s;",
        (service_name,), show_error=False,
    )
    if df.empty:
        return {"exists": False}
    row = df.iloc[0].to_dict()
    info = row.get("info") or {}
    if isinstance(info, str):
        info = json.loads(info)
    row["info"] = info
    row["exists"] = True
    row["age_seconds"] = float(row["age_seconds"]) if row["age_seconds"] is not None else None
    row["alive"] = row["age_seconds"] is not None and row["age_seconds"] <= SERVICE_STALE_SECONDS
    row["stopped_normally"] = bool(info.get("stopped_at")) and not row["alive"]
    return row


def fmt_age(seconds) -> str:
    """秒數 → 「3 秒前 / 5 分鐘前 / 2 小時前 / 3 天前」"""
    if seconds is None or pd.isna(seconds):
        return "—"
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.0f} 秒前"
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分鐘前"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小時前"
    return f"{seconds / 86400:.1f} 天前"


def fmt_ts(value) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return "—"
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


@st.cache_data(ttl=30, show_spinner=False)
def sensor_catalog() -> pd.DataFrame:
    """
    全部感測器 + 階層資訊 + 顯示用 label，給趨勢 / 報表 / 警報規則的選單使用。
    label 格式：「設備編號 / 感測器編號（暱稱）[單位]」
    """
    df = fetch_df(
        """
        SELECT s.sensor_id, s.sensor_code, s.nickname, s.sensor_type, s.unit,
               s.min_threshold, s.max_threshold, s.state_dictionary,
               d.device_id, d.device_code, d.device_name,
               pl.line_id, pl.line_name, si.site_id, si.site_name
        FROM sensors s
        LEFT JOIN devices d           ON d.device_id = s.device_id
        LEFT JOIN production_lines pl ON pl.line_id = d.line_id
        LEFT JOIN sites si            ON si.site_id = pl.site_id
        ORDER BY d.device_code NULLS LAST, s.sensor_code;
        """,
        show_error=False,
    )
    if df.empty:
        df["label"] = []
        return df

    def _label(r):
        text = f"{r['device_code']} / {r['sensor_code']}" if pd.notnull(r["device_code"]) else r["sensor_code"]
        if pd.notnull(r["nickname"]) and str(r["nickname"]).strip():
            text += f"（{r['nickname']}）"
        if pd.notnull(r["unit"]) and str(r["unit"]).strip():
            text += f" [{r['unit']}]"
        return text

    df["label"] = df.apply(_label, axis=1)
    return df


def localize_times(df: pd.DataFrame) -> pd.DataFrame:
    """帶時區的時間欄位轉成 SCADA_TIMEZONE 當地時間並去掉時區標記（匯出用）。"""
    out = df.copy()
    for col in out.columns:
        if isinstance(out[col].dtype, pd.DatetimeTZDtype):
            out[col] = out[col].dt.tz_convert(LOCAL_TZ).dt.tz_localize(None)
    return out


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    """CSV 加 BOM（utf-8-sig），Excel 直接開啟中文才不會亂碼；時間轉成當地時間。"""
    return localize_times(df).to_csv(index=False).encode("utf-8-sig")


def to_excel_bytes(sheets: dict) -> bytes | None:
    """{工作表名稱: DataFrame} → xlsx bytes；沒裝 openpyxl 時回傳 None。"""
    try:
        import io
        import openpyxl  # noqa: F401
    except ImportError:
        return None
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            # Excel 不支援帶時區的時間，轉成當地時間後去掉時區資訊
            out = localize_times(frame)
            out.to_excel(writer, sheet_name=name[:31], index=False)
    return buf.getvalue()


# ==================================================================
# 以下為 v2 admin_app.py 原有的共用函式（原封不動搬過來）
# ==================================================================
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
# Helper 3: OPC UA 相關工具函式
# ------------------------------------------------------------------
def run_async(coro):
    """
    在 Streamlit 的同步環境中執行 asyncio coroutine。
    按鈕點下去會同步等待結果回來（畫面轉圈），
    點位數量多或網路慢時可能需要等待數秒到數十秒。
    """
    return asyncio.run(coro)


def _fetch_df(query, params=None):
    """
    通用查詢 helper：執行任意 SELECT 並回傳 DataFrame。
    給「感測器階層管理」與「異常監控」兩個分頁共用。
    """
    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
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
    # 計算點的結果感測器也算「已被使用」，不能再綁實體點位
    if table_exists("calculated_points"):
        checks.append(("calculated_points", "calc_id", "expression"))
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


def _sensor_select_options(label_to_id, binding_map, keep_ids=()):
    """
    產生「綁定感測器」下拉選單的選項清單：把已經被其他點位綁走的感測器濾掉，
    選單只留下「還沒有人綁」的感測器，避免上百個已綁定項目把選單塞爆。

    ⚠️ keep_ids 是必要的，不能單純把所有已綁定的都拿掉：
        Streamlit 的 SelectboxColumn 是「整欄共用一份 options」，沒辦法逐列給
        不同選項。已經有綁定的那些列，它自己目前的值一定要留在 options 裡，
        否則該儲存格的值不在選項內，Streamlit 會顯示成空白甚至丟出例外，
        使用者只要按一次儲存就會把原本的綁定洗掉。
        所以呼叫端要把「目前畫面上這些列自己已綁定的 sensor_id」傳進來。

    :param label_to_id: _load_sensor_options() 回傳的 label -> sensor_id
    :param binding_map: _load_sensor_binding_map() 回傳的 sensor_id -> [已綁定的點位]
    :param keep_ids: 即使已被綁定也要保留在選單中的 sensor_id（通常是本畫面各列自己的綁定）
    :return: (options 清單, 被濾掉的數量)
    """
    keep = {int(s) for s in keep_ids if s is not None}
    options = [UNBOUND_LABEL]
    hidden = 0
    for label, sid in label_to_id.items():
        if sid in binding_map and sid not in keep:
            hidden += 1
            continue
        options.append(label)
    return options, hidden


def _binding_filter_caption(hidden_count):
    """選單過濾後的提示文字；沒有被濾掉任何項目時回傳 None（就不用顯示提示）。"""
    if not hidden_count:
        return None
    return (
        f"🔎 已隱藏 {hidden_count} 個「已被其他點位綁定」的感測器，選單只列出尚未綁定的。"
        "要把感測器改綁到別的點位，請先把原本那個點位改成「（未綁定）」並儲存。"
    )


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




# ------------------------------------------------------------------
# 稽核用：比對表格編輯前後的差異
# ------------------------------------------------------------------
def _is_null(x) -> bool:
    if x is None or x is pd.NA or x is pd.NaT:
        return True
    return isinstance(x, float) and x != x


def _same(a, b) -> bool:
    if _is_null(a) and _is_null(b):
        return True
    if _is_null(a) or _is_null(b):
        return False
    try:
        return bool(a == b) or str(a) == str(b)
    except Exception:
        return str(a) == str(b)


def frame_changes(before: pd.DataFrame, after: pd.DataFrame, id_col: str, limit: int = 200) -> dict:
    """
    回傳 {列 id: {欄位: [舊值, 新值]}}，只列出真的有改的欄位，寫進 audit_log。
    欄位名稱含 password 的一律遮蔽，不把密碼寫進稽核紀錄。
    """
    changes = {}
    try:
        b = before.set_index(id_col)
        a = after.set_index(id_col)
    except KeyError:
        return {"note": f"無法比對（缺少 {id_col} 欄位）"}
    common_cols = [c for c in a.columns if c in b.columns]
    for rid in a.index:
        if rid not in b.index:
            continue
        diffs = {}
        for col in common_cols:
            old, new = b.at[rid, col], a.at[rid, col]
            if _same(old, new):
                continue
            if "password" in str(col).lower():
                diffs[col] = ["***", "***（已變更）"]
            else:
                diffs[col] = [None if _is_null(old) else old, None if _is_null(new) else new]
        if diffs:
            changes[str(rid)] = diffs
            if len(changes) >= limit:
                changes["_truncated"] = True
                break
    return changes
