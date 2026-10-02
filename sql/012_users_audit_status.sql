-- ============================================================
-- 012_users_audit_status.sql
-- ============================================================
-- 目的：
--   1. app_users       網頁後台多使用者 + 角色權限（viewer / operator / engineer / admin）
--   2. audit_log       操作稽核：誰在什麼時候改了什麼設定、確認了哪個警報
--   3. service_status  採集主程式（main.py）每 10 秒回報一次心跳與執行統計，
--                      網頁才看得出「採集服務本身是不是還活著」
--
--   .env 的 ADMIN_USER / ADMIN_PASSWORD 仍然可以登入（永遠是 admin 角色），
--   當作救援帳號，所以跑完這支 migration 之後不需要先建帳號也能登入。
--
-- 執行方式（可重複執行，idempotent）：
--   psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -v app_user=<應用程式帳號> \
--        -f sql/012_users_audit_status.sql
--   -v app_user 省略時預設授權給 scada；該角色不存在則略過授權。
-- ============================================================

-- ------------------------------------------------------------
-- 1. app_users
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_users (
    username       VARCHAR(50)   PRIMARY KEY,
    display_name   VARCHAR(100),
    -- core/auth.py 產生的 pbkdf2_sha256$<iterations>$<salt>$<hash>
    password_hash  VARCHAR(255)  NOT NULL,
    role           VARCHAR(20)   NOT NULL DEFAULT 'viewer',
    enabled        BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    last_login     TIMESTAMPTZ,
    CONSTRAINT chk_app_users_role CHECK (role IN ('viewer', 'operator', 'engineer', 'admin'))
);

COMMENT ON TABLE app_users IS
    '網頁後台使用者。.env 的 ADMIN_USER 不在這張表裡，永遠視為 admin（救援帳號）。';

-- ------------------------------------------------------------
-- 2. audit_log
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL     PRIMARY KEY,
    ts         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    username   VARCHAR(100)  NOT NULL,
    -- 例如 login / sensor.update / alarm.ack / user.create
    action     VARCHAR(50)   NOT NULL,
    -- 例如 sensor:12、alarm_event:345、user:alice
    target     VARCHAR(200),
    detail     JSONB
);

CREATE INDEX IF NOT EXISTS idx_audit_log_ts   ON audit_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_user ON audit_log (username, ts DESC);

-- ------------------------------------------------------------
-- 3. service_status
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS service_status (
    service_name    VARCHAR(50)   PRIMARY KEY,   -- 目前只有 collector（main.py）
    host            VARCHAR(100),
    pid             INTEGER,
    version         VARCHAR(20),
    started_at      TIMESTAMPTZ,
    last_heartbeat  TIMESTAMPTZ,
    -- 各子系統的執行統計（寫入排程、OPC UA 各 Server、警報引擎），格式見 services/status_reporter.py
    info            JSONB
);

COMMENT ON TABLE service_status IS
    'main.py 每 10 秒更新一次 last_heartbeat；網頁判斷超過 60 秒沒更新即視為採集服務停止。';

-- ------------------------------------------------------------
-- 4. 權限
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
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON app_users, audit_log, service_status TO %I', app_role);
        EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE audit_log_id_seq TO %I', app_role);
        RAISE NOTICE '已授權 app_users / audit_log / service_status 給 %', app_role;
    ELSE
        RAISE NOTICE '角色 % 不存在，略過授權（用 -v app_user=<帳號> 指定）', app_role;
    END IF;
END $$;
