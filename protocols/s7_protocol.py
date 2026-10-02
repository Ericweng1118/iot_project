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

# ======================================================================
# 🆕 v3.3：S7Connection —— 支援 DB / M / I / Q、Rack / Slot、結構化結果與中文錯誤說明
# ======================================================================
# 與上面 SiemensS7Collector 的差別：
#   - 可讀寫 DB、M（旗標）、I（輸入）、Q（輸出）四種區域
#   - 回傳 S7Result（成功與否、資料、耗時、錯誤說明、是否為連線層級的失敗）
#   - 錯誤訊息翻成現場看得懂的說明（PUT/GET 沒開、最佳化區塊存取、Rack/Slot 錯誤…）
#   - 內建鎖：同一條連線同一時間只送一個請求（snap7 client 不是執行緒安全的）

import threading
import time as _time
from dataclasses import dataclass

from snap7.type import Area

from protocols.s7_codec import explain_error

_AREA_MAP = {"DB": Area.DB, "M": Area.MK, "I": Area.PE, "Q": Area.PA}
# 這幾種錯誤代表 CPU 有回應、只是拒絕這個請求：連線本身沒問題，不用重連
_REQUEST_LEVEL_ERRORS = ("address out of range", "function refused", "item not available",
                         "data size mismatch", "invalid transport size")


@dataclass
class S7Result:
    ok: bool
    data: bytes = b""
    error: str | None = None
    elapsed_ms: float = 0.0
    fatal: bool = False


class S7Connection:
    def __init__(self, ip: str, rack: int = 0, slot: int = 1, port: int = 102):
        self.ip = ip
        self.rack = int(rack)
        self.slot = int(slot)
        self.port = int(port or 102)
        self._client = None
        self._lock = threading.Lock()

    @property
    def label(self) -> str:
        return f"{self.ip}（Rack {self.rack} / Slot {self.slot}）" + (f":{self.port}" if self.port != 102 else "")

    def _connect_locked(self):
        if self._client is not None and self._client.get_connected():
            return None
        self._close_locked()
        client = snap7.client.Client()
        try:
            client.connect(self.ip, self.rack, self.slot, self.port)
        except Exception as e:
            try:
                client.destroy()
            except Exception:
                pass
            return explain_error(_msg(e))
        self._client = client
        return None

    def _close_locked(self):
        if self._client is not None:
            try:
                self._client.disconnect()
                self._client.destroy()
            except Exception:
                pass
        self._client = None

    def close(self):
        with self._lock:
            self._close_locked()

    def _call(self, fn) -> S7Result:
        started = _time.perf_counter()
        with self._lock:
            err = self._connect_locked()
            if err:
                return S7Result(False, error=err, fatal=True, elapsed_ms=(_time.perf_counter() - started) * 1000)
            try:
                data = fn(self._client)
            except Exception as e:
                text = _msg(e)
                request_level = any(k in text.lower() for k in _REQUEST_LEVEL_ERRORS)
                if not request_level:
                    self._close_locked()
                return S7Result(False, error=explain_error(text), fatal=not request_level,
                                elapsed_ms=(_time.perf_counter() - started) * 1000)
        return S7Result(True, data=bytes(data) if data is not None else b"",
                        elapsed_ms=(_time.perf_counter() - started) * 1000)

    def read(self, area: str, db: int, start: int, size: int) -> S7Result:
        a = _AREA_MAP[str(area or "DB").upper()]
        return self._call(lambda c: c.read_area(a, int(db) if a == Area.DB else 0, int(start), int(size)))

    def write(self, area: str, db: int, start: int, data: bytes) -> S7Result:
        a = _AREA_MAP[str(area or "DB").upper()]
        result = self._call(lambda c: c.write_area(a, int(db) if a == Area.DB else 0, int(start), bytearray(data)))
        if result.ok:
            result.data = bytes(data)
        return result

    def cpu_info(self) -> dict:
        """CPU 型號、序號、狀態（RUN/STOP）、PDU 大小。讀不到的欄位留空，不影響其他欄位。"""
        info = {}
        with self._lock:
            err = self._connect_locked()
            if err:
                return {"error": err}
            c = self._client
            for key, getter in (
                ("cpu_info", lambda: c.get_cpu_info()),
                ("state", lambda: c.get_cpu_state()),
                ("order_code", lambda: c.get_order_code()),
                ("pdu", lambda: c.get_pdu_length()),
            ):
                try:
                    info[key] = getter()
                except Exception as e:
                    info[key + "_error"] = explain_error(_msg(e))
        out = {}
        ci = info.get("cpu_info")
        if ci is not None:
            for field in ("ModuleTypeName", "SerialNumber", "ASName", "ModuleName", "Copyright"):
                v = getattr(ci, field, b"")
                out[field] = v.decode(errors="ignore").strip("\x00 ") if isinstance(v, bytes) else str(v)
        if "state" in info:
            out["state"] = str(info["state"]).replace("S7CpuStatus", "").upper()
        oc = info.get("order_code")
        if oc is not None:
            code = getattr(oc, "OrderCode", b"")
            out["order_code"] = code.decode(errors="ignore").strip("\x00 ") if isinstance(code, bytes) else str(code)
            out["firmware"] = f"V{getattr(oc, 'V1', '?')}.{getattr(oc, 'V2', '?')}.{getattr(oc, 'V3', '?')}"
        if "pdu" in info:
            out["pdu"] = info["pdu"]
        out.update({k: v for k, v in info.items() if k.endswith("_error")})
        return out


def _msg(e) -> str:
    text = e.args[0] if getattr(e, "args", None) else e
    if isinstance(text, bytes):
        text = text.decode(errors="ignore")
    return str(text).strip()
