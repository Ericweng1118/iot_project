-- ============================================================================
-- 010_rollback.sql —— 回滾 010_cumulative_counter_upload_condition.sql
-- ============================================================================
-- 把 010 改成 on_change 的那 63 個累計型感測器改回 threshold_percent，
-- 並把 010 補上的顯式門檻 1 清回 NULL。
--
-- ⚠️ 回滾之後，那 63 個累計型感測器會再次回到「變化未達 1% 就不寫入」的狀態。
--    在累計電表這種基準值百萬等級的點位上，等同於實質停止寫入
--    （這正是 2026-09-03 ~ 09-11 發生的狀況）。除非確定要回到舊行為，否則不要跑。
--    註：data_layer/timeseries_writer.py 的心跳保底（SENSOR_HEARTBEAT_INTERVAL，
--    預設 3600 秒）仍會每小時補寫一筆，不會像當初那樣完全沒有資料。
-- ============================================================================

BEGIN;

UPDATE sensors
   SET upload_condition = 'threshold_percent'
 WHERE upload_condition = 'on_change'
   AND sensor_id IN (
        44, 46, 48, 50, 52, 54, 56, 58, 60, 62, 64, 66, 68, 70, 72, 74, 76, 78,
        80, 82, 84, 86, 89, 90, 92, 95, 96, 103, 104, 106, 108, 110, 112, 114,
        116, 119, 120, 122, 124, 126, 128, 130, 132, 134, 137, 140, 142, 143,
        144, 145, 146, 147, 148, 149, 150, 151, 152, 153, 154, 155, 156, 163, 164
   );

UPDATE sensors
   SET upload_threshold = NULL
 WHERE upload_threshold = 1
   AND upload_condition IN ('threshold_percent', 'threshold_absolute');

COMMIT;
