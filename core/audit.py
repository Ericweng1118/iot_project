"""
core/audit.py
=============
操作稽核紀錄：把「誰、什麼時候、做了什麼、改了哪個對象」寫進 audit_log（sql/012）。

設計原則：稽核寫入失敗絕對不能讓原本的操作失敗。表還沒建立（migration 沒跑）時
只會在 log 留一次警告，之後安靜略過。

用法：
    from core.audit import audit
    audit("alice", "sensor.update", "sensor:12", {"upload_condition": ["threshold_percent", "on_change"]})
"""

import json
import logging

logger = logging.getLogger(__name__)

_warned = False


def audit(username: str, action: str, target: str | None = None, detail=None) -> None:
    global _warned
    from data_layer.db_connector import DatabaseConnector

    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO audit_log (username, action, target, detail) "
                    "VALUES (%s, %s, %s, %s);",
                    (
                        username or "unknown",
                        action,
                        target,
                        json.dumps(detail, ensure_ascii=False, default=str)
                        if detail is not None else None,
                    ),
                )
    except Exception as e:
        if not _warned:
            logger.warning(f"⚠️ 寫入 audit_log 失敗（尚未執行 sql/012？），之後的失敗不再重複提示: {e}")
            _warned = True
