# 多功能工業 PLC 資料採集系統 (Unified Industrial Data Collector)

一個支援 **西門子 S7**、**Modbus TCP**、**OPC UA** 三協議的統一工業資料採集系統，具備：

- PostgreSQL 批量更新（即時值 / 連線狀態）
- **TimescaleDB 時序資料庫**（廠區 → 產線 → 設備 → 感測器 → 時序數據 的階層式儲存，含去重 / 心跳寫入規則）
- 狀態字典翻譯（數字代碼 ↔ 文字狀態，不犧牲時序表的查詢效能）
- MQTT 增量上傳（三協議皆支援，可透過環境變數一鍵開關）
- Streamlit 網頁後台（點位參數管理、感測器階層管理、異常監控）

---

## 🚀 核心功能

### 1. 多協議支援

- **S7 Protocol (Siemens)** - 透過 `snap7` 庫連線 S7-1200/1500 PLC，讀取 DB 區塊數據
- **Modbus TCP** - 透過 `pymodbus` 庫支援 FC1/2/3/4/5/6/15/16 功能碼，處理多位元組資料型態
- **OPC UA** - 透過 `asyncua` 庫連線 OPC UA 伺服器，遞迴瀏覽 Address Space 並讀值

### 2. 雙層資料儲存架構

系統把資料分成「**即時層**」與「**時序層**」兩種用途：

| 層級 | 資料表 | 用途 |
|---|---|---|
| 即時層 | `tia_scada` / `modbus_scada` / `opcua_tags` | PLC 連線參數、當下最新值、連線狀態（ONLINE/OFFLINE/ERROR），給網頁後台與 MQTT 上傳用 |
| 時序層 | `sites → production_lines → devices → sensors → sensor_readings` | 廠區/產線/設備/感測器階層 + 長期歷史時序資料（TimescaleDB hypertable） |

每個點位可以透過 `sensor_id` 欄位對應到 `sensors` 階層底下的某一個感測器；對應好之後，採集程式會依「數值不同才寫入、否則每隔一段時間心跳寫入」的規則，自動把數值寫進 `sensor_readings`。沒有綁定 `sensor_id` 的點位，即時層功能完全不受影響，只是不會產生歷史時序資料。

### 3. 狀態字典翻譯（數字 ↔ 文字）

`sensor_readings.value` 固定是 `NUMERIC`，只存數字，這是刻意設計（時序表要拿來做壓縮、`AVG`/`MIN`/`MAX`/`time_bucket` 等聚合運算，混進文字會讓這些功能失效）。但很多點位（尤其 Modbus 的運轉狀態）數值代表的其實是一個狀態（例如 `1` = 待機、`2` = 運轉）。

解法是把「翻譯」這件事放在**查詢層**，而不是儲存層：`sensors` 表有一個 `state_dictionary`（JSONB）欄位，設定好之後查詢 `sensor_readings_translated` 這個 view，就會自動把數字代碼翻譯成文字，`sensor_readings` 本身完全不受影響。

### 4. 數據增量上傳 (Report by Exception)

系統會快取上一輪的值，只有當數值發生變化時才透過 MQTT 發送，節省頻寬與 Broker 負載。三個協議（Modbus / TIA / OPC UA）都會併入同一次上傳。**MQTT 上傳功能可以透過 `.env` 的 `MQTT_ENABLED` 開關整組停用**，不需要改動程式碼。

### 5. PostgreSQL 批量更新

即時層使用 `execute_values` / `UPDATE FROM VALUES`，時序層使用 `execute_values` 批次 `INSERT ... ON CONFLICT DO NOTHING`，都是一次性批量寫入，減少資料庫連線開銷與 I/O 瓶頸。

### 6. Streamlit 網頁管理後台

- 點位參數管理（Modbus / TIA / OPC UA）：新增、編輯連線參數，即時測試連線，並可用下拉選單把點位綁定到感測器（顯示「感測器編號（暱稱）」，方便對照）
- **感測器階層管理**：直接在網頁上建立廠區 → 產線 → 設備 → 感測器，可編輯 `nickname`（暱稱）、`state_dictionary`（狀態字典）等欄位
- **異常監控**：彙整連線異常、數值超出正常範圍、資料斷更三種常見異常，不用手動下 SQL

---

## 📁 專案結構

