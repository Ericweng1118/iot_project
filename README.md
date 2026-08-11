# 多功能工業 PLC 資料採集系統 (Unified Industrial Data Collector)

一個支援 **西門子 S7**、**Modbus TCP**、**OPC UA** 三協議的統一工業資料採集系統，
具備併發採集、OPC UA 訂閱推播、PostgreSQL/TimescaleDB 時序儲存、
感測器階層管理、異常監控與 MQTT 增量上傳功能。

---

## 🚀 核心功能

### 1. 多協議支援

- **S7 Protocol (Siemens)** — 透過 `snap7` 連線 S7-1200/1500 PLC，DB 區塊打包讀取
- **Modbus TCP** — 透過 `pymodbus` 支援 FC1/2/3/4/5/6/15/16，處理多位元組資料型態與 Byte/Word Order
- **OPC UA** — 透過 `asyncua`，**採用訂閱推播（Subscription）而非輪詢瀏覽**，詳見下方「OPC UA 訂閱服務」

### 2. 併發採集架構

- Modbus、TIA(S7) 兩協議在每一輪採集中**併發執行**（`ThreadPoolExecutor`），
  其中一個協議整體卡住（例如全部設備斷線在等 connect timeout）不會拖到另一個協議完全沒開始
- 同一協議內，多台 PLC/設備也是併發連線（例如 Modbus 依 IP/Port/Slave 分組、TIA 依 PLC IP 分組），
  一台離線只影響自己那條執行緒，不會拖慢其他台
- PostgreSQL 連線池使用 `ThreadedConnectionPool`（執行緒安全），支援上述多執行緒併發存取

### 3. OPC UA 訂閱服務（Report by Exception）

OPC UA 不再像 Modbus/TIA 一樣每輪重新輪詢，而是常駐一個獨立的背景服務：

- 只在**必要時**才做結構性瀏覽（browse）：第一次啟動、或使用者於網頁手動觸發重新整理
- 平常靠 Server 端的 **Subscription 推播**：有變化 Server 才通知，本服務收到後先寫進記憶體緩衝區，
  每 2 秒批次寫回資料庫，大幅減少「每輪重新爬整棵 Address Space」的開銷
- 定期心跳（讀取 ServerStatus 節點）偵測斷線，斷線後自動指數退避重連（5s → 10s → … 上限 60s），
  重連後直接用快取的點位表重建訂閱，**不需要**重新瀏覽
- 詳細操作方式見下方「OPC UA 點位設定」分頁說明

### 4. 感測器階層管理與時序資料

- 雙層資料架構：**即時層**（`modbus_scada` / `tia_scada` / `opcua_tags`，存目前值與連線狀態）
  與 **時序層**（`sensor_readings` hypertable，存歷史數值），透過 `sensor_id` 連結
- 階層架構：廠區 (`sites`) → 產線 (`production_lines`) → 設備 (`devices`) → 感測器 (`sensors`) → 讀值 (`sensor_readings`)
- 感測器可設定 `nickname`（顯示用暱稱）與 `state_dictionary`（數字轉文字狀態字典），
  搭配 `sensor_readings_translated` view 自動把數字翻譯成中文狀態
- 網頁「感測器階層管理」分頁可直接維護這整條階層
- Modbus / TIA / OPC UA 三個點位設定分頁都有「綁定感測器」下拉選單，並內建**跨協議重複綁定偵測**，
  避免同一個感測器被兩個不同點位同時綁定

### 5. 異常監控

網頁內建「異常監控」分頁，彙整三種常見異常，不用自己下 SQL 查：

- 🔌 **連線異常**：三個即時層資料表中 `plc_state ≠ ONLINE` 的點位
- 📈 **數值超出正常範圍**：依 `sensors.min_threshold` / `max_threshold` 比對最新一筆 `sensor_readings`
- ⏱️ **資料斷更**：超過使用者設定的時數門檻沒有新資料的感測器（含「從未寫入過」的感測器）

### 6. 數據增量上傳 (Report by Exception)

MQTT 端同樣採用增量發送：快取上一輪的值，只有數值變化時才發送，並定期強制全量心跳上傳，
節省頻寬與 Broker 負載。

### 7. PostgreSQL 批量更新

