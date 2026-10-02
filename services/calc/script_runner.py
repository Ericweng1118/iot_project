"""
services/calc/script_runner.py
==============================
Python 腳本計算點的執行器（v3.4，sql/021）。

為什麼要獨立子程序：
    腳本是使用者寫的任意 Python。在採集服務（main.py）裡直接 exec 的話，一個無窮迴圈、
    一次吃光記憶體、一個 C 擴充崩潰，就會讓整個採集服務停擺。所以腳本一律在獨立的
    子程序執行：
        - 每次執行有逾時（CALC_SCRIPT_TIMEOUT，預設 2 秒），超時就強制終止子程序並自動重啟
        - 子程序有記憶體上限（CALC_SCRIPT_MEMORY_MB，預設 512 MB）與較低的排程優先權
        - 子程序崩潰不影響採集服務，下一輪自動重啟

⚠️ 這不是安全沙箱：
    內建函式與可 import 的模組有白名單，能擋掉大部分「不小心」的危險操作（開檔、跑系統指令），
    但 Python 本身無法做到完全隔離，懂的人仍然可以繞過。所以腳本只有「管理員」能新增 / 修改，
    每次修改都寫入稽核紀錄，而且整個功能預設關閉（CALC_SCRIPTS_ENABLED=true 才啟用）。

腳本可以使用：
    value("代碼", 預設=None)   感測器最新值（斷線 / 品質不良 / 沒資料 → 預設值）
    tags                      {代碼: 數值}，所有有效的感測器
    quality("代碼")           "GOOD" / "UNCERTAIN" / None（無效）
    isgood("代碼")            是否有效
    now                       目前時間（SCADA_TIMEZONE，datetime）
    state                     dict，跨輪保存（必須能轉成 JSON，重啟後接續）
    log(...)                  輸出訊息（顯示在網頁，每輪最多 20 行）
    result                    指定這一輪的結果（數字）；None = 這一輪不更新
    可 import：math statistics datetime json re itertools functools collections decimal
              fractions bisect heapq random time
"""

import json
import logging
import math
import multiprocessing as mp
import os
import re
import threading
import time
import traceback

logger = logging.getLogger("calc_script")

MAX_SOURCE_LENGTH = 20000
MAX_LOG_LINES = 20
ALLOWED_MODULES = frozenset({
    "math", "statistics", "datetime", "json", "re", "itertools", "functools", "collections",
    "decimal", "fractions", "bisect", "heapq", "random", "time",
})
_SAFE_BUILTINS = (
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float", "format",
    "frozenset", "int", "isinstance", "len", "list", "map", "max", "min", "next", "pow", "range",
    "reversed", "round", "set", "sorted", "str", "sum", "tuple", "zip", "True", "False", "None",
    "Exception", "ValueError", "TypeError", "KeyError", "ZeroDivisionError", "ArithmeticError",
    "IndexError", "StopIteration",
)
_REF_RE = re.compile(
    r"""(?:value|quality|isgood)\(\s*["']([^"']+)["']|tags(?:\.get)?\(?\[?\s*["']([^"']+)["']"""
)

TEMPLATE = '''# 可用：value("代碼", 預設)、tags、quality("代碼")、isgood("代碼")、now、state、log(...)
# 最後把結果指定給 result（數字）；result = None 表示這一輪不更新

kw = value("PM01_KW", 0) + value("PM02_KW", 0)

# 範例：用 state 記住上一輪，計算尖峰需量（每天 0 點歸零）
today = now.strftime("%Y-%m-%d")
if state.get("day") != today:
    state["day"] = today
    state["peak"] = 0
state["peak"] = max(state["peak"], kw)

log("目前功率", kw, "今日尖峰", state["peak"])
result = state["peak"]
'''


def _strip_comments(source: str) -> str:
    """去掉 # 註解（範本的說明文字裡有 value("代碼")，不能當成真的引用）。語法錯誤時原樣返回。"""
    import io
    import tokenize
    try:
        tokens = [t for t in tokenize.generate_tokens(io.StringIO(source).readline) if t.type != tokenize.COMMENT]
        return tokenize.untokenize(tokens)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source


def script_refs(source: str) -> list:
    """從程式碼裡找出寫死的感測器代碼（value("X")、tags["X"]…），用於排序與顯示；動態組出來的代碼抓不到。"""
    refs = []
    for a, b in _REF_RE.findall(_strip_comments(source or "")):
        code = a or b
        if code and code not in refs:
            refs.append(code)
    return refs


def check_source(source: str) -> list:
    """編譯檢查（不執行）。回傳錯誤清單。"""
    errors = []
    if not (source or "").strip():
        return ["腳本不能是空的"]
    if len(source) > MAX_SOURCE_LENGTH:
        errors.append(f"腳本超過 {MAX_SOURCE_LENGTH} 字")
    try:
        compile(source, "<calc script>", "exec")
    except SyntaxError as e:
        errors.append(f"語法錯誤（第 {e.lineno} 行）：{e.msg}")
    if "result" not in (source or ""):
        errors.append("腳本必須設定 result（這一輪的結果）")
    return errors


# ------------------------------------------------------------------
# 子程序
# ------------------------------------------------------------------
def _limited_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level != 0 or name.split(".")[0] not in ALLOWED_MODULES:
        raise ImportError(f"不允許 import {name}（可用：{', '.join(sorted(ALLOWED_MODULES))}）")
    return __import__(name, globals, locals, fromlist, level)


