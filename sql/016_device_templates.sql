-- ============================================================
-- 016_device_templates.sql
-- ============================================================
-- 目的：設備範本。同型設備（例如 20 台同型號電表）的感測器、點位、警報規則定義一次，
--       之後輸入設備編號 / IP / 站號就能一次建好整台設備的全部設定。
--
--   definition（JSONB）格式見 data_layer/templates.py 的說明。範本可以從現有設備
--   直接產生（網頁「設備範本 → 從現有設備建立」），不需要手寫 JSON。
--
-- 執行方式（可重複執行，idempotent）：
--   psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -v app_user=<應用程式帳號> \
--        -f sql/016_device_templates.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS device_templates (
    template_id  SERIAL        PRIMARY KEY,
    name         VARCHAR(100)  NOT NULL UNIQUE,
    description  TEXT,
    -- none：只建設備與感測器｜modbus：同時建 Modbus 點位｜opcua：依節點樣式綁定已瀏覽的 OPC UA 點位
    protocol     VARCHAR(20)   NOT NULL DEFAULT 'none',
    definition   JSONB         NOT NULL,
    created_by   VARCHAR(100),
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT chk_device_templates_protocol CHECK (protocol IN ('none', 'modbus', 'opcua'))
);

COMMENT ON TABLE device_templates IS '設備範本：同型設備的感測器 / 點位 / 警報規則定義';

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
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON device_templates TO %I', app_role);
        EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE device_templates_template_id_seq TO %I', app_role);
        RAISE NOTICE '已授權 device_templates 給 %', app_role;
    ELSE
        RAISE NOTICE '角色 % 不存在，略過授權（用 -v app_user=<帳號> 指定）', app_role;
    END IF;
END $$;