使用 `execute_values` 一次性更新多筆點位，減少資料庫連線開銷與 I/O 瓶頸；
OPC UA 訂閱模式另外提供輕量版 `batch_update_opcua_values`，只更新數值不動結構欄位，
讓高頻的數值 flush 盡量精簡。

---

## 📁 專案結構

```
├── protocols/                    # 協議驅動層
│   ├── s7_protocol.py            # 西門子 S7 協議實現（DB 數據讀取）
│   ├── modbus_protocol.py        # Modbus TCP 協議實現（多位元組解析）
│   └── opcua_protocol.py         # OPC UA 協議實現（連線、遞迴瀏覽、資料讀取）
├── parsers/                      # 數據解析器
│   ├── plc_parser.py             # S7 數據解析（字節順序、線性縮放）
│   └── encoder.py                # Modbus 編碼/解碼（32/64 位元支援）
├── data_layer/                   # 數據層
│   ├── db_connector.py           # PostgreSQL 連線池管理（ThreadedConnectionPool）
│   └── batch_updater.py          # 批量更新邏輯（execute_values）
├── messaging/                    # 訊息通訊層
│   └── mqtt_publisher.py         # MQTT 發送器（自動連線、失敗重試、增量上傳）
├── services/                     # 常駐背景服務
│   └── opcua_subscription_service.py   # OPC UA 訂閱服務（獨立執行緒 + asyncio）
├── collector/                    # 各協議的採集主程式
│   ├── run_modbus_collector.py   # Modbus 資料採集（多設備併發）
│   ├── run_s7_collector.py       # TIA/S7 資料採集（多 PLC 併發）
│   └── run_opcua_collector.py    # OPC UA 一次性瀏覽採集（獨立測試用，main.py 已改用訂閱服務）
├── migrations/                   # 資料庫 Migration Script（001 ~ 005，依序執行）
│   └── 005_opcua_resubscribe_flag.sql
├── main.py                       # 統一主程式（Modbus/TIA 併發輪詢 + OPC UA 常駐訂閱服務）
├── admin_app.py                  # 網頁管理後台入口（Streamlit）
├── run_all.py                    # 同時啟動 main.py 與 admin_app.py
├── requirements.txt              # Python 依賴套件
├── Dockerfile                    # Docker 容器化設定
├── .env                          # 環境變數設定（請自行建立，勿提交 Git）
└── README.md
```

---

## ⚙️ 安裝與設定

### 1. 建立虛擬環境並安裝依賴

```bash
python3 -m venv .venv
source .venv/bin/activate  # Linux/Mac
pip install -r requirements.txt
```

### 2. 建立 `.env` 環境變數檔案

```bash
# ===== 資料庫連線設定 =====
DB_HOST=192.168.x.x
DB_PORT=5432
DB_NAME=your_database
DB_USER=your_username
DB_PASSWORD=your_password

# ===== MQTT 伺服器設定 =====
MQTT_BROKER=192.168.x.x
MQTT_PORT=1883
MQTT_USER=your_mqtt_user
MQTT_PASSWORD=your_password
MQTT_GROUP_ID=scada_unified
MQTT_TOPIC=iot-2/evt/wadata/fmt/scada_unified

# ===== 採集服務週期設定 =====
# 僅套用於 Modbus / TIA(S7)；OPC UA 改由常駐訂閱服務即時處理，不受此週期影響
POLL_INTERVAL="60.0"

# ===== 網頁小工具入口帳號、密碼、PORT =====
ADMIN_USER=user
ADMIN_PASSWORD=password
ADMIN_PORT=PORT_NUMBER
```

### 3. 執行資料庫 Migration

依序執行 `migrations/` 內的 SQL（001 ~ 005）。**若是既有系統升級**，
至少要補跑最新的：

```sql
-- 005_opcua_resubscribe_flag.sql
ALTER TABLE opcua_servers
    ADD COLUMN IF NOT EXISTS resubscribe_requested BOOLEAN NOT NULL DEFAULT FALSE;
```

> ⚠️ 若使用非 superuser 角色連線（例如 `scada`），記得確認該角色對相關資料表
> 有 `SELECT / INSERT / UPDATE / DELETE` 權限；**PostgreSQL 的 GRANT 對表和序列
> (sequence) 是分開的**，兩者都要 GRANT 才不會遇到權限錯誤。

### 4. 執行主程式

