"""計算點運算式（services/calc/expression.py）。"""

import math

import pytest

from services.calc.expression import Expression, ExpressionError, evaluation_order


def ev(text, **values):
    return Expression(text).evaluate(values)


def test_refs_and_arithmetic():
    e = Expression("{PM01_KW} + {PM02_KW} * 2 - {PM01_KW}")
    assert e.refs == ["PM01_KW", "PM02_KW"]
    assert e.evaluate({"PM01_KW": 10, "PM02_KW": 3}) == 6.0


def test_codes_with_symbols():
    assert ev("{B-03.TT 01} / 2", **{"B-03.TT 01": 9}) == 4.5


def test_functions_conditions_constants():
    assert ev("clamp({L} / 3000 * 100, 0, 100)", L=4500) == 100.0
    assert ev("1 if {I} > 5 else 0", I=7) == 1.0
    assert ev("{I} > 5 and {V} < 250", I=7, V=260) == 0.0
    assert ev("iff({I} >= 5, 10, 20)", I=5) == 10.0
    assert ev("max({A}, {B}, 0)", A=-1, B=-2) == 0.0
    assert ev("round(sqrt({A}), 2)", A=2) == 1.41
    assert ev("2 * pi") == pytest.approx(2 * math.pi)
    assert ev("-{A} ** 2", A=3) == -9.0
    assert ev("0 < {A} <= 10", A=10) == 1.0


@pytest.mark.parametrize("text,msg", [
    ("", "空"),
    ("__import__('os')", "不支援的函式"),
    ("__v0 + 1", "不認得"),
    ("{A}.real", "不支援"),
    ("open('x')", "不支援的函式"),
    ("{A} + B03", "大括號"),
    ("{A} +", "語法"),
    ("{A", "成對"),
    ("'abc'", "文字"),
    ("[1, 2]", "不支援"),
    ("max(x=1)", "不支援"),
    ("x" * 1001, "超過"),
])
def test_rejected(text, msg):
    with pytest.raises(ExpressionError, match=msg):
        Expression(text)


def test_runtime_errors():
    with pytest.raises(ExpressionError, match="除以零"):
        ev("{A} / {B}", A=1, B=0)
    with pytest.raises(ExpressionError, match="次方"):
        ev("10 ** {A}", A=1000)
    with pytest.raises(ExpressionError, match="計算錯誤"):
        ev("sqrt({A})", A=-1)


def test_evaluation_order_and_cycles():
    order, cyclic = evaluation_order({"TOTAL": {"A", "SUB"}, "SUB": {"B", "C"}, "X": {"Y"}, "Y": {"X"}})
    assert order.index("SUB") < order.index("TOTAL")
    assert cyclic == {"X", "Y"}
    order, cyclic = evaluation_order({"S": {"S"}})
    assert cyclic == {"S"} and order == []


# ---------------------------------------------------------------- v3.3
from datetime import datetime, timedelta

from core.config import LOCAL_TZ
from services.calc.expression import MISSING, EvalContext, MissingInput, period_start, persistable_state


def T(h, m=0, s=0, d=1):
    return datetime(2026, 10, d, h, m, s, tzinfo=LOCAL_TZ)


class Clock:
    """同一個 Expression + 同一份 state，模擬計算引擎一輪一輪執行。"""

    def __init__(self, text, baseline=None):
        self.expr = Expression(text)
        self.state = {}
        self.baseline = baseline

    def at(self, now, **values):
        return self.expr.evaluate(values, EvalContext(now=now, state=self.state, baseline=self.baseline))


def test_stats_logic_and_bits():
    assert ev("avg({A}, {B}, {C})", A=1, B=2, C=6) == 3.0
    assert ev("sum({A}, {B})", A=1.5, B=2) == 3.5
    assert ev("median({A}, {B}, {C})", A=9, B=1, C=5) == 5.0
    assert ev("spread({A}, {B})", A=9, B=1) == 8.0
    assert ev("switch({S}, 1, 10, 2, 20, 99)", S=2) == 20.0
    assert ev("switch({S}, 1, 10, 2, 20, 99)", S=7) == 99.0
    assert ev("between({T}, 60, 80)", T=70) == 1.0
    assert ev("bit({W}, 3)", W=0b1000) == 1.0 and ev("bit({W}, 2)", W=0b1000) == 0.0
    assert ev("bitand({W}, 6) + shl(1, 4)", W=0b1110) == 22.0
    assert ev("{W} & 1", W=3) == 1.0


def test_quality_functions_handle_missing():
    assert ev("valueor({A}, 0) + {B}", A=MISSING, B=5) == 5.0
    assert ev("coalesce({MAIN}, {BACKUP})", MAIN=MISSING, BACKUP=7) == 7.0
    assert ev("coalesce({MAIN}, {BACKUP})", MAIN=3, BACKUP=7) == 3.0
    assert ev("isgood({A})", A=MISSING) == 0.0 and ev("isgood({A})", A=1) == 1.0
    with pytest.raises(MissingInput) as e:
        ev("{A} + {B}", A=MISSING, B=1)
    assert e.value.ref == "A"
    with pytest.raises(MissingInput):
        ev("coalesce({A}, {B})", A=MISSING, B=MISSING)


def test_accessed_refs_exclude_skipped_missing():
    e = Expression("coalesce({MAIN}, {BACKUP})")
    e.evaluate({"MAIN": MISSING, "BACKUP": 2})
    assert e.accessed == {"BACKUP"}


