"""S7 編解碼、位址表示法與區塊規劃（protocols/s7_codec.py、collector/run_s7_collector.py）。"""

import struct

import pytest

from collector.run_s7_collector import plan_s7_blocks
from protocols import s7_codec as C


def test_type_sizes():
    assert C.type_size("REAL") == 4 and C.type_size("lreal") == 8 and C.type_size("INT") == 2
    assert C.type_size("STRING") == 256 and C.type_size("STRING[20]") == 22
    with pytest.raises(ValueError):
        C.type_size("FOO")


def test_decode_numbers_bits_and_strings():
    buf = bytearray(40)
    struct.pack_into(">f", buf, 0, 70.5)
    struct.pack_into(">h", buf, 4, -12)
    struct.pack_into(">d", buf, 6, 12345.678)
    buf[14] = 0b00001001
    buf[20], buf[21] = 10, 3
    buf[22:25] = b"ABC"
    assert C.decode(buf, 0, "REAL") == pytest.approx(70.5)
    assert C.decode(buf, 4, "INT") == -12 and C.decode(buf, 4, "WORD") == 65524
    assert C.decode(buf, 6, "LREAL") == pytest.approx(12345.678)
    assert C.decode(buf, 14, "BOOL", 0) is True and C.decode(buf, 14, "BOOL", 3) is True
    assert C.decode(buf, 14, "BOOL", 1) is False
    assert C.decode(buf, 20, "STRING[10]") == "ABC"
    with pytest.raises(ValueError, match="資料不足"):
        C.decode(buf, 36, "LREAL")
    with pytest.raises(ValueError):
        C.decode(buf, 0, "BOOL", 8)


@pytest.mark.parametrize("dt,value", [("INT", -300), ("DINT", 123456), ("REAL", 1.5), ("LREAL", -2.25),
                                      ("WORD", 65000), ("BYTE", 200), ("UDINT", 4_000_000_000)])
def test_encode_roundtrip(dt, value):
    assert C.decode(C.encode(value, dt), 0, dt) == pytest.approx(value)


def test_encode_bool_only_changes_one_bit():
    assert C.encode_bool(0b10100000, 3, True) == bytes([0b10101000])
    assert C.encode_bool(0b10101000, 5, False) == bytes([0b10001000])


@pytest.mark.parametrize("text,expected", [
    ("DB1.DBD4", {"area": "DB", "db": 1, "byte": 4, "bit": 0, "width": 4}),
    ("%DB10.DBX2.7", {"area": "DB", "db": 10, "byte": 2, "bit": 7, "width": 0}),
    ("db3.dbw100", {"area": "DB", "db": 3, "byte": 100, "bit": 0, "width": 2}),
    ("MW20", {"area": "M", "db": 0, "byte": 20, "bit": 0, "width": 2}),
    ("%M10.3", {"area": "M", "db": 0, "byte": 10, "bit": 3, "width": 0}),
    ("I0.1", {"area": "I", "db": 0, "byte": 0, "bit": 1, "width": 0}),
    ("E0.1", {"area": "I", "db": 0, "byte": 0, "bit": 1, "width": 0}),
    ("%IW64", {"area": "I", "db": 0, "byte": 64, "bit": 0, "width": 2}),
    ("QB4", {"area": "Q", "db": 0, "byte": 4, "bit": 0, "width": 1}),
    ("A1.0", {"area": "Q", "db": 0, "byte": 1, "bit": 0, "width": 0}),
])
def test_parse_address(text, expected):
    assert C.parse_address(text) == expected


@pytest.mark.parametrize("text", ["DB1.DBX4", "MW10.1", "DB1.DBD4.2", "X10", "M10.8", ""])
def test_parse_address_rejects(text):
    with pytest.raises(ValueError):
        C.parse_address(text)


def test_format_address():
    assert C.format_address("DB", 1, 4, "REAL") == "DB1.DBD4"
    assert C.format_address("DB", 1, 10, "BOOL", 3) == "DB1.DBX10.3"
    assert C.format_address("M", 0, 20, "INT") == "MW20"
    assert C.format_address("I", 0, 0, "BOOL", 1) == "I0.1"


def test_explain_error():
    assert "PUT/GET" in C.explain_error("CLI : function refused by CPU (Unknown error)")
    assert "最佳化" in C.explain_error("CPU : Address out of range")
    assert C.explain_error("something else") == "something else"


def t(i, off, dt="REAL", area="DB", db=1, bit=0):
    return {"id": i, "offset": off, "data_type": dt, "area": area, "db_number": db, "bit_offset": bit}


def test_plan_blocks_merge_split_and_sizes():
    tags = [t(1, 0), t(2, 4, "INT"), t(3, 6, "LREAL"), t(4, 14, "BOOL", bit=3), t(5, 250),
            t(6, 20, "INT", area="M", db=0), t(7, 0, db=2)]
    blocks = plan_s7_blocks(tags, max_gap=32, max_block=400)
    summary = sorted((b["area"], b["db"], b["start"], b["size"]) for b in blocks)
    assert ("DB", 1, 0, 15) in summary          # 0~14：LREAL 讀滿 8 bytes、BOOL 佔 1 byte
    assert ("DB", 1, 250, 4) in summary         # 相隔太遠另成一塊
    assert ("M", 0, 20, 2) in summary and ("DB", 2, 0, 4) in summary
    offsets = {tag["id"]: rel for b in blocks for tag, rel in b["items"]}
    assert offsets[3] == 6 and offsets[4] == 14


def test_plan_blocks_isolate_is_barrier_and_max_block():
    blocks = plan_s7_blocks([t(1, 0), t(2, 8), t(3, 16)], max_gap=32, isolate={2})
    assert sorted((b["start"], b["size"]) for b in blocks) == [(0, 4), (8, 4), (16, 4)]
    big = plan_s7_blocks([t(i, i * 4) for i in range(100)], max_gap=0, max_block=40)
    assert all(b["size"] <= 40 for b in big) and sum(len(b["items"]) for b in big) == 100