```bash
source .venv/bin/activate
python main.py          # 只跑採集主服務
# 或
python run_all.py       # 同時跑採集主服務 + 網頁管理後台
```

### 5. Docker 容器化執行

```bash
docker build -t unified_collector:latest .
docker run -d --name unified_collector --env-file .env unified_collector:latest
```

---

## 🧠 執行架構說明

### 主迴圈（`main.py`）

```
每輪 POLL_INTERVAL 秒：
  ├─ Modbus 採集  ─┐
  └─ TIA(S7) 採集 ─┴─ 併發執行 (ThreadPoolExecutor)，互不阻塞
  └─ MQTT 增量上傳（等上面兩個都跑完才執行，確保撈到本輪最新資料）

（OPC UA 完全不在這個迴圈裡，見下方）
```

### OPC UA 訂閱服務（`services/opcua_subscription_service.py`）

`main.py` 啟動時會另外開一個**獨立的背景執行緒**（有自己的 asyncio event loop），
跟上面的主迴圈完全脫鉤、互不阻塞：

```
服務啟動
  └─ 對每一台 enabled=TRUE 的 Server：
        ├─ 連線
        ├─ 有快取點位表就直接用；沒有才做一次完整 browse 並寫入 opcua_tags
        ├─ 建立 Subscription，監控所有已知點位
        ├─ 背景 task 1：每 2 秒把收到的變化批次 flush 進資料庫
        ├─ 背景 task 2：每 5 秒檢查一次「是否有人請求重新整理點位表」
        └─ 每 15 秒讀一次 ServerStatus 節點當心跳；讀取失敗 → 判定斷線 → 退避重連
```

**新增點位 / 移除點位的流程：**
使用者在網頁「OPC UA 點位設定」分頁按下「🚀 立即瀏覽並寫入資料庫」後：

1. 立即重新瀏覽該 Server 並把最新點位表寫入 `opcua_tags`
2. 順便把 `opcua_servers.resubscribe_requested` 設為 `TRUE`
3. 訂閱服務下一次輪詢（最多等 5 秒）偵測到旗標，重新瀏覽、比對差異、
   把新增的點位加入訂閱、把移除的點位取消訂閱，然後自動把旗標清回 `FALSE`

整個過程**不需要重啟任何服務**。

**已知限制：**

- 新增一台**全新**的 OPC UA Server，或修改 Server 的連線參數（IP/Port/帳密/安全性原則）、
  或切換 `enabled` 開關，訂閱服務目前不會動態偵測，需要**重新啟動 `main.py`** 才會套用
- 點位從 Server 端移除後，`opcua_tags` 裡的舊資料列不會自動刪除，只會停止更新數值

---

## 🖥️ 網頁管理後台操作說明（`admin_app.py`）

登入後有 5 個分頁：

### 分頁 1：📡 Modbus 點位設定

- 表格內可直接編輯任何欄位後按「💾 儲存 Modbus 修改」
- `state_dictionary` 欄位若要設定數字轉文字映射，填合法 JSON，例如 `{"1": "待機", "2": "運轉"}`
  （系統會自動正規化格式，就算表格編輯器把它顯示成單引號的 Python 字典字面量也能正確解析）
- **綁定感測器**：表格與「單筆新增」表單都有「綁定感測器 (sensor_code)」下拉選單，
  選項格式為「設備編號 / 感測器編號 (暱稱)」，選好後存檔即可，不用自己記 `sensor_id` 數字；
  下拉選單內容來自「感測器階層管理」分頁已建立的感測器
- 下方表單可單筆新增點位，並提供「🧪 測試連線與讀取」按鈕在寫入資料庫前先驗證參數是否正確

### 分頁 2：📡 TIA (S7) 點位設定

- 操作方式同 Modbus 分頁，包含「綁定感測器」下拉選單
- 新增/編輯點位前建議先用「🧪 測試連線與讀取」驗證 DB 區塊位址、資料型態是否正確
- 提醒：PLC 端需開啟 PUT/GET 存取權限，且該 DB 需**關閉**「優化區塊存取」，否則讀取會失敗

### 分頁 3：📡 OPC UA 點位設定

- **Server 清單**：可編輯連線參數，但**變更連線參數需要重啟 main.py 才會套用**
- **新增 Server**：填完連線資訊可先「🧪 測試連線與瀏覽」預覽會抓到哪些點位，確認無誤再「新增 Server」；
  新 Server 一樣需要重啟 main.py，訂閱服務才會開始監控
