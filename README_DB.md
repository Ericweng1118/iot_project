# 資料庫 Schema 參考（Database Reference）

本檔是**資料庫這一層的參考手冊**：即時層與時序層每張表的欄位定義、寫入規則、
常用查詢與維運建議。系統功能總覽、安裝設定、執行架構與網頁操作請見
[`README.md`](README.md)；migration 的執行順序見 [`sql/README.md`](sql/README.md)。

---

## 📊 資料庫架構總覽

```
【即時層】                          【時序層】
tia_scada       ─┐                sites（廠區）
modbus_scada     ├─ sensor_id ──▶     └── production_lines（產線）
opcua_tags      ─┘                          └── devices（設備）
                                                   └── sensors（感測器）
                                                          └── sensor_readings（時序數據，hypertable）
```

- 即時層三張表維持你原本的欄位與用途（PLC 連線參數、當下最新值、連線狀態），採集程式邏輯完全不變
- 每張即時層表新增一個 `sensor_id INTEGER REFERENCES sensors(sensor_id)` 欄位，作為串接時序層的橋樑
- `sensor_readings` 是唯一會快速成長的表，已轉換為 TimescaleDB hypertable 依時間自動分區
- 重複數值的過濾在**寫入端**（`data_layer/timeseries_writer.py`）處理，資料庫本身不做重複值過濾

> 以下四張即時層表由 [`sql/000_realtime_tables.sql`](sql/000_realtime_tables.sql) 建立，
> `sensor_id` 欄位則由 [`sql/001`](sql/001_sensor_hierarchy_and_mapping.sql) 補上。

### 即時層：TIA_SCADA 表格（西門子 PLC 點位）

| 欄位名稱 | 型態 | 說明 |
| --- | --- | --- |
| `id` | BIGSERIAL | 主鍵，自動遞增 |
| `name` | VARCHAR/TEXT | 點位名稱（用作 MQTT Key 與變動比對基準） |
| `plc_ip` | VARCHAR | PLC IP 地址 |
| `db_number` | INTEGER | S7 DB 區塊號碼 |
| `offset` | INTEGER | 記憶體偏移量 |
| `data_type` | VARCHAR | 資料型態（DINT, REAL, BOOL, INT 等） |
| `plc_name` | VARCHAR | PLC 設備名稱 |
| `current_data` | JSONB | 採集數據（格式：`{"val": 123.45}`） |
| `plc_state` | VARCHAR | 連線狀態（ONLINE/OFFLINE/ERROR） |
| `last_update` | TIMESTAMPTZ | 最後成功更新時間 |
| `sensor_id` 🆕 | INTEGER | 對應 `sensors.sensor_id`，NULL 代表尚未綁定，不會寫入 `sensor_readings` |

### 即時層：modbus_scada 表格（Modbus TCP 點位）

| 欄位名稱 | 型態 | 說明 |
| --- | --- | --- |
| `id` | BIGSERIAL | 主鍵，自動遞增 |
| `name` | VARCHAR/TEXT | 點位名稱 |
| `plc_ip` | VARCHAR | PLC/儀表 IP 地址 |
| `plc_port` | INTEGER | Modbus 埠號（預設 502） |
| `slave_id` | INTEGER | Modbus 從站 ID (1-247) |
| `function_code` | INTEGER | 功能碼 (1=Coils, 2=Discrete Inputs, 3=Holding Registers, 4=Input Registers) |
| `start_address` | INTEGER | 起始暫存器/位址 |
| `data_type` | VARCHAR | 資料型態（bool, word, int, dint, float, uint32, int64, float64, uint64） |
| `raw_min` / `raw_max` | REAL | 原始值上下限 |
| `eng_min` / `eng_max` | REAL | 工程值上下限 |
| `byte_order` / `word_order` | VARCHAR | 位元組 / 字組順序（BIG/LITTLE） |
| `state_dictionary` | JSONB | 狀態字典（例如：`{"1": "待機", "2": "運轉"}`） |
| `unit` | TEXT | 工程單位（選填），例如 kW / °C。僅供顯示，不參與數值換算。由 `sql/008` 建立 |
| `current_value` | REAL | 當前數值（純數值格式） |
| `current_data` | JSONB | 採集數據（格式：`{"val": 123.45}`，可能是數字或狀態字典轉出的文字） |
| `plc_state` | VARCHAR | 連線狀態（ONLINE/OFFLINE/ERROR） |
| `last_update` | TIMESTAMPTZ | 最後成功更新時間 |
| `sensor_id` 🆕 | INTEGER | 對應 `sensors.sensor_id` |