def test_time_functions():
    e = Expression("iff(timeofday() >= 9 and weekday() <= 5, 2, 1)")
    assert e.evaluate({}, EvalContext(now=T(9, 30))) == 2.0          # 2026-10-01 週四
    assert e.evaluate({}, EvalContext(now=T(8, 59))) == 1.0
    assert e.evaluate({}, EvalContext(now=T(10, d=4))) == 1.0        # 週日
    assert Expression("hour()").uses_time


def test_period_start():
    assert period_start(T(7), "day", 8) == T(8) - timedelta(days=1)
    assert period_start(T(9), "day", 8) == T(8)
    assert period_start(T(9, 30), "hour") == T(9)
    assert period_start(T(9), "week") == datetime(2026, 9, 28, tzinfo=LOCAL_TZ)
    assert period_start(T(9), "month") == datetime(2026, 10, 1, tzinfo=LOCAL_TZ)
    assert period_start(T(9), "month", 12) == datetime(2026, 9, 1, 12, tzinfo=LOCAL_TZ)
    assert period_start(T(9), "never") is None


def test_integral_kw_to_kwh_with_daily_reset_and_gap():
    c = Clock("integral({KW}, 'day')")
    assert c.at(T(23, 58), KW=60) == 0.0
    assert c.at(T(23, 59), KW=60) == pytest.approx(1.0)             # 60 kW × 1 分鐘 = 1 kWh
    assert c.at(T(0, 0, d=2), KW=60) == 0.0                          # 跨日歸零
    assert c.at(T(0, 1, d=2), KW=120) == pytest.approx(1.5)          # 梯形：(60+120)/2 × 1/60
    assert c.at(T(1, 0, d=2), KW=120) == pytest.approx(1.5)          # 停了 59 分鐘：不跨過空窗累加


def test_delta_uses_history_baseline_and_production_day():
    lookups = []

    def baseline(code, start):
        lookups.append((code, start))
        return 1000.0

    c = Clock("delta({KWH}, 'day', 8)", baseline=baseline)
    assert c.at(T(9), KWH=1012.5) == pytest.approx(12.5)
    assert c.at(T(10), KWH=1020) == pytest.approx(20)
    assert lookups == [("KWH", T(8))]                                # 同一個週期只查一次
    c.baseline = lambda code, start: 1050.0
    assert c.at(T(8, 0, d=2), KWH=1050) == 0.0                       # 新的生產日


def test_delta_without_history_starts_from_zero():
    c = Clock("delta({KWH})")
    assert c.at(T(9), KWH=500) == 0.0 and c.at(T(10), KWH=510) == 10.0


def test_ontime_and_count():
    on, cnt = Clock("ontime({I} > 5, 'day')"), Clock("count({I} > 5, 'day')")
    seq = [(T(8, 0), 0), (T(8, 1), 10), (T(8, 2), 10), (T(8, 3), 0), (T(8, 4), 10), (T(8, 5), 10)]
    for t, i in seq:
        hours, starts = on.at(t, I=i), cnt.at(t, I=i)
    assert hours == pytest.approx(3 / 60) and starts == 2


def test_dynamic_functions():
    c = Clock("prev({A})")
    assert c.at(T(8), A=1) == 1.0 and c.at(T(8, 1), A=2) == 1.0
    c = Clock("changed({A})")
    assert c.at(T(8), A=1) == 0.0 and c.at(T(8, 1), A=1) == 0.0 and c.at(T(8, 2), A=2) == 1.0
    c = Clock("rising({A} > 0)")
    assert [c.at(T(8, i), A=v) for i, v in enumerate([0, 1, 1, 0, 1])] == [0, 1, 0, 0, 1]
    c = Clock("hold({A}, {TRIG})")
    assert c.at(T(8), A=5, TRIG=1) == 5 and c.at(T(8, 1), A=9, TRIG=0) == 5 and c.at(T(8, 2), A=9, TRIG=1) == 9
    c = Clock("derivative({A})")
    assert c.at(T(8), A=0) == 0.0 and c.at(T(8, 0, 10), A=5) == 0.5
    c = Clock("movavg({A}, 60)")
    assert c.at(T(8, 0, 0), A=0) == 0 and c.at(T(8, 0, 30), A=10) == 5 and c.at(T(8, 1, 20), A=20) == 15
    c = Clock("filter({A}, 10)")
    assert c.at(T(8, 0, 0), A=0) == 0 and c.at(T(8, 0, 10), A=10) == pytest.approx(5)


def test_state_reset_on_expression_change_and_persistable():
    c = Clock("integral({KW})")
    c.at(T(8), KW=60)
    c.at(T(8, 1), KW=60)
    state = c.state
    assert any(k.startswith("integral#") for k in state)
    c2 = Clock("integral({KW}) * 2")
    c2.state = state                                                  # 換了運算式，沿用舊 state
    assert c2.at(T(8, 2), KW=60) == 0.0
    m = Clock("movavg({A}, 60)")
    m.at(T(8), A=1)
    saved = persistable_state(m.state)
    assert all("_mem_buf" not in v for v in saved.values() if isinstance(v, dict))


@pytest.mark.parametrize("text,msg", [
    ("delta({A} + 1, 'day')", "直接是感測器"),
    ("integral({A}, 'year')", "週期"),
    ("integral({A}, 5)", "週期文字"),
    ("movavg({A}, 999999)", "秒數"),
    ("hour(1)", "不需要參數"),
    ("valueor({A})", "參數數量"),
    ("{A} + 'x'", "文字"),
])
def test_v33_rejected(text, msg):
    with pytest.raises(ExpressionError, match=msg):
        Expression(text)
