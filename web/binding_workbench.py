"""
web/binding_workbench.py
========================
點位 ↔ 感測器「綁定工作台」，取代在表格儲存格裡逐列下拉選擇的做法。

為什麼不用 st.data_editor 的 SelectboxColumn：
    SelectboxColumn 整欄共用一份 options，沒辦法讓每一列排除「其他列已選的感測器」；
    而且所有修改要等按儲存才一次寫入，同一次編輯裡兩列選了同一個感測器時無從阻擋。

工作台的做法（參考 Ignition / Kepware 等 SCADA 工具「瀏覽 → 選取 → 指派」的流程）：
    🔗 綁定      左邊只列未綁定點位，右邊只列未綁定感測器，選一對按下就立即寫入
    ⛓️ 已綁定    勾選後可解除（多選）、改綁到其他感測器（1 筆）、互換感測器（2 筆）
    📋 全部點位  唯讀總覽；大量配對請用「匯入匯出」頁面的 CSV

每個動作都是一次獨立交易（data_layer/bindings.py），沒有「未儲存的編輯狀態」，
選單永遠是資料庫的最新狀態，所以不會出現重複綁定。
"""

from dataclasses import dataclass, field

import pandas as pd
import streamlit as st

from data_layer.bindings import BindingError, bind, swap, unbind
from data_layer.db_connector import DatabaseConnector
from web.common import _load_sensor_binding_map, _load_sensor_options, audit_ui


@dataclass(frozen=True)
class PointSpec:
    """描述一張點位表要怎麼顯示在工作台上。"""
    table: str                 # 點位表名（data_layer.bindings.POINT_TABLES 的 key）
    audit_action: str          # 稽核紀錄的 action，例如 "opcua_tag.bind"
    name_col: str              # 訊息裡用來稱呼點位的欄位
    columns: list = field(default_factory=list)       # 表格要顯示的欄位（id 一定要在裡面）
    search_cols: list = field(default_factory=list)   # 關鍵字搜尋比對的欄位


SENSOR_COL = "綁定感測器"


def _filter(df: pd.DataFrame, cols, text: str) -> pd.DataFrame:
    """空白分隔的多個關鍵字都要命中（不分大小寫，任一欄位命中即可）。"""
    terms = [t for t in str(text or "").lower().split() if t]
    if not terms or df.empty:
        return df
    hay = df[list(cols)].astype(str).agg(" ".join, axis=1).str.lower()
    mask = pd.Series(True, index=df.index)
    for t in terms:
        mask &= hay.str.contains(t, regex=False)
    return df[mask]


def _sid(v):
    return None if pd.isna(v) else int(v)


def _execute(spec: PointSpec, key: str, op, flash: str, detail: dict) -> None:
    """在單一交易中執行綁定操作；成功就寫稽核、留下提示訊息並重新整理畫面。"""
    try:
        with DatabaseConnector.get_connection() as conn:
            with conn.cursor() as cur:
                op(cur)
    except BindingError as e:
        st.error(f"❌ {e}")
        return
    except Exception as e:
        st.error(f"❌ 寫入失敗：{e}")
        return
    audit_ui(spec.audit_action, spec.table, detail)
    st.session_state[f"{key}_flash"] = flash
    # 換掉表格與選單的 key，清空上一次的選取（資料列已經變了，舊的列號沒有意義）
    st.session_state[f"{key}_nonce"] = st.session_state.get(f"{key}_nonce", 0) + 1
    st.rerun()


@st.dialog("確認解除綁定")
def _confirm_unbind(spec: PointSpec, key: str, rows: pd.DataFrame) -> None:
    st.write(f"即將解除以下 **{len(rows)}** 個點位的綁定：")
    st.dataframe(rows[[spec.name_col, SENSOR_COL]], hide_index=True, width="stretch")
    st.warning("解除後這些點位不再被訂閱，對應的感測器也會停止寫入新資料（歷史資料保留）。")
    c1, c2 = st.columns(2)
    if c1.button("確認解除", type="primary", width="stretch"):
        expected = {int(r["id"]): _sid(r["sensor_id"]) for _, r in rows.iterrows()}
        _execute(
            spec, key,
            lambda cur: unbind(cur, spec.table, expected),
            f"⛓️‍💥 已解除 {len(rows)} 個點位的綁定",
            {"op": "unbind", "points": {str(k): v for k, v in expected.items()}},
        )
    if c2.button("取消", width="stretch"):
        st.rerun()


