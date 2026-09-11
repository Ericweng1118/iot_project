# 升級說明：屏蔽 Modbus/TIA + 強化 OPC UA + 統一寫入排程

## 1. 這次改了什麼

| 需求 | 作法 |
| --- | --- |
| 用 .env 屏蔽 Modbus / TIA(S7)（含網頁功能） | 新增 `MODBUS_ENABLED` / `TIA_ENABLED`（預設 `true`，相容既有部署）。`main.py` 依開關決定要不要匯入/啟動該協議的採集；`admin_app.py` 依開關決定要不要顯示對應分頁（含新增/編輯）。 |
| sensor_readings 統一寫入，寫入時間由 .env 控制 | `data_layer/timeseries_writer.py` 改版：採集端只呼叫 `update_latest()` 更新記憶體最新值，實際寫入交給背景執行緒依 `SENSOR_READING_FLUSH_INTERVAL`（秒）固定週期批次寫入，三協議共用同一套排程。 |
| sensors 增加 OPC UA 訂閱頻率、上傳條件 | `sql/006_opcua_upgrade.sql` 新增 `opcua_sampling_interval_ms` / `upload_condition` / `upload_threshold` 三個欄位，`admin_app.py` 感測器階層管理分頁可直接編輯。 |
| OPC UA 伺服器端 deadband 過濾 | `sql/007_opcua_deadband.sql` 新增 `opcua_deadband_type` / `opcua_deadband_value`，訂閱服務改用 asyncua 的 `Subscription.deadband_monitor()` 建立監控項目，讓伺服器端就決定要不要把變化送過來。 |
| 只訂閱已綁定感測器的點位 | 瀏覽發現的點位仍全部存進 `opcua_tags` 供網頁挑選，但只有設定了 `sensor_id` 的點位會實際建立訂閱，未綁定的不佔訂閱資源、不消耗頻寬。 |

## 2. 部署步驟

1. 執行新的 migration（兩支都是 idempotent，可重複執行）：
   ```bash
   psql -h <DB_HOST> -U <DB_USER> -d <DB_NAME> -f sql/006_opcua_upgrade.sql
   psql -h <DB_HOST> -U <DB_USER> -d <DB_NAME> -f sql/007_opcua_deadband.sql
   ```
2. `.env` 加入（完整清單見 `env.example`）：
   ```
   MODBUS_ENABLED=false
   TIA_ENABLED=false
   OPCUA_ENABLED=true
   SENSOR_READING_FLUSH_INTERVAL=60
   OPCUA_PUBLISH_INTERVAL_MS=1000
   OPCUA_CLIENT_TIMEOUT=10
   OPCUA_HEARTBEAT_INTERVAL_SEC=15
   OPCUA_HEARTBEAT_TIMEOUT_SEC=8
   OPCUA_HEARTBEAT_MAX_FAILURES=2
   ```
   同時可以移除 v1 遺留、v2 已不再讀取的 `SENSOR_HEARTBEAT_INTERVAL` 與
   `OPCUA_POLL_INTERVAL` 兩個變數。
3. 替換以下檔案：`main.py`、`admin_app.py`、`data_layer/timeseries_writer.py`、
   `services/opcua_subscription_service.py`。其餘檔案（`collector/*`、
   `protocols/*`、`data_layer/batch_updater.py`、`data_layer/db_connector.py`、
   `messaging/mqtt_publisher.py`）不需要更動，向下相容。
4. 重啟 `main.py` / `run_all.py`。

## 3. 行為變化重點

### 3.1 協議屏蔽
- `MODBUS_ENABLED=false` / `TIA_ENABLED=false`：`main.py` 完全不匯入、不啟動該協議的採集執行緒；`admin_app.py` 完全不顯示對應分頁（不是灰掉，是整個 tab 不存在）。
- 「異常監控」分頁的連線異常查詢會依開關自動排除停用協議的來源表，不會出現一堆「當然是 OFFLINE」的假警報。
- MQTT 上傳的來源查詢（`fetch_latest_scada_map`）也會依開關跳過停用協議的表。

