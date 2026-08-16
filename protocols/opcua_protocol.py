"""
protocols/opcua_protocol.py
============================
OPC UA 協議封裝層。

負責：
1. 依 opcua_servers 表的連線資訊建立連線（支援帳密與安全性原則）
2. 從指定的 root_node_id 開始遞迴瀏覽 Address Space，找出所有 Variable 節點
3. 讀取每個 Variable 節點的目前值、資料型態與品質狀態

使用 asyncua 函式庫（pip install asyncua）

🆕 連線逾時可調整：
    網路品質不穩定的場域（例如高壓變電室這類訊號常常不太乾淨的環境），
    預設 10 秒的請求逾時容易把「只是回應比較慢」誤判成「斷線」，
    造成頻繁的斷線重連與 asyncua 內部的重試 log 洗版。
    現在可以透過 .env 的 OPCUA_CLIENT_TIMEOUT（秒）全域調整，
    或在單一 Server 的設定裡加上 client_timeout 欄位個別覆寫（目前
    opcua_servers 表尚未有這個欄位，若未來要開放網頁調整，需另外
    補一個 migration 新增該欄位；程式已經預留好讀取邏輯）。
"""

import logging
import os
from typing import Optional

from asyncua import Client, ua

logger = logging.getLogger("opcua_protocol")


def _get_default_client_timeout() -> float:
    raw = os.getenv("OPCUA_CLIENT_TIMEOUT", "10").split("#")[0].strip()
    try:
        return float(raw)
    except ValueError:
        return 10.0


class OPCUAConnectionError(Exception):
    """連線或瀏覽過程中的錯誤"""
    pass


async def connect_client(server: dict) -> Client:
    """
    依 opcua_servers 資料表的一筆設定建立連線。

    server 需包含：ip, port, username(可選), password(可選),
                   security_policy(可選), security_mode(可選),
                   client_timeout(可選，秒，個別 Server 覆寫用；沒設定則用
                   .env 的 OPCUA_CLIENT_TIMEOUT，預設 10 秒)
    """
    url = f"opc.tcp://{server['ip']}:{server['port']}"
    timeout = server.get("client_timeout") or _get_default_client_timeout()
    client = Client(url=url, timeout=timeout)

    # 帳號密碼（匿名連線則不設定）
    if server.get("username"):
        client.set_user(server["username"])
        if server.get("password"):
            client.set_password(server["password"])

    # 安全性原則（預設 None，如需憑證加密連線需另外設定憑證檔案路徑）
    security_policy = server.get("security_policy") or "None"
    security_mode = server.get("security_mode") or "None"
    if security_policy != "None" and security_mode != "None":
        # 範例：Basic256Sha256,SignAndEncrypt,cert.pem,key.pem
        # 若有憑證需求，請依實際憑證路徑補上 set_security_string
        logger.warning(
            "Server %s 設定了安全性原則 %s/%s，"
            "但目前程式未提供憑證路徑，將以預設方式嘗試連線",
            server.get("server_name"), security_policy, security_mode,
        )

    try:
        await client.connect()
    except Exception as e:
        raise OPCUAConnectionError(f"連線失敗: {e}") from e

    return client


async def _read_variable_node(node) -> Optional[dict]:
    """讀取單一 Variable 節點的資料，失敗回傳 None"""
    try:
        browse_name = (await node.read_browse_name()).Name
        display_name = (await node.read_display_name()).Text
        data_value = await node.read_data_value()

        raw_variant = data_value.Value
        value = raw_variant.Value
        variant_type = raw_variant.VariantType.name if raw_variant.VariantType else None

        status = data_value.StatusCode
        quality = "GOOD" if status.is_good() else "BAD"

        return {
            "node_id": node.nodeid.to_string(),
            "browse_name": browse_name,
            "display_name": display_name,
            "data_type": variant_type,
            "value": value,
            "quality": quality,
        }
    except Exception as e:
        logger.debug("讀取節點 %s 失敗: %s", node, e)
        return None


async def browse_recursive(
    node,
    max_depth: int,
    current_depth: int = 0,
    results: Optional[list] = None,
    visited: Optional[set] = None,
) -> list:
    """
    從指定節點開始遞迴瀏覽，收集所有 Variable 節點的資料。

    max_depth 用來限制遞迴深度，避免過大的 Address Space 造成長時間卡住。
    visited 避免因為循環參照造成無限遞迴。
    """
    if results is None:
        results = []
    if visited is None:
        visited = set()

    node_key = node.nodeid.to_string()
    if node_key in visited:
        return results
    visited.add(node_key)

    if current_depth > max_depth:
        return results

    try:
        children = await node.get_children()
    except Exception as e:
        logger.debug("取得子節點失敗 %s: %s", node, e)
        return results

    for child in children:
        try:
            node_class = await child.read_node_class()
        except Exception as e:
            logger.debug("讀取節點類別失敗 %s: %s", child, e)
            continue

        if node_class == ua.NodeClass.Variable:
            tag = await _read_variable_node(child)
            if tag:
                results.append(tag)
        elif node_class in (ua.NodeClass.Object, ua.NodeClass.ObjectType):
            await browse_recursive(
                child, max_depth, current_depth + 1, results, visited
            )

    return results


async def scan_server(server: dict) -> list:
    """
    完整流程：連線 -> 從 root_node_id 開始遞迴瀏覽 -> 回傳所有點位資料 -> 斷線

    server 需包含 opcua_servers 表的欄位（ip, port, username, password,
    root_node_id, browse_depth ...）
    """
    client = await connect_client(server)
    try:
        root_node_id = server.get("root_node_id") or "i=85"
        max_depth = server.get("browse_depth") or 5

        root_node = client.get_node(root_node_id)
        tags = await browse_recursive(root_node, max_depth)
        return tags
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass