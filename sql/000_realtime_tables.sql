-- ============================================================
-- 000_realtime_tables.sql
-- ============================================================
-- 目的：
--   建立「即時層」四張表。這四張表是 v1 時期直接在資料庫上手動建立的，
--   一直到 2026-09-01 之前都沒有任何建表腳本，導致 sql/ 目錄沒辦法從一個
--   空資料庫重建出完整 schema（001 只能對「已存在的表」加 sensor_id 欄位，
--   008 也只能補欄位、補不了整張表）。本檔補上這個缺口。
--
--   內容是依 2026-09-01 當下正式資料庫的實際定義反推出來的，因此刻意
--   「只建立 v1 當時就有的欄位」，後續 migration 加的欄位不寫在這裡，
--   各自留在原本負責的檔案，避免同一個欄位有兩個定義來源：
--
--     sensor_id （四張表）            -> sql/001_sensor_hierarchy_and_mapping.sql
--     modbus_scada.unit               -> sql/008_missing_app_columns.sql
--     opcua_servers.resubscribe_requested -> sql/008_missing_app_columns.sql
--     opcua_servers.publish_interval_ms   -> sql/009_opcua_server_publish_interval.sql
--
--   所以正確的執行順序是 000 -> 001 -> 006 -> 007 -> 008 -> 009，
--   跑完的結果會等於目前正式庫的 schema。
--
-- 執行方式（可重複執行，idempotent）：
--   psql -h <DB_HOST> -U <DB_USER> -d <DB_NAME> -f sql/000_realtime_tables.sql
--
-- ⚠️ 需要用資料表擁有者執行。已經有這四張表的既有環境不需要跑這支，
--    全部 CREATE 都是 IF NOT EXISTS，跑了也不會動到現有資料。
-- ============================================================


-- ------------------------------------------------------------
-- 1. tia_scada：西門子 S7 (TIA) 點位設定 + 即時值
--    一列 = 一個要採集的 DB 區塊位址。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tia_scada (
    id            BIGSERIAL   PRIMARY KEY,
    name          TEXT        NOT NULL,   -- 點位名稱，同時是 MQTT Key 與增量比對的基準
    plc_ip        TEXT        NOT NULL,
    db_number     INTEGER     NOT NULL,   -- S7 DB 區塊號碼
    "offset"      INTEGER     NOT NULL,   -- 區塊內偏移量。offset 是 SQL 保留字，必須加雙引號
    data_type     TEXT        NOT NULL,   -- DINT / REAL / BOOL / INT ...
    plc_name      TEXT        NOT NULL,
    current_data  JSONB,                  -- {"val": 123.45}
    plc_state     TEXT,                   -- ONLINE / OFFLINE / ERROR
    last_update   TIMESTAMPTZ
);


-- ------------------------------------------------------------
-- 2. modbus_scada：Modbus TCP 點位設定 + 即時值
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS modbus_scada (
    id                BIGSERIAL PRIMARY KEY,
    name              TEXT      NOT NULL,
    plc_ip            TEXT      NOT NULL,
    plc_port          INTEGER   NOT NULL DEFAULT 502,
    slave_id          INTEGER   NOT NULL,          -- 1 ~ 247
    function_code     INTEGER   NOT NULL DEFAULT 3,-- 1=Coils 2=Discrete 3=Holding 4=Input
    start_address     INTEGER   NOT NULL,
    data_type         TEXT      NOT NULL,          -- bool/word/int/dint/float/uint32/int64/...
    raw_min           REAL,                        -- 原始值範圍，供線性縮放
    raw_max           REAL,
    eng_min           REAL,                        -- 工程值範圍
    eng_max           REAL,
    -- BIG/LITTLE。byte_order 是暫存器內 2 個 byte 的順序，
    -- word_order 是多暫存器之間的順序。台灣電表最常見的是 BIG + LITTLE (CDAB)。
    byte_order        TEXT      NOT NULL DEFAULT 'BIG',
    word_order        TEXT      NOT NULL DEFAULT 'BIG',
    -- 狀態字典，把數值轉成文字，例如 {"0":"待機","8":"大火燃燒"}
    -- ⚠️ 轉出來的文字不會寫進 sensor_readings（value 是 NUMERIC）
    state_dictionary  JSONB,
    current_value     REAL,                        -- 純數值形式的當前值
    current_data      JSONB,                       -- {"val": ...}，可能是數字或狀態文字
    plc_state         TEXT,
    last_update       TIMESTAMPTZ
);


