-- ============================================================
-- 015_modbus_transport.sql
-- ============================================================
-- 目的：modbus_scada 新增三個欄位（v3.1 Modbus 採集改寫）
--
--   transport        tcp           Modbus TCP（預設，等於 v2 行為）
--                    rtu_over_tcp  序列轉乙太網路閘道的「透通模式」（例如 Moxa NPort 設成
--                                  TCP Server / Real COM 以外的 raw 模式），封包是 RTU 格式
--                    rtu           本機序列埠（RS-485 / RS-232），plc_ip 欄位填序列埠路徑，
--                                  例如 /dev/ttyUSB0；Docker 需要 --device 對應進容器
--   serial_settings  transport=rtu 時的「鮑率,資料位元,同位,停止位元」，例如 9600,8,N,1
--   enabled          FALSE 的點位不採集（暫時停用不必刪除設定）
--
--   沒跑這支 migration 時，採集程式自動退回 v2 行為（全部 Modbus TCP、全部採集）。
--
-- 執行方式（可重複執行，idempotent）：
--   psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -f sql/015_modbus_transport.sql
-- ============================================================

ALTER TABLE modbus_scada
    ADD COLUMN IF NOT EXISTS transport       VARCHAR(20) NOT NULL DEFAULT 'tcp',
    ADD COLUMN IF NOT EXISTS serial_settings VARCHAR(30),
    ADD COLUMN IF NOT EXISTS enabled         BOOLEAN     NOT NULL DEFAULT TRUE;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_modbus_transport') THEN
        ALTER TABLE modbus_scada
            ADD CONSTRAINT chk_modbus_transport CHECK (transport IN ('tcp', 'rtu_over_tcp', 'rtu'));
    END IF;
END $$;

COMMENT ON COLUMN modbus_scada.transport IS
    'tcp=Modbus TCP、rtu_over_tcp=序列閘道透通模式、rtu=本機序列埠（plc_ip 填 /dev/ttyUSB0 等路徑）';
COMMENT ON COLUMN modbus_scada.serial_settings IS
    'transport=rtu 時的序列埠參數「鮑率,資料位元,同位,停止位元」，例如 9600,8,N,1';
COMMENT ON COLUMN modbus_scada.enabled IS 'FALSE = 暫停採集這個點位（保留設定）';
