"""
web/pages/users.py
==================
👥 使用者管理（admin）

- 新增帳號：帳號 / 顯示名稱 / 角色 / 初始密碼（至少 8 碼、需含英文與數字）
- 表格直接改顯示名稱、角色、啟用狀態
- 重設密碼、刪除帳號
- 不能停用 / 降級 / 刪除「目前登入的自己」，避免把自己鎖在外面

.env 的 ADMIN_USER 是救援帳號，不在這張表裡、也無法在這裡修改。
"""

import streamlit as st

from core.auth import ROLE_LABELS, ROLES, hash_password, is_env_admin, validate_new_password
from web.common import audit_ui, current_user, execute, fetch_df, frame_changes, require_role, table_exists

ROLE_DESCRIPTIONS = {
    "viewer": "檢視總覽、趨勢、報表、警報、系統狀態",
    "operator": "＋ 確認警報",
    "engineer": "＋ 點位、感測器階層、警報規則等所有設定",
    "admin": "＋ 使用者管理、稽核紀錄",
}


def render():
    require_role("admin")
    st.title("👥 使用者管理")
    if not table_exists("app_users"):
        st.warning("尚未建立使用者資料表，請用資料表擁有者執行 `sql/012_users_audit_status.sql`。")
        return

    me = current_user()["username"]
    st.caption(
        "角色權限（由低到高，高階包含低階全部權限）："
        + "｜".join(f"**{ROLE_LABELS[r]}** {ROLE_DESCRIPTIONS[r]}" for r in ROLES)
    )
    st.caption("`.env` 的 ADMIN_USER 永遠可以用管理員身分登入（救援帳號），不顯示在下表。")

    df = fetch_df(
        "SELECT username, display_name, role, enabled, created_at, last_login "
        "FROM app_users ORDER BY username;"
    )
    if not df.empty:
        st.subheader(f"帳號清單（{len(df)}）")
        edited = st.data_editor(
            df, hide_index=True, width="stretch", num_rows="fixed", key="users_editor",
            disabled=["username", "created_at", "last_login"],
            column_config={
                "username": "帳號",
                "display_name": "顯示名稱",
                "role": st.column_config.SelectboxColumn("角色", options=list(ROLES), required=True),
                "enabled": "啟用",
                "created_at": st.column_config.DatetimeColumn("建立時間", format="YYYY-MM-DD HH:mm"),
                "last_login": st.column_config.DatetimeColumn("最後登入", format="YYYY-MM-DD HH:mm"),
            },
        )
        if st.button("💾 儲存修改"):
            changes = frame_changes(df, edited, "username")
            if me in changes and ("role" in changes[me] or "enabled" in changes[me]):
                st.error("不能修改自己的角色或停用自己，請由其他管理員操作。")
            else:
                for username in changes:
                    row = edited[edited["username"] == username].iloc[0]
                    execute(
                        "UPDATE app_users SET display_name=%s, role=%s, enabled=%s WHERE username=%s;",
                        (row["display_name"] or None, row["role"], bool(row["enabled"]), username),
                    )
                audit_ui("user.update", "app_users", changes)
                st.success(f"✅ 已更新 {len(changes)} 個帳號")
                st.rerun()

        c1, c2 = st.columns(2)
        with c1.form("reset_pw", clear_on_submit=True):
            st.markdown("**🔑 重設密碼**")
            target = st.selectbox("帳號", df["username"].tolist(), key="reset_user")
            pw = st.text_input("新密碼", type="password")
            if st.form_submit_button("重設"):
                err = validate_new_password(pw)
                if err:
                    st.error(err)
                else:
                    execute("UPDATE app_users SET password_hash=%s WHERE username=%s;", (hash_password(pw), target))
                    audit_ui("user.reset_password", f"user:{target}")
                    st.success(f"✅ 已重設 {target} 的密碼")
        with c2.form("delete_user", clear_on_submit=True):
            st.markdown("**🗑️ 刪除帳號**")
            target_del = st.selectbox("帳號", [u for u in df["username"] if u != me], key="del_user")
            confirm = st.text_input("輸入帳號名稱確認刪除")
            if st.form_submit_button("刪除", type="secondary"):
                if not target_del or confirm != target_del:
                    st.error("確認文字與帳號不符")
                else:
                    execute("DELETE FROM app_users WHERE username=%s;", (target_del,))
                    audit_ui("user.delete", f"user:{target_del}")
                    st.success(f"✅ 已刪除 {target_del}")
                    st.rerun()

    st.subheader("➕ 新增帳號")
    with st.form("add_user", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        username = c1.text_input("帳號（英數字）")
        display_name = c2.text_input("顯示名稱")
        role = c3.selectbox("角色", list(ROLES), format_func=lambda r: f"{ROLE_LABELS[r]}（{r}）")
        c4, c5 = st.columns(2)
        pw1 = c4.text_input("初始密碼", type="password")
        pw2 = c5.text_input("再輸入一次", type="password")
        if st.form_submit_button("新增", type="primary"):
            username = username.strip()
            err = None
            if not username or not username.replace("_", "").replace(".", "").replace("-", "").isalnum():
                err = "帳號只能使用英文字母、數字與 _ . -"
            elif is_env_admin(username):
                err = "這個帳號名稱與 .env 的 ADMIN_USER 相同，請換一個"
            elif pw1 != pw2:
                err = "兩次輸入的密碼不一致"
            else:
                err = validate_new_password(pw1)
            if err:
                st.error(err)
            else:
                try:
                    execute(
                        "INSERT INTO app_users (username, display_name, password_hash, role) VALUES (%s, %s, %s, %s);",
                        (username, display_name.strip() or None, hash_password(pw1), role),
                    )
                    audit_ui("user.create", f"user:{username}", {"role": role})
                    st.success(f"✅ 已建立帳號 {username}（{ROLE_LABELS[role]}）")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 建立失敗（帳號可能已存在）: {e}")

    if df.empty:
        st.info("目前還沒有任何個人帳號。建議為每位使用者建立自己的帳號，稽核紀錄才分得出是誰操作的。")
