# 多功能工業 PLC 資料採集系統 (Unified Industrial Data Collector)

一個支援 **西門子 S7**、**Modbus TCP**、**OPC UA** 三協議的統一工業資料採集系統，
具備併發採集、OPC UA 訂閱推播、PostgreSQL/TimescaleDB 時序儲存、
感測器階層管理、異常監控與 MQTT 增量上傳功能。

**v2 升級重點**：Modbus / TIA(S7) 可透過 `.env` 完全屏蔽（含網頁分頁），系統聚焦
強化 OPC UA——逐感測器可調訂閱頻率、伺服器端 deadband 過濾、只訂閱已綁定的點位
（省頻寬）；`sensor_readings` 改為全域統一週期性寫入，配合逐感測器的寫入條件
（百分比 / 絕對值 / 不判斷）。詳見下方各節與 [`UPGRADE_NOTES_v2.md`](UPGRADE_NOTES_v2.md)。

---

## 🚀 核心功能

### 1. 多協議支援（可個別開關）

- **OPC UA（主力協議）** — 透過 `asyncua`，採用訂閱推播（Subscription）而非輪詢瀏覽，
  支援逐感測器自訂訂閱頻率與伺服器端 deadband 過濾，詳見下方「OPC UA 訂閱服務」
- **Modbus TCP** — 透過 `pymodbus` 支援 FC1/2/3/4/5/6/15/16，處理多位元組資料型態與 Byte/Word Order
- **S7 Protocol (Siemens)** — 透過 `snap7` 連線 S7-1200/1500 PLC，DB 區塊打包讀取

🆕 **Modbus / TIA(S7) 可透過 `.env` 完全屏蔽**：`MODBUS_ENABLED=false` /
`TIA_ENABLED=false` 時，`main.py` 不會啟動該協議的採集執行緒，`admin_app.py`
網頁後台也會完全隱藏對應分頁（不是灰掉，是分頁不存在）。預設值皆為 `true`，
相容既有部署；要屏蔽的話在 `.env` 設為 `false` 即可，不需要改程式碼。

### 2. 併發採集架構

- Modbus、TIA(S7)（若都啟用）在每一輪採集中**併發執行**（`ThreadPoolExecutor`），
  其中一個協議整體卡住不會拖到另一個協議完全沒開始；兩者都停用時主迴圈直接跳過這一步
- 同一協議內，多台 PLC/設備也是併發連線，一台離線只影響自己那條執行緒
- PostgreSQL 連線池使用 `ThreadedConnectionPool`（執行緒安全）

### 3. OPC UA 訂閱服務（Report by Exception + 逐感測器頻率 + 伺服器端過濾）

OPC UA 常駐一個獨立的背景服務，跟主迴圈完全脫鉤：

- **只在必要時才做結構性瀏覽（browse）**：第一次啟動、或使用者於網頁手動觸發重新整理
- **只訂閱已綁定感測器的點位** 🆕：瀏覽發現的所有點位都會存進 `opcua_tags`（供網頁挑選），
  但只有設定了 `sensor_id` 的點位才會實際建立 OPC UA 訂閱，未綁定的點位不佔訂閱資源、
  不消耗頻寬。綁定/解除綁定後最慢 5 秒內自動生效
- **可調訂閱頻率（三層優先序）** 🆕：`sensors.opcua_sampling_interval_ms`（逐感測器）
  > `opcua_servers.publish_interval_ms`（逐 Server）> `.env` 的 `OPCUA_PUBLISH_INTERVAL_MS`
  （全域）。相同頻率的點位會被自動歸進同一組 Subscription（OPC UA 的
  publishing interval 是 Subscription 層級屬性），不同頻率各自獨立 Subscription。
  ⚠️ 部分設備（尤其嵌入式協議轉換盒）的 OPC UA Server 有自己固定的內部更新週期，
  會忽略/覆寫用戶端請求的頻率（連線 log 出現 `Revised values returned differ from
  subscription values` 就是這種情況），此時這個設定對該設備不生效
- **伺服器端 Deadband 過濾** 🆕：`sensors.opcua_deadband_type`（none/percent/absolute）
  + `opcua_deadband_value`，透過 asyncua 的 `Subscription.deadband_monitor()`
  在**伺服器端**就決定要不要把這筆變化透過網路送過來，直接減少不穩定連線上的流量
  （跟 `upload_condition` 是不同層級：deadband 決定「要不要送過來」，
  `upload_condition` 決定「收到之後要不要寫進資料庫」，兩者可疊加使用）。
  `percent` 依賴節點是否設定 EURange，不確定設備有沒有配置時建議用 `absolute`
