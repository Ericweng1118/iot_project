"""警報判斷邏輯（services/alarm/rules.py）。"""

import pytest

from services.alarm.rules import (
    AlarmRule,
    OnDelayTracker,
    build_message,
    condition_active,
    format_value,
    implicit_limit_rules,
)


@pytest.mark.parametrize("value,active,expected", [
    (100.0, False, False),   # 等於設定值不觸發（嚴格大於，與舊版異常監控一致）
    (100.1, False, True),
    (99.0, True, True),      # 已觸發、仍在遲滯帶內 → 維持
    (98.0, True, False),     # 降到 setpoint - deadband（含）→ 恢復
    (97.9, True, False),
])
def test_high_with_deadband(value, active, expected):
    assert condition_active("H", 100.0, 2.0, value, active) is expected


@pytest.mark.parametrize("value,active,expected", [
    (10.0, False, False),
    (9.9, False, True),
    (11.0, True, True),
    (12.0, True, False),
])
def test_low_with_deadband(value, active, expected):
    assert condition_active("L", 10.0, 2.0, value, active) is expected


def test_eq_ne():
    assert condition_active("EQ", 8, 0, 8.0, False)
    assert not condition_active("EQ", 8, 0, 7.0, False)
    assert condition_active("NE", 1, 0, 0.0, False)
    assert not condition_active("NE", 1, 0, 1.0, False)


def test_unknown_type_raises():
    with pytest.raises(ValueError):
        condition_active("XX", 1, 0, 1, False)


def test_on_delay():
    d = OnDelayTracker()
    assert not d.update("k", True, 10, now=0)
    assert not d.update("k", True, 10, now=9)
    assert d.update("k", True, 10, now=10)
    # 中斷一次就重新計時
    assert not d.update("k", False, 10, now=11)
    assert not d.update("k", True, 10, now=12)
    assert d.update("k", True, 10, now=22)


def test_on_delay_zero_is_immediate():
    assert OnDelayTracker().update("k", True, 0, now=5)


def test_implicit_limit_rules():
    rules = implicit_limit_rules(5, min_threshold=1, max_threshold=9, priority=3)
    assert {(r.key, r.alarm_type, r.setpoint) for r in rules} == {
        ("limit:5:H", "H", 9.0), ("limit:5:L", "L", 1.0)}
    assert implicit_limit_rules(5, None, None) == []


def test_format_value_with_state_dictionary():
    assert format_value(8, None, {"8": "大火燃燒"}) == "8（大火燃燒）"
    assert format_value(182.0, "°C") == "182 °C"
    assert format_value(None) == "—"


def test_build_message():
    rule = AlarmRule(key="rule:1", source_type="sensor_rule", sensor_id=1,
                     alarm_type="HH", setpoint=180, message="檢查冷卻水")
    msg = build_message(rule, "B03 / TT01（蒸氣溫度）", 182.5, "°C")
    assert "高高限" in msg and "182.5 °C" in msg and "180 °C" in msg and "檢查冷卻水" in msg


def test_build_message_eq_shows_current_state_only():
    rule = AlarmRule(key="rule:2", source_type="sensor_rule", sensor_id=4, alarm_type="EQ", setpoint=8)
    msg = build_message(rule, "B03 / ST01", 8, None, {"8": "大火燃燒"})
    assert msg == "B03 / ST01 狀態警報：目前為 8（大火燃燒）"


def test_format_number_no_scientific_notation():
    from services.alarm.rules import format_number
    assert format_number(1_000_075.5) == "1,000,075.5"
    assert format_number(1_000_000.0) == "1,000,000"
    assert format_number(189.123) == "189.12"
    assert format_number(0.12345) == "0.1235"
    assert format_number(-2.5) == "-2.5"
    assert format_number(1.4427e28) == "1.44e+28"
    assert format_number(-3998056475262976.0) == "-4e+15"
