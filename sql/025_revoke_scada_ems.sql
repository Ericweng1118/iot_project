-- ============================================================
-- 025_revoke_scada_ems.sql
-- ============================================================
-- 目的：
--   PME 改用專用帳號 pme（PME 的 sql/005_pme_role.sql，2026-10-19 上線）後，觀察一週
--   （到 2026-10-26）沒問題，就收回 scada 帳號在 ems schema 的過渡期權限。
--   005 第 2 段為了「PME 切換失敗時 .env 改回 scada 仍可運作」而保留的權限，這裡全部收回：
--     * ems schema 的 USAGE / CREATE
--     * ems 底下所有 table / sequence / function 的權限
--     * 兩條預設權限：pme 之後在 ems 新建的 table / sequence 自動授權給 scada
--
-- 為什麼放在 SCADA repo：scada 帳號和它的授權歸 SCADA 管，pme 帳號歸 PME 管。
--
-- ⚠️ 不是 migration，不要放進批次執行；觀察期結束、Eric 決定後手動執行一次。
--
-- 執行前確認：
--   1. PME 的 .env 是 DB_USER=pme（pg_stat_activity 看得到 usename = 'pme' 的連線）。
--   2. Grafana 等其他工具沒有用 scada 帳號查 ems 的表（收回後這類查詢會 permission denied）。
--   3. 執行後 SCADA 本身不受影響：SCADA 程式不讀寫 ems（grep 過整個 repo）。
--
-- 不在本檔範圍（需要的話另外處理）：
--   * scada 在 energy_srv / erp_srv 的 FDW user mapping（PME 已有自己的 pme mapping）。
--   * 「FOR ROLE scada IN SCHEMA ems → api_reader」的舊預設權限：收回 CREATE 後 scada
--     不能再在 ems 建物件，這條不會再生效，留著無害。
--
-- 可重複執行（REVOKE 沒有該權限時不報錯）；角色不存在時只印 NOTICE。
-- 執行方式（superuser 或 ems 擁有者 pme；目前用 eric）：
--   psql -h <DB_HOST> -U eric -d scada -v ON_ERROR_STOP=1 -f sql/025_revoke_scada_ems.sql
-- 回滾：sql/025_rollback.sql
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

    -- 物件權限：先收物件、再收 schema
    REVOKE ALL ON ALL TABLES    IN SCHEMA ems FROM scada;
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA ems FROM scada;
    REVOKE ALL ON ALL FUNCTIONS IN SCHEMA ems FROM scada;
    REVOKE ALL ON SCHEMA ems FROM scada;

    -- hypertable（目前只有 ems.interval_data）的 chunk 放在 _timescaledb_internal，不在 ems，
    -- 上面的 ALL TABLES IN SCHEMA ems 收不到；建立 chunk 時複製了 hypertable 的 ACL，scada 仍可
    -- 直接 SELECT _timescaledb_internal._hyper_*_chunk。對 hypertable 本身 REVOKE 才會傳到所有 chunk。
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        FOR r IN
            SELECT format('%I.%I', hypertable_schema, hypertable_name) AS ht
            FROM timescaledb_information.hypertables WHERE hypertable_schema = 'ems'
        LOOP
            EXECUTE format('REVOKE ALL ON %s FROM scada', r.ht);
        END LOOP;
    END IF;

    -- 005 第 94、95 行的預設權限：pme 之後新建的物件不再自動授權給 scada
    ALTER DEFAULT PRIVILEGES FOR ROLE pme IN SCHEMA ems REVOKE ALL ON TABLES    FROM scada;
    ALTER DEFAULT PRIVILEGES FOR ROLE pme IN SCHEMA ems REVOKE ALL ON SEQUENCES FROM scada;

    RAISE NOTICE '已收回 scada 在 ems 的所有權限與兩條 pme→scada 預設權限';
END $$;

COMMIT;

-- 驗證（預期依序為 f, f, 0, 0, 0）：
--   SELECT has_schema_privilege('scada', 'ems', 'USAGE');
--   SELECT has_schema_privilege('scada', 'ems', 'CREATE');
--   SELECT count(*) FROM information_schema.role_table_grants
--    WHERE grantee = 'scada' AND table_schema = 'ems';
--   SELECT count(*) FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace
--    WHERE n.nspname = 'ems' AND d.defaclrole = 'pme'::regrole
--      AND d.defaclacl::text LIKE '%scada=%';
--   -- ems hypertable 的 chunk（在 _timescaledb_internal）不再帶 scada 權限
--   SELECT count(*) FROM timescaledb_information.chunks ch
--     JOIN pg_class c ON c.relname = ch.chunk_name
--     JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = ch.chunk_schema
--    WHERE ch.hypertable_schema = 'ems' AND c.relacl::text LIKE '%scada=%';