- 平常靠 Server 端的 Subscription 推播：有變化才通知，本服務收到後先寫進記憶體緩衝區，
  每 2 秒批次更新 `opcua_tags` 即時值，並把已綁定感測器的最新值丟進
  `SensorReadingWriter`（見下方「統一週期性寫入」）
- **統一維護迴圈** 🆕：原本「檢查 resubscribe 旗標」與「定期刷新 sensor_id 綁定」兩個
  背景 task，合併成單一 `_watch_and_maintain()`，每 5 秒檢查一次點位表 / 感測器綁定 /
  取樣頻率 / deadband 設定是否有變化，有變化就整批重建訂閱（犧牲一點效率換取分組邏輯
  單純、不容易在邊界情況留下不一致的訂閱殘留）
- **連線穩定性可調參數** 🆕：`OPCUA_CLIENT_TIMEOUT`（單次請求逾時）、
  `OPCUA_HEARTBEAT_INTERVAL_SEC`（心跳週期）、`OPCUA_HEARTBEAT_TIMEOUT_SEC`
  （單次心跳逾時）、`OPCUA_HEARTBEAT_MAX_FAILURES`（連續失敗幾次才判定斷線），
  網路品質不穩的場域可以調高，容忍偶發逾時，不要動不動就整組重建連線/訂閱；
  同時內建重複訊息抑制，避免 asyncua 斷線時每秒狂噴同一則訊息洗版 log
- 心跳目標優先用已訂閱成功的實際點位（不用系統節點 `i=2259`，因為並非所有
  PLC 內建簡化版 OPC UA Server 都完整實作該子節點）
- 斷線後自動指數退避重連（`RECONNECT_BASE_DELAY` → `RECONNECT_MAX_DELAY`），
  `BadTooManySessions` 則用固定較長的 `SESSION_LIMIT_RETRY_DELAY` 重試

### 4. 感測器階層管理與時序資料

- 雙層資料架構：**即時層**（`modbus_scada` / `tia_scada` / `opcua_tags`，存目前值與連線狀態）
  與 **時序層**（`sensor_readings` hypertable，存歷史數值），透過 `sensor_id` 連結
- 階層架構：廠區 (`sites`) → 產線 (`production_lines`) → 設備 (`devices`) → 感測器 (`sensors`) → 讀值 (`sensor_readings`)
- 網頁「感測器階層管理」分頁可直接維護這整條階層，包含 OPC UA 訂閱頻率、deadband、
  上傳條件等進階設定
- Modbus / TIA / OPC UA 三個點位設定分頁（視 `.env` 開關而定是否顯示）都有「綁定感測器」
  下拉選單，並內建跨協議重複綁定偵測

🆕 **`sensor_readings` 統一週期性寫入**（由 `data_layer/timeseries_writer.py` 的
`SensorReadingWriter` 負責）：

原本各協議在自己那一輪採集結束時各自判斷寫入，現在拆成兩層：

1. **最新值快取**（高頻、純記憶體）：採集端（OPC UA 訂閱服務為主，Modbus/TIA 若啟用
   也共用）每次讀到新值就呼叫 `update_latest()`，只更新記憶體、不碰資料庫
2. **統一寫入排程**（低頻、固定週期，由 `.env` 的 `SENSOR_READING_FLUSH_INTERVAL`
   秒控制）：背景執行緒固定週期醒來，把所有感測器的最新值一次性依各自的
   `sensors.upload_condition` 判斷後批次寫入 `sensor_readings`，四選一：
   - `always`：不判斷，每一輪都寫
   - `on_change`：只有數值與上次「實際寫入」的值不同才寫
   - `threshold_percent`（**新建感測器的預設值**）：變化百分比達到 `upload_threshold`
     才寫，以上次實際寫入的值為基準（`upload_threshold` 填百分比數字，例如 `1` = 1%）；
     上次寫入值為 0 時無法算百分比，退化為「新值不再是 0 就寫」
   - `threshold_absolute`：變化絕對值達到 `upload_threshold` 才寫（適合各感測器量級
     差異大、百分比意義不明確的情境）
3. **心跳保底**（由 `.env` 的 `SENSOR_HEARTBEAT_INTERVAL` 秒控制，預設 3600）：
   不論上面判斷結果如何，只要距離上次實際寫入超過這個時間就強制補寫一筆

