"""
web/pages/config_opcua.py
=========================
OPC UA Server 與點位設定（v2 admin_app.py 的「📡 OPC UA 點位設定」分頁，原封不動搬過來）。
需要 engineer 以上權限。
"""


import pandas as pd
import streamlit as st

from data_layer.batch_updater import batch_update_opcua_tags
from data_layer.db_connector import DatabaseConnector
from web.binding_workbench import PointSpec, render_binding_workbench
from web.common import (
    audit_ui,
    frame_changes,
    request_opcua_resubscribe,
    require_role,
    run_async,
)

try:
    from protocols.opcua_protocol import scan_server, OPCUAConnectionError

    OPCUA_AVAILABLE = True
except ImportError:
    OPCUA_AVAILABLE = False


def _fmt_current(v):
    """current_data 是 {'val': ...} 的 JSON，表格只顯示數值本身。"""
    if isinstance(v, dict) and "val" in v:
        return str(v["val"])
    return "" if v is None else str(v)


def render():
    require_role("engineer")
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
        """
        讀取 Server 清單。publish_interval_ms 由 sql/009 建立，若該 migration
        還沒跑，退回不含此欄位的查詢，只是少一個可編輯欄位，不讓整個
        OPC UA 分頁因為缺一個選填欄位就打不開。
        """
        base = """id, server_name, ip, port, username, password,
                  security_policy, security_mode, root_node_id, browse_depth"""
        tail = "enabled, conn_state, last_scan, last_error"

        def _run(cols):
            with DatabaseConnector.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT {cols} FROM opcua_servers ORDER BY id ASC;"
                    )
                    names = [desc[0] for desc in cur.description]
                    return pd.DataFrame(cur.fetchall(), columns=names)

        try:
            return _run(f"{base}, publish_interval_ms, {tail}")
        except Exception:
            pass

        try:
            df = _run(f"{base}, {tail}")
            st.warning(
                "⚠️ 尚未執行 `sql/009_opcua_server_publish_interval.sql`，"
                "「逐 Server 預設取樣頻率」欄位暫時無法使用，"
                "所有 Server 一律沿用 `.env` 的 `OPCUA_PUBLISH_INTERVAL_MS`。"
            )
            return df
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
    st.caption(
        "此表格為**編輯既有資料**用：新增請使用下方的專用表單。"
        "刪除目前沒有提供網頁入口，需要時請直接在資料庫執行 DELETE。"
    )
    if not df_opcua_servers.empty:
        disabled_cols_opcua = ["id", "conn_state", "last_scan", "last_error"]

        st.caption(
            "「publish_interval_ms」是這台 Server 的預設訂閱取樣頻率（毫秒），"
            "留空代表沿用 .env 的 OPCUA_PUBLISH_INTERVAL_MS。"
            "個別點位若在「感測器階層管理」設了 opcua_sampling_interval_ms，"
            "該點位以感測器的設定為準（優先序：感測器 > Server > .env）。"
        )

        edited_opcua_df = st.data_editor(
            df_opcua_servers,
            # ⚠️ 這裡刻意用 "fixed" 而非 "dynamic"：儲存邏輯只會對既有列做 UPDATE，
            #    表格上新增的列（id 為空）會被略過、刪掉的列也不會真的從資料庫移除。
            #    開著 dynamic 會讓使用者以為新增/刪除成功（還會跳「儲存成功」），
            #    實際上什麼都沒發生。新增請用下方的專用表單。
            num_rows="fixed",
            key="opcua_editor",
            disabled=disabled_cols_opcua,
            column_config={
                "publish_interval_ms": st.column_config.NumberColumn(
                    "預設取樣頻率 (publish_interval_ms)",
                    min_value=50,
                    max_value=3600000,
                    step=100,
                    help="毫秒。留空 = 沿用 .env 的 OPCUA_PUBLISH_INTERVAL_MS。"
                    "部分設備有自己固定的內部更新週期，會忽略這個請求值。",
                ),
            },
            width="stretch",
        )

        if st.button("💾 儲存 OPC UA Server 修改", type="primary"):
            try:
                with DatabaseConnector.get_connection() as conn:
                    with conn.cursor() as cur:
                        for index, row in edited_opcua_df.iterrows():
                            if pd.notnull(row["id"]):
                                # sql/009 還沒跑時欄位不存在，這裡跟著跳過，
                                # 其餘參數照常儲存
                                has_interval = "publish_interval_ms" in edited_opcua_df.columns
                                interval_set = (
                                    "publish_interval_ms=%s," if has_interval else ""
                                )
                                sql = f"""
                                UPDATE opcua_servers SET
                                    server_name=%s, ip=%s, port=%s, username=%s, password=%s,
                                    security_policy=%s, security_mode=%s, root_node_id=%s,
                                    browse_depth=%s, {interval_set} enabled=%s
                                WHERE id=%s;
                                """
                                params = [
                                    row["server_name"],
                                    row["ip"],
                                    int(row["port"]),
                                    row["username"] if pd.notnull(row["username"]) else None,
                                    row["password"] if pd.notnull(row["password"]) else None,
                                    row["security_policy"],
                                    row["security_mode"],
                                    row["root_node_id"],
                                    int(row["browse_depth"]),
                                ]
                                if has_interval:
                                    # 留空 = NULL = 沿用 .env 全域值（不是 0，0 會被
                                    # CHECK constraint 擋下，語意也與「未指定」不同）
                                    params.append(
                                        int(row["publish_interval_ms"])
                                        if pd.notnull(row["publish_interval_ms"])
                                        else None
                                    )
                                params += [bool(row["enabled"]), int(row["id"])]
                                cur.execute(sql, tuple(params))
                        conn.commit()
                audit_ui("opcua_server.update", "opcua_servers", frame_changes(df_opcua_servers, edited_opcua_df, "id"))
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
                audit_ui("opcua_server.create", f"opcua_server:{o_server_name}")
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

                    audit_ui("opcua_server.browse", f"opcua_server:{int(selected_id)}", {"tags": len(tags)})
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
    st.caption(
        "🆕 訂閱服務只會對「已綁定感測器」的點位持續訂閱更新數值，節省頻寬。"
        "尚未綁定的點位下面仍看得到（供你挑選要綁哪一個），但 current_data 只會停留在"
        "上次瀏覽當下的快照，不會即時變動；綁定後最慢 5 秒內會自動開始持續更新，"
        "或按上方「🚀 立即瀏覽並寫入資料庫」可以手動重新抓一次最新快照。"
    )

    filter_label = "全部 Server"
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
        df_opcua_tags["current_data"] = df_opcua_tags["current_data"].map(_fmt_current)
        columns = ["id", "browse_name", "display_name", "data_type", "current_data", "quality", "last_update"]
        if filter_label == "全部 Server":
            columns.insert(1, "server_name")
        render_binding_workbench(
            PointSpec(
                table="opcua_tags",
                audit_action="opcua_tag.bind",
                name_col="browse_name",
                columns=columns,
                search_cols=["server_name", "node_id", "browse_name", "display_name"],
            ),
            df_opcua_tags,
            key="opcua_bind",
        )
    else:
        st.info("目前尚無任何 OPC UA 點位資料，請先新增 Server 並執行瀏覽。")
