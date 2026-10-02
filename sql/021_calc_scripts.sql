-- ============================================================
-- 021_calc_scripts.sql
-- ============================================================
-- 目的：計算點支援 Python 腳本（v3.4）
--
--   kind        expression（預設，運算式）/ python（Python 腳本，程式碼存在 expression 欄位）
--   last_log    腳本 log() 的輸出（最後一輪），給網頁顯示除錯
--
--   Python 腳本由獨立子程序執行（services/calc/script_runner.py），有逾時與記憶體上限；
--   功能預設關閉，需在 .env 設定 CALC_SCRIPTS_ENABLED=true，且只有「管理員」能新增 / 修改。
--
-- 執行方式（可重複執行，idempotent）：
--   psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -f sql/021_calc_scripts.sql
-- 前置需求：sql/017_calculated_points.sql
-- ============================================================

ALTER TABLE calculated_points
    ADD COLUMN IF NOT EXISTS kind     VARCHAR(10) NOT NULL DEFAULT 'expression',
    ADD COLUMN IF NOT EXISTS last_log TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_calculated_points_kind') THEN
        ALTER TABLE calculated_points
            ADD CONSTRAINT chk_calculated_points_kind CHECK (kind IN ('expression', 'python'));
    END IF;
END $$;

COMMENT ON COLUMN calculated_points.kind IS 'expression = 運算式；python = Python 腳本（程式碼存在 expression 欄位）';
COMMENT ON COLUMN calculated_points.last_log IS 'Python 腳本最後一輪 log() 的輸出';