> ⚠️ **注意**：`sensor_readings.value` 是 `NUMERIC`，只能存數字。透過 `state_dictionary` 轉換出來的中文狀態字（如「待機」）**不會**寫進 `sensor_readings`，但仍會正常寫進 `modbus_scada.current_data`，MQTT 增量上傳不受影響。

### 即時層：opcua_servers 表格（已知 OPC UA Server 清單）

| 欄位名稱 | 型態 | 說明 |
| --- | --- | --- |
| `id` | SERIAL PK | |
| `server_name` | VARCHAR(100) | Server 顯示名稱（用於 MQTT Key），唯一 |
| `ip` / `port` | VARCHAR / INTEGER | 連線位址（port 預設 4840） |
| `username` / `password` | VARCHAR | 可為 NULL（匿名連線） |
| `security_policy` / `security_mode` | VARCHAR | 預設皆為 `None` |
| `root_node_id` | VARCHAR(100) | 瀏覽起始節點，預設 `i=85`（Objects 資料夾） |
| `browse_depth` | INTEGER | 遞迴瀏覽深度上限，預設 5 |
| `enabled` | BOOLEAN | 是否啟用此 Server |
| `publish_interval_ms` | INTEGER | 這台 Server 的預設訂閱取樣頻率（毫秒）。NULL = 沿用 `.env` 的 `OPCUA_PUBLISH_INTERVAL_MS`。由 `sql/009` 建立 |
| `resubscribe_requested` | BOOLEAN NOT NULL | 預設 `FALSE`。網頁按下「🚀 立即瀏覽並寫入資料庫」時設為 `TRUE`，訂閱服務的維護迴圈讀到後重建訂閱並清回 `FALSE`。由 `sql/008` 建立 |
| `conn_state` | VARCHAR(20) | 預設 `UNKNOWN`；運作中為 ONLINE / OFFLINE / ERROR |
| `last_scan` / `last_error` | TIMESTAMPTZ / TEXT | |

### 即時層：opcua_tags 表格（週期性瀏覽出來的點位與最新數值）

| 欄位名稱 | 型態 | 說明 |
| --- | --- | --- |
| `id` | SERIAL PK | |
| `server_id` | INTEGER | REFERENCES `opcua_servers(id)` ON DELETE CASCADE |
| `server_name` | VARCHAR(100) | 冗餘存一份，方便 MQTT Key 組合 |
| `node_id` | VARCHAR(200) | OPC UA NodeId 字串，例如 `ns=2;s=Temp01` |
| `browse_name` / `display_name` | VARCHAR(200) | |
| `data_type` | VARCHAR(50) | OPC UA VariantType 名稱 |
| `current_data` | JSONB | 格式：`{"val": 123.45}` |
| `quality` | VARCHAR(20) | GOOD / BAD / UNCERTAIN |
| `plc_state` | VARCHAR(20) | ONLINE / OFFLINE，預設 OFFLINE |
| `last_update` | TIMESTAMPTZ | |
| `sensor_id` 🆕 | INTEGER | 對應 `sensors.sensor_id`。upsert 時刻意不覆蓋此欄位，保護手動綁定的對應關係 |

### 時序層：sites / production_lines / devices / sensors / sensor_readings

| 資料表 | 主要欄位 | 說明 |
| --- | --- | --- |
| `sites` | `site_id`, `site_name`, `location` | 廠區（階層最上層） |
| `production_lines` | `line_id`, `site_id`, `line_name` | 產線，屬於某個廠區 |
| `devices` | `device_id`, `line_id`, `device_code`(唯一), `device_name`, `device_type`, `manufacturer`, `install_date`, `status`, `device_nickname`, `cost` | 設備，屬於某條產線；`status` 預設 `active`。`device_nickname` / `cost` 由 `sql/008` 建立，目前程式碼沒有讀取，保留給外部報表 |
| `sensors` | `sensor_id`, `device_id`, `sensor_code`(唯一), `sensor_type`, `unit`, `min_threshold`, `max_threshold`, `nickname`, `state_dictionary` **＋ v2 新增欄位（見下表）** | 感測器，屬於某台設備；閾值欄位供「異常監控」分頁比對用，資料庫本身不會自動觸發告警 |
| `sensor_readings` | `reading_id`, `sensor_id`, `reading_time`, `value` | 時序數據，**TimescaleDB hypertable**，主鍵為 `(sensor_id, reading_time)` |

