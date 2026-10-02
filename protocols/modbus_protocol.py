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

# ======================================================================
# 🆕 v3.1：ModbusConnection —— 一條實體連線、多個站號、結構化的讀寫結果
# ======================================================================
# 與上面 ModbusTCPCollector 的差別：
#   - 支援三種傳輸方式：tcp（Modbus TCP）、rtu_over_tcp（序列轉乙太網路閘道的透通模式，
#     例如 Moxa NPort 設成 TCP Server）、rtu（本機 RS-485 序列埠，需要 pyserial）
#   - 站號（slave id）每次請求指定：RS-485 閘道後面常常掛好幾台設備，共用同一條連線，
#     不要每台設備各開一條（很多閘道只允許 1~4 條同時連線）
#   - 回傳 ModbusResult（成功與否、數值、例外碼、耗時），呼叫端不用自己解析 pymodbus 回應
#   - 內建鎖：同一條連線同一時間只送一個請求（RS-485 是半雙工，一定要排隊）

import threading
import time as _time
from dataclasses import dataclass, field

from pymodbus import Framer

from protocols.modbus_codec import BIT_FUNCTIONS, describe_exception, parse_serial_settings

# pymodbus 自己會對每次連線失敗印 ERROR（離線設備每輪都洗一次版），
# 錯誤已經由 ModbusResult 回報、由採集程式彙整記錄，這裡把套件內部 log 壓到 CRITICAL。
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)

TRANSPORTS = {
    "tcp": "Modbus TCP",
    "rtu_over_tcp": "RTU over TCP（序列閘道透通模式）",
    "rtu": "RTU 序列埠（RS-485 / RS-232）",
}


@dataclass
class ModbusResult:
    ok: bool
    values: list = field(default_factory=list)
    error: str | None = None
    exception_code: int | None = None   # 設備有回應、但回的是例外（代表設備在線）
    elapsed_ms: float = 0.0
    fatal: bool = False                  # 連線本身失敗（連不上 / 中斷），同一條連線的後續請求都不用試了

    @property
    def device_responded(self) -> bool:
        """成功或回傳例外碼都代表設備在線；只有逾時 / 連不上才是真的離線。"""
        return self.ok or self.exception_code is not None


class ModbusConnection:
    def __init__(self, transport: str = "tcp", host: str = "", port: int = 502,
                 timeout: float = 3.0, serial_settings: str | None = None, retries: int = 1):
        self.transport = (transport or "tcp").lower()
        self.host = host
        self.port = int(port or 502)
        self.timeout = float(timeout)
        self.serial_settings = serial_settings
        self.retries = int(retries)
        self._client = None
        self._lock = threading.Lock()

    @property
    def label(self) -> str:
        if self.transport == "rtu":
            return f"{self.host}（{self.serial_settings or '9600,8,N,1'}）"
        return f"{self.host}:{self.port}" + ("（RTU over TCP）" if self.transport == "rtu_over_tcp" else "")

    def _build_client(self):
        common = {"timeout": self.timeout, "retries": self.retries}
        if self.transport == "tcp":
            return ModbusTcpClient(host=self.host, port=self.port, **common)
        if self.transport == "rtu_over_tcp":
            return ModbusTcpClient(host=self.host, port=self.port, framer=Framer.RTU, **common)
        if self.transport == "rtu":
            from pymodbus.client import ModbusSerialClient
            return ModbusSerialClient(port=self.host, framer=Framer.RTU,
                                      **parse_serial_settings(self.serial_settings), **common)
        raise ValueError(f"不支援的傳輸方式: {self.transport}")

    def connect(self) -> bool:
        with self._lock:
            return self._connect_locked()

    def _connect_locked(self) -> bool:
        if self._client is not None and self._client.connected:
            return True
        self._close_locked()
        self._client = self._build_client()
        try:
            return bool(self._client.connect())
        except Exception as e:
            logger.debug(f"連線 Modbus {self.label} 失敗: {e}")
            return False

    def close(self):
        with self._lock:
            self._close_locked()

    def _close_locked(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None

    def _call(self, func_name: str, slave: int, **kwargs) -> ModbusResult:
        started = _time.perf_counter()
        with self._lock:
            if not self._connect_locked():
                return ModbusResult(False, error=f"無法連線至 {self.label}", fatal=True,
                                    elapsed_ms=(_time.perf_counter() - started) * 1000)
            try:
                response = getattr(self._client, func_name)(slave=int(slave), **kwargs)
            except Exception as e:
                # 逾時 / 連線中斷：把連線丟掉，下次重建
                self._close_locked()
                return ModbusResult(False, error=f"通訊失敗：{e}", fatal=True,
                                    elapsed_ms=(_time.perf_counter() - started) * 1000)
        elapsed = (_time.perf_counter() - started) * 1000
        if response.isError():
            code = getattr(response, "exception_code", None)
            if code is not None:
                return ModbusResult(False, error=describe_exception(code), exception_code=int(code),
                                    elapsed_ms=elapsed)
            return ModbusResult(False, error="設備無回應（逾時）：站號錯誤、設備關機，或 RS-485 接線 / 鮑率不對",
                                elapsed_ms=elapsed)
        return ModbusResult(True, values=response, elapsed_ms=elapsed)

    def read(self, function_code: int, address: int, count: int, slave: int = 1) -> ModbusResult:
        fc = int(function_code)
        func = {1: "read_coils", 2: "read_discrete_inputs",
                3: "read_holding_registers", 4: "read_input_registers"}.get(fc)
        if func is None:
            return ModbusResult(False, error=f"不支援的讀取功能碼 {fc}")
        result = self._call(func, slave, address=int(address), count=int(count))
        if result.ok:
            result.values = list(result.values.bits[:count]) if fc in BIT_FUNCTIONS \
                else list(result.values.registers)
        return result

    def write_coil(self, address: int, value: bool, slave: int = 1) -> ModbusResult:
        result = self._call("write_coil", slave, address=int(address), value=bool(value))
        if result.ok:
            result.values = [bool(value)]
        return result

    def write_registers(self, address: int, values: list, slave: int = 1) -> ModbusResult:
        """單一暫存器用 FC06，多個用 FC16（部分設備不支援 FC16 寫單一暫存器）。"""
        values = [int(v) & 0xFFFF for v in values]
        if len(values) == 1:
            result = self._call("write_register", slave, address=int(address), value=values[0])
        else:
            result = self._call("write_registers", slave, address=int(address), values=values)
        if result.ok:
            result.values = values
        return result
