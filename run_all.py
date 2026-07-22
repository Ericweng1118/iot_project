import subprocess
import sys
import time

def main():
    print("🚀 [1/2] 正在啟動採集主程式 (main.py)...")
    # 使用當前虛擬環境的 python 執行 main.py
    main_process = subprocess.Popen([sys.executable, "main.py"])

    print("🚀 [2/2] 正在啟動 Streamlit 管理後台 (admin_app.py)...")
    # 啟動 streamlit 服務
    streamlit_process = subprocess.Popen(["streamlit", "run", "admin_app.py"])

    print("\n✅ 所有服務已在背景啟動！")
    print("💡 按下 Ctrl + C 可同時安全關閉所有服務。\n")

    try:
        # 保持主程序存活，監控背景子進程
        while True:
            time.sleep(1)
            # 若任一服務非預期崩潰，可在此發現
            if main_process.poll() is not None:
                print("⚠️ 警告：main.py 已停止運行！")
            if streamlit_process.poll() is not None:
                print("⚠️ 警告：admin_app.py 已停止運行！")

    except KeyboardInterrupt:
        print("\n🛑 收到中斷訊號，正在關閉所有服務...")
        main_process.terminate()
        streamlit_process.terminate()
        
        main_process.wait()
        streamlit_process.wait()
        print("✨ 所有服務已安全停止。")

if __name__ == "__main__":
    main()