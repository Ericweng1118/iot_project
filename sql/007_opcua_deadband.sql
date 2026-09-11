-- ============================================================
-- 007_opcua_deadband.sql
-- ============================================================
-- 目的：
--   為 sensors 加上「伺服器端 deadband 過濾」設定，讓 OPC UA Server 在
--   來源端就決定要不要把這筆數值變化透過網路送過來，直接減少不穩定連線
--   上的流量。程式碼見 services/opcua_subscription_service.py 的
--   _load_sensor_deadband_config() / _create_subscriptions_for_tags()。
--
--   ⚠️ 這與 006 的 upload_condition 是兩個不同層級的過濾，可疊加使用：
--     - opcua_deadband_type ：伺服器端決定「要不要把這筆變化送過來」（省頻寬）
--     - upload_condition    ：本系統收到之後決定「要不要寫進 sensor_readings」（省 DB）
--
-- 執行方式（可重複執行，idempotent）：
--   psql -h <DB_HOST> -U <DB_USER> -d <DB_NAME> -f sql/007_opcua_deadband.sql
--
-- 前置需求：sql/006_opcua_upgrade.sql
-- ============================================================

ALTER TABLE sensors
    ADD COLUMN IF NOT EXISTS opcua_deadband_type  VARCHAR(20) NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS opcua_deadband_value NUMERIC;

-- 既有資料若有 NULL / 空字串，先正規化成 'none'，再套 CHECK constraint
UPDATE sensors SET opcua_deadband_type = 'none'
 WHERE opcua_deadband_type IS NULL OR opcua_deadband_type = '';

ALTER TABLE sensors DROP CONSTRAINT IF EXISTS chk_sensors_opcua_deadband_type;
ALTER TABLE sensors
    ADD CONSTRAINT chk_sensors_opcua_deadband_type
    CHECK (opcua_deadband_type IN ('none', 'percent', 'absolute'));

COMMENT ON COLUMN sensors.opcua_deadband_type IS
    'OPC UA MonitoredItem 的伺服器端 DataChangeFilter 判斷方式：
       none     - 不套用 deadband，只要有變化伺服器就送（預設）
       absolute - 數值變化絕對值達到 opcua_deadband_value 才送
       percent  - 變化百分比達到 opcua_deadband_value 才送。
                  依 OPC UA 標準以節點的 EURange（工程量測範圍）換算，
                  節點若未設定 EURange，多數伺服器會忽略此設定；
                  不確定設備有沒有配置時建議改用 absolute。';

COMMENT ON COLUMN sensors.opcua_deadband_value IS
    '搭配 opcua_deadband_type 使用的門檻值：
       percent  時填百分比數字（例如 1 = 1%）
       absolute 時填絕對值
     opcua_deadband_type = none 時此欄位不生效。';