- **手動瀏覽**：針對已存在的 Server，「🚀 立即瀏覽並寫入資料庫」可以立即重新整理點位表，
  完成後會自動通知訂閱服務更新監控內容，通常幾秒內生效
- **已採集的點位資料**：可依 Server 篩選檢視目前所有點位的即時數值，並有「綁定感測器」欄位可編輯，
  改完按「💾 儲存 OPC UA 點位綁定」（其餘欄位如 node_id、數值等唯讀，只能透過瀏覽更新）

### 🔗 感測器綁定與跨協議重複偵測

Modbus / TIA / OPC UA 三個分頁的「綁定感測器」欄位共用同一套邏輯：

- 選單只列出已經在「感測器階層管理」分頁建立好的感測器，格式為「設備編號 / 感測器編號 (暱稱)」
- **同一個感測器不能同時被兩個不同點位綁定**：存檔時系統會檢查該感測器是否已被
  Modbus/TIA/OPC UA 任一其他點位使用，若衝突會擋下該筆並標明是哪個點位衝突（例如
  `modbus_scada.12（溫度感測器_01）`），該筆不會被儲存，其餘沒有衝突的筆數仍會正常存檔
- 綁定後，採集程式會自動把該點位的數值同時寫入 `sensor_readings` 時序表（透過 `sensor_id` 連結）

### 分頁 4：🧬 感測器階層管理

依序建立廠區 → 產線 → 設備 → 感測器：

1. **🏭 廠區 (sites)**：填廠區名稱、位置
2. **🏗️ 產線 (production_lines)**：選擇所屬廠區，填產線名稱
3. **⚙️ 設備 (devices)**：選擇所屬產線，填設備編號（唯一）、名稱、類型、製造商、狀態
4. **🌡️ 感測器 (sensors)**：選擇所屬設備，填感測器編號（唯一）、選填暱稱、類型、單位、
   正常值上下限、選填狀態字典

建立好感測器後，回到 Modbus / TIA / OPC UA 點位設定分頁，把對應點位的「綁定感測器」
欄位選成這個感測器，採集程式就會自動把數值同時寫入 `sensor_readings` 時序表。

> 💡 感測器一旦建立，`sensor_code` 和所屬設備不能再改（避免破壞既有歷史資料的關聯），
> 但 `nickname`、類型、單位、閾值、狀態字典都可以隨時在表格內編輯調整。

### 分頁 5：🚨 異常監控

- **連線異常**：列出所有 `plc_state` 不是 `ONLINE` 的點位，橫跨 Modbus / TIA / OPC UA 三種來源
- **數值超出正常範圍**：列出已綁定感測器中，最新讀值超出 `min_threshold` / `max_threshold` 的項目
- **資料斷更**：可自訂「太久沒更新」的時數門檻，列出超過門檻沒有新資料的感測器，
  另外也會列出「從建立以來從未寫入過任何讀值」的感測器（通常代表尚未綁定點位，或該點位一直讀取失敗）

---

## 📊 資料庫表格設計

### 即時層

#### `modbus_scada`（Modbus TCP 點位）

| 欄位 | 型態 | 說明 |
| --- | --- | --- |
| `id` | SERIAL | 主鍵 |
| `name` | TEXT | 點位名稱（MQTT Key 與增量比對基準） |
| `plc_ip` / `plc_port` / `slave_id` | — | 連線資訊 |
| `function_code` | INTEGER | 1/2/3/4 |
| `start_address` | INTEGER | 起始位址 |
| `data_type` | VARCHAR | bool/word/int/dint/float/uint32/int64/float64/uint64 |
| `raw_min` / `raw_max` / `eng_min` / `eng_max` | REAL | 線性 Scaling 參數 |
| `byte_order` / `word_order` | VARCHAR | BIG / LITTLE |
| `state_dictionary` | JSONB | 數字轉文字映射，例如 `{"1": "待機"}` |
| `sensor_id` | INTEGER | FK → `sensors(sensor_id)`，可為 NULL；綁定後數值會同步寫入 `sensor_readings` |
| `current_value` | REAL | 目前數值（純數字） |
| `current_data` | JSONB | 採集數據，格式 `{"val": 123.45}` |
| `plc_state` | VARCHAR | ONLINE / OFFLINE / ERROR |
| `last_update` | TIMESTAMPTZ | 最後成功更新時間 |

