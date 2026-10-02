"""
services/alarm/
===============
警報管理子系統：

    rules.py     判斷邏輯（純函式，可單元測試）
    engine.py    AlarmEngine：main.py 內的背景執行緒，判斷並寫入 alarm_events
    notifier.py  Notifier：Webhook / Email / Telegram 通知

資料表見 sql/011_alarm_management.sql。
"""
