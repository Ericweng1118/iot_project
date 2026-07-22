import logging
from pymodbus.client import ModbusTcpClient

logger = logging.getLogger(__name__)

class ModbusTCPCollector:
    def __init__(self, host: str, port: int = 502, slave_id: int = 1, timeout: float = 3.0):
        """
        初始化 Modbus TCP 通訊控制器
        :param host: 設備 IP 位址
        :param port: Modbus TCP 預設通訊埠為 502
        :param slave_id: 設備站號 (Unit ID / Slave ID)
        :param timeout: 連線超時時間 (秒)
        """
        self.host = host
        self.port = port
        self.slave_id = slave_id
        self.timeout = timeout
        self.client = ModbusTcpClient(host=self.host, port=self.port, timeout=self.timeout)
        self.is_connected = False

    def connect(self) -> bool:
        """建立 Modbus TCP 連線"""
        if self.client.is_socket_open():
            self.is_connected = True
            return True

        try:
            logger.info(f"正在連線至 Modbus TCP 設備 -> {self.host}:{self.port} (Slave={self.slave_id})...")
            if self.client.connect():
                self.is_connected = True
                logger.info(f"成功連線至 Modbus 設備: {self.host}:{self.port}")
                return True
            else:
                self.is_connected = False
                logger.error(f"無法連線至 Modbus 設備: {self.host}:{self.port}")
                return False
        except Exception as e:
            self.is_connected = False
            logger.error(f"連線至 Modbus 設備 {self.host} 發生例外異常: {e}")
            return False

    def disconnect(self):
        """安全中斷連線"""
        try:
            self.client.close()
            logger.info(f"已中斷與 Modbus 設備 {self.host} 的連線。")
        except Exception as e:
            logger.error(f"中斷 Modbus 連線時發生錯誤: {e}")
        finally:
            self.is_connected = False

    # ====================================================
    # 📥 讀取功能碼 (Read Functions: FC01 ~ FC04)
    # ====================================================
    
    def read_coils(self, address: int, count: int = 1):
        """FC01: 讀取線圈狀態 (Read Coils / 數位輸出)"""
        if not self.is_connected and not self.connect():
            return None
        try:
            response = self.client.read_coils(address=address, count=count, slave=self.slave_id)
            if response.isError():
                logger.error(f"讀取 Coils 失敗 [Addr={address}, Count={count}]: {response}")
                return None
            return response.bits[:count]
        except Exception as e:
            logger.error(f"讀取 Coils 異常: {e}")
            self.is_connected = False
            return None

    def read_discrete_inputs(self, address: int, count: int = 1):
        """FC02: 讀取離散輸入 (Read Discrete Inputs / 數位輸入)"""
        if not self.is_connected and not self.connect():
            return None
        try:
            response = self.client.read_discrete_inputs(address=address, count=count, slave=self.slave_id)
            if response.isError():
                logger.error(f"讀取 Discrete Inputs 失敗 [Addr={address}, Count={count}]: {response}")
                return None
            return response.bits[:count]
        except Exception as e:
            logger.error(f"讀取 Discrete Inputs 異常: {e}")
            self.is_connected = False
            return None

    def read_holding_registers(self, address: int, count: int = 1):
        """FC03: 讀取保持暫存器 (Read Holding Registers / 可讀寫參數)"""
        if not self.is_connected and not self.connect():
            return None
        try:
            response = self.client.read_holding_registers(address=address, count=count, slave=self.slave_id)
            if response.isError():
                logger.error(f"讀取 Holding Registers 失敗 [Addr={address}, Count={count}]: {response}")
                return None
            return response.registers
        except Exception as e:
            logger.error(f"讀取 Holding Registers 異常: {e}")
            self.is_connected = False
            return None

    def read_input_registers(self, address: int, count: int = 1):
        """FC04: 讀取輸入暫存器 (Read Input Registers / 唯讀感測器)"""
        if not self.is_connected and not self.connect():
            return None
        try:
            response = self.client.read_input_registers(address=address, count=count, slave=self.slave_id)
            if response.isError():
                logger.error(f"讀取 Input Registers 失敗 [Addr={address}, Count={count}]: {response}")
                return None
            return response.registers
        except Exception as e:
            logger.error(f"讀取 Input Registers 異常: {e}")
            self.is_connected = False
            return None

    # ====================================================
    # 📤 寫入功能碼 (Write Functions: FC05, FC06, FC15, FC16)
    # ====================================================

    def write_single_coil(self, address: int, value: bool) -> bool:
        """FC05: 寫入單個線圈 (Write Single Coil / 觸發開關)"""
        if not self.is_connected and not self.connect():
            return False
        try:
            response = self.client.write_coil(address=address, value=value, slave=self.slave_id)
            if response.isError():
                logger.error(f"寫入 Single Coil 失敗 [Addr={address}, Value={value}]: {response}")
                return False
            logger.info(f"成功寫入 Single Coil [Addr={address}] -> {value}")
            return True
        except Exception as e:
            logger.error(f"寫入 Single Coil 異常: {e}")
            self.is_connected = False
            return False

    def write_single_register(self, address: int, value: int) -> bool:
        """FC06: 寫入單個保持暫存器 (Write Single Holding Register / 16-bit)"""
        if not self.is_connected and not self.connect():
            return False
        try:
            response = self.client.write_register(address=address, value=value, slave=self.slave_id)
            if response.isError():
                logger.error(f"寫入 Single Register 失敗 [Addr={address}, Value={value}]: {response}")
                return False
            logger.info(f"成功寫入 Single Register [Addr={address}] -> {value}")
            return True
        except Exception as e:
            logger.error(f"寫入 Single Register 異常: {e}")
            self.is_connected = False
            return False

    def write_multiple_coils(self, address: int, values: list[bool]) -> bool:
        """FC15: 批次寫入多個線圈 (Write Multiple Coils)"""
        if not self.is_connected and not self.connect():
            return False
        try:
            response = self.client.write_coils(address=address, values=values, slave=self.slave_id)
            if response.isError():
                logger.error(f"寫入 Multiple Coils 失敗 [Addr={address}, Values={values}]: {response}")
                return False
            logger.info(f"成功寫入 Multiple Coils [Addr={address}] (數量: {len(values)})")
            return True
        except Exception as e:
            logger.error(f"寫入 Multiple Coils 異常: {e}")
            self.is_connected = False
            return False

    def write_multiple_registers(self, address: int, values: list[int]) -> bool:
        """FC16: 批次寫入多個暫存器 (Write Multiple Registers / 用於 32-bit Float 或多點設定)"""
        if not self.is_connected and not self.connect():
            return False
        try:
            response = self.client.write_registers(address=address, values=values, slave=self.slave_id)
            if response.isError():
                logger.error(f"寫入 Multiple Registers 失敗 [Addr={address}, Values={values}]: {response}")
                return False
            logger.info(f"成功寫入 Multiple Registers [Addr={address}] (長度: {len(values)})")
            return True
        except Exception as e:
            logger.error(f"寫入 Multiple Registers 異常: {e}")
            self.is_connected = False
            return False