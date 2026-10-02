"""
web/pages/alarm_rules.py
========================
🔔 警報規則與通知設定（engineer 以上）

- 規則清單：直接在表格修改設定值 / 遲滯 / 延遲 / 等級 / 訊息 / 啟用，勾「刪除」後儲存即刪除
- 新增規則：選感測器 → 類型 → 設定值，下方即時顯示「什麼情況會觸發 / 恢復」的白話說明
- 隱含規則：sensors.min_threshold / max_threshold 會自動變成 L / H 警報（等級「中」），
  在「感測器階層管理」維護，這裡列出供對照
- 通知頻道：顯示 .env 目前啟用了哪些頻道，並可發送測試通知確認設定正確

規則變更最慢 15 秒（ALARM_RULE_REFRESH_INTERVAL）後由警報引擎套用，不需要重啟。
"""

import pandas as pd
import streamlit as st

from services.alarm.notifier import Notifier
from services.alarm.rules import ALARM_TYPE_LABELS, PRIORITY_LABELS, RULE_TYPES
from web.common import (
    audit_ui,
    current_user,
    execute,
    fetch_df,
    frame_changes,
    require_role,
    sensor_catalog,
    table_exists,
)

TYPE_OPTIONS = [f"{t}｜{ALARM_TYPE_LABELS[t]}" for t in RULE_TYPES]
PRIORITY_OPTIONS = [f"{p}｜{PRIORITY_LABELS[p]}" for p in (1, 2, 3, 4)]


def _type_code(option: str) -> str:
    return option.split("｜")[0]


def _priority_code(option: str) -> int:
    return int(option.split("｜")[0])


def describe_rule(alarm_type: str, setpoint: float, deadband: float, delay: int, unit: str = "") -> str:
    u = f" {unit}" if unit else ""
    delay_text = f"連續 {delay} 秒" if delay else "立即"
    if alarm_type in ("HH", "H"):
        clear = setpoint - deadband
        return (f"數值 **大於 {setpoint:g}{u}** 時，{delay_text}觸發；"
                f"降到 **{clear:g}{u} 以下（含）** 才恢復。")
    if alarm_type in ("LL", "L"):
        clear = setpoint + deadband
        return (f"數值 **小於 {setpoint:g}{u}** 時，{delay_text}觸發；"
                f"升到 **{clear:g}{u} 以上（含）** 才恢復。")
    if alarm_type == "EQ":
        return f"數值 **等於 {setpoint:g}** 時，{delay_text}觸發；不等於時恢復。"
    return f"數值 **不等於 {setpoint:g}** 時，{delay_text}觸發；等於時恢復。"


def _render_notify_settings():
    notifier = Notifier()
    desc = notifier.describe_channels()
    with st.expander("📣 通知頻道（設定在 .env）", expanded=not notifier.enabled):
        if not notifier.enabled:
            st.warning(
                "目前沒有啟用任何通知頻道，警報只會出現在網頁上。"
                "要推播到手機 / 信箱，請在 .env 設定下列其中一種後重新啟動服務："
            )
        rows = [
            ("Webhook（n8n / Slack / Discord / Teams…）", desc["webhook"], "ALARM_WEBHOOK_URLS"),
            ("Email", desc["email"], "SMTP_HOST、SMTP_PORT、SMTP_USER、SMTP_PASSWORD、ALARM_EMAIL_TO"),
            ("Telegram", desc["telegram"], "TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID"),
        ]
        st.dataframe(
            pd.DataFrame([{"頻道": n, "狀態": f"✅ {v}" if v else "未設定", "設定項目": k} for n, v, k in rows]),
            hide_index=True, width="stretch",
        )
        st.caption(
            f"通知等級：{desc['min_priority']}（ALARM_NOTIFY_MIN_PRIORITY）｜"
            f"恢復時通知：{'是' if desc['notify_on_clear'] else '否'}（ALARM_NOTIFY_ON_CLEAR）"
        )
        if notifier.enabled and st.button("🧪 發送測試通知"):
            with st.spinner("發送中…"):
                results = notifier.send_test(current_user()["username"])
            for name, ok, err in results:
                (st.success if ok else st.error)(f"{name}：{'成功' if ok else err}")
            audit_ui("alarm.notify_test", None, {"results": [(n, ok) for n, ok, _ in results]})


