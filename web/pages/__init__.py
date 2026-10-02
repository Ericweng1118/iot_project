"""
web/pages/
==========
每個模組提供一個 render()，由 admin_app.py 用 st.navigation 組成選單。

監控：overview（即時總覽）、trends（歷史趨勢）、alarms（警報中心）、reports（報表匯出）
設定：config_opcua / config_modbus / config_tia（點位）、config_hierarchy（感測器階層）、
      alarm_rules（警報規則與通知）
系統：system（系統狀態）、users（使用者管理）、audit_log（稽核紀錄）
其他：diagnostics（v2「異常監控」，嵌在警報中心的「診斷檢查」分頁）
"""