```
├── protocols/                     # 協議驅動層
│   ├── s7_protocol.py             # 西門子 S7 協議實現（DB 數據讀取）
│   ├── modbus_protocol.py         # Modbus TCP 協議實現（多位元組解析）
│   └── opcua_protocol.py          # OPC UA 協議實現（資料讀取與瀏覽）
├── parsers/                       # 數據解析器
│   ├── plc_parser.py              # S7 數據解析（字節順序、線性縮放）
│   └── encoder.py                 # Modbus 編碼/解碼（32/64 位元支援）
├── data_layer/                    # 數據層
│   ├── db_connector.py            # PostgreSQL 連線管理與查詢封裝
│   ├── batch_updater.py           # 即時層批量更新邏輯（execute_values）
│   └── timeseries_writer.py       # 時序層寫入模組（去重 / 心跳規則，批次寫入 sensor_readings）
├── messaging/                     # 訊息通訊層
│   └── mqtt_publisher.py          # MQTT 發送器（自動連線、失敗重試、增量上傳）
├── collector/                     # 存放協議撈資料用的主程式
│   ├── run_modbus_collector.py    # 撈 Modbus 資料用的主程式
│   ├── run_s7_collector.py        # 撈 S7 / TIA 資料用的主程式
│   └── run_opcua_collector.py     # 撈 OPC UA 資料用的主程式
├── sql/                            # 資料庫遷移腳本（依編號順序執行）
│   ├── 001_sensor_hierarchy_and_mapping.sql   # 建立時序層五張表 + 即時層加 sensor_id
│   ├── 002_add_sensor_nickname.sql            # sensors 加 nickname 欄位
│   ├── 003_fix_tia_scada_last_update_type.sql # 修正 tia_scada.last_update 型態
│   └── 004_add_sensor_state_dictionary.sql    # sensors 加 state_dictionary + 翻譯 view
├── main.py                        # 統一主程式（三協議整合 + MQTT + 時序寫入）
├── requirements.txt                 # Python 依賴套件
├── Dockerfile                       # Docker 容器化設定
├── .env                              # 環境變數設定（請自行建立，勿提交 Git）
├── admin_app.py                    # 網頁介面入口（點位設定 + 感測器階層管理 + 異常監控）
└── run_all.py                      # 統一主程式（三協議整合 + 網頁一起開啟）
```

---

## ⚙️ 安裝與設定

### 1. 建立虛擬環境並安裝依賴

```bash
python3 -m venv .venv
source .venv/bin/activate  # Linux/Mac
pip install -r requirements.txt
```

### 2. 建立資料庫（TimescaleDB 需求）

先確認 PostgreSQL 已安裝 TimescaleDB 擴充套件（14 以上 + TimescaleDB 2.x 以上），接著**依編號順序**執行 `sql/` 底下的遷移腳本：

```bash
psql -h <DB_HOST> -U <DB_USER> -d <DB_NAME> -f sql/001_sensor_hierarchy_and_mapping.sql
psql -h <DB_HOST> -U <DB_USER> -d <DB_NAME> -f sql/002_add_sensor_nickname.sql
psql -h <DB_HOST> -U <DB_USER> -d <DB_NAME> -f sql/003_fix_tia_scada_last_update_type.sql
psql -h <DB_HOST> -U <DB_USER> -d <DB_NAME> -f sql/004_add_sensor_state_dictionary.sql
```

> ⚠️ **權限務必補齊，這是最常踩的坑**：新增的資料表 / 序列 (sequence) 都需要單獨授權，資料表權限跟序列權限是分開兩件事，圖形介面（如 pgAdmin）授權時容易漏掉序列。直接用 SQL 一次補齊最保險：
> ```sql
> GRANT SELECT, INSERT, UPDATE ON sites, production_lines, devices, sensors, sensor_readings TO your_db_user;
> GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO your_db_user;
> ALTER DEFAULT PRIVILEGES IN SCHEMA public
>     GRANT USAGE, SELECT ON SEQUENCES TO your_db_user;
> ```

### 3. 建立 `.env` 環境變數檔案

