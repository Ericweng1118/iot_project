"""
web/auth_ui.py
==============
登入畫面、登出、變更自己的密碼。

- 連續 5 次登入失敗後鎖定 60 秒（以瀏覽器 session 計算），擋掉最基本的暴力嘗試
- 登入成功 / 失敗都寫進 audit_log
- 🆕 閒置自動登出：超過 ADMIN_IDLE_TIMEOUT_MIN 分鐘（預設 30）沒有操作就登出。
  ADMIN_IDLE_TIMEOUT_EXEMPT_ROLES（預設 viewer）列出的角色不受限 —— 控制室的監看螢幕
  請用檢視者帳號登入，就不會被登出；檢視者不能改任何設定，放著不登出也沒有風險。
  「操作」指的是會讓整頁重跑的互動（點選單、按按鈕、改篩選條件），畫面的自動更新不算。
"""

import time

import streamlit as st

from core.audit import audit
from core.auth import ROLE_LABELS, authenticate, hash_password, validate_new_password, verify_password
from core.config import env_float, env_list
from web.common import current_user, execute, fetch_df

MAX_FAILURES = 5
LOCKOUT_SECONDS = 60
IDLE_TIMEOUT_SECONDS = env_float("ADMIN_IDLE_TIMEOUT_MIN", 30.0) * 60
IDLE_EXEMPT_ROLES = set(env_list("ADMIN_IDLE_TIMEOUT_EXEMPT_ROLES", "viewer"))


def _idle_applies(user: dict) -> bool:
    return IDLE_TIMEOUT_SECONDS > 0 and user.get("role") not in IDLE_EXEMPT_ROLES


def idle_expired() -> bool:
    """給自動更新的 fragment 用：已經閒置超時就回傳 True（由呼叫端觸發整頁重跑）。"""
    user = st.session_state.get("user")
    if not user or not _idle_applies(user):
        return False
    return time.time() - st.session_state.get("last_activity", time.time()) > IDLE_TIMEOUT_SECONDS


def enforce_idle_timeout(user: dict) -> None:
    """每次整頁執行時呼叫：超時就登出，否則把這次互動記為最後活動時間。"""
    if not _idle_applies(user):
        return
    now = time.time()
    if now - st.session_state.get("last_activity", now) > IDLE_TIMEOUT_SECONDS:
        audit(user["username"], "logout.idle")
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.session_state["logout_reason"] = (
            f"閒置超過 {IDLE_TIMEOUT_SECONDS / 60:.0f} 分鐘，已自動登出，請重新登入。"
        )
        st.rerun()
    st.session_state["last_activity"] = now


def idle_caption(user: dict) -> str | None:
    if not _idle_applies(user):
        return None
    return f"⏱️ 閒置 {IDLE_TIMEOUT_SECONDS / 60:.0f} 分鐘自動登出"


def require_login() -> dict:
    """已登入回傳使用者 dict；未登入時顯示登入表單並停止渲染。"""
    user = st.session_state.get("user")
    if user:
        return user

    _, center, _ = st.columns([1, 1.4, 1])
    with center:
        st.markdown("<div style='height:8vh'></div>", unsafe_allow_html=True)
        st.title("🏭 IIoT SCADA")
        st.caption("監控・趨勢・警報・設定")

        if st.session_state.get("logout_reason"):
            st.info(st.session_state["logout_reason"])

        locked_until = st.session_state.get("login_locked_until", 0)
        remaining = locked_until - time.time()
        if remaining > 0:
            st.error(f"🔒 登入失敗次數過多，請 {remaining:.0f} 秒後再試。")
            st.stop()

        with st.form("login_form"):
            username = st.text_input("帳號", autocomplete="username")
            password = st.text_input("密碼", type="password", autocomplete="current-password")
            submitted = st.form_submit_button("登入", type="primary", width="stretch")

        if submitted:
            user = authenticate(username, password)
            if user:
                st.session_state["user"] = user
                st.session_state["login_failures"] = 0
                st.session_state["last_activity"] = time.time()
                st.session_state.pop("logout_reason", None)
                audit(user["username"], "login", None, {"source": user["source"]})
                st.rerun()
            failures = st.session_state.get("login_failures", 0) + 1
            st.session_state["login_failures"] = failures
            audit(username or "(空白)", "login.failed", None, {"attempt": failures})
            if failures >= MAX_FAILURES:
                st.session_state["login_locked_until"] = time.time() + LOCKOUT_SECONDS
                st.session_state["login_failures"] = 0
                st.error(f"🔒 連續 {MAX_FAILURES} 次登入失敗，鎖定 {LOCKOUT_SECONDS} 秒。")
            else:
                st.error(f"❌ 帳號或密碼錯誤（{failures}/{MAX_FAILURES}）")
    st.stop()


def login_placeholder():
    """未登入時隱藏選單用的空白頁（見 admin_app.py），實際畫面是 require_login() 的登入表單。"""


def logout():
    user = current_user()
    audit(user["username"], "logout")
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.rerun()


@st.dialog("🔑 變更密碼")
def change_password_dialog():
    user = current_user()
    if user.get("source") != "db":
        st.info(
            "目前登入的是 .env 設定的救援帳號（ADMIN_USER），密碼只能在 .env 修改後重新啟動。"
            "建議到「使用者管理」建立個人帳號日常使用。"
        )
        return
    with st.form("change_password_form"):
        old = st.text_input("目前密碼", type="password")
        new = st.text_input("新密碼（至少 8 碼，需含英文與數字）", type="password")
        confirm = st.text_input("再輸入一次新密碼", type="password")
        ok = st.form_submit_button("變更", type="primary")
    if not ok:
        return
    df = fetch_df("SELECT password_hash FROM app_users WHERE username = %s;", (user["username"],))
    if df.empty or not verify_password(old, df.iloc[0]["password_hash"]):
        st.error("目前密碼不正確")
        return
    if new != confirm:
        st.error("兩次輸入的新密碼不一致")
        return
    err = validate_new_password(new)
    if err:
        st.error(err)
        return
    execute("UPDATE app_users SET password_hash = %s WHERE username = %s;",
            (hash_password(new), user["username"]))
    audit(user["username"], "user.change_password", f"user:{user['username']}")
    st.success("✅ 密碼已變更")


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role)
