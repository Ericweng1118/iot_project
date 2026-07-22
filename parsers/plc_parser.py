import snap7.util
import struct

def parse_s7_value(buffer, offset, data_type, bit_offset=0):
    """
    將 TIA Portal / S7 二進制 Buffer 轉換為 Python 資料型態
    西門子 S7 全系列皆採用 Big-Endian (大端序) 儲存
    
    :param buffer: snap7 讀取回來的 bytearray
    :param offset: 記憶體偏移量 (Byte 位置)
    :param data_type: TIA 資料型態 (如 INT, UDINT, REAL, LREAL 等)
    :param bit_offset: 如果是 BOOL，代表它在該 Byte 的第幾個 bit (0-7)
    """
    dt = data_type.upper().strip()
    
    # ----------------------------------------------------
    # 1. 位元與基礎二進制型態 (Bit / Hex)
    # ----------------------------------------------------
    if dt == 'BOOL':
        return snap7.util.get_bool(buffer, offset, bit_offset)
    elif dt == 'BYTE':     # 8-bit 無號
        return struct.unpack_from('>B', buffer, offset)[0]
    elif dt == 'WORD':     # 16-bit 無號
        return struct.unpack_from('>H', buffer, offset)[0]
    elif dt == 'DWORD':    # 32-bit 無號
        return struct.unpack_from('>I', buffer, offset)[0]
    elif dt == 'LWORD':    # 64-bit 無號 (S7-1500 支援)
        return struct.unpack_from('>Q', buffer, offset)[0]
        
    # ----------------------------------------------------
    # 2. 有號整數 (Signed Integers)
    # ----------------------------------------------------
    elif dt == 'SINT':     # Short Int (8-bit)
        return struct.unpack_from('>b', buffer, offset)[0]
    elif dt == 'INT':      # Integer (16-bit)
        return struct.unpack_from('>h', buffer, offset)[0]
    elif dt == 'DINT':     # Double Int (32-bit)
        return struct.unpack_from('>i', buffer, offset)[0]
    elif dt == 'LINT':     # Long Int (64-bit, S7-1500 支援)
        return struct.unpack_from('>q', buffer, offset)[0]
        
    # ----------------------------------------------------
    # 3. 無號整數 (Unsigned Integers)
    # ----------------------------------------------------
    elif dt == 'USINT':    # Unsigned Short Int (8-bit)
        return struct.unpack_from('>B', buffer, offset)[0]
    elif dt == 'UINT':     # Unsigned Int (16-bit)
        return struct.unpack_from('>H', buffer, offset)[0]
    elif dt == 'UDINT':    # Unsigned Double Int (32-bit)
        return struct.unpack_from('>I', buffer, offset)[0]
    elif dt == 'ULINT':    # Unsigned Long Int (64-bit)
        return struct.unpack_from('>Q', buffer, offset)[0]
        
    # ----------------------------------------------------
    # 4. 浮點數 (Reals)
    # ----------------------------------------------------
    elif dt == 'REAL':     # 單精度浮點數 (32-bit Float)
        return struct.unpack_from('>f', buffer, offset)[0]
    elif dt == 'LREAL':    # 雙精度浮點數 (64-bit Double, S7-1500 高精度常用)
        return struct.unpack_from('>d', buffer, offset)[0]
        
    # ----------------------------------------------------
    # 5. 字元與字串 (Characters / Strings)
    # ----------------------------------------------------
    elif dt == 'CHAR':     # 單字元
        return snap7.util.get_char(buffer, offset)
    elif dt == 'STRING':   # TIA 標準字串
        # 西門子 STRING 前兩個位元組為 [最大長度, 當前長度]，snap7 會自動解析並跳過
        return snap7.util.get_string(buffer, offset)
        
    else:
        raise ValueError(f"❌ 未支援的 TIA Portal / S7 資料型態: {data_type}")

def linear_scaling(val, raw_min, raw_max, eng_min, eng_max):
    """
    線性縮放 (工程值轉換，如 4-20mA 模擬量轉為 0-100 kg 實際壓力量)
    """
    if None in (raw_min, raw_max, eng_min, eng_max):
        return val
    if raw_max == raw_min:
        return val
    
    # 限制原始值不超過上下限邊界
    val = max(min(val, raw_max), raw_min)
    return ((val - raw_min) / (raw_max - raw_min)) * (eng_max - eng_min) + eng_min