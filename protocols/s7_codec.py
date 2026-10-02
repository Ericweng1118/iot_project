"""
protocols/s7_codec.py
=====================
Siemens S7 資料的編解碼與位址表示法（純函式，不碰網路，可單元測試）。
採集程式、S7 點位設定頁、S7 線上調適工具共用，保證「調適工具看到的值 = 採集到的值」。

S7 全系列都是 Big-Endian，不需要像 Modbus 那樣處理位元組順序。

資料型態（TIA Portal 名稱）與大小：
    BOOL(1 bit) BYTE/USINT/SINT/CHAR(1) WORD/INT/UINT(2) DWORD/DINT/UDINT/REAL(4)
    LWORD/LINT/ULINT/LREAL(8) STRING[n](n+2，預設 n=254)

位址表示法（TIA Portal / STEP 7，% 可有可無，大小寫不拘）：
    DB1.DBX10.3   DB1 第 10 byte 第 3 bit       DB1.DBB10 / DBW10 / DBD10  位元組 / 字 / 雙字
    M10.3  MB10  MW10  MD10                     內部旗標（Merker）
    I0.1   IB0   IW64  ID0  （德文 E 也可以）   輸入（Process Input）
    Q0.1   QB0   QW64  QD0  （德文 A 也可以）   輸出（Process Output）
"""

import re
import struct

AREAS = {"DB": "資料塊（DB）", "M": "內部旗標（M）", "I": "輸入（I）", "Q": "輸出（Q）"}

# 型態 -> (位元組數, struct 格式)
TYPES = {
    "BOOL": (1, None),
    "BYTE": (1, ">B"), "USINT": (1, ">B"), "SINT": (1, ">b"), "CHAR": (1, None),
    "WORD": (2, ">H"), "UINT": (2, ">H"), "INT": (2, ">h"),
    "DWORD": (4, ">I"), "UDINT": (4, ">I"), "DINT": (4, ">i"), "REAL": (4, ">f"),
    "LWORD": (8, ">Q"), "ULINT": (8, ">Q"), "LINT": (8, ">q"), "LREAL": (8, ">d"),
    "STRING": (256, None),
}
NUMERIC_TYPES = [t for t in TYPES if t not in ("CHAR", "STRING")]
_STRING_RE = re.compile(r"^STRING\s*\[\s*(\d+)\s*\]$")

# snap7 / CPU 常見錯誤 → 現場看得懂的說明
ERROR_HINTS = [
    ("function refused by cpu", "CPU 拒絕存取：S7-1200/1500 請在 TIA Portal「防護與安全 → 連線機制」勾選"
                                "「允許來自遠端物件的 PUT/GET 通訊存取」，並下載到 PLC"),
    ("address out of range", "位址超出範圍：DB 不存在或長度不夠；S7-1200/1500 的 DB 必須取消「最佳化的區塊存取」"
                             "（DB 屬性 → 屬性），否則沒有固定的位元組位址"),
    ("item not available", "DB 不存在，或是「最佳化的區塊存取」DB（請在 DB 屬性取消勾選後重新下載）"),
    ("connection timed out", "連線逾時：IP 錯誤、PLC 未上電、防火牆或網路不通"),
    ("connection refused", "連線被拒：該 IP 沒有 S7 服務（TCP 102），確認是 PLC 的 IP"),
    ("iso : an error occurred during recv", "通訊中斷：網路不穩或 PLC 主動斷線"),
    ("cpu : invalid rack/slot", "Rack / Slot 錯誤：S7-1200/1500 通常是 0 / 1，S7-300 是 0 / 2"),
    ("connection failed", "連線失敗：確認 IP、Rack / Slot（S7-1200/1500 = 0/1、S7-300 = 0/2）"),
]


def explain_error(message: str) -> str:
    text = str(message)
    low = text.lower()
    for key, hint in ERROR_HINTS:
        if key in low:
            return f"{hint}（{text.strip()}）"
    return text


def normalize_type(data_type: str) -> str:
    dt = str(data_type or "").strip().upper()
    return "STRING" if dt.startswith("STRING") else dt


def type_size(data_type: str) -> int:
    dt = str(data_type or "").strip().upper()
    m = _STRING_RE.match(dt)
    if m:
        return int(m.group(1)) + 2
    if dt not in TYPES:
        raise ValueError(f"不支援的 S7 資料型態：{data_type}")
    return TYPES[dt][0]


def decode(buffer, offset: int, data_type: str, bit: int = 0):
    """從 buffer 的 offset 解出值；數值型態回傳 float / int / bool，CHAR / STRING 回傳字串。"""
    dt = normalize_type(data_type)
    size = type_size(data_type)
    if offset < 0 or offset + (1 if dt == "BOOL" else size if dt != "STRING" else 2) > len(buffer):
        raise ValueError(f"資料不足：{data_type} 需要從第 {offset} byte 起 {size} bytes，只讀到 {len(buffer)} bytes")
    if dt == "BOOL":
        if not 0 <= int(bit) <= 7:
            raise ValueError("BOOL 的 bit 必須是 0~7")
        return bool(buffer[offset] >> int(bit) & 1)
    if dt == "CHAR":
        return chr(buffer[offset])
    if dt == "STRING":
        max_len, cur_len = buffer[offset], buffer[offset + 1]
        cur_len = min(cur_len, max_len, len(buffer) - offset - 2)
        return bytes(buffer[offset + 2: offset + 2 + cur_len]).decode("latin-1")
    return struct.unpack_from(TYPES[dt][1], bytes(buffer), offset)[0]


