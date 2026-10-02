-- ============================================================
-- 017_calculated_points.sql
-- ============================================================
-- 目的：計算點（虛擬感測器）。用運算式把其他感測器的即時值算成一個新的值，例如
--       總功率 = 三台電表功率相加、單位耗氣 = 蒸氣量 / 燃氣量、運轉判斷 = 電流 > 5。
--
--   計算結果寫到 sensor_id 指向的「一般感測器」，所以歷史資料、趨勢、報表、警報規則
--   全部跟實體感測器一樣用，不需要另外處理。運算式語法見 services/calc/expression.py。
--
--   current_value / quality / state / last_error / last_update 是計算引擎（main.py）
--   每輪回寫的即時狀態，給網頁顯示。
--
-- 執行方式（可重複執行，idempotent）：
--   psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -v app_user=<應用程式帳號> \
--        -f sql/017_calculated_points.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS calculated_points (
    calc_id        SERIAL        PRIMARY KEY,
    -- 一個感測器只能是一個計算點的結果；這個感測器不應該再綁定實體點位
    sensor_id      INTEGER       NOT NULL UNIQUE REFERENCES sensors(sensor_id) ON DELETE CASCADE,
    expression     TEXT          NOT NULL,
    description    TEXT,
    enabled        BOOLEAN       NOT NULL DEFAULT TRUE,
    current_value  DOUBLE PRECISION,
    quality        VARCHAR(20),               -- GOOD / UNCERTAIN（與 opcua_tags.quality 同樣的寫法）
    state          VARCHAR(20)   DEFAULT 'OFFLINE',  -- ONLINE / OFFLINE（輸入無資料）/ ERROR（運算式錯誤）
    last_error     TEXT,
    last_update    TIMESTAMPTZ,
    created_by     VARCHAR(100),
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ   NOT NULL DEFAULT now()
);

COMMENT ON TABLE calculated_points IS '計算點：以運算式從其他感測器的即時值算出新感測器的值';

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
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON calculated_points TO %I', app_role);
        EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE calculated_points_calc_id_seq TO %I', app_role);
        RAISE NOTICE '已授權 calculated_points 給 %', app_role;
    ELSE
        RAISE NOTICE '角色 % 不存在，略過授權（用 -v app_user=<帳號> 指定）', app_role;
    END IF;
END $$;
