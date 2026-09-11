-- ============================================================
-- 008_missing_app_columns.sql
-- ============================================================
-- 目的：
--   補齊「正式資料庫早就有、但沒有任何 migration 腳本建立」的欄位。
--
--   這些欄位是 v1 ~ v2 開發期間直接在資料庫上手動 ALTER 出來的，程式碼
--   一直在用，但腳本沒跟著補。造成的後果是：照著 README 用
--   001 -> 006 -> 007 建一台全新的機器，程式會在下列地方失敗：
--
--     sensors.nickname / state_dictionary
--         -> admin_app.py「感測器階層管理」分頁的 SELECT 直接報欄位不存在
--            （_load_sensor_options()、感測器編輯表格、異常監控查詢等 11 處）
--     opcua_servers.resubscribe_requested
--         -> admin_app.py 的「🚀 立即瀏覽並寫入資料庫」按鈕、
--            opcua_subscription_service.py 的 _check_and_clear_resubscribe_flag()
--            都會失敗，等於維護迴圈偵測不到手動觸發
--     modbus_scada.unit
--         -> admin_app.py Modbus 分頁的「單筆新增」INSERT 失敗
--
--   本檔跑完之後，001 -> 006 -> 007 -> 008 的結果才會等於目前正式庫的 schema。
--
-- 執行方式（可重複執行，idempotent）：
--   psql -h <DB_HOST> -U <DB_USER> -d <DB_NAME> -f sql/008_missing_app_columns.sql
--
-- ⚠️ 需要用資料表擁有者（目前是 eric）執行；應用程式帳號 scada 只有
--    SELECT/INSERT/UPDATE，沒有 ALTER TABLE 權限。
--
-- 前置需求：sql/001_sensor_hierarchy_and_mapping.sql
-- ============================================================

-- ------------------------------------------------------------
-- 1. sensors：暱稱與狀態字典
-- ------------------------------------------------------------
ALTER TABLE sensors
    ADD COLUMN IF NOT EXISTS nickname         VARCHAR(100),
    ADD COLUMN IF NOT EXISTS state_dictionary JSONB;

-- 網頁的感測器下拉選單會用 nickname 排序/搜尋，168 筆規模影響不大，
-- 但正式庫已經有這個索引，這裡一併建立以維持 schema 一致。
CREATE INDEX IF NOT EXISTS idx_sensors_nickname ON sensors (nickname);

COMMENT ON COLUMN sensors.nickname IS
    '感測器暱稱（選填）。純粹給人看的識別字，例如「B03 蒸氣流量計」；
     admin_app.py 的感測器下拉選單會顯示成「sensor_code (nickname)」。';

COMMENT ON COLUMN sensors.state_dictionary IS
    '狀態字典（選填，JSONB）。把數值對應成文字狀態，例如 {"0":"待機","8":"大火燃燒"}，
     格式範例見專案根目錄的 狀態字典範例.json。
     ⚠️ 注意：轉換出來的文字不會寫進 sensor_readings（value 是 NUMERIC，只存數字），
     僅供即時層顯示與 MQTT 上傳使用。';

-- ------------------------------------------------------------
-- 2. opcua_servers：網頁手動觸發「立即瀏覽」用的旗標
-- ------------------------------------------------------------
ALTER TABLE opcua_servers
    ADD COLUMN IF NOT EXISTS resubscribe_requested BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN opcua_servers.resubscribe_requested IS
    '使用者於網頁按下「🚀 立即瀏覽並寫入資料庫」時由 admin_app.py 設為 TRUE。
     opcua_subscription_service.py 的維護迴圈每 5 秒檢查一次，讀到 TRUE 就
     重新 browse 並重建訂閱，然後把旗標清回 FALSE。';

-- ------------------------------------------------------------
-- 3. modbus_scada：工程單位
-- ------------------------------------------------------------
ALTER TABLE modbus_scada
    ADD COLUMN IF NOT EXISTS unit TEXT;

COMMENT ON COLUMN modbus_scada.unit IS
    'Modbus 點位的工程單位（選填），例如 kW / °C。僅供顯示，不參與數值換算。';

-- ------------------------------------------------------------
-- 4. devices：正式庫已存在但程式碼目前沒有讀取的欄位
--    列在這裡是為了讓「腳本重建出來的 schema」與正式庫完全一致，
--    避免下一次做 schema 比對時又出現無法解釋的差異。
--    （若確定不再需要，可以在確認沒有其他工具/報表在讀之後另開 migration 移除。）
-- ------------------------------------------------------------
ALTER TABLE devices
    ADD COLUMN IF NOT EXISTS device_nickname VARCHAR,
    ADD COLUMN IF NOT EXISTS cost            INTEGER;

COMMENT ON COLUMN devices.device_nickname IS
    '設備暱稱（選填）。目前採集程式與 admin_app.py 都沒有讀取，保留給外部報表使用。';

COMMENT ON COLUMN devices.cost IS
    '設備成本（選填）。目前採集程式與 admin_app.py 都沒有讀取，保留給外部報表使用。';
