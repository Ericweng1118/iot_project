"""
collector/modbus_blocks.py
==========================
Modbus 批次讀取規劃：把同一站號、同一功能碼、位址相近的點位合併成一次請求。

v2 是「一個點位一次請求」，一台設備 50 個點就是 50 次來回，RS-485 9600 bps 下
一輪要好幾秒；合併成幾個區塊後通常只要 1~3 次請求。

規則：
    - 同 (slave, function_code) 才能合併
    - 兩個點位之間的空隙 <= max_gap 個暫存器才合併（空隙越大，白讀的資料越多，
      也越可能踩到設備沒有定義的位址而整塊回 Illegal Data Address）
    - 一個區塊最多 max_registers 個暫存器（協議上限 125，保守預設 100）；
      位元類（FC01/02）最多 max_bits 個
    - isolate 裡的點位單獨成一個區塊，而且當作分隔點（其他區塊不會跨過它的位址）：
      上一輪整塊讀取失敗時，採集程式會逐一讀取找出「有問題的點位」，之後就讓它自己一塊，
      不再拖累同區塊的其他點位
    - strict_groups 裡的 (slave, function_code) 只合併「位址連續」的點位（空隙 = 0）：
      有些設備只要讀到沒有定義的暫存器就整塊拒絕，整塊失敗、但逐點讀取全部成功時，
      代表問題出在空隙，採集程式會把這組改成嚴格模式
"""

from dataclasses import dataclass, field

from protocols.modbus_codec import BIT_FUNCTIONS, register_count


@dataclass
class Block:
    slave: int
    function_code: int
    start: int
    count: int
    items: list = field(default_factory=list)   # [(tag dict, 在區塊內的位移)]


def _span(tag) -> int:
    if int(tag["function_code"]) in BIT_FUNCTIONS:
        return 1
    return register_count(tag["data_type"])


def plan_blocks(tags, max_registers: int = 100, max_bits: int = 800, max_gap: int = 10,
                isolate=frozenset(), strict_groups=frozenset()) -> list:
    groups = {}
    for tag in tags:
        key = (int(tag.get("slave_id") or 1), int(tag["function_code"]))
        groups.setdefault(key, []).append(tag)

    blocks = []
    for (slave, fc), group in sorted(groups.items()):
        limit = max_bits if fc in BIT_FUNCTIONS else max_registers
        gap = 0 if (slave, fc) in strict_groups else max_gap
        current = None
        for tag in sorted(group, key=lambda t: (int(t["start_address"]), t.get("id", 0))):
            addr, span = int(tag["start_address"]), _span(tag)
            if tag.get("id") in isolate:
                if current is not None:
                    blocks.append(current)
                    current = None
                blocks.append(Block(slave, fc, addr, span, [(tag, 0)]))
                continue
            if current is not None:
                end = current.start + current.count
                new_end = max(end, addr + span)
                if addr - end <= gap and new_end - current.start <= limit:
                    current.count = new_end - current.start
                    current.items.append((tag, addr - current.start))
                    continue
                blocks.append(current)
            current = Block(slave, fc, addr, span, [(tag, 0)])
        if current is not None:
            blocks.append(current)
    return blocks