def render_binding_workbench(spec: PointSpec, points: pd.DataFrame, key: str) -> None:
    """
    :param points: 這次要顯示的點位，至少要有 id、sensor_id 與 spec.columns 裡的欄位
    :param key: 這個工作台在頁面上的唯一 key（同頁有多個工作台時用來區分）
    """
    label_to_id, id_to_label = _load_sensor_options()
    used = set(_load_sensor_binding_map())
    free_sensors = [label for label, sid in label_to_id.items() if sid not in used]
    nonce = st.session_state.get(f"{key}_nonce", 0)

    flash = st.session_state.pop(f"{key}_flash", None)
    if flash:
        st.success(flash)

    pts = points.copy()
    pts[SENSOR_COL] = pts["sensor_id"].map(
        lambda s: None if pd.isna(s) else id_to_label.get(int(s), f"sensor_id={int(s)}")
    )
    unbound = pts[pts["sensor_id"].isna()]
    bound = pts[pts["sensor_id"].notna()]

    tab_bind, tab_bound, tab_all = st.tabs([
        f"🔗 綁定（未綁定點位 {len(unbound)}）",
        f"⛓️ 已綁定 {len(bound)}（解除 / 改綁）",
        f"📋 全部點位 {len(pts)}",
    ])

    # ------------------------------------------------------------------
    # 🔗 綁定：未綁定點位 × 未綁定感測器
    # ------------------------------------------------------------------
    with tab_bind:
        left, right = st.columns([3, 2], gap="medium")
        with left:
            q = st.text_input(
                "搜尋未綁定點位", key=f"{key}_q_unbound",
                placeholder="輸入關鍵字，多個關鍵字用空白分隔，例如：Ba7 KWH",
            )
            view = _filter(unbound, spec.search_cols, q)
            st.caption(f"符合 {len(view)} 筆，點選一列來選取點位")
            ev = st.dataframe(
                view[spec.columns], hide_index=True, width="stretch", height=460,
                on_select="rerun", selection_mode="single-row",
                key=f"{key}_ub_{nonce}_{q}",
            )
            point = view.iloc[ev.selection.rows[0]] if ev.selection.rows else None

        with right:
            st.markdown("**① 左邊點選點位　② 選感測器　③ 按綁定**")
            if point is None:
                st.info("👈 先在左邊表格點選一個點位")
            else:
                with st.container(border=True):
                    st.markdown(f"**點位**：`{point[spec.name_col]}`")
                    extra = [c for c in spec.columns if c not in ("id", spec.name_col)][:3]
                    st.caption("　".join(f"{c}: {point[c]}" for c in extra))
            sensor_label = st.selectbox(
                f"感測器（只列出尚未綁定的 {len(free_sensors)} 個）",
                free_sensors, index=None, key=f"{key}_free_{nonce}",
                placeholder="點一下後直接打字搜尋",
                help="已經被任何點位或計算點使用的感測器不會出現在這裡。"
                "要改用某個已綁定的感測器，請先到「已綁定」分頁解除它原本的綁定。",
            )
            if not free_sensors:
                st.caption("所有感測器都已綁定；需要新的感測器請到「感測器階層管理」建立。")
            if st.button(
                "🔗 綁定", type="primary", width="stretch", key=f"{key}_bind_btn",
                disabled=point is None or sensor_label is None,
            ):
                pid, sid = int(point["id"]), label_to_id[sensor_label]
                _execute(
                    spec, key,
                    lambda cur: bind(cur, spec.table, pid, sid, expected=None),
                    f"🔗 已綁定：{point[spec.name_col]} → {sensor_label}",
                    {"op": "bind", "point": pid, "sensor_id": sid},
                )

    # ------------------------------------------------------------------
    # ⛓️ 已綁定：解除 / 改綁 / 互換
    # ------------------------------------------------------------------
    with tab_bound:
        q2 = st.text_input(
            "搜尋已綁定點位", key=f"{key}_q_bound",
            placeholder="可搜尋點位名稱或感測器編號 / 暱稱",
        )
        view = _filter(bound, list(spec.search_cols) + [SENSOR_COL], q2)
        st.caption(f"符合 {len(view)} 筆，勾選最左邊的方框選取（可多選）")
        ev = st.dataframe(
            view[[SENSOR_COL] + list(spec.columns)], hide_index=True, width="stretch", height=420,
            on_select="rerun", selection_mode="multi-row",
            key=f"{key}_b_{nonce}_{q2}",
        )
        sel = view.iloc[ev.selection.rows]

        if sel.empty:
            st.caption("選 1 筆以上可解除綁定；選 1 筆可改綁到其他感測器；選 2 筆可互換感測器。")
        else:
            c1, c2, c3 = st.columns(3, gap="medium")
            with c1:
                st.markdown("**解除綁定**")
                if st.button(f"⛓️‍💥 解除所選 {len(sel)} 筆", width="stretch", key=f"{key}_unbind_btn"):
                    _confirm_unbind(spec, key, sel)
            with c2:
                st.markdown("**改綁到其他感測器**")
                if len(sel) != 1:
                    st.caption("只選 1 筆時可用")
                else:
                    row = sel.iloc[0]
                    new_label = st.selectbox(
                        "新的感測器", free_sensors, index=None, key=f"{key}_rebind_{nonce}",
                        placeholder="打字搜尋未綁定的感測器", label_visibility="collapsed",
                    )
                    if st.button("🔁 改綁", width="stretch", key=f"{key}_rebind_btn",
                                 disabled=new_label is None):
                        pid, old, sid = int(row["id"]), _sid(row["sensor_id"]), label_to_id[new_label]
                        _execute(
                            spec, key,
                            lambda cur: bind(cur, spec.table, pid, sid, expected=old),
                            f"🔁 已改綁：{row[spec.name_col]}：{row[SENSOR_COL]} → {new_label}",
                            {"op": "rebind", "point": pid, "from": old, "to": sid},
                        )
            with c3:
                st.markdown("**互換感測器**")
                if len(sel) != 2:
                    st.caption("選剛好 2 筆時可用（例如兩個點位綁反了）")
                else:
                    a, b = sel.iloc[0], sel.iloc[1]
                    st.caption(f"{a[spec.name_col]} ⇄ {b[spec.name_col]}")
                    if st.button("⇄ 互換", width="stretch", key=f"{key}_swap_btn"):
                        pa, pb = int(a["id"]), int(b["id"])
                        sa, sb = _sid(a["sensor_id"]), _sid(b["sensor_id"])
                        _execute(
                            spec, key,
                            lambda cur: swap(cur, spec.table, pa, pb, expected_a=sa, expected_b=sb),
                            f"⇄ 已互換：{a[spec.name_col]} 與 {b[spec.name_col]} 的感測器",
                            {"op": "swap", "points": [pa, pb], "sensors": [sa, sb]},
                        )

    # ------------------------------------------------------------------
    # 📋 全部點位（唯讀）
    # ------------------------------------------------------------------
    with tab_all:
        q3 = st.text_input("搜尋", key=f"{key}_q_all", placeholder="點位名稱或感測器")
        view = _filter(pts, list(spec.search_cols) + [SENSOR_COL], q3)
        st.caption(
            f"符合 {len(view)} 筆。這裡是唯讀總覽；"
            "要一次配對大量點位，請到「匯入匯出」頁面匯出 CSV、在 Excel 填好 sensor_code 再匯入。"
        )
        st.dataframe(
            view[list(spec.columns) + [SENSOR_COL]], hide_index=True, width="stretch", height=460,
        )
