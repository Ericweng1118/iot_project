"""
protocols/modbus_codec.py
=========================
Modbus 暫存器的編解碼（純函式，不碰網路，可單元測試）。採集程式、點位設定頁、
線上調適工具都用這一份，避免「調適工具解出來的值跟採集程式不一樣」。

位元組順序（Byte Order / Word Order）：
    byte_order  單一暫存器（16-bit）內兩個 byte 的順序，Modbus 標準是 BIG
    word_order  多個暫存器之間的順序，LITTLE 代表 word swap

    | 俗稱 | byte_order | word_order | 32-bit 排列 |
    | ABCD | BIG        | BIG        | Modbus 官方標準 |
    | CDAB | BIG        | LITTLE     | 台灣電表最常見 |
    | BADC | LITTLE     | BIG        | |
    | DCBA | LITTLE     | LITTLE     | |

位址表示法：
    協議層的位址從 0 開始（本系統 start_address 存的就是這個）。設備手冊常用
    Modicon 表示法（40001 = Holding Register 第 0 號），parse_modicon_address() 負責轉換。
"""

import math
import struct

# 型態名稱 -> (暫存器數, struct 格式)。別名對應到標準名稱，相容 v1/v2 點位表裡的寫法。
DATA_TYPES = {
    "bool": (1, None),
    "uint16": (1, "H"),
    "int16": (1, "h"),
    "uint32": (2, "I"),
    "int32": (2, "i"),
    "float32": (2, "f"),
    "uint64": (4, "Q"),
    "int64": (4, "q"),
    "float64": (4, "d"),
}
ALIASES = {
    "word": "uint16", "int": "int16", "dint": "int32", "float": "float32",
    "real": "float32", "double": "float64",
}
TYPE_LABELS = {
    "bool": "BOOL（線圈 / 0 與非 0）",
    "uint16": "UINT16（WORD）", "int16": "INT16（INT）",
    "uint32": "UINT32", "int32": "INT32（DINT）", "float32": "FLOAT32（REAL）",
    "uint64": "UINT64", "int64": "INT64", "float64": "FLOAT64（DOUBLE）",
}

ORDER_PRESETS = {
    "ABCD": ("BIG", "BIG"),
    "CDAB": ("BIG", "LITTLE"),
    "BADC": ("LITTLE", "BIG"),
    "DCBA": ("LITTLE", "LITTLE"),
}

FUNCTION_CODES = {
    1: "FC01 讀線圈（Coils，0xxxx）",
    2: "FC02 讀離散輸入（Discrete Inputs，1xxxx）",
    3: "FC03 讀保持暫存器（Holding Registers，4xxxx）",
    4: "FC04 讀輸入暫存器（Input Registers，3xxxx）",
}
BIT_FUNCTIONS = (1, 2)

# 設備回應的 Modbus 例外碼，翻成現場看得懂的說明
EXCEPTION_CODES = {
    1: "不支援的功能碼（Illegal Function）：設備不支援這個 FC，換 FC03 / FC04 試試",
    2: "位址不存在（Illegal Data Address）：起始位址或數量超出設備範圍，檢查是否差 1（40001 = 位址 0）",
    3: "資料值不合法（Illegal Data Value）：讀取數量太多或寫入值超出範圍",
    4: "設備內部錯誤（Slave Device Failure）",
    5: "設備忙碌中，已接受請求（Acknowledge）",
    6: "設備忙碌（Slave Device Busy），稍後重試",
    8: "記憶體同位檢查錯誤（Memory Parity Error）",
    10: "閘道無可用路徑（Gateway Path Unavailable）：閘道設定的站號 / 序列埠不對",
    11: "閘道後方設備無回應（Gateway Target Failed to Respond）：站號錯誤、RS-485 接線或鮑率不對",
}


def normalize_type(data_type: str) -> str:
    dt = str(data_type or "").strip().lower()
    return ALIASES.get(dt, dt)


def register_count(data_type: str) -> int:
    return DATA_TYPES.get(normalize_type(data_type), (1, None))[0]


def order_name(byte_order: str, word_order: str) -> str:
    key = (str(byte_order or "BIG").upper(), str(word_order or "BIG").upper())
    return next((name for name, v in ORDER_PRESETS.items() if v == key), "ABCD")


def _to_bytes(registers, byte_order="BIG", word_order="BIG") -> bytes:
    regs = list(registers)
    if str(word_order or "BIG").upper() == "LITTLE":
        regs = regs[::-1]
    fmt = "<H" if str(byte_order or "BIG").upper() == "LITTLE" else ">H"
    return b"".join(struct.pack(fmt, int(r) & 0xFFFF) for r in regs)


def decode(registers, data_type: str, byte_order: str = "BIG", word_order: str = "BIG"):
    """
    暫存器（或 FC01/02 的位元）解碼成 float；資料不足或型態不明時回傳 None。
    與 v2 collector 的 parse_registers() 行為一致。
    """
    if not registers:
        return None
    dt = normalize_type(data_type)
    if isinstance(registers[0], bool):
        return 1.0 if registers[0] else 0.0
    if dt == "bool":
        return 1.0 if int(registers[0]) != 0 else 0.0
    if dt not in DATA_TYPES:
        return None
    count, fmt = DATA_TYPES[dt]
    if len(registers) < count:
        return None
    raw = _to_bytes(registers[:count], byte_order, word_order)
    return float(struct.unpack(">" + fmt, raw)[0])


