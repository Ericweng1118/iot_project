-- ============================================================
-- 025_rollback.sql —— 回滾 025_revoke_scada_ems.sql
-- ============================================================
-- 把 scada 在 ems 的過渡期權限加回來，內容與 PME 的 sql/005_pme_role.sql 第 2 段相同。
-- 使用時機：收回後發現還有東西用 scada 帳號讀寫 ems（例如 PME 需要暫時改回 scada 帳號，
-- 或 Grafana 的資料來源還用 scada）。
--
-- ⚠️ 不是 migration，不要放進批次執行。
-- 執行方式（superuser 或 ems 擁有者 pme；目前用 eric）：
--   psql -h <DB_HOST> -U eric -d scada -v ON_ERROR_STOP=1 -f sql/025_rollback.sql
-- ============================================================

BEGIN;

DO $$
DECLARE
    r record;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'scada')
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pme')
       OR NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'ems') THEN
        RAISE NOTICE 'scada / pme 角色或 ems schema 不存在，略過';
        RETURN;
    END IF;

    GRANT ALL ON SCHEMA ems TO scada;
    GRANT ALL ON ALL TABLES    IN SCHEMA ems TO scada;
    GRANT ALL ON ALL SEQUENCES IN SCHEMA ems TO scada;
    GRANT ALL ON ALL FUNCTIONS IN SCHEMA ems TO scada;
    -- hypertable 的 chunk 在 _timescaledb_internal，對 hypertable 本身 GRANT 才會傳到所有 chunk
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        FOR r IN
            SELECT format('%I.%I', hypertable_schema, hypertable_name) AS ht
            FROM timescaledb_information.hypertables WHERE hypertable_schema = 'ems'
        LOOP
            EXECUTE format('GRANT ALL ON %s TO scada', r.ht);
        END LOOP;
    END IF;
    ALTER DEFAULT PRIVILEGES FOR ROLE pme IN SCHEMA ems GRANT ALL ON TABLES    TO scada;
    ALTER DEFAULT PRIVILEGES FOR ROLE pme IN SCHEMA ems GRANT ALL ON SEQUENCES TO scada;

    RAISE NOTICE '已恢復 scada 在 ems 的過渡期權限（同 PME 005 第 2 段）';
END $$;

COMMIT;
