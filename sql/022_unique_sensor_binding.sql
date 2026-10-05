-- ============================================================
-- 022_unique_sensor_binding.sql
-- ============================================================
-- 目的：在資料庫層保證「同一張點位表裡，一個感測器只能被一個點位綁定」
--
--   之前只靠網頁在儲存前檢查，同一次編輯裡兩列選了同一個感測器、或兩個人同時
--   操作，都可能寫進重複綁定。加上 UNIQUE 後，介面再有漏洞也寫不進去。
--
--   * UNIQUE (sensor_id) 允許多筆 NULL（未綁定），不影響未綁定的點位。
--   * DEFERRABLE INITIALLY DEFERRED：交易結束時才檢查，同一次交易內把綁定
--     從 A 換到 B、兩個點位互換感測器（批次匯入、綁定工作台）都不會在中途報錯。
--   * 跨表（例如 Modbus 與 OPC UA 搶同一個感測器）無法用 UNIQUE 表達，
--     仍由 data_layer/bindings.py 在 advisory lock 下檢查。
--
--   既有資料若已有重複綁定，ADD CONSTRAINT 會失敗並列出衝突值，
--   請先在網頁上解除重複的綁定再執行。可用下列查詢檢查：
--     SELECT sensor_id, array_agg(id) FROM opcua_tags
--     WHERE sensor_id IS NOT NULL GROUP BY sensor_id HAVING count(*) > 1;
--
-- 執行方式（可重複執行，idempotent）：
--   psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -f sql/022_unique_sensor_binding.sql
-- ============================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_opcua_tags_sensor_id'
                   AND conrelid = 'opcua_tags'::regclass) THEN
        ALTER TABLE opcua_tags ADD CONSTRAINT uq_opcua_tags_sensor_id
            UNIQUE (sensor_id) DEFERRABLE INITIALLY DEFERRED;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_modbus_scada_sensor_id'
                   AND conrelid = 'modbus_scada'::regclass) THEN
        ALTER TABLE modbus_scada ADD CONSTRAINT uq_modbus_scada_sensor_id
            UNIQUE (sensor_id) DEFERRABLE INITIALLY DEFERRED;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_tia_scada_sensor_id'
                   AND conrelid = 'tia_scada'::regclass) THEN
        ALTER TABLE tia_scada ADD CONSTRAINT uq_tia_scada_sensor_id
            UNIQUE (sensor_id) DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;
