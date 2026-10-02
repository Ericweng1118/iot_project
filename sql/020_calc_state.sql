-- ============================================================
-- 020_calc_state.sql
-- ============================================================
-- 目的：計算點的跨輪狀態（v3.3 累計 / 動態函式）
--
--   integral（kW→kWh）、delta（今日用量的基準值）、ontime（運轉時數）、count（啟動次數）、
--   prev / hold / filter 等函式需要記住上一輪的資料。計算引擎每輪把狀態寫進 calc_state，
--   main.py 重啟後讀回來接續累計，不會歸零。
--   移動視窗（movavg / movmin / movmax）不存（重啟後重新累積），避免狀態變得很大。
--
--   沒跑這支 migration 也能用這些函式，只是重啟後累計值會從 0 開始
--   （delta 例外：它的基準值本來就從 sensor_readings 查，不受影響）。
--
-- 執行方式（可重複執行，idempotent）：
--   psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -f sql/020_calc_state.sql
-- ============================================================

ALTER TABLE calculated_points ADD COLUMN IF NOT EXISTS calc_state JSONB;

COMMENT ON COLUMN calculated_points.calc_state IS
    '計算引擎的跨輪狀態（累計值、上一輪的值…），由 main.py 維護，請勿手動修改。運算式改變時自動重置。';