#### `tia_scada`（西門子 PLC 點位）

| 欄位 | 型態 | 說明 |
| --- | --- | --- |
| `id` | SERIAL | 主鍵 |
| `name` / `plc_name` / `plc_ip` | — | 識別與連線資訊 |
| `db_number` / `offset` | INTEGER | S7 DB 區塊與偏移量 |
| `data_type` | VARCHAR | DINT/REAL/BOOL/INT/WORD/DWORD 等 |
| `sensor_id` | INTEGER | FK → `sensors(sensor_id)`，可為 NULL；綁定後數值會同步寫入 `sensor_readings` |
| `current_data` | JSONB | 格式 `{"val": 123.45}` |
| `plc_state` | VARCHAR | ONLINE / OFFLINE / ERROR |
| `last_update` | TIMESTAMPTZ | 最後成功更新時間 |

#### `opcua_servers`（已知 OPC UA Server 連線資訊）

| 欄位 | 型態 | 說明 |
| --- | --- | --- |
| `id` | SERIAL | 主鍵 |
| `server_name` | VARCHAR(100) | 唯一，用於 MQTT Key |
| `ip` / `port` | — | 連線資訊 |
| `username` / `password` | — | 可為 NULL（匿名連線） |
| `security_policy` / `security_mode` | VARCHAR(30) | 預設 None |
| `root_node_id` | VARCHAR(100) | 瀏覽起始節點，預設 `i=85` |
| `browse_depth` | INTEGER | 遞迴瀏覽深度上限 |
| `enabled` | BOOLEAN | 是否啟用此 Server |
| `resubscribe_requested` | BOOLEAN | 🆕 訂閱服務用：是否有「重新整理點位表」請求待處理 |
| `conn_state` | VARCHAR(20) | ONLINE / OFFLINE / ERROR |
| `last_scan` / `last_error` | — | 最後瀏覽時間 / 最後錯誤訊息 |

#### `opcua_tags`（瀏覽出來的點位與最新數值）

| 欄位 | 型態 | 說明 |
| --- | --- | --- |
| `id` | SERIAL | 主鍵 |
| `server_id` | INTEGER | FK → `opcua_servers(id)` |
| `server_name` | VARCHAR(100) | 冗餘存一份，方便 MQTT Key 組合 |
| `node_id` | VARCHAR(200) | OPC UA NodeId，例如 `ns=2;s=Temp01` |
| `browse_name` / `display_name` | VARCHAR(200) | — |
| `data_type` | VARCHAR(50) | OPC UA VariantType 名稱 |
| `sensor_id` | INTEGER | FK → `sensors(sensor_id)`，可為 NULL；綁定後數值會同步寫入 `sensor_readings` |
| `current_data` | JSONB | 格式 `{"val": 123.45}` |
| `quality` | VARCHAR(20) | GOOD / BAD / UNCERTAIN |
| `plc_state` | VARCHAR(20) | ONLINE / OFFLINE，預設 OFFLINE |
| `last_update` | TIMESTAMPTZ | — |

### 時序層（廠區 → 產線 → 設備 → 感測器 → 讀值）

```
sites (廠區)
  └─ production_lines (產線)
        └─ devices (設備)
              └─ sensors (感測器)
                    └─ sensor_readings (時序讀值 hypertable)
```

- `sensors.sensor_id` 是連結即時層與時序層的橋樑
- `sensors` 額外有 `nickname`（顯示暱稱）與 `state_dictionary`（數字轉文字狀態字典）
- `sensor_readings_translated` view：依 `sensors.state_dictionary` 自動把 `sensor_readings.value`
  翻譯成對應的文字狀態，查詢時不用自己再 join 一次

---

## 🎛️ Modbus TCP 參數解析

### Byte Order（位元組順序）

決定單一暫存器（16-bit）內部 2 個 Byte 誰先誰後。

- **BIG**：高位元組在前（Modbus 標準預設）
- **LITTLE**：低位元組在前

### Word Order（字組順序）

決定多暫存器（32/64-bit）之間的排列順序。

- **BIG**：高位暫存器在前
- **LITTLE**：低位暫存器在前