> ⚠️ **累計型計數器不要用 `threshold_percent`。** 累計電表/流量計的基準值可以到
> 百萬等級、日增量卻只有幾百，「變化 1%」要累積四十幾天才達得到，結果就是資料看起來
> 整個斷掉、但採集其實一切正常。這類單調遞增的點位請用 `on_change`（值一跳動就寫，
> 不需要為每支點位猜門檻）或 `threshold_absolute`（想降低資料量時，門檻用工程單位設定）。
> 正式庫的 63 個累計型感測器已由 [`sql/010`](sql/010_cumulative_counter_upload_condition.sql)
> 統一改成 `on_change`，詳見 [`todo.md`](todo.md) 待辦 #2 的事故紀錄。

> 💡 心跳保底存在的理由：沒有它，門檻設得不合理的點位會**完全沒有任何紀錄、也不會有
> 任何錯誤 log**，只能靠人工發現。有了心跳，「設備沒有新資料」跟「採集系統掛了」
> 在資料上才能區分開來。除非你有別的斷更偵測機制，否則不建議設成 0（關閉）。

判斷全部在記憶體做，服務啟動時會把每個 sensor 目前資料庫裡最新一筆讀回來當快取；
`upload_condition` / `upload_threshold` 調整後最慢下一個統一寫入週期內生效，
不需要重啟服務。三個協議共用同一個 `sensor_reading_writer` 單例，內部用
`threading.Lock` 保護，多執行緒/背景服務同時呼叫也安全。

> `upload_condition`（決定收到之後要不要寫進資料庫）跟 OPC UA 的
> `opcua_deadband_type`（決定伺服器端要不要把這筆變化送過來）是兩個不同層級的
> 過濾，可以疊加使用：deadband 先在源頭省頻寬，upload_condition 再在寫入端省
> DB 空間跟 I/O。

### 5. 異常監控

網頁內建「異常監控」分頁，彙整三種常見異常，且會依 `.env` 協議開關自動排除
停用協議的來源，不會出現一堆「當然是 OFFLINE」的假警報：

- 🔌 **連線異常**：即時層資料表中 `plc_state ≠ ONLINE` 的點位
- 📈 **數值超出正常範圍**：依 `sensors.min_threshold` / `max_threshold` 比對最新一筆 `sensor_readings`
- ⏱️ **資料斷更**：超過使用者設定的時數門檻沒有新資料的感測器（含「從未寫入過」的感測器）

### 6. 數據增量上傳 (Report by Exception)

MQTT 端採用增量發送：快取上一輪的值，只有數值變化時才發送，並定期強制全量心跳上傳。
`MQTT_ENABLED=false` 可完全關閉 MQTT；MQTT 的資料來源查詢也會依 `MODBUS_ENABLED` /
`TIA_ENABLED` 自動跳過停用協議的表。

### 7. PostgreSQL 批量更新

使用 `execute_values` 一次性更新多筆點位；OPC UA 訂閱模式另外提供輕量版
`batch_update_opcua_values`，只更新數值不動結構欄位。

---

## 📁 專案結構

```
├── protocols/                    # 協議驅動層
│   ├── s7_protocol.py            # 西門子 S7 協議實現
│   ├── modbus_protocol.py        # Modbus TCP 協議實現
│   └── opcua_protocol.py         # OPC UA 協議實現（連線逾時可由 .env 調整）
├── parsers/                      # 數據解析器
│   ├── plc_parser.py
│   └── encoder.py
├── data_layer/                   # 數據層
│   ├── db_connector.py           # PostgreSQL 連線池管理（ThreadedConnectionPool）
│   ├── batch_updater.py          # 即時層批量更新
│   └── timeseries_writer.py      # 🆕 sensor_readings 統一週期性寫入器
├── messaging/
│   └── mqtt_publisher.py
├── services/                     # 常駐背景服務
│   └── opcua_subscription_service.py   # 🆕 OPC UA 訂閱服務（逐感測器頻率/deadband/僅訂閱已綁定點位）
├── collector/
│   ├── run_modbus_collector.py
│   ├── run_s7_collector.py
│   └── run_opcua_collector.py    # 獨立測試用一次性瀏覽腳本，main.py 平時走訂閱服務
├── sql/                          # DB migration，依序執行 000 → 001 → 006 → 007 → 008 → 009
│   ├── README.md                 # 各 migration 用途、執行順序、編號斷層說明
│   ├── 000_realtime_tables.sql   # 即時層四張表（tia_scada / modbus_scada / opcua_servers / opcua_tags）
│   ├── 001_sensor_hierarchy_and_mapping.sql   # 階層表 + sensor_readings hypertable + sensor_id 欄位
│   ├── 006_opcua_upgrade.sql     # sensors 新增 opcua_sampling_interval_ms / upload_condition / upload_threshold
│   ├── 007_opcua_deadband.sql    # sensors 新增 opcua_deadband_type / opcua_deadband_value
│   ├── 008_missing_app_columns.sql            # 補齊沒有腳本、但程式碼在用的欄位（全新部署必跑）
│   └── 009_opcua_server_publish_interval.sql  # opcua_servers 新增 publish_interval_ms
├── main.py                       # 統一主程式（依 .env 開關決定啟動哪些協議）
├── admin_app.py                  # 網頁管理後台（依 .env 開關隱藏對應分頁）
├── run_all.py                    # 同時啟動 main.py + admin_app.py
├── requirements.txt
├── Dockerfile
├── env.example                   # .env 範本，複製成 .env 後填值（.env 已 gitignore）
├── 狀態字典範例.json              # state_dictionary 欄位的填寫範例（參考用，程式不讀取）
├── todo.md                       # 待辦事項與已知技術債
├── README.md                     # 本檔：功能、架構、操作、排錯
├── README_DB.md                  # 資料庫 schema 參考（即時層四張表的完整欄位定義）
└── UPGRADE_NOTES_v2.md           # v1 → v2 的行為變化與已知取捨
```

