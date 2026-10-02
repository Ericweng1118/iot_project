-- ============================================================
-- 013_timeseries_policy.sql（選用）
-- ============================================================
-- 目的：
--   處理 todo.md 待辦 #1「sensor_readings 資料量成長」：啟用 TimescaleDB 原生壓縮。
--   超過 :compress_after（預設 30 天）的 chunk 會自動壓縮，依實際案例通常可省下
--   80% ~ 95% 的磁碟空間，且壓縮後仍可正常 SELECT（趨勢圖、報表照常可用）。
--
--   ⚠️ 本檔「只」啟用壓縮，不刪除任何資料。
--      資料保留政策（自動刪除超過 N 年的資料）是不可逆的營運決策，刻意不在這裡啟用，
--      需要時請看檔尾的註解區塊，確認保留年限後再手動執行。
--
-- 注意事項（請先讀完再執行）：
--   1. 先在測試環境驗證：壓縮第一次執行會把所有超過門檻的 chunk 一次壓完，
--      資料量大時會花一些時間、佔用 CPU / IO，建議離峰時段執行。
--   2. 壓縮後的 chunk 不適合大量 UPDATE / DELETE（TimescaleDB 2.11+ 可以做，但很慢）。
--      若有「回頭修正一個月前資料」的需求，請把 :compress_after 調長。
--   3. compress_segmentby = sensor_id：配合趨勢 / 報表「依感測器查時間區間」的查詢模式。
--   4. 寫入排程只寫「現在附近」的資料，不會寫進已壓縮的舊 chunk，不影響採集。
--
-- 執行方式（可重複執行，idempotent）：
--   psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -f sql/013_timeseries_policy.sql
--   psql ... -v compress_after="'14 days'" -f sql/013_timeseries_policy.sql   -- 自訂門檻
--
-- 回復（停用壓縮政策，已壓縮的 chunk 仍維持壓縮狀態）：
--   SELECT remove_compression_policy('sensor_readings', if_exists => true);
--   -- 解壓縮全部：SELECT decompress_chunk(c, true) FROM show_chunks('sensor_readings') c;
-- ============================================================

\if :{?compress_after}
\else
\set compress_after '\'30 days\''
\endif

ALTER TABLE sensor_readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'sensor_id',
    timescaledb.compress_orderby   = 'reading_time DESC'
);

SELECT add_compression_policy('sensor_readings', INTERVAL :compress_after, if_not_exists => true);

-- 執行後可用這個查詢確認壓縮效果：
--   SELECT pg_size_pretty(before_compression_total_bytes) AS 壓縮前,
--          pg_size_pretty(after_compression_total_bytes)  AS 壓縮後
--   FROM hypertable_compression_stats('sensor_readings');

-- ------------------------------------------------------------
-- 資料保留政策（預設不執行）
-- 確認「超過幾年的歷史資料可以永久刪除」之後，再手動執行下面這行：
--   SELECT add_retention_policy('sensor_readings', INTERVAL '3 years', if_not_exists => true);
-- 取消：
--   SELECT remove_retention_policy('sensor_readings', if_exists => true);
-- ------------------------------------------------------------