```bash
# ===== 資料庫連線設定 =====
DB_HOST=192.168.x.x          # PostgreSQL 伺服器 IP
DB_PORT=5432                 # PostgreSQL 埠號
DB_NAME=your_database        # 資料庫名稱
DB_USER=your_username        # 資料庫使用者
DB_PASSWORD=your_password    # 資料庫密碼

# ===== MQTT 伺服器設定 =====
MQTT_ENABLED=true            # 是否啟用 MQTT 增量上傳；設為 false 可整組停用（不用改程式碼）
MQTT_BROKER=192.168.x.x      # MQTT Broker IP
MQTT_PORT=1883               # MQTT 埠號（預設 1883）
MQTT_USER=your_mqtt_user     # MQTT 使用者（可選）
MQTT_PASSWORD=your_password  # MQTT 密碼（可選）
MQTT_GROUP_ID=scada_unified  # 設備/群組識別 ID
MQTT_TOPIC=iot-2/evt/wadata/fmt/scada_unified  # MQTT 發布主題

# ==========================================
# 採集服務週期設定
# ==========================================
POLL_INTERVAL="60.0"           # 採集週期 (秒)
OPCUA_POLL_INTERVAL="60.0"     # OPC UA 查詢瀏覽週期

# ===== 時序層 (sensor_readings) 寫入規則設定 =====
# 數值沒有變化時，最久多久仍要補寫一筆進 sensor_readings 當作心跳（秒），預設 3600 秒 = 1 小時
SENSOR_HEARTBEAT_INTERVAL=3600

# ===== 網頁小工具設定 =====
ADMIN_USER=user
ADMIN_PASSWORD=password
ADMIN_PORT=PORT_NUMBER
```

### 4. 執行主程式

```bash
source .venv/bin/activate
python main.py
```

也可以用 `python run_all.py` 同時啟動採集主程式與 Streamlit 網頁後台。

### 方法二：Docker 容器化執行

```bash
docker build -t unified_collector:latest .
docker run -d --name unified_collector --env-file .env unified_collector:latest
```

---

## 📊 資料庫架構總覽

```
【即時層】                          【時序層】
tia_scada       ─┐                sites（廠區）
modbus_scada     ├─ sensor_id ──▶     └── production_lines（產線）
opcua_tags      ─┘                          └── devices（設備）
                                                   └── sensors（感測器，含 nickname / state_dictionary）
                                                          └── sensor_readings（純數字時序資料，hypertable）
                                                                 └── sensor_readings_translated（view，自動翻譯文字狀態）
```

- 即時層三張表維持你原本的欄位與用途（PLC 連線參數、當下最新值、連線狀態），採集程式邏輯完全不變
- 每張即時層表新增一個 `sensor_id INTEGER REFERENCES sensors(sensor_id)` 欄位，作為串接時序層的橋樑
- `sensor_readings` 是唯一會快速成長的表，已轉換為 TimescaleDB hypertable 依時間自動分區
- 重複數值的過濾在**寫入端**（`data_layer/timeseries_writer.py`）處理，資料庫本身不做重複值過濾

### 即時層：TIA_SCADA 表格（西門子 PLC 點位）

| 欄位名稱 | 型態 | 說明 |
| --- | --- | --- |
| `id` | SERIAL | 主鍵，自動遞增 |
| `name` | VARCHAR/TEXT | 點位名稱（用作 MQTT Key 與變動比對基準） |
| `plc_ip` | VARCHAR | PLC IP 地址 |
| `db_number` | INTEGER | S7 DB 區塊號碼 |
| `offset` | INTEGER | 記憶體偏移量 |
| `data_type` | VARCHAR | 資料型態（DINT, REAL, BOOL, INT 等） |
| `plc_name` | VARCHAR | PLC 設備名稱 |
| `current_data` | JSONB | 採集數據（格式：`{"val": 123.45}`） |
| `plc_state` | VARCHAR | 連線狀態（ONLINE/OFFLINE/ERROR） |
| `last_update` | **TIMESTAMPTZ** | 最後成功更新時間（原本誤設為 `TIME WITH TIME ZONE`，已由 `003` 遷移腳本修正） |
| `sensor_id` | INTEGER | 對應 `sensors.sensor_id`，NULL 代表尚未綁定，不會寫入 `sensor_readings` |

### 即時層：modbus_scada 表格（Modbus TCP 點位）

