-- ============================================================
-- 006_opcua_upgrade.sql
-- ============================================================
-- 目的：
--   1. 聚焦強化 OPC UA：讓每個感測器可以有自己的 OPC UA 訂閱取樣頻率，
--      不用全部點位共用 Server 層級的單一頻率。
--   2. sensor_readings 改為「統一週期性寫入」（寫入週期改由 .env 的
--      SENSOR_READING_FLUSH_INTERVAL 控制，程式碼見
--      data_layer/timeseries_writer.py）。這裡新增的 upload_condition /
--      upload_threshold 欄位，用來決定「統一寫入這一輪」時，
--      個別感測器要不要真的被寫進去。
--
-- 執行方式：
--   psql -h <DB_HOST> -U <DB_USER> -d <DB_NAME> -f sql/006_opcua_upgrade.sql
-- ============================================================

ALTER TABLE sensors
    ADD COLUMN IF NOT EXISTS opcua_sampling_interval_ms INTEGER,
    ADD COLUMN IF NOT EXISTS upload_condition VARCHAR(20) NOT NULL DEFAULT 'always',
    ADD COLUMN IF NOT EXISTS upload_threshold NUMERIC;

-- 避免重複執行時 ADD CONSTRAINT 報錯，先嘗試移除同名 constraint 再新增
ALTER TABLE sensors DROP CONSTRAINT IF EXISTS chk_sensors_upload_condition;
ALTER TABLE sensors
    ADD CONSTRAINT chk_sensors_upload_condition
    CHECK (upload_condition IN ('always', 'on_change', 'threshold'));

COMMENT ON COLUMN sensors.opcua_sampling_interval_ms IS
    'OPC UA 訂閱該點位的取樣頻率（毫秒）。NULL = 沿用 Server 層級 publish_interval_ms
     （或 .env 的 OPCUA_PUBLISH_INTERVAL_MS）預設值。相同頻率的點位會被歸進同一個
     Subscription，不同頻率各自建立獨立的 Subscription。';

COMMENT ON COLUMN sensors.upload_condition IS
    'sensor_readings 統一週期性寫入時，這個感測器的判斷條件：
       always     - 每一輪（每 SENSOR_READING_FLUSH_INTERVAL 秒）都寫
       on_change  - 只有數值與上次「實際寫入」的值不同才寫
       threshold  - 數值變化量（絕對值）達到 upload_threshold 才寫';

COMMENT ON COLUMN sensors.upload_threshold IS
    'upload_condition = threshold 時使用：數值變化超過此絕對值才寫入 sensor_readings。
     upload_condition 不是 threshold 時此欄位不生效。';
