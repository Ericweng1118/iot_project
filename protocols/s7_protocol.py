import logging
import snap7


logger = logging.getLogger(__name__)

class SiemensS7Collector:
    def __init__(self, ip, rack=0, slot=1):
        """
        初始化西門子 S7 連線控制器
        :param ip: PLC 的 IP 位址
        :param rack: 機架號 (S7-1200/1500 預設為 0)
        :param slot: 插槽號 (S7-1200 預設為 1)
        """
        self.ip = ip
        self.rack = rack
        self.slot = slot
        self.client = snap7.client.Client()
        self.is_connected = False

    def connect(self):
        """建立與 PLC 的連線"""
        if self.client.get_connected():
            self.is_connected = True
            return True
        
        try:
            logger.info(f"正在連線至西門子 PLC -> {self.ip} (Rack={self.rack}, Slot={self.slot})...")
            self.client.connect(self.ip, self.rack, self.slot)
            self.is_connected = True
            logger.info(f"成功連線至 PLC: {self.ip}")
            return True
        except Exception as e:  # <--- 直接改用萬用 Exception，無視版本差異
            self.is_connected = False
            logger.error(f"連線至 PLC {self.ip} 失敗: {e}")
            return False

    def disconnect(self):
        """安全中斷連線"""
        try:
            self.client.disconnect()
            logger.info(f"已中斷與 PLC {self.ip} 的連線。")
        except Exception as e:
            logger.error(f"中斷 PLC 連線時發生錯誤: {e}")
        finally:
            self.is_connected = False

    def read_db_block(self, db_number, start_offset, size):
        """
        高效能分組讀取：一次讀取整個 DB 區塊的特定長度
        """
        if not self.is_connected:
            if not self.connect():
                return None

        try:
            data = self.client.db_read(db_number, start_offset, size)
            return data
        except Exception as e:  # <--- 同樣改用萬用 Exception
            logger.error(f"讀取 PLC {self.ip} DB{db_number} 失敗: {e}")
            self.is_connected = False
            return None