> `sql/` 的編號 002 ~ 005 不存在，不是遺失——那幾版 v1 期間的變更當時是直接在資料庫上手動執行、沒有留下腳本。全新部署依序跑 `000` → `001` → `006` → `007` → `008` → `009` 即可，詳見 [`sql/README.md`](sql/README.md)。

---

## ⚙️ 安裝與設定

### 1. 建立虛擬環境並安裝依賴

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 建立 `.env` 環境變數檔案

以 `env.example` 為範本複製一份，再填入實際值：

```bash
cp env.example .env
```

`env.example` 是**唯一**的環境變數清單來源（已與程式碼實際讀取的 key 對齊），
以下只列出比較需要留意的幾項：

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `MODBUS_ENABLED` / `TIA_ENABLED` / `OPCUA_ENABLED` | `true` | 設為 `false` 完全屏蔽該協議：`main.py` 不啟動採集執行緒，`admin_app.py` 隱藏對應分頁 |
| `POLL_INTERVAL` | `60.0` | Modbus / TIA(S7) 的採集週期（秒）。OPC UA 走常駐訂閱服務，**不受此值影響** |
| `SENSOR_READING_FLUSH_INTERVAL` | `60` | `sensor_readings` 統一批次寫入的固定週期（秒）。不建議設 <5 秒 |
| `SENSOR_HEARTBEAT_INTERVAL` | `3600` | 心跳保底（秒）：距上次實際寫入超過這個時間就強制補寫一筆，不管 `upload_condition` 判斷結果。設 `0` 關閉，**但不建議** |
| `OPCUA_PUBLISH_INTERVAL_MS` | `1000` | **全域**預設訂閱取樣頻率（毫秒）。優先序：`sensors.opcua_sampling_interval_ms`（逐感測器）> `opcua_servers.publish_interval_ms`（逐 Server）> 本值 |
| `OPCUA_CLIENT_TIMEOUT` | `10` | 單次 OPC UA 請求逾時秒數，網路不穩的場域可調高 |
| `OPCUA_HEARTBEAT_INTERVAL_SEC` / `_TIMEOUT_SEC` / `_MAX_FAILURES` | `15` / `8` / `2` | 心跳週期、單次心跳逾時、連續失敗幾次才判定斷線。調高可容忍偶發逾時，避免頻繁整組重建訂閱 |
| `MQTT_ENABLED` | `true` | 設為 `false` 完全不建立 MQTT 連線 |
| `ADMIN_USER` / `ADMIN_PASSWORD` / `ADMIN_PORT` | — | 網頁後台的登入帳密與埠號，**務必改掉預設值** |

> `.env` 已列入 `.gitignore`，請勿提交；`env.example` 只放範例值，不要填入正式環境的密碼。

### 3. 執行資料庫 Migration

**全新部署**：依序執行 `sql/` 內的腳本（002 ~ 005 不存在，跳過即可）。`000` 會建立
即時層四張表，`001` 建立階層表與 `sensor_readings` hypertable，其餘補欄位。

```bash
for f in sql/0*.sql; do
  psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -f "$f" || break
done
```

**既有系統升級**：補跑還沒跑過的那幾支即可，全部腳本都是 idempotent，重複執行安全。

```bash
psql -h <DB_HOST> -U <DB_USER> -d <DB_NAME> -f sql/006_opcua_upgrade.sql
psql -h <DB_HOST> -U <DB_USER> -d <DB_NAME> -f sql/007_opcua_deadband.sql
psql -h <DB_HOST> -U <DB_USER> -d <DB_NAME> -f sql/008_missing_app_columns.sql
psql -h <DB_HOST> -U <DB_USER> -d <DB_NAME> -f sql/009_opcua_server_publish_interval.sql
psql -h <DB_HOST> -U <DB_USER> -d <DB_NAME> -f sql/010_cumulative_counter_upload_condition.sql
```