| 欄位名稱 | 型態 | 說明 |
| --- | --- | --- |
| `id` | SERIAL | 主鍵，自動遞增 |
| `name` | VARCHAR/TEXT | 點位名稱 |
| `plc_ip` / `plc_port` | VARCHAR / INTEGER | 連線位址（port 預設 502） |
| `slave_id` | INTEGER | Modbus 從站 ID (1-247) |
| `function_code` | INTEGER | 功能碼 (1=Coils, 2=Discrete Inputs, 3=Holding Registers, 4=Input Registers) |
| `start_address` | INTEGER | 起始暫存器/位址 |
| `data_type` | VARCHAR | 資料型態（bool, word, int, dint, float, uint32, int64, float64, uint64） |
| `raw_min` / `raw_max` | REAL | 原始值上下限 |
| `eng_min` / `eng_max` | REAL | 工程值上下限 |
| `byte_order` / `word_order` | VARCHAR | 位元組 / 字組順序（BIG/LITTLE） |
| `state_dictionary` | JSONB | 狀態字典（例如：`{"1": "待機", "2": "運轉"}`），僅套用在 `current_data` 顯示用 |
| `current_value` | REAL | 當前數值（純數值格式） |
| `current_data` | JSONB | 採集數據（格式：`{"val": ...}`，可能是數字或狀態字典轉出的文字） |
| `plc_state` | VARCHAR | 連線狀態（ONLINE/OFFLINE/ERROR） |
| `last_update` | TIMESTAMPTZ | 最後成功更新時間 |
| `sensor_id` | INTEGER | 對應 `sensors.sensor_id` |

> ⚠️ 寫進 `sensor_readings` 的一律是**轉換前的原始數字代碼**（例如 `1`、`2`），不是狀態字典轉出的中文文字，這樣才能維持時序表的聚合效能。要看文字，查 `sensor_readings_translated`。

### 即時層：opcua_servers / opcua_tags

- `opcua_servers`：Server 連線參數（ip/port/帳密/安全性原則/瀏覽起始節點/瀏覽深度/連線狀態）
- `opcua_tags`：週期性瀏覽出來的點位與最新數值，多一個 `sensor_id` 欄位；upsert 時**刻意不覆蓋** `sensor_id`，保護手動綁定的對應關係

### 時序層：sites / production_lines / devices / sensors / sensor_readings

| 資料表 | 主要欄位 | 說明 |
| --- | --- | --- |
| `sites` | `site_id`, `site_name`, `location` | 廠區（階層最上層） |
| `production_lines` | `line_id`, `site_id`, `line_name` | 產線，屬於某個廠區 |
| `devices` | `device_id`, `line_id`, `device_code`(唯一), `device_name`, `device_type`, `manufacturer`, `install_date`, `status` | 設備，屬於某條產線 |
| `sensors` | `sensor_id`, `device_id`, `sensor_code`(唯一), `nickname`, `sensor_type`, `unit`, `min_threshold`, `max_threshold`, `state_dictionary` | 感測器，屬於某台設備。`nickname` 方便對照識別；`state_dictionary`（JSONB，選填）用於數字↔文字翻譯 |
| `sensor_readings` | `reading_id`, `sensor_id`, `reading_time`, `value` | 時序數據，**TimescaleDB hypertable**，主鍵為 `(sensor_id, reading_time)`，`value` 固定 `NUMERIC` |
| `sensor_readings_translated` | (view) | 在 `sensor_readings` 之上自動套用 `sensors.state_dictionary` 翻譯成文字，原始表不受影響 |

---

## 📝 資料寫入規則（時序層）

由 `data_layer/timeseries_writer.py` 的 `SensorReadingWriter` 負責，規則如下：

1. 該 `sensor_id` 第一次出現 → 寫入
2. 新值與快取中「上一筆已寫入的值」不同 → 寫入
3. 數值相同，但距離上次寫入已超過 `SENSOR_HEARTBEAT_INTERVAL`（預設 1 小時）→ 仍寫入（心跳，證明感測器仍在正常回報）
4. 其餘情況（數值相同 且 未到心跳時間）→ 不寫入，跳過

判斷全部在記憶體快取中完成，不會每輪都查一次資料庫；服務啟動時會呼叫 `load_initial_cache()`，把每個 `sensor_id` 在資料庫裡目前最新的一筆讀回來初始化快取，避免程式重啟後心跳判斷從頭算。

實際寫入時機（依採集程式而定）：

