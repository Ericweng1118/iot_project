import logging
from pymodbus.payload import BinaryPayloadDecoder
from pymodbus.constants import Endian

logger = logging.getLogger(__name__)

def decode_modbus_registers(registers, data_type, byte_order='BIG', word_order='BIG'):
    """
    根據位元組(Byte)與字組(Word)順序解析 Modbus 暫存器數據
    :param registers: pymodbus 讀回來的暫存器列表 (List of 16-bit ints)
    :param data_type: 資料型態 (int16, uint16, int32, uint32, float32, int64, float64, bool)
    :param byte_order: BIG 或 LITTLE
    :param word_order: BIG 或 LITTLE
    """
    if not registers:
        return None

    # 對應 pymodbus 的 Endian 列舉設定
    b_order = Endian.BIG if byte_order.upper() == 'BIG' else Endian.LITTLE
    w_order = Endian.BIG if word_order.upper() == 'BIG' else Endian.LITTLE

    try:
        # 使用 pymodbus 內建的解碼器處理大小端切換
        decoder = BinaryPayloadDecoder.fromRegisters(
            registers, 
            byteorder=b_order, 
            wordorder=w_order
        )
        
        dt = data_type.lower()
        if dt in ['int16', 'int']:
            return decoder.decode_16bit_int()
        elif dt in ['uint16', 'word']:
            return decoder.decode_16bit_uint()
        elif dt in ['int32', 'dint']:
            return decoder.decode_32bit_int()
        elif dt in ['uint32']:
            return decoder.decode_32bit_uint()
        elif dt in ['float32', 'float', 'real']:
            return decoder.decode_32bit_float()
        elif dt in ['int64']:
            return decoder.decode_64bit_int()
        elif dt in ['uint64']:
            return decoder.decode_64bit_uint()
        elif dt in ['float64', 'double']:
            return decoder.decode_64bit_float()
        elif dt == 'bool':
            return bool(registers[0])
        else:
            logger.warning(f"未知的 Modbus 資料型態 {data_type}，預設回傳第一個暫存器原始值。")
            return registers[0]
            
    except Exception as e:
        logger.error(f"Modbus 暫存器解碼發生錯誤: {e}")
        return None