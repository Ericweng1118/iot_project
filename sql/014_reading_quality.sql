-- ============================================================
-- 014_reading_quality.sql
-- ============================================================
-- 目的：
--   sensor_readings 新增 quality 欄位，記錄每一筆資料的品質，事後看歷史才分得出
--   「正常量測」、「數值沒變的保持值」、「設備回報品質不良」、「通訊中斷」：
--
--     0 GOOD        正常量測
--     1 HELD        保持值（數值沒變、來源存活，由心跳 / always 條件以目前時間補寫）
--     2 UNCERTAIN   設備回報 Uncertain
--     3 BAD         設備回報 Bad（只記錄轉為不良的那一筆）
--     4 COMM_LOST   通訊中斷標記（數值沿用最後一筆）
--     NULL          本 migration 之前的舊資料，視為 GOOD
--
--   定義與程式端常數見 data_layer/quality.py。
--
-- 對既有資料的影響：
--   ADD COLUMN 不帶預設值在 PostgreSQL 只改 metadata，不會重寫既有資料，
--   幾百萬筆的 hypertable 也是瞬間完成；已啟用壓縮（sql/013）的 hypertable 同樣支援。
--   寫入程式會自動偵測這個欄位（最慢 10 分鐘），沒跑這支 migration 也能正常運作，只是不記錄品質。
--
-- 執行方式（可重複執行，idempotent）：
--   psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -f sql/014_reading_quality.sql
-- ============================================================

ALTER TABLE sensor_readings ADD COLUMN IF NOT EXISTS quality SMALLINT;

COMMENT ON COLUMN sensor_readings.quality IS
    '0=正常 1=保持值 2=不確定 3=品質不良 4=通訊中斷，NULL=舊資料（視為正常）。3/4 是標記，統計時應排除。';