### 常見組合速查表

| 工業俗稱 | `byte_order` | `word_order` | 備註 |
| --- | --- | --- | --- |
| Big Endian (ABCD) | BIG | BIG | Modbus 官方標準 |
| Word Swap (CDAB) | BIG | LITTLE | 🌟 台灣電表最常見！ |
| Byte Swap (BADC) | LITTLE | BIG | — |
| Little Endian (DCBA) | LITTLE | LITTLE | 部分歐美設備 |

---

## 🔧 模組說明

| 模組 | 說明 |
| --- | --- |
| `collector/run_modbus_collector.py` | Modbus 資料採集主程式，多設備併發連線 |
| `collector/run_s7_collector.py` | TIA/S7 資料採集主程式，多 PLC 併發連線 |
| `collector/run_opcua_collector.py` | OPC UA 一次性完整瀏覽採集（獨立腳本，可手動執行測試；`main.py` 平時改用訂閱服務） |
| `services/opcua_subscription_service.py` | OPC UA 常駐訂閱服務，背景執行緒 + 專屬 asyncio event loop |
| `protocols/s7_protocol.py` | 西門子 S7 協議封裝（`snap7`），DB 區塊打包讀取 |
| `protocols/modbus_protocol.py` | Modbus TCP 協議封裝（`pymodbus`） |
| `protocols/opcua_protocol.py` | OPC UA 協議封裝（`asyncua`）：連線、遞迴瀏覽、單節點讀取 |
| `parsers/plc_parser.py` | S7 位元組解析、線性縮放 |
| `parsers/encoder.py` | Modbus Big/Little Endian 編解碼 |
| `data_layer/db_connector.py` | PostgreSQL 連線池（`ThreadedConnectionPool`，執行緒安全） |
| `data_layer/batch_updater.py` | 批量寫入（`execute_values` UPDATE/UPSERT），含訂閱模式專用輕量版 |
| `messaging/mqtt_publisher.py` | MQTT 發送器，增量比對、心跳全量上傳、自動重連 |
| `main.py` | 主程式入口：Modbus/TIA 併發輪詢 + 啟動 OPC UA 訂閱服務 + MQTT 上傳 |
| `admin_app.py` | 網頁管理後台（Streamlit），5 個分頁：Modbus / TIA / OPC UA / 感測器階層管理 / 異常監控 |
| `run_all.py` | 同時啟動 `main.py` 與 `admin_app.py` |

---

## 🩺 常見問題排查

| 現象 | 可能原因 |
| --- | --- |
| OPC UA 網頁按了「立即瀏覽」但訂閱服務沒反應 | 確認已跑過 `005_opcua_resubscribe_flag.sql`；確認 `main.py` 是用新版啟動（背景訂閱服務有印出「🚀 [訂閱服務] ... 已啟動」的 log） |
| 新增 OPC UA Server 後網頁看得到，但一直沒有數值 | 新 Server 需要重啟 `main.py` 才會被訂閱服務接手，請重啟後觀察 log |
| `state_dictionary` 存檔報 `invalid input syntax for type json` | 請填合法 JSON（雙引號），或直接重新整理頁面讓表格重新載入；系統已內建正規化邏輯，多數情況可自動修正 |
| 資料庫報 permission denied | 檢查該資料庫使用者是否對相關表**和序列 (sequence)** 都有 GRANT，兩者要分開授權 |
| Modbus/TIA 某台設備離線時，其他設備也被拖慢 | 確認 `db_connector.py` 已改用 `ThreadedConnectionPool`，且各採集腳本使用的是併發版本（`_collect_one_device` / `_collect_one_plc`） |
| 儲存綁定時被擋下、提示「已被其他點位使用」 | 這是跨協議重複綁定偵測在運作，同一個感測器不能同時綁兩個點位；訊息會標明是哪個點位（例如 `modbus_scada.12`）佔用了該感測器，先去那個點位解除綁定（改選「（未綁定）」）再重新綁定 |
| 「單筆新增 Modbus 點位」以前送出後失敗或寫入怪資料 | 舊版程式碼欄位順序跟參數順序沒對齊（`unit` 誤植到 `function_code` 位置，導致後面全部欄位錯位），目前版本已修正對齊，若還在用更早期的檔案請直接替換成最新版 `admin_app.py` |