"""Python 腳本計算點的執行器（services/calc/script_runner.py）：真的開子程序跑。"""

from datetime import datetime

import pytest

from core.config import LOCAL_TZ
from services.calc.script_runner import TEMPLATE, ScriptRunner, check_source, script_refs

NOW = datetime(2026, 10, 2, 9, 30, tzinfo=LOCAL_TZ)


@pytest.fixture(scope="module")
def runner():
    r = ScriptRunner(timeout=1.5, memory_mb=512)
    yield r
    r.stop()


def run(runner, source, inputs=None, state=None, qualities=None):
    inputs = inputs or {}
    return runner.run(1, source, inputs, qualities or {k: "GOOD" for k in inputs}, state or {}, NOW)


def test_basic_result_state_and_log(runner):
    r = run(runner, 'state["n"] = state.get("n", 0) + 1\nlog("hi", 1)\nresult = value("A") * 2 + state["n"]',
            {"A": 10.0}, {"n": 4})
    assert r.ok and r.value == 25.0 and r.state == {"n": 5} and r.logs == ["hi 1"]


def test_template_runs(runner):
    r = run(runner, TEMPLATE, {"PM01_KW": 3.0, "PM02_KW": 4.0})
    assert r.ok and r.value == 7.0 and r.state["peak"] == 7.0


def test_missing_value_default_and_quality(runner):
    r = run(runner, 'result = value("A", -1) if not isgood("A") else 0', {})
    assert r.ok and r.value == -1
    r = run(runner, 'result = 1 if quality("A") == "UNCERTAIN" else 0', {"A": 1.0}, qualities={"A": "UNCERTAIN"})
    assert r.value == 1.0


def test_allowed_and_blocked_imports(runner):
    assert run(runner, "import statistics\nresult = statistics.mean([1, 2, 3])").value == 2.0
    r = run(runner, "import os\nresult = 1")
    assert not r.ok and "不允許 import os" in r.error
    r = run(runner, "result = open('/etc/passwd').read()")
    assert not r.ok and "NameError" in r.error


def test_error_reports_line_number(runner):
    r = run(runner, "x = 1\ny = x / 0\nresult = y")
    assert not r.ok and "第 2 行" in r.error and "ZeroDivisionError" in r.error


def test_invalid_results_and_state(runner):
    assert "result 必須是數字" in run(runner, "result = 'abc'").error
    assert "NaN" in run(runner, "result = float('nan')").error
    assert "JSON" in run(runner, "state['x'] = {1, 2}\nresult = 1").error
    r = run(runner, "result = None")
    assert r.ok and r.value is None


def test_timeout_kills_and_recovers(runner):
    r = run(runner, "while True:\n    pass\nresult = 1")
    assert not r.ok and r.timeout and "逾時" in r.error
    r = run(runner, "result = 42")                                  # 子程序已自動重啟
    assert r.ok and r.value == 42.0


def test_crash_isolated(runner):
    r = run(runner, "import time\nraise SystemExit(3)\nresult = 1")
    assert not r.ok
    assert run(runner, "result = 1").ok


def test_memory_limit(runner):
    r = run(runner, "x = [0] * 1_000_000_000\nresult = 1")
    assert not r.ok and ("記憶體" in (r.error or "") or "MemoryError" in (r.error or ""))
    assert run(runner, "result = 2").value == 2.0


def test_check_source_and_refs():
    assert check_source("result = (") and "語法錯誤" in check_source("result = (")[0]
    assert "result" in check_source("x = 1")[0]
    assert check_source("result = 1") == []
    assert script_refs('a = value("PM01_KW") + tags["PM02_KW"] + tags.get(\'X\', 0)\nisgood("Y")') == \
        ["PM01_KW", "Y", "PM02_KW", "X"] or set(script_refs('a = value("PM01_KW") + tags["PM02_KW"] + tags.get(\'X\', 0)\nisgood("Y")')) == {"PM01_KW", "PM02_KW", "X", "Y"}


def test_refs_ignore_comments():
    assert script_refs('# value("代碼") 只是說明\nresult = value("REAL_1")') == ["REAL_1"]
    assert script_refs(TEMPLATE) == ["PM01_KW", "PM02_KW"]