| 協議 | 暫存 (`stage`) 時機 | 批次寫入 (`flush`) 時機 |
| --- | --- | --- |
| S7 | 每個點位解析成功後 | 該輪 S7 採集結束時 |
| Modbus | 每個點位讀取成功且非狀態字典文字時 | 該輪 Modbus 採集結束時 |
| OPC UA | 每個 Server upsert 完 `opcua_tags` 後，依查回的 `sensor_id` 暫存 | 全部 Server 掃描完成後 |

> 💡 三個 collector 內都有 `sensor_reading_writer` 這個共用單例，`flush()` 呼叫一定要存在，缺了這行資料會停在記憶體緩衝區、永遠寫不進資料庫，而且不容易察覺（詳見下方疑難排解表）。

---

## 🖥️ 網頁管理後台（admin_app.py）

啟動：`streamlit run admin_app.py --server.port <ADMIN_PORT>`（或直接用 `run_all.py` 一起啟動），需輸入 `.env` 設定的帳號密碼登入。

| 分頁 | 功能 |
| --- | --- |
| 📡 Modbus 點位設定 | 新增/編輯 Modbus 點位連線參數、即時測試讀取、綁定感測器（下拉選單顯示「編號（暱稱）」） |
| 📡 TIA (S7) 點位設定 | 新增/編輯 S7 點位連線參數、即時測試讀取、綁定感測器 |
| 📡 OPC UA 點位設定 | 新增/編輯 OPC UA Server、測試連線與瀏覽、手動立即瀏覽寫入資料庫、綁定已採集點位的感測器 |
| 🧬 感測器階層管理 | 建立廠區 → 產線 → 設備 → 感測器四層資料；感測器清單可直接編輯 `nickname`、`sensor_type`、`unit`、閾值、`state_dictionary` |
| 🚨 異常監控 | 彙整連線異常（`plc_state ≠ ONLINE`）、數值超出 `min_threshold`/`max_threshold`、資料斷更（超過設定時數沒有新的 `sensor_readings`）三種異常 |

**建議操作順序**：

1. 到「感測器階層管理」由上而下建好階層資料（至少建一個 `sensor`），需要的話填 `nickname`、`state_dictionary`
2. 回到對應協議分頁，把點位的感測器綁定欄位選好並儲存
3. 下一輪採集開始後 `sensor_readings` 就會有資料；未綁定的點位不影響原本即時值 / MQTT 功能
4. 平常巡檢直接看「🚨 異常監控」分頁即可，不用手動下 SQL

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

| 工業俗稱 | `byte_order` | `word_order` | 備註 |
| --- | --- | --- | --- |
| Big Endian (ABCD) | BIG | BIG | Modbus 官方標準 |
| Word Swap (CDAB) | BIG | LITTLE | 🌟 **台灣電表最常見！** |
| Byte Swap (BADC) | LITTLE | BIG | - |
| Little Endian (DCBA) | LITTLE | LITTLE | 部分歐美設備 |

---

## 常用查詢範例

### 查詢某設備底下所有感測器的最新數值

```sql
SELECT s.sensor_code, s.nickname, s.unit, r.value, r.reading_time
FROM sensors s
JOIN LATERAL (
    SELECT value, reading_time
    FROM sensor_readings
    WHERE sensor_id = s.sensor_id
    ORDER BY reading_time DESC
    LIMIT 1
) r ON true
WHERE s.device_id = 1;
```

### 每個 sensor 各拿最新 N 筆（避免單一 sensor 洗版整個查詢結果）

```sql
-- 直接用 ORDER BY sensor_id DESC, reading_time DESC LIMIT 100
-- 如果某個 sensor_id 資料量特別大，會把其他 sensor 都擠出 LIMIT 之外，要注意
SELECT * FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY sensor_id ORDER BY reading_time DESC) AS rn
    FROM sensor_readings
) t
WHERE rn <= 5
ORDER BY sensor_id, reading_time DESC;
```

### 查詢自動翻譯成文字狀態的歷史資料

```sql
SELECT * FROM sensor_readings_translated
WHERE sensor_id = 5
ORDER BY reading_time DESC
LIMIT 20;
```

### 計算某感測器每小時平均值

```sql
SELECT
    time_bucket('1 hour', reading_time) AS hour,
    AVG(value) AS avg_value,
    MIN(value) AS min_value,
    MAX(value) AS max_value
FROM sensor_readings
WHERE sensor_id = 1
  AND reading_time > now() - INTERVAL '7 days'
GROUP BY hour
ORDER BY hour;
```

