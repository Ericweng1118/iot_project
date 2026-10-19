-- ============================================================
-- 023_pme_app_users_grant.sql
-- ============================================================
-- 目的：
--   授權 PME 的資料庫帳號（預設 pme）管理 public.app_users，
--   讓 PME 網頁的「使用者管理」（admin 專用）可以新增帳號、改顯示名稱 / 角色、
--   啟停用、重設密碼。PME 與 SCADA 共用同一張表登入，不另建表。
--
-- 授權範圍（欄位層級，刻意最小）：
--   INSERT (username, password_hash, display_name, role, enabled)
--       created_at 走預設值，last_login 由登入流程更新，不開放新增時指定。
--   UPDATE (password_hash, display_name, role, enabled)
--       不含 username（帳號不可改名）；last_login 的 UPDATE 授權是 PME 的 005 給的，這裡不動。
--   不授權 DELETE（停用帳號請用 enabled = false）。
--   app_users 沒有 serial id，所以不需要 SEQUENCE 授權。
--
-- ⚠️ 風險（已由 Eric 決定接受）：
--   pme 能建立任何角色（含 admin）的帳號、重設任何帳號（含 SCADA admin）的密碼，
--   等於 PME 的 DB 帳號擁有 SCADA 的 admin 權限。PME 端需自行限制 admin 才能操作，
--   並寫 audit_log（action 為 ems.user.*）。.env 的 ADMIN_USER 救援帳號不在表內，不受影響，
--   但 PME 不得建立與 ADMIN_USER 同名的列（否則會擋掉救援帳號登入）。
--
-- 前提：
--   pme 角色已由 PME 的 sql/005_pme_role.sql 建立。角色不存在時只印 NOTICE 並略過，不會報錯。
--
-- 執行方式（可重複執行，idempotent；需資料表擁有者或 superuser）：
--   psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -v pme_user=pme \
--        -f sql/023_pme_app_users_grant.sql
--   -v pme_user 省略時預設 pme。
--
-- 回滾：sql/023_rollback.sql（不是 migration，不要放進批次執行）。
-- ============================================================

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
        -- 新增帳號：只開放 PME 需要給值的欄位，其餘（created_at、last_login）走預設
        EXECUTE format(
            'GRANT INSERT (username, password_hash, display_name, role, enabled) ON app_users TO %I',
            pme_role);
        -- 修改帳號：不含 username（PK，不可改名）
        EXECUTE format(
            'GRANT UPDATE (password_hash, display_name, role, enabled) ON app_users TO %I',
            pme_role);
        RAISE NOTICE '已授權 % 對 app_users 的欄位層級 INSERT / UPDATE（不含 DELETE）', pme_role;
    ELSE
        RAISE NOTICE '角色 % 不存在，略過授權（請先執行 PME 的 005_pme_role.sql，或用 -v pme_user=<帳號> 指定）', pme_role;
    END IF;
END $$;

COMMENT ON TABLE app_users IS
    '網頁後台使用者（SCADA 與 PME 共用登入）。.env 的 ADMIN_USER 不在這張表裡，永遠視為 admin（救援帳號）。'
    'pme 角色可 INSERT / UPDATE 帳號資料欄位（見 sql/023），不可 DELETE，停用請用 enabled = false。';