-- ------------------------------------------------------------
-- 3. opcua_servers：OPC UA Server 清單與連線參數
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS opcua_servers (
    id               SERIAL       PRIMARY KEY,
    server_name      VARCHAR(100) NOT NULL UNIQUE,  -- 也用來組 MQTT Key，故要求唯一
    ip               VARCHAR(50)  NOT NULL,
    port             INTEGER      NOT NULL DEFAULT 4840,
    username         VARCHAR(100),                  -- NULL = 匿名連線
    password         VARCHAR(200),
    security_policy  VARCHAR(30)  DEFAULT 'None',
    security_mode    VARCHAR(30)  DEFAULT 'None',
    root_node_id     VARCHAR(100) DEFAULT 'i=85',   -- 瀏覽起點，i=85 是 Objects 資料夾
    browse_depth     INTEGER      DEFAULT 5,        -- 遞迴瀏覽深度上限
    enabled          BOOLEAN      DEFAULT TRUE,     -- FALSE 時訂閱服務不會連這台
    conn_state       VARCHAR(20)  DEFAULT 'UNKNOWN',-- 運作中會變成 ONLINE/OFFLINE/ERROR
    last_scan        TIMESTAMPTZ,
    last_error       TEXT
);


-- ------------------------------------------------------------
-- 4. opcua_tags：瀏覽出來的點位與最新數值
--    一列 = 一個 OPC UA 節點。訂閱服務只會對「已綁定 sensor_id」的列
--    建立訂閱，未綁定的列停留在上次瀏覽的快照。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS opcua_tags (
    id            SERIAL       PRIMARY KEY,
    server_id     INTEGER      NOT NULL
                  REFERENCES opcua_servers(id) ON DELETE CASCADE,
    server_name   VARCHAR(100) NOT NULL,   -- 冗餘存一份，組 MQTT Key 時免 JOIN
    node_id       VARCHAR(200) NOT NULL,   -- 例如 ns=2;s=Temp01
    browse_name   VARCHAR(200),
    display_name  VARCHAR(200),
    data_type     VARCHAR(50),             -- OPC UA VariantType 名稱
    current_data  JSONB,                   -- {"val": 123.45}
    quality       VARCHAR(20),             -- GOOD / BAD / UNCERTAIN
    plc_state     VARCHAR(20)  DEFAULT 'OFFLINE',
    last_update   TIMESTAMPTZ,
    -- 同一台 Server 底下 node_id 不重複：瀏覽是用 upsert 寫回的，
    -- 這個 UNIQUE 就是 ON CONFLICT 的依據。
    UNIQUE (server_id, node_id)
);

-- 查詢用索引（PK 與 UNIQUE 的索引由 constraint 自動建立，這裡不重複）
CREATE INDEX IF NOT EXISTS idx_opcua_tags_server_id ON opcua_tags (server_id);
CREATE INDEX IF NOT EXISTS idx_opcua_tags_name      ON opcua_tags (server_name);


-- ------------------------------------------------------------
-- 欄位說明（COMMENT）
-- ------------------------------------------------------------
COMMENT ON TABLE tia_scada     IS '即時層：西門子 S7 (TIA) 點位設定與最新值';
COMMENT ON TABLE modbus_scada  IS '即時層：Modbus TCP 點位設定與最新值';
COMMENT ON TABLE opcua_servers IS '即時層：OPC UA Server 清單與連線參數';
COMMENT ON TABLE opcua_tags    IS '即時層：OPC UA 瀏覽出來的點位與最新值';

COMMENT ON COLUMN tia_scada."offset" IS
    'S7 DB 區塊內的位元組偏移量。offset 是 SQL 保留字，查詢時必須寫成 "offset"。';
COMMENT ON COLUMN modbus_scada.byte_order IS
    'BIG / LITTLE，單一暫存器（16-bit）內 2 個位元組的順序。Modbus 標準是 BIG。';
COMMENT ON COLUMN modbus_scada.word_order IS
    'BIG / LITTLE，多暫存器（32/64-bit）之間的順序。台灣電表常見 BIG+LITTLE（CDAB）。';
COMMENT ON COLUMN opcua_tags.server_name IS
    '冗餘欄位，與 opcua_servers.server_name 同值，組 MQTT Key 時免 JOIN。';
