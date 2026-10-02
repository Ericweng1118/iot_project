"""
core/
=====
跨模組共用的基礎元件，採集主程式（main.py）與網頁後台（admin_app.py）都會用到：

    config.py  .env 讀取與型別轉換（容忍尾端 # 註解與空白）
    auth.py    使用者帳號、密碼雜湊（PBKDF2-SHA256）、角色權限判斷
    audit.py   操作稽核紀錄（誰在什麼時候改了什麼）

這一層不依賴 Streamlit，可以被背景服務與單元測試直接 import。
"""
