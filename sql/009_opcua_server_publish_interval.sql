-- ============================================================
-- 009_opcua_server_publish_interval.sql
-- ============================================================
-- 目的：
--   把 opcua_servers.publish_interval_ms 真正建出來。
--
--   在這支 migration 之前，README / env.example / opcua_subscription_service.py
--   的 docstring 都描述「Server 層級預設訂閱取樣頻率，可被 sensors 的
--   opcua_sampling_interval_ms 覆寫」，但：
--     - 資料庫沒有這個欄位
--     - collector/run_opcua_collector.py 的 load_opcua_servers() 也沒有 SELECT 它
--   所以 server.get("publish_interval_ms") 永遠是 None，永遠 fallback 到
--   .env 的 OPCUA_PUBLISH_INTERVAL_MS —— 全部 Server 只能共用同一個頻率。
--
--   本檔補上欄位後，頻率的優先順序才會是文件描述的三層：
--     1. sensors.opcua_sampling_interval_ms       （逐感測器，最優先）
--     2. opcua_servers.publish_interval_ms        （逐 Server，本檔新增）
--     3. .env 的 OPCUA_PUBLISH_INTERVAL_MS        （全域預設，最後手段）
--
--   NULL 代表「這台 Server 不特別指定，沿用 .env 全域值」，因此刻意不設
--   DEFAULT 值 —— 有 DEFAULT 的話既有 19 台 Server 會被一次性綁死在某個
--   數字上，反而失去「沿用全域設定」的語意。
--
-- 執行方式（可重複執行，idempotent）：
--   psql -h <DB_HOST> -U <DB_USER> -d <DB_NAME> -f sql/009_opcua_server_publish_interval.sql
--
-- ⚠️ 需要用資料表擁有者（目前是 eric）執行。
--
-- 前置需求：sql/008_missing_app_columns.sql
--
-- 搭配的程式碼變更：
--   collector/run_opcua_collector.py  load_opcua_servers() 加撈此欄位
--   admin_app.py                      OPC UA Server 清單新增可編輯欄位
-- ============================================================

ALTER TABLE opcua_servers
    ADD COLUMN IF NOT EXISTS publish_interval_ms INTEGER;

-- 合理範圍檢查：OPC UA 的 publishing interval 以毫秒計。
-- 下限 50ms 是為了擋掉誤填 0 / 負數（0 在 OPC UA 語意是「由伺服器自行決定」，
-- 容易造成誤解）；上限 3600000ms = 1 小時，超過這個值幾乎可以確定是填錯單位
-- （例如把秒當毫秒填）。NULL 一律放行，代表沿用 .env 全域值。
ALTER TABLE opcua_servers DROP CONSTRAINT IF EXISTS chk_opcua_servers_publish_interval;
ALTER TABLE opcua_servers
    ADD CONSTRAINT chk_opcua_servers_publish_interval
    CHECK (publish_interval_ms IS NULL
           OR (publish_interval_ms >= 50 AND publish_interval_ms <= 3600000));

COMMENT ON COLUMN opcua_servers.publish_interval_ms IS
    '這台 Server 的預設訂閱取樣頻率（毫秒）。NULL = 沿用 .env 的
     OPCUA_PUBLISH_INTERVAL_MS。個別點位若在 sensors.opcua_sampling_interval_ms
     另外設定，該點位以 sensors 的值為準。
     ⚠️ 部分設備（尤其嵌入式協議轉換盒）的 OPC UA Server 有自己固定的內部更新
     週期，會忽略用戶端請求的頻率（log 出現 Revised values returned differ from
     subscription values 即為此情況），此時本設定對該設備不生效。';
