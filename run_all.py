"""
run_all.py
==========
同時啟動採集主程式（main.py）與網頁後台（admin_app.py），並監護兩個子程序：

- 🆕 子程序非預期結束時自動重啟（指數退避 5 → 10 → 20 … 最多 300 秒；
  連續穩定執行超過 10 分鐘後退避時間歸零）。原本只會印一行警告，main.py 掛掉
  就一直掛著，直到有人發現。
- 🆕 收到 SIGTERM（docker stop）或 SIGINT（Ctrl+C）時，把訊號轉給子程序並等待
  它們正常結束。原本只處理 KeyboardInterrupt，docker stop 送的 SIGTERM 會讓
  run_all.py 直接被殺掉，main.py 來不及做最後一次 sensor_readings 寫入。
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

def _env(key, default=""):
    return (os.getenv(key) or default).split("#")[0].strip()


port = _env("ADMIN_PORT", "8501")
ssl_cert = _env("ADMIN_SSL_CERT")
ssl_key = _env("ADMIN_SSL_KEY")

RESTART_BASE_DELAY = 5
RESTART_MAX_DELAY = 300
STABLE_RUN_SECONDS = 600

_stopping = False


def _handle_stop(signum, frame):
    global _stopping
    _stopping = True


class Child:
    def __init__(self, name, cmd):
        self.name = name
        self.cmd = cmd
        self.proc = None
        self.started_at = 0.0
        self.restart_delay = RESTART_BASE_DELAY
        self.next_start_at = 0.0

    def start(self):
        print(f"🚀 正在啟動 {self.name} ...", flush=True)
        self.proc = subprocess.Popen(self.cmd, cwd=PROJECT_DIR)
        self.started_at = time.time()

    def supervise(self):
        """子程序結束時安排重啟；回傳 None。"""
        if self.proc is None:
            if time.time() >= self.next_start_at:
                self.start()
            return
        code = self.proc.poll()
        if code is None:
            return
        ran = time.time() - self.started_at
        if ran >= STABLE_RUN_SECONDS:
            self.restart_delay = RESTART_BASE_DELAY
        print(
            f"⚠️ {self.name} 已停止（exit code={code}，執行 {ran:.0f} 秒），"
            f"{self.restart_delay} 秒後自動重啟",
            flush=True,
        )
        self.proc = None
        self.next_start_at = time.time() + self.restart_delay
        self.restart_delay = min(self.restart_delay * 2, RESTART_MAX_DELAY)

    def terminate(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def wait(self, timeout):
        if not self.proc:
            return
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"⚠️ {self.name} 未在 {timeout} 秒內結束，強制終止", flush=True)
            self.proc.kill()
            self.proc.wait()


def streamlit_command():
    cmd = [sys.executable, "-m", "streamlit", "run", "admin_app.py", "--server.port", str(port)]
    # 🆕 HTTPS：.env 設定 ADMIN_SSL_CERT / ADMIN_SSL_KEY（相對路徑以專案目錄為準）就啟用。
    #    憑證可用 scripts/gen_self_signed_cert.sh 產生自簽憑證，或改用反向代理（見 docs/MANUAL.md）。
    if ssl_cert or ssl_key:
        cert, key = PROJECT_DIR / ssl_cert, PROJECT_DIR / ssl_key
        if cert.is_file() and key.is_file():
            cmd += ["--server.sslCertFile", str(cert), "--server.sslKeyFile", str(key)]
            print(f"🔒 網頁後台啟用 HTTPS（憑證：{cert}）", flush=True)
        else:
            print(f"⚠️ 找不到 HTTPS 憑證檔（ADMIN_SSL_CERT={ssl_cert}、ADMIN_SSL_KEY={ssl_key}），"
                  "改用 HTTP 啟動", flush=True)
    return cmd


def main():
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    children = [
        Child("採集主程式 (main.py)", [sys.executable, "main.py"]),
        Child("網頁後台 (admin_app.py)", streamlit_command()),
    ]
    for child in children:
        child.start()

    print("\n✅ 所有服務已在背景啟動！子程序異常結束會自動重啟。")
    print("💡 按下 Ctrl + C（或 docker stop）可同時安全關閉所有服務。\n", flush=True)

    while not _stopping:
        time.sleep(1)
        for child in children:
            child.supervise()

    print("\n🛑 收到停止訊號，正在關閉所有服務...", flush=True)
    for child in children:
        child.terminate()
    # main.py 停止時要等 OPC UA 訂閱與寫入排程收尾（各自最多 10 秒）
    for child in children:
        child.wait(timeout=25)
    print("✨ 所有服務已安全停止。", flush=True)


if __name__ == "__main__":
    main()
