"""
web/nav.py
==========
頁面登錄表：admin_app.py 建立 st.Page 之後登記在這裡，其他頁面要互相連結時
（例如總覽 → 趨勢）用 st.page_link / st.switch_page 搭配 query_params 切換，
不會整頁重新載入、也就不會掉登入狀態。

不要用 HTML <a href> 連到站內頁面：那會開一個新的 Streamlit session，使用者得重新登入。
"""

PAGES: dict = {}


def get(key: str):
    """回傳已登記的 StreamlitPage；不存在（例如權限不足沒有建立）時回傳 None。"""
    return PAGES.get(key)
