-- ============================================================
-- 018_report_schedules.sql
-- ============================================================
-- 目的：排程報表。每日 / 每週 / 每月在指定時間自動產生上一期的 Excel 報表，
--       寄到指定信箱（SMTP 設定沿用警報通知）或送到 Webhook。
--
--   排程由 main.py 的 ReportScheduler（services/report_scheduler.py）每分鐘檢查一次；
--   採集服務停機錯過的排程，恢復後只補寄「最近一期」，不會一次補寄一堆。
--
-- 執行方式（可重複執行，idempotent）：
--   psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -v app_user=<應用程式帳號> \
--        -f sql/018_report_schedules.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS report_schedules (
    schedule_id   SERIAL        PRIMARY KEY,
    name          VARCHAR(100)  NOT NULL,
    enabled       BOOLEAN       NOT NULL DEFAULT TRUE,
    -- daily：每天寄前一天｜weekly：每週寄上週一 ~ 週日｜monthly：每月寄上個月
    frequency     VARCHAR(10)   NOT NULL,
    send_time     TIME          NOT NULL DEFAULT '07:00',   -- SCADA_TIMEZONE 當地時間
    weekday       SMALLINT      NOT NULL DEFAULT 0,         -- weekly 用，0 = 週一
    day_of_month  SMALLINT      NOT NULL DEFAULT 1,         -- monthly 用，1 ~ 28
    -- auto：每日報表用每小時、每週 / 每月用每日
    granularity   VARCHAR(10)   NOT NULL DEFAULT 'auto',
    metrics       TEXT[]        NOT NULL DEFAULT '{avg}',   -- avg / min / max / last / delta
    device_codes  TEXT[]        NOT NULL DEFAULT '{}',
    sensor_codes  TEXT[]        NOT NULL DEFAULT '{}',
    recipients    TEXT[]        NOT NULL DEFAULT '{}',      -- 空白 = 用 .env 的 ALARM_EMAIL_TO
    send_email    BOOLEAN       NOT NULL DEFAULT TRUE,
    send_webhook  BOOLEAN       NOT NULL DEFAULT FALSE,
    last_run_at   TIMESTAMPTZ,                              -- 最後一次「排定」的執行時間
    last_period   TEXT,
    last_status   VARCHAR(20),
    last_error    TEXT,
    created_by    VARCHAR(100),
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT chk_report_frequency   CHECK (frequency IN ('daily', 'weekly', 'monthly')),
    CONSTRAINT chk_report_granularity CHECK (granularity IN ('auto', '1 hour', '1 day', '1 month')),
    CONSTRAINT chk_report_weekday     CHECK (weekday BETWEEN 0 AND 6),
    CONSTRAINT chk_report_day         CHECK (day_of_month BETWEEN 1 AND 28)
);

COMMENT ON TABLE report_schedules IS '排程報表：定期產生上一期的 Excel 報表寄送';

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
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON report_schedules TO %I', app_role);
        EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE report_schedules_schedule_id_seq TO %I', app_role);
        RAISE NOTICE '已授權 report_schedules 給 %', app_role;
    ELSE
        RAISE NOTICE '角色 % 不存在，略過授權（用 -v app_user=<帳號> 指定）', app_role;
    END IF;
END $$;
