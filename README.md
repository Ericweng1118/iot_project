# 多功能工業 PLC 資料採集系統 (Unified Industrial Data Collector)

一個支援**西門子 S7** 與 **Modbus TCP** 雙協議的統一工業資料採集系統，具備 PostgreSQL 批量更新與 MQTT 增量上傳功能。

---

## 🚀 核心功能

### 1. 多協議支援

- **S7 Protocol (Siemens)** - 透過 `snap7` 庫連線 S7-1200/1500 PLC，讀取 DB 區塊數據
- **Modbus TCP** - 透過 `pymodbus` 庫支援 FC1/2/3/4/5/6/15/16 功能碼，處理多位元組資料型態

### 2. 數據增量上傳 (Report by Exception)

系統會快取上一輪的值，只有當數值發生變化時才通過 MQTT 發送，節省頻寬與 Broker 負載。

### 3. PostgreSQL 批量更新

使用 `execute_values` 一次性更新所有點位，減少資料庫連線開銷與 I/O 瓶頸。

---

## 📁 專案結構

```
s7_protocol.py├── protocols/              # 協議驅動層
│   ├── s7_protocol.py     # 西門子 S7 協議實現（DB 數據讀取）
│   └── modbus_protocol.py # Modbus TCP 協議實現（多位元組解析）
├── parsers/               # 數據解析器
│   ├── plc_parser.py      # S7 數據解析（字節順序、線性縮放）
│   └── encoder.py         # Modbus 編碼/解碼（32/64 位元支援）
├── data_layer/            # 數據層
│   ├── db_connector.py    # PostgreSQL 連線管理與查詢封裝
│   └── batch_updater.py   # 批量更新邏輯（execute_values）
├── messaging/             # 訊息通訊層
│   └── mqtt_publisher.py     # MQTT 發送器（自動連線、失敗重試）
├── collector/			#存放協議撈資料用的主程式
│   ├── modbus_protocol.py    # 撈Modbus資料用的主程式
│   └── run_s7_collector.py   # 撈S7_TIA資料用的主程式
├── main.py   # 統一主程式（雙協議整合）
├── requirements.txt       # Python 依賴套件
├── Dockerfile            # Docker 容器化設定
├── .env                  # 環境變數設定（請自行建立，勿提交 Git）
├── admin_app.py		#網頁介面入口
└── run_all.py		# 統一主程式（雙協議整合+網頁開啟）

```

---

## ⚙️ 安裝與設定

### 方法一：本機執行

#### 1. 建立虛擬環境並安裝依賴

```bash
# 建立虛擬環境
python3 -m venv .venv

# 激活虛擬環境
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# 安裝所有 Python 依賴套件
pip install -r requirements.txt
```

#### 2. 建立 `.env` 環境變數檔案

```bash
# ===== 資料庫連線設定 =====
DB_HOST=192.168.x.x          # PostgreSQL 伺服器 IP
DB_PORT=5432                 # PostgreSQL 埠號
DB_NAME=your_database        # 資料庫名稱
DB_USER=your_username        # 資料庫使用者
DB_PASSWORD=your_password    # 資料庫密碼

# ===== MQTT 伺服器設定 =====
MQTT_BROKER=192.168.x.x      # MQTT Broker IP
MQTT_PORT=1883               # MQTT 埠號（預設 1883）
MQTT_USER=your_mqtt_user     # MQTT 使用者（可選）
MQTT_PASSWORD=your_password  # MQTT 密碼（可選）
MQTT_GROUP_ID=scada_unified  # 設備/群組識別 ID
MQTT_TOPIC=iot-2/evt/wadata/fmt/scada_unified  # MQTT 發布主題

# ==========================================
# 採集服務週期設定
# ==========================================
# 採集週期 (秒)
POLL_INTERVAL="60.0"
```

#### 3. 執行主程式

```bash
source .venv/bin/activate    # 若未激活虛擬環境
python main.py
```

---

### 方法二：Docker 容器化執行

#### 1. 建立 Docker 映像檔

```bash
docker build -t unified_collector:latest .
```

#### 2. 執行容器