def _execute(code_obj, inputs: dict, qualities: dict, state: dict, now):
    import builtins

    logs = []

    def log(*args):
        if len(logs) < MAX_LOG_LINES:
            logs.append(" ".join(str(a) for a in args)[:500])

    safe = {k: getattr(builtins, k) for k in _SAFE_BUILTINS if hasattr(builtins, k)}
    safe["__import__"] = _limited_import
    safe["print"] = log
    ns = {
        "__builtins__": safe,
        "math": math,
        "tags": dict(inputs),
        "value": lambda code, default=None: inputs.get(code, default),
        "quality": lambda code: qualities.get(code),
        "isgood": lambda code: code in inputs,
        "now": now,
        "state": state,
        "log": log,
        "result": None,
    }
    exec(code_obj, ns)  # noqa: S102 —— 這就是腳本功能本身；隔離靠子程序 + 白名單 + 權限
    return ns.get("result"), ns.get("state"), logs


def _worker_main(conn, memory_mb: int):
    try:
        import resource
        limit = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, ValueError, OSError):
        pass
    try:
        os.nice(5)
    except OSError:
        pass
    from datetime import datetime
    cache = {}
    while True:
        try:
            msg = conn.recv()
        except EOFError:
            return
        if msg is None:
            return
        calc_id, source, inputs, qualities, state, now_iso = msg
        started = time.perf_counter()
        try:
            key = (calc_id, hash(source))
            code_obj = cache.get(key)
            if code_obj is None:
                code_obj = compile(source, f"<計算點 {calc_id}>", "exec")
                cache[key] = code_obj
                if len(cache) > 500:
                    cache.clear()
            result, new_state, logs = _execute(code_obj, inputs, qualities, state,
                                               datetime.fromisoformat(now_iso))
            if result is not None:
                if isinstance(result, bool):
                    result = float(result)
                if not isinstance(result, (int, float)):
                    raise TypeError(f"result 必須是數字，收到 {type(result).__name__}：{result!r}"[:200])
                result = float(result)
                if math.isnan(result) or math.isinf(result):
                    raise ValueError("result 不是有效數字（NaN / 無限大）")
            if not isinstance(new_state, dict):
                raise TypeError("state 必須維持是 dict")
            try:
                json.dumps(new_state)
            except (TypeError, ValueError) as e:
                raise TypeError(f"state 裡只能放可以轉成 JSON 的資料（數字、文字、list、dict）：{e}")
            conn.send(("ok", result, new_state, logs, None, (time.perf_counter() - started) * 1000))
        except MemoryError:
            conn.send(("error", None, state, [], "記憶體超過上限（CALC_SCRIPT_MEMORY_MB）",
                       (time.perf_counter() - started) * 1000))
        except BaseException as e:  # noqa: BLE001 —— 腳本的任何錯誤都要回報，不能讓子程序結束
            tb = traceback.extract_tb(e.__traceback__)
            line = next((f.lineno for f in reversed(tb) if f.filename.startswith("<計算點")), None)
            where = f"第 {line} 行：" if line else ""
            conn.send(("error", None, state, [], f"{where}{type(e).__name__}: {e}"[:500],
                       (time.perf_counter() - started) * 1000))


class ScriptResult:
    __slots__ = ("ok", "value", "state", "logs", "error", "elapsed_ms", "timeout")

    def __init__(self, ok, value=None, state=None, logs=None, error=None, elapsed_ms=0.0, timeout=False):
        self.ok, self.value, self.state, self.logs = ok, value, state, logs or []
        self.error, self.elapsed_ms, self.timeout = error, elapsed_ms, timeout


class ScriptRunner:
    """管理一個執行腳本的子程序；run() 是同步呼叫，執行緒安全。"""

    def __init__(self, timeout: float | None = None, memory_mb: int | None = None):
        self.timeout = float(timeout if timeout is not None else
                             os.getenv("CALC_SCRIPT_TIMEOUT", "2").split("#")[0].strip() or 2)
        self.memory_mb = int(memory_mb if memory_mb is not None else
                             os.getenv("CALC_SCRIPT_MEMORY_MB", "512").split("#")[0].strip() or 512)
        self._ctx = mp.get_context("spawn")      # 乾淨的子程序：不繼承採集服務的執行緒與 DB 連線
        self._proc = None
        self._conn = None
        self._lock = threading.Lock()
        self.restarts = 0

    def _start(self):
        parent, child = self._ctx.Pipe()
        self._proc = self._ctx.Process(target=_worker_main, args=(child, self.memory_mb),
                                       name="calc-script-worker", daemon=True)
        self._proc.start()
        child.close()
        self._conn = parent

    def _kill(self):
        if self._proc is not None:
            self._proc.kill()
            self._proc.join(timeout=2)
        self._proc = None
        self._conn = None

    def run(self, calc_id, source, inputs: dict, qualities: dict, state: dict, now) -> ScriptResult:
        with self._lock:
            if self._proc is None or not self._proc.is_alive():
                if self._proc is not None:
                    self.restarts += 1
                self._kill()
                self._start()
            started = time.perf_counter()
            try:
                self._conn.send((calc_id, source, inputs, qualities, state, now.isoformat()))
                if not self._conn.poll(self.timeout):
                    self._kill()
                    self.restarts += 1
                    return ScriptResult(False, state=state, timeout=True,
                                        error=f"執行逾時（超過 {self.timeout:g} 秒），已強制終止",
                                        elapsed_ms=(time.perf_counter() - started) * 1000)
                status, value, new_state, logs, error, elapsed = self._conn.recv()
            except (EOFError, BrokenPipeError, OSError) as e:
                self._kill()
                self.restarts += 1
                return ScriptResult(False, state=state, error=f"腳本執行器異常結束：{e}",
                                    elapsed_ms=(time.perf_counter() - started) * 1000)
        return ScriptResult(status == "ok", value, new_state, logs, error, elapsed)

    def stop(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.send(None)
                except (BrokenPipeError, OSError):
                    pass
            if self._proc is not None:
                self._proc.join(timeout=2)
            self._kill()