`006` 幫 `sensors` 加上 `opcua_sampling_interval_ms` / `upload_condition` /
`upload_threshold`；`007` 加上 `opcua_deadband_type` / `opcua_deadband_value`；
`008` 補齊 `sensors.nickname` / `state_dictionary`、`opcua_servers.resubscribe_requested`、
`modbus_scada.unit` 等「程式碼在用但沒有腳本建立」的欄位；`009` 加上
`opcua_servers.publish_interval_ms`；`010` 把累計型感測器的 `upload_condition`
改成 `on_change`（**既有部署一定要跑**，否則累計型點位會長期沒有新資料）。

> ⚠️ migration 要用**資料表擁有者**執行。應用程式帳號（`.env` 裡的 `DB_USER`）
> 通常只有 `SELECT / INSERT / UPDATE`，沒有 `ALTER TABLE` 權限，拿它跑會失敗。
> 例外：`010` 只有 `UPDATE`、不改 schema，用應用程式帳號也跑得動。

> 💡 即時層四張表由 `000` 建立，`001` 才對它們加 `sensor_id`，所以 `000` 一定要先跑。
> 既有環境四張表都已存在，全部 `CREATE` 都是 `IF NOT EXISTS`，跑了不會動到現有資料。

> ⚠️ 若使用非 superuser 角色連線，記得確認該角色對相關資料表有
> `SELECT / INSERT / UPDATE / DELETE` 權限，且序列 (sequence) 要另外 GRANT。

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
啟動時：
  ├─ 依 .env 開關決定匯入/啟動哪些協議（MODBUS_ENABLED / TIA_ENABLED / OPCUA_ENABLED）
  ├─ sensor_reading_writer.load_initial_cache() + .start()  # 🆕 啟動統一寫入背景執行緒
  ├─ 初始化 MQTT Publisher（若啟用）
  └─ 啟動 OPC UA 訂閱服務（若啟用）

每輪 POLL_INTERVAL 秒（僅在 Modbus 或 TIA 至少一個啟用時才有意義）：
  ├─ Modbus 採集（若啟用）─┐
  └─ TIA(S7) 採集（若啟用）─┴─ 併發執行，互不阻塞；兩者都停用則跳過
  └─ MQTT 增量上傳（若啟用，資料來源依協議開關自動排除停用的表）

關閉時：
  └─ 依序停止 OPC UA 訂閱服務 → sensor_reading_writer → MQTT → DB 連線池
```

OPC UA 完全不在這個迴圈裡，走獨立的常駐背景服務（見下方）。

### OPC UA 訂閱服務（`services/opcua_subscription_service.py`）

```
服務啟動
  └─ 對每一台 enabled=TRUE 的 Server：
        ├─ 連線（連線逾時由 OPCUA_CLIENT_TIMEOUT 控制）
        ├─ 有快取點位表就直接用；沒有才做一次完整 browse 並寫入 opcua_tags
        ├─ 🆕 只對「已綁定 sensor_id」的點位建立訂閱（未綁定的存在 opcua_tags 供瀏覽，
        │      但不佔訂閱資源）；一個都沒綁定時僅維持連線與心跳，不建立任何訂閱
        ├─ 🆕 依每個點位解析出的取樣頻率分組，各組各自 create_subscription()；
        │      同一組內再依 deadband 設定（none/percent/absolute）分別呼叫
        │      subscribe_data_change() 或 deadband_monitor()
        ├─ 背景 task 1：每 2 秒把收到的變化批次 flush 進 opcua_tags 即時值，
        │              並把已綁定感測器的點位丟進 sensor_reading_writer.update_latest()
        │              （不在這裡直接寫 DB，寫入交給統一排程）
        ├─ 背景 task 2（🆕 _watch_and_maintain，取代原本兩個分開的 task）：
        │              每 5 秒檢查一次「立即瀏覽」旗標 / 感測器綁定 / 取樣頻率 /
        │              deadband 設定是否有變化，有變化就整批重建訂閱
        └─ 定期讀心跳目標節點確認連線存活（🆕 逾時 OPCUA_HEARTBEAT_TIMEOUT_SEC、
                連續失敗 OPCUA_HEARTBEAT_MAX_FAILURES 次才判定斷線，容忍偶發逾時）；
                判定斷線 → 退避重連