---

#### `sensors` 的 v2 新增欄位

由 [`sql/006_opcua_upgrade.sql`](sql/006_opcua_upgrade.sql)、
[`sql/007_opcua_deadband.sql`](sql/007_opcua_deadband.sql) 與
[`sql/008_missing_app_columns.sql`](sql/008_missing_app_columns.sql) 建立：

| 欄位 | 型態 | 預設 | 說明 |
| --- | --- | --- | --- |
| `opcua_sampling_interval_ms` | INTEGER | NULL | OPC UA 訂閱該點位的取樣頻率（毫秒）。NULL = 沿用 `opcua_servers.publish_interval_ms` 或 `.env` 的 `OPCUA_PUBLISH_INTERVAL_MS` |
| `opcua_deadband_type` | VARCHAR(20) | `none` | 伺服器端 DataChangeFilter：`none` / `percent` / `absolute`。決定**伺服器要不要把這筆變化送過來** |
| `opcua_deadband_value` | NUMERIC | NULL | 搭配 `opcua_deadband_type` 的門檻值；`none` 時不生效 |
| `upload_condition` | VARCHAR(20) | `threshold_percent` | 統一寫入排程的判斷條件：`always` / `on_change` / `threshold_percent` / `threshold_absolute`。決定**收到之後要不要寫進 `sensor_readings`** |
| `upload_threshold` | NUMERIC | `1` | 搭配 `upload_condition` 的門檻值：`threshold_percent` 填百分比數字（`1` = 1%），`threshold_absolute` 填絕對值 |
| `nickname` | VARCHAR(100) | NULL | 感測器暱稱（選填），網頁下拉選單會顯示成 `sensor_code (nickname)`。有 `idx_sensors_nickname` 索引 |
| `state_dictionary` | JSONB | NULL | 狀態字典（選填），把數值對應成文字，例如 `{"0":"待機","8":"大火燃燒"}`，格式範例見 `狀態字典範例.json` |

兩組欄位有各自 CHECK constraint（`chk_sensors_opcua_deadband_type`、
`chk_sensors_upload_condition`），寫入不在允許清單內的字串會被資料庫擋下。

> `opcua_deadband_type` 與 `upload_condition` 是**兩個不同層級**的過濾，可疊加：
> 前者在來源端省頻寬，後者在寫入端省 DB 空間與 I/O。

---

## 📝 資料寫入規則（時序層）

由 [`data_layer/timeseries_writer.py`](data_layer/timeseries_writer.py) 的
`SensorReadingWriter` 單例負責，v2 起拆成兩層：

**1. 最新值快取（高頻、純記憶體、無 DB I/O）**

採集端（OPC UA 訂閱服務為主，Modbus / TIA 若啟用也共用同一個單例）每讀到新值就
呼叫 `update_latest(sensor_id, value)`，只更新記憶體。`sensor_id` 為 NULL 的點位
直接略過，不會進時序層。

**2. 統一寫入排程（低頻、固定週期）**

背景執行緒每 `SENSOR_READING_FLUSH_INTERVAL` 秒（`.env`，預設 60）醒來一次，
先重讀 `sensors` 的 `upload_condition` / `upload_threshold`（所以網頁上改完最慢
下一輪生效、不用重啟），再逐一判斷、批次 `INSERT ... ON CONFLICT DO NOTHING`：

| `upload_condition` | 寫入條件 |
| --- | --- |
| `always` | 不判斷，每一輪都寫 |
| `on_change` | 數值與上次**實際寫入**的值不同才寫 |
| `threshold_percent`（預設） | 變化百分比達到 `upload_threshold` 才寫，以上次實際寫入值為基準。上次寫入值為 0 時百分比無定義，退化為「新值不再是 0 就寫」 |
| `threshold_absolute` | 變化絕對值達到 `upload_threshold` 才寫 |

