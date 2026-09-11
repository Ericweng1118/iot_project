-- ============================================================
-- 006_opcua_upgrade.sql
-- ============================================================
-- 目的：
--   1. 聚焦強化 OPC UA：讓每個感測器可以有自己的 OPC UA 訂閱取樣頻率，
--      不用全部點位共用 Server 層級的單一頻率。
--   2. sensor_readings 改為「統一週期性寫入」（寫入週期由 .env 的
--      SENSOR_READING_FLUSH_INTERVAL 控制，程式碼見
--      data_layer/timeseries_writer.py）。這裡新增的 upload_condition /
--      upload_threshold 欄位，用來決定「統一寫入這一輪」時，
--      個別感測器要不要真的被寫進去。
--
-- 執行方式（可重複執行，idempotent）：
--   psql -h <DB_HOST> -U <DB_USER> -d <DB_NAME> -f sql/006_opcua_upgrade.sql
--
-- 前置需求：sql/001_sensor_hierarchy_and_mapping.sql（建立 sensors 表）
-- ============================================================

ALTER TABLE sensors
    ADD COLUMN IF NOT EXISTS opcua_sampling_interval_ms INTEGER,
    ADD COLUMN IF NOT EXISTS upload_condition VARCHAR(20) NOT NULL DEFAULT 'threshold_percent',
    ADD COLUMN IF NOT EXISTS upload_threshold NUMERIC;

-- 既有資料庫若曾套用過本檔的舊版本，欄位預設值會停在 'always'，這裡一併校正，
-- 讓新建立的感測器與 admin_app.py 的預設選項一致。
ALTER TABLE sensors ALTER COLUMN upload_condition SET DEFAULT 'threshold_percent';

-- ------------------------------------------------------------
-- 舊值轉換：本檔初版只支援 always / on_change / threshold 三選一，
-- threshold 的語意是「變化絕對值」。v2 正式版拆成 threshold_percent /
-- threshold_absolute，這裡把遺留的 'threshold' 對應到語意相同的
-- threshold_absolute，避免下面的 CHECK constraint 建立失敗。
-- （timeseries_writer.py 仍保留讀到 'threshold' 時的相容分支，
--   但資料庫這一層轉乾淨比較不會混淆。）
-- ------------------------------------------------------------
UPDATE sensors SET upload_condition = 'threshold_absolute'
 WHERE upload_condition = 'threshold';

-- 未設定過的感測器套用專案預設：變化達 1% 才寫入 sensor_readings
UPDATE sensors SET upload_condition = 'threshold_percent'
 WHERE upload_condition IS NULL OR upload_condition = '';

UPDATE sensors SET upload_threshold = 1
 WHERE upload_threshold IS NULL
   AND upload_condition IN ('threshold_percent', 'threshold_absolute');

-- 避免重複執行時 ADD CONSTRAINT 報錯，先嘗試移除同名 constraint 再新增
ALTER TABLE sensors DROP CONSTRAINT IF EXISTS chk_sensors_upload_condition;
ALTER TABLE sensors
    ADD CONSTRAINT chk_sensors_upload_condition
    CHECK (upload_condition IN (
        'always', 'on_change', 'threshold_percent', 'threshold_absolute'
    ));

COMMENT ON COLUMN sensors.opcua_sampling_interval_ms IS
    'OPC UA 訂閱該點位的取樣頻率（毫秒）。NULL = 沿用 Server 層級 publish_interval_ms
     （或 .env 的 OPCUA_PUBLISH_INTERVAL_MS）預設值。相同頻率的點位會被歸進同一個
     Subscription，不同頻率各自建立獨立的 Subscription。';

COMMENT ON COLUMN sensors.upload_condition IS
    'sensor_readings 統一週期性寫入時，這個感測器的判斷條件：
       always             - 每一輪（每 SENSOR_READING_FLUSH_INTERVAL 秒）都寫
       on_change          - 只有數值與上次「實際寫入」的值不同才寫
       threshold_percent  - 變化百分比（以上次實際寫入值為基準）達到 upload_threshold 才寫，
                            upload_threshold 填百分比數字，例如 1 = 1%（專案預設）
       threshold_absolute - 變化絕對值達到 upload_threshold 才寫';

COMMENT ON COLUMN sensors.upload_threshold IS
    '搭配 upload_condition 使用的門檻值：
       threshold_percent  時填百分比數字（1 = 1%）
       threshold_absolute 時填絕對值
     upload_condition = always / on_change 時此欄位不生效。';
