import snap7.util

def parse_s7_value(buffer, offset, data_type, bit_offset=0):
    """
    將 S7 二進制 Buffer 轉換為 Python 資料型態
    :param buffer: snap7 讀取回來的 bytearray
    :param offset: 記憶體偏移量 (Byte 位置)
    :param data_type: 資料型態 (BOOL, INT, DINT, REAL)
    :param bit_offset: 如果是 BOOL，代表它在該 Byte 的第幾個 bit (0-7)
    """
    dt = data_type.upper()
    
    if dt == 'BOOL':
        return snap7.util.get_bool(buffer, offset, bit_offset)
    elif dt == 'INT':
        return snap7.util.get_int(buffer, offset)
    elif dt == 'DINT':
        return snap7.util.get_dint(buffer, offset)
    elif dt == 'REAL':
        return snap7.util.get_real(buffer, offset)
    else:
        raise ValueError(f"未支援的 S7 資料型態: {data_type}")

def linear_scaling(val, raw_min, raw_max, eng_min, eng_max):
    """
    線性縮放 (工程值轉換，如 4-20mA 轉 0-100 kg)
    """
    if None in (raw_min, raw_max, eng_min, eng_max):
        return val
    if raw_max == raw_min:
        return val
    
    # 限制原始值不超過上下限邊界
    val = max(min(val, raw_max), raw_min)
    return ((val - raw_min) / (raw_max - raw_min)) * (eng_max - eng_min) + eng_min