補充規則：

- 該 `sensor_id` **第一次出現**（快取沒有上次寫入值）→ 一律寫入
- 服務啟動時 `load_initial_cache()` 會把每個 `sensor_id` 目前資料庫裡最新一筆讀
  回來當基準，避免重啟後判斷從頭算
- `sensor_readings.value` 是 `NUMERIC`，非數值（例如 Modbus 經 `state_dictionary`
  轉出的中文狀態字）會被略過，但仍正常寫進即時層的 `current_data`
- 全部判斷都在記憶體完成，內部用 `threading.Lock` 保護，多執行緒同時呼叫安全

> ⚠️ v1 的 `SENSOR_HEARTBEAT_INTERVAL`（數值沒變化時定期補寫一筆）**已在 v2 移除**，
> 程式碼不再讀取這個變數。等效行為請改用 `upload_condition = always` 搭配
> `SENSOR_READING_FLUSH_INTERVAL`。

---

## 常用查詢範例

### 查詢某設備底下所有感測器的最新數值

```sql
SELECT s.sensor_code, s.sensor_type, s.unit, r.value, r.reading_time
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

### 查詢某感測器過去 24 小時的數據

```sql
SELECT reading_time, value
FROM sensor_readings
WHERE sensor_id = 1
  AND reading_time > now() - INTERVAL '24 hours'
ORDER BY reading_time;
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
SELECT
    s.sensor_code,
    r.value,
    s.min_threshold,
    s.max_threshold,
    r.reading_time
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

---

---

## 🆕 v3 新增資料表（`sql/011`、`sql/012`）

### `alarm_rules`：警報規則

| 欄位 | 型態 | 說明 |
| --- | --- | --- |
| `rule_id` | SERIAL PK | |
| `sensor_id` | INTEGER FK → sensors（ON DELETE CASCADE） | |
| `alarm_type` | VARCHAR(4) | `HH` / `H` / `L` / `LL` / `EQ` / `NE` |
| `setpoint` | NUMERIC | 設定值 |
| `deadband` | NUMERIC ≥ 0 | 遲滯：H 類要降到 setpoint − deadband 才恢復，L 類反之 |
| `on_delay_sec` | INTEGER ≥ 0 | 條件連續成立多少秒才觸發 |
| `priority` | SMALLINT 1~4 | 1 緊急 / 2 高 / 3 中 / 4 低 |
| `message` | VARCHAR(200) | 附加訊息（處置方式等） |
| `enabled` | BOOLEAN | |

`sensors.min_threshold` / `max_threshold` 另外會被警報引擎視為隱含的 L / H 規則，不存在這張表。

### `alarm_events`：警報事件

| 欄位 | 說明 |
| --- | --- |
| `alarm_key` | 警報來源識別：`rule:<rule_id>`、`limit:<sensor_id>:H`、`server:<id>:offline`、`plc:<table>:<ip>:offline` |
| `source_type` | `sensor_rule` / `sensor_limit` / `server_offline` / `device_offline` |
| `raised_at` / `cleared_at` | 發生 / 恢復時間；`cleared_at IS NULL` = 仍在發生 |
| `acked_at` / `acked_by` / `ack_comment` | 確認紀錄 |
| `trigger_value` / `setpoint` / `clear_value` | 觸發值、設定值、恢復時的值 |

部分唯一索引 `uq_alarm_events_active_key (alarm_key) WHERE cleared_at IS NULL` 確保同一來源同時只有一筆發生中。
「目前警報」= `cleared_at IS NULL OR acked_at IS NULL`。

### `app_users` / `audit_log` / `service_status`

| 表 | 重點欄位 |
| --- | --- |
| `app_users` | `username` PK、`password_hash`（`pbkdf2_sha256$迭代$salt$hash`）、`role`（viewer/operator/engineer/admin）、`enabled`、`last_login` |
| `audit_log` | `ts`、`username`、`action`（如 `sensor.update`、`alarm.ack`、`login.failed`）、`target`、`detail` JSONB（欄位舊值 → 新值） |
| `service_status` | `service_name` PK（`collector`）、`last_heartbeat`、`started_at`、`info` JSONB（寫入排程 / OPC UA / 警報引擎統計） |