### 檢查是否有感測器超過閾值

```sql
SELECT s.sensor_code, s.nickname, r.value, s.min_threshold, s.max_threshold, r.reading_time
FROM sensors s
JOIN LATERAL (
    SELECT value, reading_time
    FROM sensor_readings
    WHERE sensor_id = s.sensor_id
    ORDER BY reading_time DESC
    LIMIT 1
) r ON true
WHERE r.value < s.min_threshold OR r.value > s.max_threshold;
```

### 找出哪些即時層點位還沒綁定 sensor_id

```sql
SELECT 'modbus' AS source, id, name FROM modbus_scada WHERE sensor_id IS NULL
UNION ALL
SELECT 'tia', id, name FROM tia_scada WHERE sensor_id IS NULL
UNION ALL
SELECT 'opcua', id, node_id FROM opcua_tags WHERE sensor_id IS NULL;
```

### 把查詢 session 的顯示時區改成台灣時間

```sql
-- 只在這次連線 session 生效
SET timezone = 'Asia/Taipei';

-- 整個資料庫預設都改（推薦），改完要重新連線才生效
ALTER DATABASE your_db_name SET timezone TO 'Asia/Taipei';
```

> `TIMESTAMPTZ` 內部一律用 UTC 儲存，`+00`／`+08` 只是顯示用的時區偏移量，不是資料寫錯，詳見下方疑難排解表。

---

## 🔧 模組說明

### `collector/run_modbus_collector.py`

Modbus 的資料擷取主程式。讀值 → 寫回 `modbus_scada`（即時層）→ 依 `sensor_id` 暫存數值（原始數字，非狀態字典文字）→ 批次 flush 進 `sensor_readings`（時序層）。

### `collector/run_s7_collector.py`

TIA_S7 的資料擷取主程式，流程同上。

### `collector/run_opcua_collector.py`

OPC UA 的資料擷取主程式。並行掃描所有啟用中的 Server，upsert 進 `opcua_tags` 時刻意保留既有的 `sensor_id` 綁定，全部掃描完後統一呼叫 `sensor_reading_writer.flush()` 寫入時序層，並依查詢結果併入 `main.py` 的 MQTT 增量上傳。

### `protocols/s7_protocol.py` / `protocols/modbus_protocol.py` / `protocols/opcua_protocol.py`

各協議連線與讀值封裝，維持原始設計未變動。

### `parsers/plc_parser.py` / `parsers/encoder.py`

S7 / Modbus 的位元組解析與線性縮放，維持原始設計未變動。

### `data_layer/db_connector.py`

PostgreSQL 連線池管理器。

### `data_layer/batch_updater.py`

即時層批量更新模組。OPC UA upsert 完後會額外查回這批 `node_id` 目前的 `sensor_id` 綁定狀態，交給 `timeseries_writer` 暫存；目前保留了診斷用的 `logger.info` 級別 log（`🔗`/`🧪` 開頭），確認系統穩定運作後可考慮調降回 `debug`。

### `data_layer/timeseries_writer.py`

時序層寫入模組。依「數值不同才寫、否則心跳補寫」規則，用記憶體快取判斷是否需要寫入 `sensor_readings`，並批次 flush。目前保留了診斷用的 log（`🧾`/`📦`/`🚿` 開頭，含 `writer_id` 物件識別碼），用來排查「有暫存但沒真的寫入」這類問題；確認穩定後可調降回 `debug` 等級，避免點位一多洗版。

### `messaging/mqtt_publisher.py`

MQTT 發送器：處理與 MQTT Broker 的連線、認證與發布，支援 QoS 1，並做增量比對（數值變化才發送）。

### `main.py`

統一主程式入口。整合 S7 / Modbus / OPC UA 三協議，依序執行輪詢、比對、即時層批量更新、時序層寫入，並在 `MQTT_ENABLED=true` 時把三協議資料一併做 MQTT 增量上傳。

### `admin_app.py`

Streamlit 網頁管理後台：連線參數管理、感測器階層與綁定管理（含 nickname / state_dictionary）、異常監控儀表板。需要帳號密碼登入。

### `run_all.py`

同時啟動 `main.py`（採集主程式）與 `admin_app.py`（Streamlit 網頁後台）。

---

