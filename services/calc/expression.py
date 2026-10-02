"""
services/calc/expression.py
===========================
計算點的運算式：解析與安全計算（純邏輯，可單元測試）。

語法：
    感測器用大括號引用感測器編號：{B03_EM01} - {B06_EM01}
    運算子：+ - * / % // **、比較 > >= < <= == !=、and or not（結果 1 / 0）、a if 條件 else b
    常數：pi e

函式（FUNCTION_DOCS 有完整說明，網頁「計算點 → 運算式語法」會列出）：
    數學    abs min max round sqrt log log10 exp floor ceil sign clamp
    統計    avg sum median spread
    邏輯    iff switch between
    品質    isgood valueor coalesce          —— 輸入斷線時改用備援值，而不是整個計算點斷線
    位元    bit bitand bitor bitxor shl shr  —— 拆 PLC 狀態字
    時間    hour minute weekday day month year timeofday —— 時間電價、班別判斷（SCADA_TIMEZONE）
    累計    integral delta ontime count       —— kW→kWh、今日用量、今日運轉時數、今日啟動次數
    動態    prev changed rising hold derivative movavg movmin movmax filter

    累計類可指定週期 'hour' / 'day' / 'week' / 'month' / 'never'，以及「生產日」起始小時
    （例如 delta({KWH}, 'day', 8) = 今天 08:00 起的用量）。

輸入斷線的規則：
    一般運算碰到斷線 / 品質不良 / 沒有資料的輸入 → 這一輪不算、計算點標記斷線（不用舊值硬算）。
    isgood / valueor / coalesce 是例外：它們本來就是用來處理斷線的。

狀態：
    累計 / 動態類函式需要記住上一輪的資料（ctx.state）。計算引擎會把狀態存進
    calculated_points.calc_state（sql/020），服務重啟後累計值接續，不會歸零。
    移動視窗（movavg 等）只存在記憶體，重啟後重新累積。
    ⚠️ 條件式（a if c else b）只會執行被選到的分支，放在沒被選到的分支裡的累計函式那一輪不會更新。

安全性：
    不使用 eval()。運算式先轉成 Python AST，只允許列出的節點與函式；次方指數上限 100、
    運算式長度上限 1000 字、移動視窗最長 1 天，避免運算式卡死採集服務或吃光記憶體。
"""

import ast
import hashlib
import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

MAX_LENGTH = 1000
MAX_EXPONENT = 100
MAX_WINDOW_SECONDS = 86400
INTEGRAL_MAX_GAP = 300.0      # 兩次計算相隔超過這麼久（斷線、重啟）就不跨過空窗累加，避免一次灌進一大段
REF_PATTERN = re.compile(r"\{([^{}]+)\}")
PERIODS = ("hour", "day", "week", "month", "never")


class ExpressionError(ValueError):
    pass


class MissingInput(ExpressionError):
    """輸入感測器斷線 / 品質不良 / 沒有資料。"""

    def __init__(self, ref):
        super().__init__(f"{ref} 無有效資料")
        self.ref = ref


class _Missing:
    def __repr__(self):
        return "MISSING"


MISSING = _Missing()


@dataclass
class EvalContext:
    """一個計算點的執行環境：目前時間、跨輪狀態、歷史基準值查詢。"""
    now: datetime
    state: dict = field(default_factory=dict)
    # baseline(感測器編號, 時間點) -> 該時間點的值（delta 用；None = 查不到）
    baseline: object = None
    trial: bool = False          # 網頁試算：沒有歷史狀態，累計類從 0 開始


def period_start(now: datetime, period: str, start_hour: float = 0) -> datetime | None:
    """目前所在週期的起點（當地時間）。'never' 回傳 None。"""
    if period == "never":
        return None
    if period == "hour":
        return now.replace(minute=0, second=0, microsecond=0)
    h = int(start_hour)
    m = int(round((start_hour - h) * 60))
    base = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if period == "day":
        return base if now >= base else base - timedelta(days=1)
    if period == "week":
        start = base - timedelta(days=now.weekday())
        return start if now >= start else start - timedelta(days=7)
    if period == "month":
        start = base.replace(day=1)
        if now >= start:
            return start
        prev = (start - timedelta(days=1)).replace(day=1)
        return prev
    raise ExpressionError(f"週期只能是 {'/'.join(PERIODS)}")