### 🆕 v3.1 新增欄位（`sql/014`、`sql/015`）

| 欄位 | 說明 |
| --- | --- |
| `sensor_readings.quality` SMALLINT | 0 正常｜1 保持值（數值沒變、心跳補寫）｜2 不確定｜3 品質不良（只記錄轉為不良的那一筆）｜4 通訊中斷（斷線時記一筆，數值沿用最後值）｜NULL 舊資料（視為正常）。**統計時請排除 3 / 4**：`WHERE quality IS NULL OR quality < 3` |
| `modbus_scada.transport` | `tcp` / `rtu_over_tcp` / `rtu`（`rtu` 時 `plc_ip` 填序列埠路徑） |
| `modbus_scada.serial_settings` | `transport=rtu` 時的「鮑率,資料位元,同位,停止位元」，例如 `9600,8,N,1` |
| `modbus_scada.enabled` | FALSE = 暫停採集（保留設定） |

### 🆕 v3.2 新增資料表（`sql/016` ~ `sql/018`）

| 表 | 重點欄位 |
| --- | --- |
| `device_templates` | `name`（唯一）、`protocol`（none / modbus / opcua）、`definition` JSONB（感測器、點位、警報規則，格式見 `data_layer/templates.py`） |
| `calculated_points` | `sensor_id`（唯一，結果感測器）、`expression`、`enabled`；`current_value` / `state`（ONLINE / OFFLINE / ERROR）/ `last_error` 由計算引擎回寫 |
| `report_schedules` | `frequency`（daily / weekly / monthly）、`send_time`、`weekday`、`day_of_month`、`metrics[]`、`device_codes[]`、`sensor_codes[]`、`recipients[]`、`last_run_at` / `last_status` |

---

## 🛠️ 維運建議

### 資料壓縮（選用，資料量成長後再啟用）

已整理成 [`sql/013_timeseries_policy.sql`](sql/013_timeseries_policy.sql)（idempotent、只壓縮不刪資料、
可用 `-v compress_after="'14 days'"` 調整門檻）。啟用後網頁「系統狀態」會顯示政策。

### 資料保留策略（選用，不可逆）

```sql
-- 確認保留年限後才執行，超過的資料會永久刪除
SELECT add_retention_policy('sensor_readings', INTERVAL '3 years', if_not_exists => true);
```

### 定期確認 hypertable 分區狀況

```sql
SELECT * FROM timescaledb_information.chunks
WHERE hypertable_name = 'sensor_readings'
ORDER BY range_start DESC
LIMIT 10;
```

### 監控寫入延遲/斷線設備

建議另外撰寫排程（例如用 `pg_cron` 或外部排程工具），定期檢查「超過 N 小時沒有新資料」的感測器，即可視為設備離線或斷線，及早發現異常。

### 常見問題排查（資料庫相關）

| 現象 | 可能原因 | 處理方式 |
| --- | --- | --- |
| `permission denied for table sensor_readings` | DB 使用者權限不足，或 hypertable 底層新 chunk 沒繼承權限 | 用管理員帳號補 GRANT，並設定 `ALTER DEFAULT PRIVILEGES` |
| `sensor_readings` 一直沒有資料 | 點位尚未綁定 `sensor_id` | 到 admin_app.py「感測器階層管理」建好階層 + 綁定 |
| Modbus 點位有值但沒進 `sensor_readings` | 該點位透過 `state_dictionary` 轉成文字狀態，非數值 | 屬正常行為，`sensor_readings` 僅存數字 |

---

---

## 📎 附錄：欄位命名慣例

| 慣例 | 說明 |
| --- | --- |
| `*_id` | 主鍵，皆為流水號 |
| `*_code` | 對外可見的業務編號（唯一），與內部流水號分開 |
| `*_time` | 時間戳記，統一使用 `TIMESTAMPTZ`（含時區）避免時區混淆 |
| `sensor_id` | 即時層三張表用來對應時序層 `sensors` 階層的外鍵，可為 NULL |