## 🛠️ 維運建議

### 資料壓縮（選用，資料量成長後再啟用）

```sql
ALTER TABLE sensor_readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'sensor_id'
);

SELECT add_compression_policy('sensor_readings', INTERVAL '30 days');
```

### 資料保留策略（選用）

```sql
SELECT add_retention_policy('sensor_readings', INTERVAL '1 year');
```

### 定期確認 hypertable 分區狀況

```sql
SELECT * FROM timescaledb_information.chunks
WHERE hypertable_name = 'sensor_readings'
ORDER BY range_start DESC
LIMIT 10;
```

### 監控寫入延遲/斷線設備

建議另外撰寫排程（例如用 `pg_cron` 或外部排程工具），定期檢查「超過 N 小時沒有新資料」的感測器；或直接使用網頁後台的「🚨 異常監控」分頁，內建這個功能且時數門檻可調整。

---

## ❓ 常見問題排查

| 現象 | 可能原因 | 處理方式 |
| --- | --- | --- |
| `permission denied for table xxx` | DB 使用者權限不足 | `GRANT SELECT, INSERT, UPDATE ON xxx TO your_db_user;` |
| `permission denied for sequence xxx_id_seq` | 序列（sequence）權限跟資料表權限是分開的兩個物件，容易漏授權 | `GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO your_db_user;` |
| `sensor_readings` 一直沒有資料，但 log 也沒有明顯錯誤 | 1) 點位尚未綁定 `sensor_id`；2) `collector/run_opcua_collector.py` 缺少 `sensor_reading_writer.flush()` 呼叫，導致資料停在記憶體緩衝區、程式重啟就消失 | 1) 到 admin_app.py 綁定 `sensor_id`；2) 確認三個 collector 檔案都有 `flush()` 呼叫（可用 `grep -n flush collector/*.py` 確認） |
| `UNION could not convert type time with time zone to timestamp with time zone` | 某張表（例如 `tia_scada`）的 `last_update` 誤設為 `TIME WITH TIME ZONE`，跟其他表的 `TIMESTAMPTZ` 型態不一致 | 跑 `sql/003_fix_tia_scada_last_update_type.sql` 修正型態 |
| `reading_time` 顯示 `+00` 而不是台灣的 `+08` | 這不是資料錯誤，`TIMESTAMPTZ` 內部一律以 UTC 儲存，顯示的時區偏移量取決於查詢 session 的 `timezone` 設定 | `ALTER DATABASE your_db_name SET timezone TO 'Asia/Taipei';`（需重新連線生效） |
| 查詢 `ORDER BY sensor_id DESC, reading_time DESC LIMIT 100` 好像只看到某一個 sensor 的資料 | 該 sensor 資料量已經超過 LIMIT 筆數，把其他 sensor 都擠出結果之外，資料庫裡其實都有 | 改用 `PARTITION BY sensor_id` 的視窗函式查詢（見上方查詢範例），或針對單一 `sensor_id` 查詢 |
| Modbus 狀態字典的中文文字（如「待機」）沒有進 `sensor_readings` | 屬正常行為，`sensor_readings.value` 只接受數字 | 到 `sensors.state_dictionary` 設定對照表，查詢 `sensor_readings_translated` 這個 view 即可看到翻譯後的文字 |
| 已設定 `MQTT_ENABLED=false` 仍看到 MQTT 連線 log | `main.py` 裡殘留重複的 `MQTTPublisher()` 初始化程式碼 | 檢查 `main()` 內是否有兩段初始化邏輯，刪除舊的那段，只保留 `if mqtt_enabled: ...` 那組 |

---

## 📎 附錄：欄位命名慣例

| 慣例 | 說明 |
| --- | --- |
| `*_id` | 主鍵，皆為流水號 |
| `*_code` | 對外可見的業務編號（唯一），與內部流水號分開 |
| `*_time` | 時間戳記，統一使用 `TIMESTAMPTZ`（含時區）避免時區混淆 |
| `sensor_id` | 即時層三張表用來對應時序層 `sensors` 階層的外鍵，可為 NULL |
| `nickname` | 人類可讀的暱稱，純粹方便對照識別，不參與任何程式邏輯判斷 |
| `state_dictionary` | JSONB 格式的「數字代碼 → 文字狀態」對照表，只在查詢/顯示層套用，不影響底層數值儲存 |