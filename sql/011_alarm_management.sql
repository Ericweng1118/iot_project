-- ============================================================
-- 011_alarm_management.sql
-- ============================================================
-- 目的：
--   建立警報管理（Alarm Management）需要的兩張表，取代原本「異常監控」分頁
--   只能「查詢當下狀態」的做法：
--
--     alarm_rules   逐感測器的警報規則（HH / H / L / LL / 等於 / 不等於），
--                   含遲滯（deadband）、延遲觸發（on-delay）、優先等級
--     alarm_events  警報事件歷史：何時發生、何時恢復、誰在何時確認（ACK）
--
--   警報引擎（services/alarm/engine.py）跑在 main.py 裡，直接讀記憶體中的最新值
--   判斷，不必等 sensor_readings 的統一寫入週期；網頁「警報中心」讀 alarm_events
--   顯示、確認。
--
--   sensors.min_threshold / max_threshold 仍然有效：警報引擎會把它們當成隱含的
--   L / H 規則（優先等級「中」），既有設定不需要搬家。要更細的設定（HH/LL、
--   延遲、遲滯、自訂訊息）才需要另外在 alarm_rules 建規則。
--   可用 .env 的 ALARM_USE_SENSOR_LIMITS=false 關閉這個行為。
--
-- 執行方式（可重複執行，idempotent）：
--   psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -v app_user=<應用程式帳號> \
--        -f sql/011_alarm_management.sql
--   -v app_user 省略時預設授權給 scada；該角色不存在則略過授權。
--
-- 前置需求：000、001（sensors）、opcua_servers
-- ============================================================

-- ------------------------------------------------------------
-- 1. alarm_rules：警報規則
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alarm_rules (
    rule_id       SERIAL       PRIMARY KEY,
    sensor_id     INTEGER      NOT NULL REFERENCES sensors(sensor_id) ON DELETE CASCADE,
    -- HH / H：數值 > setpoint 觸發；L / LL：數值 < setpoint 觸發
    -- EQ：數值 = setpoint 觸發（例如故障碼）；NE：數值 ≠ setpoint 觸發（例如應為 1 的運轉訊號）
    alarm_type    VARCHAR(4)   NOT NULL,
    setpoint      NUMERIC      NOT NULL,
    -- 遲滯：H/HH 要降到 setpoint - deadband 以下才恢復，L/LL 反之，避免在門檻附近反覆跳動
    deadband      NUMERIC      NOT NULL DEFAULT 0,
    -- 延遲觸發：條件要「連續」成立這麼多秒才真的發出警報，濾掉瞬間突波
    on_delay_sec  INTEGER      NOT NULL DEFAULT 0,
    -- 1=緊急 2=高 3=中 4=低
    priority      SMALLINT     NOT NULL DEFAULT 2,
    message       VARCHAR(200),
    enabled       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT chk_alarm_rules_type     CHECK (alarm_type IN ('HH', 'H', 'L', 'LL', 'EQ', 'NE')),
    CONSTRAINT chk_alarm_rules_priority CHECK (priority BETWEEN 1 AND 4),
    CONSTRAINT chk_alarm_rules_deadband CHECK (deadband >= 0),
    CONSTRAINT chk_alarm_rules_delay    CHECK (on_delay_sec >= 0)
);

CREATE INDEX IF NOT EXISTS idx_alarm_rules_sensor ON alarm_rules (sensor_id);

COMMENT ON TABLE alarm_rules IS
    '警報規則。sensors.min_threshold / max_threshold 另外會被警報引擎視為隱含的 L / H 規則。';

-- ------------------------------------------------------------
-- 2. alarm_events：警報事件（同時是目前警報清單與歷史紀錄）
--    「目前警報」= cleared_at IS NULL（仍在發生）或 acked_at IS NULL（已恢復但還沒人確認）
--    這是 ISA-18.2 的慣例：恢復正常但沒有人確認過的警報，仍要留在清單上讓人看到。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alarm_events (
    event_id       BIGSERIAL    PRIMARY KEY,
    -- 同一個警報來源的唯一識別，例如 rule:12、limit:34:H、server:3:offline
    alarm_key      VARCHAR(100) NOT NULL,
    -- sensor_rule / sensor_limit / server_offline / device_offline
    source_type    VARCHAR(20)  NOT NULL,
    sensor_id      INTEGER      REFERENCES sensors(sensor_id) ON DELETE SET NULL,
    server_id      INTEGER      REFERENCES opcua_servers(id) ON DELETE SET NULL,
    rule_id        INTEGER      REFERENCES alarm_rules(rule_id) ON DELETE SET NULL,
    alarm_type     VARCHAR(20)  NOT NULL,
    priority       SMALLINT     NOT NULL,
    message        TEXT         NOT NULL,
    trigger_value  NUMERIC,
    setpoint       NUMERIC,
    raised_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    cleared_at     TIMESTAMPTZ,
    clear_value    NUMERIC,
    acked_at       TIMESTAMPTZ,
    acked_by       VARCHAR(100),
    ack_comment    TEXT
);

-- 同一個 alarm_key 同一時間只能有一筆「仍在發生」的事件。
-- 警報引擎重啟後重複判斷到同一個條件時，INSERT ... ON CONFLICT 會直接略過。
CREATE UNIQUE INDEX IF NOT EXISTS uq_alarm_events_active_key
    ON alarm_events (alarm_key) WHERE cleared_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_alarm_events_raised   ON alarm_events (raised_at DESC);
CREATE INDEX IF NOT EXISTS idx_alarm_events_sensor   ON alarm_events (sensor_id, raised_at DESC);
CREATE INDEX IF NOT EXISTS idx_alarm_events_unacked  ON alarm_events (raised_at DESC) WHERE acked_at IS NULL;

COMMENT ON TABLE alarm_events IS
    '警報事件。目前警報 = cleared_at IS NULL OR acked_at IS NULL。';

-- ------------------------------------------------------------
-- 3. 權限：應用程式帳號需要讀寫這兩張表與序列
-- ------------------------------------------------------------
\if :{?app_user}
\else
\set app_user scada
\endif
\o /dev/null
SELECT set_config('migration.app_user', :'app_user', false);
\o

DO $$
DECLARE
    app_role text := current_setting('migration.app_user');
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = app_role) THEN
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON alarm_rules, alarm_events TO %I', app_role);
        EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE alarm_rules_rule_id_seq, alarm_events_event_id_seq TO %I', app_role);
        RAISE NOTICE '已授權 alarm_rules / alarm_events 給 %', app_role;
    ELSE
        RAISE NOTICE '角色 % 不存在，略過授權（用 -v app_user=<帳號> 指定）', app_role;
    END IF;
END $$;