# ------------------------------------------------------------------
# 一般函式（無狀態）
# ------------------------------------------------------------------

def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _iff(cond, a, b):
    return a if cond else b


def _switch(x, *pairs):
    if len(pairs) < 1:
        raise TypeError("switch 至少要有預設值")
    default = pairs[-1] if len(pairs) % 2 == 1 else None
    for i in range(0, len(pairs) - (len(pairs) % 2), 2):
        if x == pairs[i]:
            return pairs[i + 1]
    if default is None:
        raise ValueError("switch 沒有符合的條件，也沒有預設值（最後一個參數）")
    return default


def _between(x, lo, hi):
    return lo <= x <= hi


def _sign(x):
    return (x > 0) - (x < 0)


def _bit(x, n):
    return (int(x) >> int(n)) & 1


def _shift(x, n, left):
    n = int(n)
    if not 0 <= n <= 63:
        raise ValueError("位移量必須是 0~63")
    return int(x) << n if left else int(x) >> n


FUNCTIONS = {
    "abs": abs, "min": min, "max": max, "round": round, "sqrt": math.sqrt, "log": math.log,
    "log10": math.log10, "exp": math.exp, "floor": math.floor, "ceil": math.ceil, "sign": _sign,
    "clamp": _clamp, "iff": _iff, "switch": _switch, "between": _between,
    "avg": lambda *a: statistics.fmean(a), "sum": lambda *a: math.fsum(a),
    "median": lambda *a: statistics.median(a), "spread": lambda *a: max(a) - min(a),
    "bit": _bit, "bitand": lambda a, b: int(a) & int(b), "bitor": lambda a, b: int(a) | int(b),
    "bitxor": lambda a, b: int(a) ^ int(b), "shl": lambda x, n: _shift(x, n, True),
    "shr": lambda x, n: _shift(x, n, False),
}
# 可以接受「斷線」輸入的函式
QUALITY_FUNCTIONS = ("isgood", "valueor", "coalesce")
TIME_FUNCTIONS = ("hour", "minute", "weekday", "day", "month", "year", "timeofday")
# 有狀態的函式：(最少參數, 最多參數, 第一個參數必須直接是感測器引用)
STATEFUL = {
    "prev": (1, 1, False), "changed": (1, 1, False), "rising": (1, 1, False), "hold": (2, 2, False),
    "derivative": (1, 1, False), "movavg": (2, 2, False), "movmin": (2, 2, False), "movmax": (2, 2, False),
    "filter": (2, 2, False), "integral": (1, 3, False), "ontime": (1, 3, False), "count": (1, 3, False),
    "delta": (1, 3, True),
}
ALL_FUNCTIONS = set(FUNCTIONS) | set(QUALITY_FUNCTIONS) | set(TIME_FUNCTIONS) | set(STATEFUL)
CONSTANTS = {"pi": math.pi, "e": math.e}