def _render_rules(catalog: pd.DataFrame):
    labels = dict(zip(catalog["sensor_id"], catalog["label"]))
    df = fetch_df(
        """
        SELECT rule_id, sensor_id, alarm_type, setpoint::float8 AS setpoint,
               deadband::float8 AS deadband, on_delay_sec, priority, message, enabled
        FROM alarm_rules ORDER BY sensor_id, alarm_type;
        """
    )
    st.subheader(f"📋 警報規則（{len(df)} 條）")
    if df.empty:
        st.info("目前沒有自訂警報規則，請用下方表單新增。")
        return

    keyword = st.text_input("篩選", placeholder="感測器 / 設備關鍵字", key="ar_kw")
    view = pd.DataFrame({
        "rule_id": df["rule_id"],
        "感測器": df["sensor_id"].map(lambda s: labels.get(s, f"sensor {s}")),
        "類型": df["alarm_type"].map(lambda t: f"{t}｜{ALARM_TYPE_LABELS.get(t, t)}"),
        "設定值": df["setpoint"],
        "遲滯": df["deadband"],
        "延遲(秒)": df["on_delay_sec"],
        "等級": df["priority"].map(lambda p: f"{int(p)}｜{PRIORITY_LABELS.get(int(p))}"),
        "訊息": df["message"],
        "啟用": df["enabled"],
        "刪除": False,
    })
    if keyword:
        view = view[view["感測器"].str.contains(keyword, case=False, regex=False)]

    edited = st.data_editor(
        view, hide_index=True, width="stretch", num_rows="fixed", key="alarm_rules_editor",
        disabled=["rule_id", "感測器"],
        column_config={
            "rule_id": st.column_config.NumberColumn("#", width="small"),
            "類型": st.column_config.SelectboxColumn(options=TYPE_OPTIONS, required=True),
            "設定值": st.column_config.NumberColumn(required=True),
            "遲滯": st.column_config.NumberColumn(min_value=0, help="恢復時要離開設定值多少，避免在門檻附近反覆跳動"),
            "延遲(秒)": st.column_config.NumberColumn(min_value=0, step=1, help="條件要連續成立這麼久才發出警報"),
            "等級": st.column_config.SelectboxColumn(options=PRIORITY_OPTIONS, required=True),
            "訊息": st.column_config.TextColumn(max_chars=200, help="附加在警報訊息後面，例如處置方式"),
            "刪除": st.column_config.CheckboxColumn(help="勾選後按「儲存」刪除"),
        },
    )
    if st.button("💾 儲存規則變更", type="primary"):
        to_delete = edited[edited["刪除"]]["rule_id"].astype(int).tolist()
        changes = frame_changes(view.drop(columns=["刪除"]), edited.drop(columns=["刪除"]), "rule_id")
        try:
            for rid in changes:
                if rid.startswith("_"):
                    continue
                row = edited[edited["rule_id"] == int(rid)].iloc[0]
                execute(
                    """
                    UPDATE alarm_rules SET alarm_type=%s, setpoint=%s, deadband=%s, on_delay_sec=%s,
                           priority=%s, message=%s, enabled=%s, updated_at=now()
                    WHERE rule_id=%s;
                    """,
                    (
                        _type_code(row["類型"]), float(row["設定值"]),
                        float(row["遲滯"]) if pd.notnull(row["遲滯"]) else 0.0,
                        int(row["延遲(秒)"]) if pd.notnull(row["延遲(秒)"]) else 0,
                        _priority_code(row["等級"]),
                        row["訊息"] if pd.notnull(row["訊息"]) and str(row["訊息"]).strip() else None,
                        bool(row["啟用"]), int(rid),
                    ),
                )
            if to_delete:
                execute("DELETE FROM alarm_rules WHERE rule_id = ANY(%s);", (to_delete,))
            audit_ui("alarm_rule.update", "alarm_rules", {"changes": changes, "deleted": to_delete})
            st.success(f"✅ 已更新 {len([c for c in changes if not c.startswith('_')])} 條、刪除 {len(to_delete)} 條規則，"
                       "警報引擎約 15 秒內套用。")
            st.rerun()
        except Exception as e:
            st.error(f"❌ 儲存失敗: {e}")