```bash
docker run -d \
  --name unified_collector \
  --env-file .env \
  unified_collector:latest
```

---

## 📊 資料庫表格設計

### TIA_SCADA 表格（西門子 PLC 點位）

| 欄位名稱         | 型態         | 說明                                     |
| ---------------- | ------------ | ---------------------------------------- |
| `id`           | SERIAL       | 主鍵，自動遞增                           |
| `name`         | VARCHAR/TEXT | 點位名稱（用作 MQTT Key 與變動比對基準） |
| `plc_ip`       | VARCHAR      | PLC IP 地址                              |
| `db_number`    | INTEGER      | S7 DB 區塊號碼                           |
| `offset`       | INTEGER      | 記憶體偏移量                             |
| `data_type`    | VARCHAR      | 資料型態（DINT, REAL, BOOL, INT 等）     |
| `plc_name`     | VARCHAR      | PLC 設備名稱                             |
| `current_data` | JSONB        | 採集數據（格式：`{"val": 123.45}`）    |
| `plc_state`    | VARCHAR      | 連線狀態（ONLINE/OFFLINE/ERROR）         |
| `last_update`  | TIMESTAMPTZ  | 最後成功更新時間                         |

**SQL 範例：**

```sql
INSERT INTO scada (name, plc_ip, db_number, "offset", data_type, plc_name)
VALUES 
  ('溫度感測器_01', '192.168.1.10', 100, 0, 'REAL', 'S7-1200_PLCE'),
  ('壓力感測器_01', '192.168.1.10', 100, 4, 'DINT', 'S7-1200_PLCE');
```

---

### modbus_scada 表格（Modbus TCP 點位）

| 欄位名稱             | 型態         | 說明                                                                        |
| -------------------- | ------------ | --------------------------------------------------------------------------- |
| `id`               | SERIAL       | 主鍵，自動遞增                                                              |
| `name`             | VARCHAR/TEXT | 點位名稱                                                                    |
| `plc_ip`           | VARCHAR      | PLC/儀表 IP 地址                                                            |
| `plc_port`         | INTEGER      | Modbus 埠號（預設 502）                                                     |
| `slave_id`         | INTEGER      | Modbus 從站 ID (1-247)                                                      |
| `function_code`    | INTEGER      | 功能碼 (1=Coils, 2=Discrete Inputs, 3=Holding Registers, 4=Input Registers) |
| `start_address`    | INTEGER      | 起始暫存器/位址                                                             |
| `data_type`        | VARCHAR      | 資料型態（bool, word, int, dint, float, uint32, int64, float64, uint64）    |
| `raw_min`          | REAL         | 原始值最小值                                                                |
| `raw_max`          | REAL         | 原始值最大值                                                                |
| `eng_min`          | REAL         | 工程值最小值                                                                |
| `eng_max`          | REAL         | 工程值最大值                                                                |
| `byte_order`       | VARCHAR      | 位元組順序（BIG/LITTLE）                                                    |
| `word_order`       | VARCHAR      | 字組順序（BIG/LITTLE）                                                      |
| `state_dictionary` | JSONB        | 狀態字典（例如：`{"1": "待機", "2": "運轉"}`）                            |
| `current_value`    | REAL         | 當前數值（純數值格式）                                                      |
| `current_data`     | JSONB        | 採集數據（格式：`{"val": 123.45}`）                                       |
| `plc_state`        | VARCHAR      | 連線狀態（ONLINE/OFFLINE/ERROR）                                            |
| `last_update`      | TIMESTAMPTZ  | 最後成功更新時間                                                            |

**SQL 範例：**

```sql
INSERT INTO modbus_scada 
  (name, plc_ip, slave_id, function_code, start_address, data_type, raw_min, raw_max, eng_min, eng_max, byte_order, word_order)
VALUES 
  ('電表_有功電力', '192.168.1.20', 1, 4, 0, 'float32', 0, 1000, 0, 1000, 'BIG', 'BIG'),
  ('溫控器_設定溫度', '192.168.1.21', 1, 3, 100, 'dint', -100, 500, -100, 500, 'BIG', 'LITTLE');
```

