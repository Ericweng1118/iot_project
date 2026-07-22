import logging
from pymodbus.payload import BinaryPayloadDecoder
from pymodbus.constants import Endian

logger = logging.getLogger(__name__)

def decode_modbus_registers(registers, data_type, byte_order='BIG', word_order='BIG'):
    """
    根據位元組與字組順序解析 Modbus 暫存器
    """
    if not registers:
        return None

    # 對應 pymodbus 的 Endian 列舉
    b_order = Endian.BIG if byte_order.upper() == 'BIG' else Endian.LITTLE
    w_order = Endian.BIG if word_order.upper() == 'BIG' else Endian.LITTLE

    try:
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
            logger.warning(f"未知 Modbus 型態 {data_type}，預設回傳第一個暫存器值。")
            return registers[0]
            
    except Exception as e:
        logger.error(f"Modbus 暫存器解碼出錯: {e}")
        return None