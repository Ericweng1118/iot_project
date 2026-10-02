-- ============================================================
-- 019_s7_extensions.sql
-- ============================================================
-- 目的：tia_scada（S7 點位）補齊 v3.3 採集改寫需要的欄位
--
--   area        DB（預設，等於 v2 行為）/ M（旗標）/ I（輸入）/ Q（輸出）
--   bit_offset  BOOL 的位元 0~7。v2 沒有這個欄位，BOOL 一律讀第 0 bit，
--               DB1.DBX10.3 這種位址根本設定不出來
--   rack/slot   S7-1200/1500 = 0/1（預設，等於 v2 寫死的值）；S7-300 = 0/2；S7-400 依機架
--   enabled     FALSE = 暫停採集（保留設定）
--   unit        工程單位（與 modbus_scada 一致）
--
--   沒跑這支 migration 時，採集程式自動退回 v2 行為（只讀 DB、BOOL 讀第 0 bit、Rack 0 / Slot 1）。
--
-- 執行方式（可重複執行，idempotent）：
--   psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -f sql/019_s7_extensions.sql
-- ============================================================

ALTER TABLE tia_scada
    ADD COLUMN IF NOT EXISTS area        VARCHAR(2) NOT NULL DEFAULT 'DB',
    ADD COLUMN IF NOT EXISTS bit_offset  SMALLINT   NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS rack        SMALLINT   NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS slot        SMALLINT   NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS enabled     BOOLEAN    NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS unit        TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_tia_area') THEN
        ALTER TABLE tia_scada ADD CONSTRAINT chk_tia_area CHECK (area IN ('DB', 'M', 'I', 'Q'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_tia_bit') THEN
        ALTER TABLE tia_scada ADD CONSTRAINT chk_tia_bit CHECK (bit_offset BETWEEN 0 AND 7);
    END IF;
END $$;

COMMENT ON COLUMN tia_scada.area IS 'DB / M / I / Q；非 DB 區域時 db_number 不使用';
COMMENT ON COLUMN tia_scada.bit_offset IS 'BOOL 的位元 0~7，例如 DB1.DBX10.3 → offset 10、bit_offset 3';
COMMENT ON COLUMN tia_scada.rack IS 'S7-1200/1500 = 0；S7-300 = 0';
COMMENT ON COLUMN tia_scada.slot IS 'S7-1200/1500 = 1；S7-300 = 2';