---

## 🎛️ Modbus TCP 參數解析

### Byte Order（位元組順序）

決定單一暫存器（16-bit）內部的 2 個 Byte 誰先誰後。

- **BIG**：高位元組在前（Modbus 標準預設）
- **LITTLE**：低位元組在前

### Word Order（字組順序）

決定多暫存器（32/64-bit）之間的排列順序。

- **BIG**：高位暫存器在前
- **LITTLE**：低位暫存器在前

### 常見組合速查表

| 工業俗稱             | `byte_order` | `word_order` | 備註                         |
| -------------------- | -------------- | -------------- | ---------------------------- |
| Big Endian (ABCD)    | BIG            | BIG            | Modbus 官方標準              |
| Word Swap (CDAB)     | BIG            | LITTLE         | 🌟**台灣電表最常見！** |
| Byte Swap (BADC)     | LITTLE         | BIG            | -                            |
| Little Endian (DCBA) | LITTLE         | LITTLE         | 部分歐美設備                 |

---

## 🔧 模組說明

### `protocols/s7_protocol.py`

西門子 S7 協議封裝，使用 `snap7` 庫讀取 DB 區塊數據。處理位元、字節、字詞的記憶體對齊與解析。

### `protocols/modbus_protocol.py`

Modbus TCP 協議封裝，使用 `pymodbus` 庫連線並發讀取指令。支援 FC1/2/3/4，自動處理多位元組打包。

### `parsers/plc_parser.py`

S7 數據解析器：將原始位元组轉換為對應的 Python 數值，並執行線性縮放（raw → eng）。

### `parsers/encoder.py`

Modbus 編碼解碼器：利用 `struct` 模組處理 Big/Little Endian 排列，支援 float32/64、int/uint32/64 等型態。

### `data_layer/db_connector.py`

PostgreSQL 連線管理器（ORM 層）。封裝 psycopg2 連線建立、查詢執行與資源釋放。

### `data_layer/batch_updater.py`

批量更新模組：使用 `execute_values` 配合 `UPDATE FROM VALUES` 語法，一次性寫入多筆數據。

### `messaging/mqtt_sender.py`

MQTT 發送器：處理與 MQTT Broker 的連線、認證與發布。支援 QoS 1 確保送達。

### `main.py`

統一主程式入口。整合 S7 與 Modbus 雙協議，執行輪詢、比對、批量更新與增量上傳流程。

---

## 📝 自訂開發

### 新增支援新的資料型態

修改對應的 parser/encoder 檔案中的 `struct.pack/unpack` 格式字串：

| 型態         | struct 格式   | 位元數 | 暫存器數量 |
| ------------ | ------------- | ------ | ---------- |
| INT16        | `h` / `H` | 16     | 1          |
| DINT/INT32   | `i` / `I` | 32     | 2          |
| REAL/FLOAT32 | `f`         | 32     | 2          |
| INT64        | `q`         | 64     | 4          |
| FLOAT64      | `d`         | 64     | 4          |

### 新增新的 PLC 協議

1. 在 `protocols/` 目錄下建立新協議模組
2. 在 `unified_collector.py` 中匯入並註冊對應的讀取函數
3. 在資料庫中新增相對應的配置表格

---

## 🐛 常見問題排除

### 1. `ModuleNotFoundError: No module named 'pymodbus'`

```bash
pip install -r requirements.txt
```

### 2. PostgreSQL 連線失敗

檢查 `.env` 中的 `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD` 是否正確。

### 3. Modbus 讀取數值異常

嘗試更換 `byte_order` 與 `word_order` 組合，參考上方「常見組合速查表」。

### 4. MQTT 發送失敗

檢查 MQTT Broker IP、Port、帳號密碼是否正確，並確認網路可達。

---

## 📄 License

MIT License

---

## 🤝 貢獻方式

1. Fork 本專案
2. 建立功能分支 (`git checkout -b feature/xxx`)
3. 提交更改 (`git commit -m 'Add xxx'`)
4. 推送到遠端 (`git push origin feature/xxx`)
5. 開啟 Pull Request
