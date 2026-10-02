"""Modbus 編解碼與批次讀取規劃（protocols/modbus_codec.py、collector/modbus_blocks.py）。"""

import math

import pytest

from collector.modbus_blocks import plan_blocks
from protocols.modbus_codec import (
    apply_linear_scaling,
    decode,
    encode,
    interpretations,
    is_plausible,
    modicon_address,
    order_name,
    parse_modicon_address,
    parse_serial_settings,
    register_count,
)

ORDERS = [("BIG", "BIG"), ("BIG", "LITTLE"), ("LITTLE", "BIG"), ("LITTLE", "LITTLE")]


@pytest.mark.parametrize("bo,wo", ORDERS)
@pytest.mark.parametrize("dt,value", [
    ("float32", 123.5), ("int32", -123456), ("uint32", 3_000_000_000), ("int16", -42),
    ("uint16", 65000), ("float64", -1.25e10), ("int64", -9_000_000_000), ("uint64", 2**63 + 5),
])
def test_encode_decode_roundtrip(dt, value, bo, wo):
    regs = encode(value, dt, bo, wo)
    assert len(regs) == register_count(dt)
    assert decode(regs, dt, bo, wo) == pytest.approx(value)


def test_cdab_matches_known_meter_layout():
    # 123.5 的 IEEE754 = 0x42F7 0x0000；CDAB（台灣電表常見）低位字在前
    assert decode([0x0000, 0x42F7], "float", "BIG", "LITTLE") == pytest.approx(123.5)
    assert decode([0x42F7, 0x0000], "float32", "BIG", "BIG") == pytest.approx(123.5)


def test_aliases_and_bits():
    assert register_count("dint") == 2 and register_count("double") == 4 and register_count("word") == 1
    assert decode([True], "bool") == 1.0
    assert decode([0], "bool") == 0.0
    assert decode([5], "unknown_type") is None
    assert decode([1], "float32") is None            # 暫存器不足


def test_interpretations_and_plausibility():
    rows = interpretations([0x0000, 0x42F7, 0x0000], "float32")
    assert len(rows) == 2
    assert rows[0]["CDAB"] == pytest.approx(123.5) and is_plausible(rows[0]["CDAB"])
    assert not is_plausible(rows[0]["ABCD"]) or rows[0]["ABCD"] == 0
    assert not is_plausible(float("nan")) and not is_plausible(1e30) and is_plausible(0.0)


def test_modicon_addresses():
    assert parse_modicon_address("40001") == (3, 0)
    assert parse_modicon_address("400100") == (3, 99)
    assert parse_modicon_address("30010") == (4, 9)
    assert parse_modicon_address("00001") == (1, 0)
    assert parse_modicon_address("10005") == (2, 4)
    assert parse_modicon_address("123") == (None, 123)
    assert parse_modicon_address("40000") == (None, None)
    assert parse_modicon_address("abc") == (None, None)
    assert modicon_address(3, 0) == "40001" and modicon_address(4, 9) == "30010"
    assert modicon_address(3, 12345) == "412346"


def test_serial_settings_and_order_name():
    assert parse_serial_settings("19200,8,E,1") == {"baudrate": 19200, "bytesize": 8, "parity": "E", "stopbits": 1}
    assert parse_serial_settings("")["baudrate"] == 9600
    with pytest.raises(ValueError):
        parse_serial_settings("9600,8,X,1")
    assert order_name("BIG", "LITTLE") == "CDAB" and order_name(None, None) == "ABCD"


def test_scaling():
    assert apply_linear_scaling(5000, 0, 10000, 0, 100) == 50
    assert apply_linear_scaling(20000, 0, 10000, 0, 100) == 100      # 夾在範圍內
    assert apply_linear_scaling(5, None, 10, 0, 1) == 5
    assert apply_linear_scaling(5, math.nan, 10, 0, 1) == 5           # pandas 讀出的 NaN


def tag(i, addr, dt="uint16", fc=3, slave=1):
    return {"id": i, "start_address": addr, "data_type": dt, "function_code": fc, "slave_id": slave}


def test_plan_merges_near_addresses_per_slave_and_fc():
    tags = [tag(1, 0), tag(2, 1, "float32"), tag(3, 5), tag(4, 100), tag(5, 0, fc=4), tag(6, 0, slave=2)]
    blocks = plan_blocks(tags, max_gap=10)
    summary = sorted((b.slave, b.function_code, b.start, b.count, [t["id"] for t, _ in b.items]) for b in blocks)
    assert (1, 3, 0, 6, [1, 2, 3]) in summary          # 0、1~2、5 合併成 0~5
    assert (1, 3, 100, 1, [4]) in summary              # 相隔太遠，另一塊
    assert (1, 4, 0, 1, [5]) in summary                # 不同功能碼不合併
    assert (2, 3, 0, 1, [6]) in summary                # 不同站號不合併
    offsets = {t["id"]: off for b in blocks for t, off in b.items}
    assert offsets[3] == 5 and offsets[2] == 1


def test_plan_respects_max_registers():
    tags = [tag(i, i * 2, "float32") for i in range(60)]   # 120 個暫存器
    blocks = plan_blocks(tags, max_registers=50, max_gap=0)
    assert all(b.count <= 50 for b in blocks) and sum(len(b.items) for b in blocks) == 60


def test_plan_isolate_is_a_barrier_and_strict_mode():
    tags = [tag(1, 0), tag(2, 5), tag(3, 8)]
    blocks = plan_blocks(tags, max_gap=10, isolate={2})
    ranges = sorted((b.start, b.count) for b in blocks)
    assert ranges == [(0, 1), (5, 1), (8, 1)]            # 不會跨過被隔離的位址 5
    strict = plan_blocks([tag(1, 0), tag(2, 1), tag(3, 5)], max_gap=10, strict_groups={(1, 3)})
    assert sorted((b.start, b.count) for b in strict) == [(0, 2), (5, 1)]


def test_plan_coils_use_bit_span():
    blocks = plan_blocks([tag(1, 0, "bool", fc=1), tag(2, 3, "bool", fc=1)], max_gap=5)
    assert len(blocks) == 1 and blocks[0].count == 4