FUNCTION_DOCS = [
    ("數學", "abs(x) / sign(x) / sqrt(x) / log(x) / log10(x) / exp(x) / floor(x) / ceil(x)", "基本數學"),
    ("數學", "round(x, 小數位)", "四捨五入"),
    ("數學", "min(a, b, …) / max(a, b, …)", "最小 / 最大"),
    ("數學", "clamp(x, 下限, 上限)", "限制在範圍內"),
    ("統計", "avg(a, b, …) / sum(…) / median(…) / spread(…)", "平均、合計、中位數、最大減最小（多台設備比較）"),
    ("邏輯", "iff(條件, 成立值, 不成立值)", "同 a if 條件 else b"),
    ("邏輯", "switch(x, 值1, 結果1, 值2, 結果2, …, 預設)", "對照表，例如狀態碼轉分類"),
    ("邏輯", "between(x, 下限, 上限)", "在範圍內為 1"),
    ("品質", "isgood({X})", "X 有效為 1、斷線 / 品質不良為 0（不會讓計算點斷線）"),
    ("品質", "valueor({X}, 預設值)", "X 斷線時改用預設值"),
    ("品質", "coalesce({X}, {備援}, …)", "依序取第一個有效的值（主備援感測器）"),
    ("位元", "bit({STATUS}, n)", "取第 n 個位元（0 起算），拆 PLC 狀態字"),
    ("位元", "bitand / bitor / bitxor(a, b)、shl / shr(x, n)", "位元運算與位移"),
    ("時間", "hour() / minute() / weekday() / day() / month() / year()", "目前時間；weekday 1=週一 … 7=週日"),
    ("時間", "timeofday()", "目前時刻的小時數（13:30 = 13.5），時間電價判斷用"),
    ("累計", "integral(x, 週期, 起始小時)", "對時間積分，單位是「x × 小時」：kW → kWh。週期內累計、週期開始歸零"),
    ("累計", "delta({計數器}, 週期, 起始小時)", "本期用量 = 目前值 − 週期開始時的值（從歷史資料取基準），例如今日用電"),
    ("累計", "ontime(條件, 週期, 起始小時)", "本期條件成立的時數，例如今日運轉時數"),
    ("累計", "count(條件, 週期, 起始小時)", "本期條件由 0 變 1 的次數，例如今日啟動次數"),
    ("動態", "prev(x) / changed(x) / rising(條件)", "上一輪的值、有沒有變、是否剛從 0 變 1"),
    ("動態", "hold(x, 條件)", "條件成立時取樣 x，其他時間維持上次取樣的值"),
    ("動態", "derivative(x)", "每秒變化量（上一輪到這一輪）"),
    ("動態", "movavg(x, 秒) / movmin(x, 秒) / movmax(x, 秒)", "移動平均 / 最小 / 最大（最長 1 天）"),
    ("動態", "filter(x, 時間常數秒)", "一階低通濾波，去除雜訊"),
]

_BIN_OPS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b, ast.Mod: lambda a, b: a % b, ast.FloorDiv: lambda a, b: a // b,
    ast.Pow: None, ast.BitAnd: lambda a, b: int(a) & int(b), ast.BitOr: lambda a, b: int(a) | int(b),
    ast.BitXor: lambda a, b: int(a) ^ int(b),
}
_CMP_OPS = {
    ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b, ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b, ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b,
}