```

**新增點位 / 移除點位 / 調整訂閱設定的流程：**
使用者在網頁「OPC UA 點位設定」分頁按下「🚀 立即瀏覽並寫入資料庫」，或直接在
「感測器階層管理」分頁調整某感測器的綁定/取樣頻率/deadband，訂閱服務最慢
5 秒內會自動偵測到並重建訂閱，**不需要重啟任何服務**。

**心跳與斷線判定的設計取捨：** 心跳目標優先使用已訂閱成功的實際點位；連線
逾時、心跳逾時、連續失敗容忍次數皆可由 `.env` 調整，網路品質不穩的場域建議
拉高 `OPCUA_CLIENT_TIMEOUT` 與 `OPCUA_HEARTBEAT_MAX_FAILURES`，避免偶發的
回應延遲被誤判成斷線、頻繁整組重建訂閱。真正的硬性斷線（TCP session 已死）
仍會被正確偵測並觸發重連，這組參數只影響「多久 / 多寬容才判定為斷線」，
無法讓真的不穩的網路變穩定。

**Session 數限制的處理：** 首次瀏覽 / 重新整理點位表時，重複使用訂閱服務本身
已經開好的連線去瀏覽，不會另外多開連線；偵測到 `BadTooManySessions` 改用
固定 120 秒等待才重試。

**log 雜訊處理：** `asyncua` 套件內部 logger 等級調到 `WARNING`，並加上
🆕 重複訊息抑制 filter（同一則訊息 10 秒內只印一次），避免斷線時每秒狂噴
`Publish iteration crashed; retrying in 1s` 洗版。

---

## 🖥️ 網頁管理後台操作說明（`admin_app.py`）

登入後的分頁會依 `.env` 的 `MODBUS_ENABLED` / `TIA_ENABLED` 動態顯示：兩者都啟用時
是 5 個分頁，任一停用就少一個分頁（側邊欄會提示目前停用了哪些協議）。

### 📡 Modbus / TIA (S7) 點位設定（依 `.env` 開關顯示）

操作方式與既有版本相同：表格內編輯、單筆新增、測試連線與讀取、綁定感測器。

### 📡 OPC UA 點位設定

- **Server 清單**：可編輯連線參數，訂閱服務會在 15 秒內自動偵測變更並重新連線套用
- **新增 Server**：可先「🧪 測試連線與瀏覽」預覽會抓到哪些點位
- **手動瀏覽**：針對已存在的 Server，「🚀 立即瀏覽並寫入資料庫」可立即重新整理點位表
- **已採集的點位資料**：可依 Server 篩選檢視目前所有點位的即時數值並綁定感測器。
  🆕 提醒：只有已綁定感測器的點位會持續訂閱更新數值，未綁定的點位停留在上次瀏覽快照

### 🧬 感測器階層管理

依序建立廠區 → 產線 → 設備 → 感測器。感測器編輯表格與新增表單都包含：

| 欄位 | 說明 |
| --- | --- |
| `opcua_sampling_interval_ms` | 🆕 個別覆寫 OPC UA 訂閱取樣頻率（毫秒），留空沿用 Server 層級預設值 |
| `opcua_deadband_type` / `opcua_deadband_value` | 🆕 伺服器端過濾（none/percent/absolute + 門檻），減少不必要的通知透過網路送過來 |
| `upload_condition` / `upload_threshold` | 🆕 `sensor_readings` 統一寫入時的判斷條件（always/on_change/threshold_percent/threshold_absolute + 門檻） |

調整後最慢 5 秒（OPC UA 訂閱重建）或下一個統一寫入週期（DB 寫入判斷）內生效，
都不需要重啟服務。

### 🚨 異常監控

連線異常查詢會依 `.env` 協議開關自動排除停用協議的來源表；其餘功能不變。

---

## 📊 資料庫表格設計（僅列出 v2 新增/變更欄位，完整欄位見 SQL migration 檔）

### `sensors`（🆕 v2 新增欄位）

| 欄位 | 型態 | 說明 |
| --- | --- | --- |
| `opcua_sampling_interval_ms` | INTEGER | OPC UA 訂閱該點位的取樣頻率（毫秒），NULL = 沿用 Server 層級預設值 |
| `opcua_deadband_type` | VARCHAR(20) | `none` / `percent` / `absolute`，伺服器端 DataChangeFilter 判斷方式 |
| `opcua_deadband_value` | NUMERIC | 搭配 `opcua_deadband_type` 使用的門檻值 |
| `upload_condition` | VARCHAR(20) | `always` / `on_change` / `threshold_percent` / `threshold_absolute`，新建感測器預設 `threshold_percent`。**累計型計數器請用 `on_change`**，見上方第 4 節的警告 |
| `upload_threshold` | NUMERIC | 搭配 `upload_condition` 使用的門檻值（百分比數字或絕對值）。`always` / `on_change` 時不生效；為 NULL 時程式會 fallback（`threshold_percent` → 1%、`threshold_absolute` → 0），`010` 已把門檻型的 NULL 補成顯式的 `1` |

---

## 🎛️ Modbus TCP 參數解析

### Byte Order / Word Order 速查表

| 工業俗稱 | `byte_order` | `word_order` | 備註 |
| --- | --- | --- | --- |
| Big Endian (ABCD) | BIG | BIG | Modbus 官方標準 |
| Word Swap (CDAB) | BIG | LITTLE | 🌟 台灣電表最常見！ |
| Byte Swap (BADC) | LITTLE | BIG | - |
| Little Endian (DCBA) | LITTLE | LITTLE | 部分歐美設備 |

---

## 🔧 模組說明

| 模組 | 說明 |
| --- | --- |
| `collector/run_modbus_collector.py` | Modbus 資料採集主程式，多設備併發連線 |
| `collector/run_s7_collector.py` | TIA/S7 資料採集主程式，多 PLC 併發連線 |
| `collector/run_opcua_collector.py` | OPC UA 一次性完整瀏覽採集（獨立測試腳本，`main.py` 平時走訂閱服務） |
| `services/opcua_subscription_service.py` | 🆕 OPC UA 常駐訂閱服務：逐感測器取樣頻率分組、伺服器端 deadband、只訂閱已綁定點位、統一維護迴圈、可調連線穩定性參數 |
| `protocols/s7_protocol.py` | 西門子 S7 協議封裝（`snap7`） |
| `protocols/modbus_protocol.py` | Modbus TCP 協議封裝（`pymodbus`） |
| `protocols/opcua_protocol.py` | 🆕 OPC UA 協議封裝（`asyncua`），連線逾時可由 `OPCUA_CLIENT_TIMEOUT` 調整 |
| `parsers/plc_parser.py` | S7 位元組解析、線性縮放 |
| `parsers/encoder.py` | Modbus Big/Little Endian 編解碼 |
| `data_layer/db_connector.py` | PostgreSQL 連線池（`ThreadedConnectionPool`） |
| `data_layer/batch_updater.py` | 即時層批量寫入（`execute_values` UPDATE/UPSERT） |
| `data_layer/timeseries_writer.py` | 🆕 `SensorReadingWriter` 單例：統一週期性寫入排程 + 逐感測器 `upload_condition` 判斷，三協議共用，執行緒安全 |
| `messaging/mqtt_publisher.py` | MQTT 發送器，增量比對、心跳全量上傳、自動重連 |
| `main.py` | 主程式入口，依 `.env` 開關決定啟動哪些協議 |
| `admin_app.py` | 網頁管理後台（Streamlit），分頁依 `.env` 開關動態顯示 |
| `run_all.py` | 同時啟動 `main.py` 與 `admin_app.py` |
| `sql/` | 資料庫 migration，執行順序與編號斷層說明見 [`sql/README.md`](sql/README.md) |

---

## 🩺 常見問題排查

| 現象 | 可能原因 |
| --- | --- |
| 設定 `MODBUS_ENABLED=false` 後網頁還看得到 Modbus 分頁 | 確認用的是新版 `admin_app.py`（分頁清單依開關動態組出），並確認 `.env` 真的被載入（`ADMIN_PORT` 等其他變數有沒有生效可以交叉驗證） |
| `sensor_readings` 好像變比較慢才有新資料 | 這是預期行為：v2 改成 `SENSOR_READING_FLUSH_INTERVAL` 固定週期統一寫入（預設 60 秒），不是採集到就馬上寫；調小這個值可以縮短延遲，但太小意義不大且會增加 DB 負擔 |
| 感測器調了 `upload_condition` 沒有立即生效 | 最慢下一個 `SENSOR_READING_FLUSH_INTERVAL` 週期才會套用新設定（背景排程每輪都會重新讀一次 `sensors` 表），不是即時的 |
| **某個感測器完全沒有新資料，但 OPC UA 連線正常、`opcua_tags.current_data` 也一直在變** | 先查它的 `upload_condition`。若是**累計型計數器**套用 `threshold_percent`，基準值百萬等級時「變化 1%」要累積四十幾天，判斷永遠不成立，而且不會有任何錯誤 log。改成 `on_change`（或跑 `sql/010`）。驗證方式：`SELECT upload_condition, upload_threshold FROM sensors WHERE sensor_id=<id>;` 再比對 `sensor_readings` 最後一筆的值與 `opcua_tags` 目前值差幾 %。心跳保底生效後，這種點位至少仍會每 `SENSOR_HEARTBEAT_INTERVAL` 有一筆，不會完全消失 |
| **資料庫裡的時間戳比實際時間少 8 小時**（`opcua_tags.last_update` 看起來像早就停止更新） | 容器沒有設定 `TZ` 時跑在 UTC，程式若用 naive 的 `datetime.now()` 寫進 `timestamptz` 欄位，PostgreSQL 會照 session 的 `+08` 解讀，結果整批偏移。程式端已全部改用 `datetime.now().astimezone()`；若自行新增寫入時間的程式碼，**務必也帶時區**。快速確認：`docker exec <容器> date` 跟主機 `date` 對一下 |
| `sensors.upload_threshold` / `opcua_deadband_value` 存檔報欄位不存在 | 尚未執行 `sql/006_opcua_upgrade.sql` / `sql/007_opcua_deadband.sql`，補跑 migration |
| 在網頁上改感測器的上傳條件，存檔報 `violates check constraint "chk_sensors_upload_condition"` | 資料庫套用的是 `006` 的舊版本，該版 CHECK 只允許 `always` / `on_change` / `threshold`，擋掉了現在使用的 `threshold_percent` / `threshold_absolute`。重跑最新版 `sql/006_opcua_upgrade.sql` 即可（會自動把遺留的 `threshold` 轉成 `threshold_absolute` 再換上新的 constraint） |
| OPC UA log 出現 `Revised values returned differ from subscription values` | 正常訊息，代表伺服器端修改了實際生效的訂閱參數（常見於固定內部更新週期的設備，例如收到 `RevisedPublishingInterval=1000.0` 代表該設備不管你要求多慢，都用自己的 1 秒週期）。這種設備調整 `opcua_sampling_interval_ms` 不會真的降低它的負載，改用 `opcua_deadband_type=absolute` 從伺服器端過濾雜訊才有實際效果 |
| OPC UA 連線頻繁斷線重連，但重連都很快成功 | 通常是網路品質問題，不是程式邏輯錯誤；可以調高 `OPCUA_CLIENT_TIMEOUT` / `OPCUA_HEARTBEAT_MAX_FAILURES` 減少誤判次數、降低 log 噪音，但無法讓底層網路本身變穩定；若斷線頻率有規律性（例如每隔固定分鐘），較可能是網路設備（NAT timeout 等）問題 |
| 設定了 `opcua_deadband_type=percent` 但沒有效果 | Percent deadband 依賴節點是否設定 EURange（工程量測範圍），很多設備（尤其協議轉換盒）不會主動配置，導致此設定被忽略；改用 `absolute` |
| 未綁定感測器的 OPC UA 點位，`current_data` 都不會更新 | v2 起，只有已綁定 `sensor_id` 的點位才會持續訂閱更新數值，這是為了省頻寬的預期行為；未綁定的點位可以按「🚀 立即瀏覽並寫入資料庫」手動刷新一次快照，或直接綁定它 |
| OPC UA Server 剛新增/剛啟動，log 顯示「沒有已綁定感測器的點位，暫不建立訂閱」 | 正常訊息，不是錯誤；到「感測器階層管理」把該 Server 底下至少一個點位綁定 `sensor_id`，最慢 5 秒內會自動建立訂閱 |
| `permission denied for table sensor_readings` | DB 使用者權限不足，用管理員帳號補 `GRANT`，並設定 `ALTER DEFAULT PRIVILEGES`；PostgreSQL 的 GRANT 對表和序列 (sequence) 是分開的 |
| `run_all.py` 啟動報 `FileNotFoundError: ... 'streamlit'` | 虛擬環境本身沒裝 streamlit，或沒用 `sys.executable -m streamlit` 啟動 |

---

## 📚 文件導覽

| 文件 | 內容 |
| --- | --- |
| **README.md**（本檔） | 功能總覽、安裝設定、執行架構、網頁操作、常見問題排查 |
| [`README_DB.md`](README_DB.md) | 資料庫 schema 參考：即時層四張表的完整欄位定義、時序層階層表、寫入規則 |
| [`sql/README.md`](sql/README.md) | migration 的執行順序、各檔用途、編號 002~005 斷層的原因 |
| [`UPGRADE_NOTES_v2.md`](UPGRADE_NOTES_v2.md) | v1 → v2 的行為變化、部署步驟、已知取捨 |
| [`todo.md`](todo.md) | 待辦事項與已知技術債（含資料量成長、schema 漂移防範） |