def encode(value, data_type: str) -> bytes:
    """數值編碼成 bytes（寫入用；BOOL 請用 encode_bool 修改單一位元）。"""
    dt = normalize_type(data_type)
    if dt in ("BOOL", "CHAR", "STRING"):
        raise ValueError(f"{dt} 不能用 encode()，BOOL 請用 encode_bool()")
    fmt = TYPES[dt][1]
    if fmt in (">f", ">d"):
        return struct.pack(fmt, float(value))
    return struct.pack(fmt, int(round(float(value))))


def encode_bool(current_byte: int, bit: int, value: bool) -> bytes:
    """修改單一位元：讀出原本的 byte、只改這一個 bit，避免覆蓋同一個 byte 的其他 7 個訊號。"""
    mask = 1 << int(bit)
    return bytes([(current_byte | mask) if value else (current_byte & ~mask & 0xFF)])


def interpretations(buffer, start: int = 0):
    """調適工具用：每個位元組起點用各種型態解碼（越界的欄位留空）。"""
    rows = []
    for i in range(len(buffer)):
        row = {"位址": start + i, "HEX": f"{buffer[i]:02X}", "BYTE": buffer[i],
               "位元 7..0": f"{buffer[i]:08b}"}
        for dt in ("INT", "WORD", "DINT", "DWORD", "REAL", "LREAL"):
            size = TYPES[dt][0]
            row[dt] = decode(buffer, i, dt) if i + size <= len(buffer) else None
        rows.append(row)
    return rows


_ADDR_RE = re.compile(
    r"^%?(?:DB(?P<db>\d+)\.DB(?P<dbw>[XBWD])(?P<dbbyte>\d+)(?:\.(?P<dbbit>\d))?"
    r"|(?P<area>[MIEQA])(?P<w>[BWD]?)(?P<byte>\d+)(?:\.(?P<bit>\d))?)$",
    re.IGNORECASE,
)
_WIDTH = {"X": 0, "B": 1, "W": 2, "D": 4, "": 0}
_AREA_ALIAS = {"M": "M", "I": "I", "E": "I", "Q": "Q", "A": "Q"}


def parse_address(text: str) -> dict:
    """
    "DB1.DBD4" → {"area": "DB", "db": 1, "byte": 4, "bit": 0, "width": 4}
    "M10.3"    → {"area": "M", "db": 0, "byte": 10, "bit": 3, "width": 0}（width 0 = 位元）
    格式錯誤丟 ValueError。
    """
    s = str(text or "").strip().replace(" ", "")
    m = _ADDR_RE.match(s)
    if not m:
        raise ValueError(f"看不懂的 S7 位址「{text}」，例如 DB1.DBD4、DB1.DBX0.3、MW10、I0.1、QB4")
    if m.group("db") is not None:
        width = _WIDTH[m.group("dbw").upper()]
        bit = m.group("dbbit")
        if width == 0 and bit is None:
            raise ValueError("DBX 位址要加位元，例如 DB1.DBX0.3")
        if width != 0 and bit is not None:
            raise ValueError(f"DB{m.group('dbw').upper()} 位址不能帶位元")
        out = {"area": "DB", "db": int(m.group("db")), "byte": int(m.group("dbbyte")),
               "bit": int(bit or 0), "width": width}
    else:
        width = _WIDTH[m.group("w").upper()]
        bit = m.group("bit")
        if width == 0 and bit is None:
            raise ValueError(f"位元位址要加位元，例如 {m.group('area').upper()}0.1")
        if width != 0 and bit is not None:
            raise ValueError("位元組 / 字 / 雙字位址不能帶位元")
        out = {"area": _AREA_ALIAS[m.group("area").upper()], "db": 0, "byte": int(m.group("byte")),
               "bit": int(bit or 0), "width": width}
    if out["bit"] > 7:
        raise ValueError("位元必須是 0~7")
    return out


def format_address(area: str, db: int, byte: int, data_type: str, bit: int = 0) -> str:
    """反過來：產生 TIA 表示法，例如 ("DB", 1, 4, "REAL") → DB1.DBD4。"""
    dt = normalize_type(data_type)
    area = (area or "DB").upper()
    if dt == "BOOL":
        return f"DB{db}.DBX{byte}.{bit}" if area == "DB" else f"{area}{byte}.{bit}"
    size = type_size(data_type) if dt != "STRING" else 1
    letter = {1: "B", 2: "W", 4: "D"}.get(size, "B")
    if area == "DB":
        return f"DB{db}.DB{letter}{byte}" + (f"（{dt}）" if size == 8 or dt == "STRING" else "")
    return f"{area}{letter}{byte}" + (f"（{dt}）" if size == 8 or dt == "STRING" else "")


def suggest_type(width: int) -> str:
    return {0: "BOOL", 1: "BYTE", 2: "INT", 4: "REAL"}.get(width, "INT")