def encode(value, data_type: str, byte_order: str = "BIG", word_order: str = "BIG") -> list:
    """數值編碼成暫存器清單（寫入用），decode() 的反運算。"""
    dt = normalize_type(data_type)
    if dt == "bool":
        return [1 if float(value) else 0]
    count, fmt = DATA_TYPES[dt]
    if fmt in ("f", "d"):
        raw = struct.pack(">" + fmt, float(value))
    else:
        raw = struct.pack(">" + fmt, int(round(float(value))))
    regs = [struct.unpack(">H", raw[i:i + 2])[0] for i in range(0, len(raw), 2)]
    if str(byte_order or "BIG").upper() == "LITTLE":
        regs = [((r & 0xFF) << 8) | (r >> 8) for r in regs]
    if str(word_order or "BIG").upper() == "LITTLE":
        regs = regs[::-1]
    return regs


def apply_linear_scaling(val, raw_min, raw_max, eng_min, eng_max):
    """原始值 → 工程值（線性換算，超出原始範圍時夾在邊界）。任一參數缺漏時原值返回。"""
    if val is None:
        return None
    if None in (raw_min, raw_max, eng_min, eng_max):
        return val
    if any(isinstance(x, float) and math.isnan(x) for x in (raw_min, raw_max, eng_min, eng_max)):
        return val
    if raw_max == raw_min:
        return val
    val = max(min(val, raw_max), raw_min)
    return ((val - raw_min) / (raw_max - raw_min)) * (eng_max - eng_min) + eng_min


def is_plausible(value) -> bool:
    """
    調適工具用來「猜」哪一種解碼比較像真的量測值：有限數字、量級合理（不是 1e-38 或 1e+30
    這種位元組順序錯誤時常見的極端值）。只是提示，不是判定。
    """
    if value is None or isinstance(value, bool):
        return False
    if math.isnan(value) or math.isinf(value):
        return False
    if value == 0:
        return True
    return 1e-4 <= abs(value) <= 1e9


def interpretations(registers, data_type: str = "float32"):
    """
    對一串暫存器，從每個起點用四種位元組順序解碼，給調適工具找出正確設定。
    回傳 [{"offset": i, "ABCD": v, "CDAB": v, "BADC": v, "DCBA": v}, ...]
    """
    dt = normalize_type(data_type)
    count = register_count(dt)
    rows = []
    for i in range(0, max(len(registers) - count + 1, 0)):
        chunk = registers[i:i + count]
        row = {"offset": i}
        for name, (bo, wo) in ORDER_PRESETS.items():
            row[name] = decode(chunk, dt, bo, wo)
        rows.append(row)
    return rows


def parse_modicon_address(text: str):
    """
    "40001" / "400001" / "30010" / "00005" / "10001" → (function_code, 0 起算位址)。
    純數字且小於 10000 視為已經是協議位址（0 起算），回傳 (None, 位址)。
    無法解析時回傳 (None, None)。
    """
    s = str(text or "").strip()
    if not s.isdigit():
        return None, None
    n = int(s)
    if len(s) in (5, 6) and s[0] in "0134" and n >= 1:
        prefix = int(s[0])
        number = int(s[1:])
        if number < 1:
            return None, None
        fc = {0: 1, 1: 2, 3: 4, 4: 3}[prefix]
        return fc, number - 1
    return None, n


def modicon_address(function_code: int, address: int) -> str:
    """協議位址 → Modicon 表示法（給畫面對照用），例如 (3, 0) → 40001。"""
    prefix = {1: 0, 2: 1, 3: 4, 4: 3}.get(int(function_code))
    if prefix is None:
        return str(address)
    return f"{prefix}{int(address) + 1:05d}" if address + 1 > 9999 else f"{prefix}{int(address) + 1:04d}"


def parse_serial_settings(text: str):
    """
    "9600,8,N,1" → dict(baudrate=9600, bytesize=8, parity="N", stopbits=1)。
    空字串回傳預設 9600,8,N,1；格式錯誤丟 ValueError。
    """
    s = str(text or "").strip() or "9600,8,N,1"
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 4:
        raise ValueError(f"序列埠參數格式應為 鮑率,資料位元,同位,停止位元（例如 9600,8,N,1），收到：{text}")
    baud, bits, parity, stop = parts
    parity = parity.upper()
    if parity not in ("N", "E", "O"):
        raise ValueError(f"同位檢查只能是 N / E / O，收到：{parity}")
    return {"baudrate": int(baud), "bytesize": int(bits), "parity": parity, "stopbits": int(stop)}


def describe_exception(code) -> str:
    try:
        code = int(code)
    except (TypeError, ValueError):
        return str(code)
    return f"例外碼 {code:02d}：" + EXCEPTION_CODES.get(code, "未知的例外碼")