class Expression:
    def __init__(self, text: str):
        text = (text or "").strip()
        if not text:
            raise ExpressionError("運算式不能是空的")
        if len(text) > MAX_LENGTH:
            raise ExpressionError(f"運算式超過 {MAX_LENGTH} 字")
        self.text = text
        self.refs: list = []          # 引用的感測器編號（依出現順序、不重複）

        def _sub(m):
            code = m.group(1).strip()
            if not code:
                raise ExpressionError("大括號裡要放感測器編號，例如 {B03_TT01}")
            if code not in self.refs:
                self.refs.append(code)
            return f"__v{self.refs.index(code)}"

        source = REF_PATTERN.sub(_sub, text)
        if "{" in source or "}" in source:
            raise ExpressionError("大括號沒有成對")
        try:
            self._tree = ast.parse(source, mode="eval")
        except SyntaxError as e:
            raise ExpressionError(f"語法錯誤：{e.msg}")
        # 只有大括號引用產生的 __v0、__v1… 是合法的變數名稱（使用者直接打 __v5 也不行）
        self._names = {f"__v{i}" for i in range(len(self.refs))}
        self._call_ids = {}           # 有狀態函式呼叫點 → 編號（狀態的 key）
        self.stateful = False
        self.uses_time = False
        self._check(self._tree.body, in_call=None)
        self.signature = hashlib.sha1(source.encode()).hexdigest()[:12]
        self.accessed: set = set()

    # 編譯時就檢查一次所有節點，執行時才不會跑到一半才發現不支援
    def _check(self, node, in_call):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                if in_call not in STATEFUL:
                    raise ExpressionError(f"文字只能用在累計函式的週期參數：{node.value!r}")
                if node.value not in PERIODS:
                    raise ExpressionError(f"週期只能是 {' / '.join(PERIODS)}，收到 {node.value!r}")
            elif not isinstance(node.value, (int, float, bool)):
                raise ExpressionError(f"不支援的常數：{node.value!r}")
        elif isinstance(node, ast.Name):
            if not (node.id in self._names or node.id in CONSTANTS):
                raise ExpressionError(f"不認得「{node.id}」：感測器請用大括號，例如 {{{node.id}}}")
        elif isinstance(node, ast.BinOp):
            if type(node.op) not in _BIN_OPS:
                raise ExpressionError("不支援的運算子")
            self._check(node.left, None)
            self._check(node.right, None)
        elif isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
                raise ExpressionError("不支援的運算子")
            self._check(node.operand, None)
        elif isinstance(node, ast.BoolOp):
            for v in node.values:
                self._check(v, None)
        elif isinstance(node, ast.Compare):
            if any(type(op) not in _CMP_OPS for op in node.ops):
                raise ExpressionError("不支援的比較運算子")
            self._check(node.left, None)
            for c in node.comparators:
                self._check(c, None)
        elif isinstance(node, ast.IfExp):
            for n in (node.test, node.body, node.orelse):
                self._check(n, None)
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", None)
            if not isinstance(node.func, ast.Name) or name not in ALL_FUNCTIONS:
                raise ExpressionError(f"不支援的函式「{name or '?'}」，可用：{', '.join(sorted(ALL_FUNCTIONS))}")
            if node.keywords:
                raise ExpressionError("函式不支援具名參數")
            n = len(node.args)
            if name in TIME_FUNCTIONS:
                if n:
                    raise ExpressionError(f"{name}() 不需要參數")
                self.uses_time = True
            elif name == "isgood" and n != 1 or name == "valueor" and n != 2 or name == "coalesce" and n < 1:
                raise ExpressionError(f"{name} 的參數數量不對")
            elif name in STATEFUL:
                lo, hi, needs_ref = STATEFUL[name]
                if not lo <= n <= hi:
                    raise ExpressionError(f"{name} 需要 {lo}~{hi} 個參數")
                if needs_ref and not (isinstance(node.args[0], ast.Name) and node.args[0].id in self._names):
                    raise ExpressionError(f"{name} 的第一個參數必須直接是感測器，例如 {name}({{KWH}}, 'day')")
                if name in ("integral", "ontime", "count", "delta") and n >= 2 and not (
                        isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)):
                    raise ExpressionError(f"{name} 的第二個參數是週期文字，例如 'day'")
                if name in ("movavg", "movmin", "movmax", "filter") and not (
                        isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, (int, float))
                        and 0 < node.args[1].value <= MAX_WINDOW_SECONDS):
                    raise ExpressionError(f"{name} 的秒數必須是 1~{MAX_WINDOW_SECONDS} 的數字")
                self.stateful = True
                self._call_ids[id(node)] = f"{name}#{len(self._call_ids)}"
            for a in node.args:
                self._check(a, name)
        else:
            raise ExpressionError(f"不支援的語法：{type(node).__name__}")

    # ------------------------------------------------------------------
    def evaluate(self, values: dict, ctx: EvalContext | None = None) -> float:
        """
        values: {感測器編號: 數值 或 MISSING}。結果一律轉成 float（布林 → 1.0 / 0.0）。
        沒有給 ctx 時用一次性的環境（現在時間、空狀態），適合試算與單元測試。
        """
        ctx = ctx or EvalContext(now=datetime.now().astimezone(), trial=True)
        if ctx.state.get("__sig") != self.signature:
            ctx.state.clear()          # 運算式改過：舊的累計狀態不再適用
            ctx.state["__sig"] = self.signature
        self.accessed = set()
        env = {}
        for i, code in enumerate(self.refs):
            v = values.get(code, MISSING)
            env[f"__v{i}"] = v if v is MISSING else float(v)
        self._ctx = ctx
        try:
            result = self._eval(self._tree.body, env)
        except MissingInput:
            raise
        except ZeroDivisionError:
            raise ExpressionError("除以零")
        except (ValueError, OverflowError, statistics.StatisticsError) as e:
            raise ExpressionError(f"計算錯誤：{e}")
        except TypeError as e:
            raise ExpressionError(f"函式參數錯誤：{e}")
        finally:
            self._ctx = None
        if result is MISSING:
            raise MissingInput(self.refs[0] if self.refs else "?")
        result = float(result)
        if math.isnan(result) or math.isinf(result):
            raise ExpressionError("計算結果不是有效數字")
        return result

    def _ref_of(self, node):
        return self.refs[int(node.id[3:])] if isinstance(node, ast.Name) and node.id in self._names else None

    def _need(self, v, node):
        if v is MISSING:
            raise MissingInput(self._ref_of(node) or "輸入")
        return v

    def _eval(self, node, env, allow_missing=False):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in env:
                v = env[node.id]
                if v is not MISSING:
                    self.accessed.add(self._ref_of(node))
                return v if allow_missing else self._need(v, node)
            return CONSTANTS[node.id]
        if isinstance(node, ast.BinOp):
            left, right = self._eval(node.left, env), self._eval(node.right, env)
            if isinstance(node.op, ast.Pow):
                if abs(right) > MAX_EXPONENT:
                    raise ValueError(f"次方的指數超過 {MAX_EXPONENT}")
                return left ** right
            return _BIN_OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp):
            v = self._eval(node.operand, env)
            if isinstance(node.op, ast.USub):
                return -v
            if isinstance(node.op, ast.Not):
                return not v
            return +v
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return all(self._eval(v, env) for v in node.values)
            return any(self._eval(v, env) for v in node.values)
        if isinstance(node, ast.Compare):
            left = self._eval(node.left, env)
            for op, comp in zip(node.ops, node.comparators):
                right = self._eval(comp, env)
                if not _CMP_OPS[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return self._eval(node.body, env) if self._eval(node.test, env) else self._eval(node.orelse, env)
        if isinstance(node, ast.Call):
            return self._call(node, env)
        raise ExpressionError(f"不支援的語法：{type(node).__name__}")

    def _call(self, node, env):
        name = node.func.id
        if name == "isgood":
            return self._eval(node.args[0], env, allow_missing=True) is not MISSING
        if name == "valueor":
            v = self._eval(node.args[0], env, allow_missing=True)
            return self._eval(node.args[1], env) if v is MISSING else v
        if name == "coalesce":
            for a in node.args:
                v = self._eval(a, env, allow_missing=True)
                if v is not MISSING:
                    return v
            raise MissingInput(" / ".join(filter(None, (self._ref_of(a) for a in node.args))) or "全部輸入")
        if name in TIME_FUNCTIONS:
            now = self._ctx.now
            return {"hour": now.hour, "minute": now.minute, "weekday": now.isoweekday(), "day": now.day,
                    "month": now.month, "year": now.year,
                    "timeofday": now.hour + now.minute / 60 + now.second / 3600}[name]
        if name in STATEFUL:
            return self._stateful(name, node, env)
        return FUNCTIONS[name](*[self._eval(a, env) for a in node.args])

    # ------------------------------------------------------------------
    # 有狀態的函式
    # ------------------------------------------------------------------
    def _stateful(self, name, node, env):
        ctx = self._ctx
        key = self._call_ids[id(node)]
        st = ctx.state.setdefault(key, {})
        now = ctx.now
        ts = now.timestamp()
        args = node.args
        x = self._eval(args[0], env)

        def period_args():
            period = args[1].value if len(args) >= 2 else "day"
            start_hour = float(self._eval(args[2], env)) if len(args) >= 3 else 0.0
            if not 0 <= start_hour < 24:
                raise ValueError("起始小時必須是 0~23.99")
            return period, start_hour

        def reset_if_new_period(period, start_hour):
            start = period_start(now, period, start_hour)
            marker = start.isoformat() if start else "never"
            if st.get("period") != marker:
                st["period"] = marker
                return True, start
            return False, start

        if name == "prev":
            out = st.get("v", x)
            st["v"] = x
            return out
        if name == "changed":
            out = "v" in st and st["v"] != x
            st["v"] = x
            return out
        if name == "rising":
            out = bool(x) and not st.get("v", bool(x))
            st["v"] = bool(x)
            return out
        if name == "hold":
            if self._eval(args[1], env) or "v" not in st:
                st["v"] = x
            return st["v"]
        if name == "derivative":
            out = 0.0
            if "v" in st and ts > st["t"]:
                out = (x - st["v"]) / (ts - st["t"])
            st.update(v=x, t=ts)
            return out
        if name == "filter":
            tau = float(args[1].value)
            if "v" not in st:
                st.update(v=x, t=ts)
                return x
            dt = max(ts - st["t"], 0)
            alpha = dt / (tau + dt) if tau + dt > 0 else 1
            st["v"] = st["v"] + alpha * (x - st["v"])
            st["t"] = ts
            return st["v"]
        if name in ("movavg", "movmin", "movmax"):
            window = float(args[1].value)
            buf = st.setdefault("_mem_buf", [])          # _mem_ 開頭的狀態不寫入資料庫
            buf.append((ts, x))
            while buf and ts - buf[0][0] > window:
                buf.pop(0)
            vals = [v for _, v in buf]
            return {"movavg": statistics.fmean, "movmin": min, "movmax": max}[name](vals)
        if name == "integral":
            period, start_hour = period_args()
            new_period, _ = reset_if_new_period(period, start_hour)
            if new_period:
                st["acc"] = 0.0
            acc = st.get("acc", 0.0)
            if "v" in st and 0 < ts - st["t"] <= INTEGRAL_MAX_GAP and not new_period:
                acc += (st["v"] + x) / 2 * (ts - st["t"]) / 3600.0
            st.update(acc=acc, v=x, t=ts)
            return acc
        if name == "ontime":
            period, start_hour = period_args()
            new_period, _ = reset_if_new_period(period, start_hour)
            if new_period:
                st["acc"] = 0.0
            acc = st.get("acc", 0.0)
            if st.get("c") and "t" in st and 0 < ts - st["t"] <= INTEGRAL_MAX_GAP and not new_period:
                acc += (ts - st["t"]) / 3600.0
            st.update(acc=acc, c=bool(x), t=ts)
            return acc
        if name == "count":
            period, start_hour = period_args()
            new_period, _ = reset_if_new_period(period, start_hour)
            if new_period:
                st["n"] = 0
            if bool(x) and not st.get("c", bool(x)):
                st["n"] = st.get("n", 0) + 1
            st["c"] = bool(x)
            return st.get("n", 0)
        if name == "delta":
            period, start_hour = period_args()
            new_period, start = reset_if_new_period(period, start_hour)
            if new_period or "base" not in st:
                base = None
                if start is not None and ctx.baseline is not None:
                    base = ctx.baseline(self._ref_of(args[0]), start)
                # 查不到歷史（新感測器、試算）：以目前值當基準，從 0 開始
                st["base"] = x if base is None else float(base)
            return x - st["base"]
        raise ExpressionError(f"不支援的函式：{name}")


def persistable_state(state: dict) -> dict:
    """存進資料庫的狀態：去掉 _mem_ 開頭（移動視窗等只存記憶體）的欄位。"""
    out = {}
    for k, v in state.items():
        if isinstance(v, dict):
            out[k] = {kk: vv for kk, vv in v.items() if not kk.startswith("_mem_")}
        else:
            out[k] = v
    return out


def evaluation_order(dependencies: dict) -> tuple[list, set]:
    """
    計算點可以引用其他計算點。dependencies: {目標編號: {引用的編號}}。
    回傳 (計算順序, 有循環引用的目標)。只會排序 dependencies 裡的目標，其他編號視為一般感測器。
    """
    order, done, cyclic = [], set(), set()
    visiting = set()

    def visit(node, path):
        if node in done or node in cyclic:
            return node not in cyclic
        if node in visiting:
            cyclic.update(path[path.index(node):])
            return False
        visiting.add(node)
        ok = True
        for dep in dependencies.get(node, ()):
            if dep in dependencies and not visit(dep, path + [dep]):
                ok = False
        visiting.discard(node)
        if ok:
            done.add(node)
            order.append(node)
        else:
            cyclic.add(node)
        return ok

    for target in dependencies:
        visit(target, [target])
    return order, cyclic