def _render_add_form(catalog: pd.DataFrame):
    st.subheader("➕ 新增警報規則")
    labels = dict(zip(catalog["sensor_id"], catalog["label"]))
    units = dict(zip(catalog["sensor_id"], catalog["unit"].fillna("")))

    c1, c2 = st.columns([2, 1])
    sensor_id = c1.selectbox("感測器", list(labels), format_func=labels.get, index=None,
                             placeholder="輸入關鍵字搜尋…", key="ar_new_sensor")
    type_opt = c2.selectbox("類型", TYPE_OPTIONS, index=1, key="ar_new_type")
    c3, c4, c5, c6 = st.columns(4)
    setpoint = c3.number_input("設定值", value=0.0, format="%g", key="ar_new_sp")
    deadband = c4.number_input("遲滯", min_value=0.0, value=0.0, format="%g", key="ar_new_db",
                               help="恢復時要離開設定值多少，避免在門檻附近反覆觸發")
    delay = c5.number_input("延遲觸發（秒）", min_value=0, value=0, step=5, key="ar_new_delay",
                            help="條件要連續成立這麼久才發出警報，濾掉瞬間突波")
    prio_opt = c6.selectbox("等級", PRIORITY_OPTIONS, index=1, key="ar_new_prio")
    message = st.text_input("附加訊息（選填）", max_chars=200, key="ar_new_msg",
                            placeholder="例如：請檢查冷卻水泵浦")

    alarm_type = _type_code(type_opt)
    st.info("📝 " + describe_rule(alarm_type, setpoint, deadband, int(delay),
                                  units.get(sensor_id, "") if sensor_id else ""))

    if sensor_id is not None:
        current = fetch_df(
            "SELECT value::float8 AS v, reading_time FROM sensor_readings "
            "WHERE sensor_id = %s ORDER BY reading_time DESC LIMIT 1;",
            (int(sensor_id),), show_error=False,
        )
        if not current.empty:
            st.caption(f"這個感測器最新一筆數值：{current.iloc[0]['v']:g} {units.get(sensor_id, '')}")

    if st.button("➕ 新增規則", type="primary", disabled=sensor_id is None):
        try:
            execute(
                """
                INSERT INTO alarm_rules (sensor_id, alarm_type, setpoint, deadband, on_delay_sec, priority, message)
                VALUES (%s, %s, %s, %s, %s, %s, %s);
                """,
                (int(sensor_id), alarm_type, float(setpoint), float(deadband), int(delay),
                 _priority_code(prio_opt), message.strip() or None),
            )
            audit_ui("alarm_rule.create", f"sensor:{int(sensor_id)}", {
                "alarm_type": alarm_type, "setpoint": setpoint, "deadband": deadband,
                "on_delay_sec": int(delay), "priority": _priority_code(prio_opt),
            })
            st.success("✅ 已新增，警報引擎約 15 秒內套用。")
            st.rerun()
        except Exception as e:
            st.error(f"❌ 新增失敗: {e}")


def _render_implicit(catalog: pd.DataFrame):
    limits = catalog[catalog["min_threshold"].notna() | catalog["max_threshold"].notna()]
    with st.expander(f"ℹ️ 由感測器上下限自動產生的警報（{len(limits)} 個感測器）"):
        st.caption(
            "感測器的 min_threshold / max_threshold 會自動視為 L / H 警報（等級「中」、無延遲、無遲滯），"
            "在「感測器階層管理」維護。需要延遲 / 遲滯 / 其他等級時，請改在上方建立自訂規則並清空上下限，"
            "避免同一個條件產生兩筆警報。.env 設 ALARM_USE_SENSOR_LIMITS=false 可停用這個行為。"
        )
        if not limits.empty:
            st.dataframe(
                limits[["label", "min_threshold", "max_threshold"]].rename(
                    columns={"label": "感測器", "min_threshold": "下限 (L)", "max_threshold": "上限 (H)"}),
                hide_index=True, width="stretch",
            )


def render():
    require_role("engineer")
    st.title("🔔 警報規則")
    if not table_exists("alarm_rules"):
        st.warning("尚未建立警報資料表，請用資料表擁有者執行 `sql/011_alarm_management.sql`。")
        return

    catalog = sensor_catalog()
    _render_notify_settings()
    if catalog.empty:
        st.info("目前沒有任何感測器，請先到「感測器階層管理」建立。")
        return
    _render_rules(catalog)
    st.divider()
    _render_add_form(catalog)
    _render_implicit(catalog)
