"""
core/audit.py
=============
操作稽核紀錄：把「誰、什麼時候、做了什麼、改了哪個對象」寫進 audit_log（sql/012）。

設計原則：
  * audit()：一般操作用。稽核寫入失敗絕對不能讓原本的操作失敗。表還沒建立（migration 沒跑）時
    只會在 log 留一次警告，之後安靜略過。
  * audit_in_tx()：會影響 PME 計算的設定變更用（Modbus / S7 點位、批次匯入）。與設定變更寫在
    同一個交易，稽核寫不進去就整筆變更一起回滾。PME 依 audit_log 偵測倍率 / data_type 變更並
    重新起算累計值，漏一筆就會算出假的用量暴增，而且不會有任何警告（見 docs/PME_INTERFACE.md）。

用法：
    from core.audit import audit, audit_in_tx
    audit("alice", "sensor.update", "sensor:12", {"upload_condition": ["threshold_percent", "on_change"]})

    with DatabaseConnector.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE modbus_scada ...")
            audit_in_tx(cur, "alice", "modbus.update", "modbus_scada", {...})
"""

import json
import logging

logger = logging.getLogger(__name__)

_warned = False

_INSERT_SQL = (
    "INSERT INTO audit_log (username, action, target, detail) VALUES (%s, %s, %s, %s);"
)


def _params(username: str, action: str, target: str | None, detail) -> tuple:
    return (
        username or "unknown",
        action,
        target,
        json.dumps(detail, ensure_ascii=False, default=str)
        if detail is not None
        else None,
    )


def audit(username: str, action: str, target: str | None = None, detail=None) -> bool:
    """回傳是否寫入成功；一般操作不用理會，需要確認送達的呼叫端（例如 spool.drained）才看。"""
    global _warned
    from data_layer.db_connector import DatabaseConnector

    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_INSERT_SQL, _params(username, action, target, detail))
        return True
    except Exception as e:
        if not _warned:
            logger.warning(
                f"⚠️ 寫入 audit_log 失敗（尚未執行 sql/012？），之後的失敗不再重複提示: {e}"
            )
            _warned = True
        return False


def audit_in_tx(
    cur, username: str, action: str, target: str | None = None, detail=None
) -> None:
    """用呼叫端的 cursor 寫入（不 commit）；失敗直接丟例外，讓呼叫端整個交易回滾。"""
    cur.execute(_INSERT_SQL, _params(username, action, target, detail))
