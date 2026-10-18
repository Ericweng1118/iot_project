# IIoT SCADA — 多協議工業資料採集與監控系統

支援 **OPC UA**、**Modbus**（TCP / RTU over TCP / RTU）、**Siemens S7** 的 SCADA 系統，
資料存進 PostgreSQL / TimescaleDB，用 Streamlit 網頁後台監控與設定。目前版本 **v3.4**。

> 📖 功能細節、執行架構、常見問題排查見 [docs/MANUAL.md](docs/MANUAL.md)；
> 各版本的升級步驟與行為變化見 [UPGRADE_NOTES_v3.md](UPGRADE_NOTES_v3.md)。

## 功能一覽

| 分類 | 功能 |
| --- | --- |
| 採集 | OPC UA 訂閱（逐點頻率、伺服器端 deadband、只訂閱已綁定點位）；Modbus 批次讀取、連線重用、多站號共用閘道；S7 DB / M / I / Q、BOOL 位元、全型態；各協議可用 `.env` 個別關閉 |
| 歷史資料 | 統一週期寫入 `sensor_readings`，逐感測器上傳條件 + 心跳保底；每筆帶品質（正常 / 保持 / 不確定 / 不良 / 通訊中斷）；資料庫斷線時存本機、恢復後補寫 |
| 監控 | 即時總覽（依廠區 / 產線 / 設備分組）、歷史趨勢（自動降採樣、斷線標示）、報表匯出（時 / 日 / 月，Excel / CSV）、排程寄送報表 |
| 警報 | HH / H / L / LL / EQ / NE、遲滯、延遲觸發、四級優先、ACK、通訊中斷警報；Webhook / Email / Telegram 通知 |
| 計算點 | 運算式 50+ 函式（統計、邏輯、品質、位元、時間、`integral` / `delta` / `ontime` / `count` 累計、移動平均、濾波）；Python 腳本（管理員限定、預設關閉） |
| 工程工具 | Modbus / S7 線上調適（讀記憶體、多型態解碼、點位驗證、一鍵建點）、批次匯入匯出（Excel / CSV 預覽後套用）、設備範本 |
| 系統 | 角色權限（檢視者 / 操作員 / 工程師 / 管理員）、操作稽核、服務心跳與系統狀態頁、HTTPS、閒置登出 |

## 快速開始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env            # 填入 DB 連線、ADMIN_USER / ADMIN_PASSWORD 等
python run_all.py              # 採集服務 main.py + 網頁後台 admin_app.py
```

**資料庫 migration**：用資料表擁有者帳號依序執行，檔案清單與注意事項見 [sql/README.md](sql/README.md)。
不要用 `sql/0*.sql` 萬用字元：會一起跑到 `010_rollback.sql` 與選用的 `013`。

**Docker**：

```bash
docker build -t unified_collector:latest .
docker run -d --name unified_collector --env-file .env -p 1118:1118 \
           -v /opt/scada/data:/app/data --stop-timeout 30 unified_collector:latest
```

`/app/data` 是本機緩存，一定要掛 volume；`--stop-timeout 30` 讓服務有時間收尾。

**測試**：

```bash
pip install -r requirements-dev.txt
python -m pytest tests
```

需要資料庫的整合測試（`tests/test_bindings.py` 等）、migration、權限腳本，一律先在**隔離測試庫**跑，
不要用 `.env`（它指向正式庫）。[`scripts/test_db.sh`](scripts/test_db.sh) 會用正式庫同一個 image
起一個容器（127.0.0.1:55432、記憶體上限 512 MB），複製角色、結構、Energy 庫與最近 7 天的時序資料，用完即刪：

```bash
scripts/test_db.sh up && scripts/test_db.sh load   # 約 1 分鐘
eval "$(scripts/test_db.sh env)"                    # 這個 shell 之後連的都是測試庫
python -m pytest tests                              # 整合測試不再被跳過
scripts/test_db.sh down                             # 刪除容器與全部資料
```

參數（port、天數、記憶體、密碼）見腳本開頭說明。PME 也共用這個測試庫。

## 常用設定（完整清單見 `env.example`）

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `OPCUA_ENABLED` / `MODBUS_ENABLED` / `TIA_ENABLED` | `true` | 協議開關，關閉後網頁也隱藏對應頁面 |
| `POLL_INTERVAL` | `60` | Modbus / S7 採集週期（秒） |
| `SENSOR_READING_FLUSH_INTERVAL` | `60` | 歷史資料寫入週期（秒） |
| `SENSOR_HEARTBEAT_INTERVAL` | `3600` | 心跳保底，超過這段時間沒寫就補寫一筆 |
| `SCADA_TIMEZONE` | `Asia/Taipei` | 顯示與報表使用的時區 |
| `ALARM_WEBHOOK_URLS` / `SMTP_*` / `TELEGRAM_*` | — | 警報通知頻道 |
| `CALC_SCRIPTS_ENABLED` | `false` | Python 腳本計算點開關 |
| `ADMIN_USER` / `ADMIN_PASSWORD` | — | 救援管理員帳號，務必修改 |

## 專案結構

```
core/          共用設定、帳號權限、稽核
protocols/     OPC UA / Modbus / S7 驅動與編解碼
collector/     Modbus、S7 採集
data_layer/    連線池、歷史寫入、本機緩存、匯入匯出、設備範本
services/      OPC UA 訂閱、警報、計算點、報表排程、心跳回報
web/           網頁後台頁面（web/pages/ 一頁一個模組）
sql/           資料庫 migration
tests/         單元測試
main.py        採集主程式    admin_app.py  網頁入口    run_all.py  同時啟動兩者並監護
```

## 文件

| 文件 | 內容 |
| --- | --- |
| [docs/MANUAL.md](docs/MANUAL.md) | 詳細手冊：各功能說明、執行架構、網頁操作、常見問題排查 |
| [docs/PME_INTERFACE.md](docs/PME_INTERFACE.md) | 與 PME（能源管理系統）共用資料庫的介面約定：依賴的表欄位、quality 語意、`spool.drained` 事件、通知規則 |
| [README_DB.md](README_DB.md) | 資料庫 schema 參考 |
| [sql/README.md](sql/README.md) | migration 執行順序與各檔用途 |
| [UPGRADE_NOTES_v2.md](UPGRADE_NOTES_v2.md) / [UPGRADE_NOTES_v3.md](UPGRADE_NOTES_v3.md) | 各版本升級步驟、行為變化 |
| [todo.md](todo.md) | 待辦事項與技術債 |
