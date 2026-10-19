-- ============================================================================
-- 023_rollback.sql —— 回滾 023_pme_app_users_grant.sql
-- ============================================================================
-- 收回 pme 對 app_users 的欄位層級 INSERT / UPDATE。
-- 不動 PME 的 005 授權的 SELECT 與 UPDATE (last_login)，PME 仍可登入與記錄最後登入時間。
--
-- ⚠️ 不是 migration，只有確定要關掉 PME 的使用者管理時才手動執行，不要放進批次。
--    已由 PME 建立或修改的帳號不會被還原，只是之後 PME 不能再寫。
--
-- 執行方式（需資料表擁有者或 superuser）：
--   psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -v pme_user=pme \
--        -f sql/023_rollback.sql
-- ============================================================================

\if :{?pme_user}
\else
\set pme_user pme
\endif
\o /dev/null
SELECT set_config('migration.pme_user', :'pme_user', false);
\o

DO $$
DECLARE
    pme_role text := current_setting('migration.pme_user');
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = pme_role) THEN
        EXECUTE format(
            'REVOKE INSERT (username, password_hash, display_name, role, enabled) ON app_users FROM %I',
            pme_role);
        EXECUTE format(
            'REVOKE UPDATE (password_hash, display_name, role, enabled) ON app_users FROM %I',
            pme_role);
        RAISE NOTICE '已收回 % 對 app_users 的 INSERT / UPDATE（保留 SELECT 與 UPDATE (last_login)）', pme_role;
    ELSE
        RAISE NOTICE '角色 % 不存在，無需回滾', pme_role;
    END IF;
END $$;

COMMENT ON TABLE app_users IS
    '網頁後台使用者。.env 的 ADMIN_USER 不在這張表裡，永遠視為 admin（救援帳號）。';