### 3.2 sensor_readings 統一寫入排程（重要行為變化）
- 舊版：每個採集器在自己那一輪採集結束時，依「首次出現/數值變化/心跳」逐點位判斷並寫入，寫入時機分散、跟採集週期綁死。
- 新版：所有協議（含 OPC UA 訂閱服務）只負責把最新值丟進記憶體（`update_latest`），真正落地寫入 `sensor_readings` 統一由 `SensorReadingWriter` 背景執行緒依 `SENSOR_READING_FLUSH_INTERVAL` 固定週期處理。
- 這一輪「要不要真的寫」改成逐感測器規則（`sensors.upload_condition`）：
  - `always`：每個週期都寫（等於每 `SENSOR_READING_FLUSH_INTERVAL` 秒一筆）
  - `on_change`：只有數值跟上次「實際寫入」的值不同才寫
  - `threshold_percent`（**預設**）：變化百分比達到 `upload_threshold` 才寫（`1` = 1%），以上次實際寫入值為基準
  - `threshold_absolute`：變化絕對值達到 `upload_threshold` 才寫

  > ⚠️ v2 初版曾短暫使用過單一的 `threshold`（語意為絕對值），正式版拆成上面兩種。
  > `timeseries_writer.py` 仍保留讀到 `threshold` 時的相容分支；資料庫這一層則由
  > `sql/006_opcua_upgrade.sql` 自動把遺留值轉成 `threshold_absolute`，並換上
  > 允許四種值的新 CHECK constraint。**若你的資料庫套用的是 006 的舊版本，
  > 網頁存檔會報 `violates check constraint "chk_sensors_upload_condition"`，
  > 重跑一次最新版的 006 即可解決。**
- `stage()` / 舊版逐次 `flush()` 呼叫仍相容（`collector/run_modbus_collector.py`、`collector/run_s7_collector.py` 不需要改），但實際寫入時機已經統一交給背景排程，各採集器結尾的 `flush()` 呼叫等同「手動催一次」，不影響正確性。

### 3.3 OPC UA 逐感測器訂閱頻率
- `sensors.opcua_sampling_interval_ms` 可覆寫個別點位的取樣頻率（毫秒）。
- OPC UA 的 publishing interval 是 Subscription 層級屬性，因此同一個 Server 底下，相同頻率的點位會被自動歸進同一組 Subscription；沒設定覆寫值的點位沿用 Server 層級預設值（`opcua_servers.publish_interval_ms` 或 `.env` 的 `OPCUA_PUBLISH_INTERVAL_MS`）。
- 感測器綁定、取樣頻率設定變更後，最慢在 5 秒內（`MAINTENANCE_POLL_INTERVAL`）會被背景維護迴圈偵測到並自動重建訂閱，不需要重啟服務、也不用手動按「立即瀏覽」（但按了也一樣有效，會多觸發一次完整 browse）。

## 4. 已知取捨

- 訂閱頻率或綁定變更時，目前是「整批重建全部 Subscription」而不是精細 diff，換取分組邏輯簡單、不容易在邊界情況下留下不一致的訂閱殘留。點位數量非常多時，重建當下會有短暫的訂閱空窗（毫秒等級，含在 5 秒維護週期內）。
- v1 的 `SENSOR_HEARTBEAT_INTERVAL`（數值沒變化時定期補寫一筆）在 v2 被 `upload_condition` 取代，程式碼已不再讀取；等效行為請改用 `upload_condition = always`。
- `SENSOR_READING_FLUSH_INTERVAL` 建議不要設太短（例如 <5 秒），因為 `upload_condition` 的判斷、DB 查詢都是整批一次處理，週期太短意義不大且會增加 DB 負擔。
