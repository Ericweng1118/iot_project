# 資料庫 Migration 說明

依檔名數字**由小到大**執行（`000` 最先），全部都是 idempotent（可重複執行）：

| 檔案 | 內容 | 何時要跑 |
| --- | --- | --- |
| `000_realtime_tables.sql` | 建立即時層四張表：`tia_scada` / `modbus_scada` / `opcua_servers` / `opcua_tags`（含索引與 UNIQUE / FK constraint） | 全新部署 |
| `001_sensor_hierarchy_and_mapping.sql` | 建立 TimescaleDB 擴充、`sites` → `production_lines` → `devices` → `sensors` → `sensor_readings` 階層表，並在 `tia_scada` / `modbus_scada` / `opcua_tags` 加上 `sensor_id` 欄位 | 全新部署 |
| `006_opcua_upgrade.sql` | `sensors` 新增 `opcua_sampling_interval_ms` / `upload_condition` / `upload_threshold`（v2 統一寫入排程） | 全新部署、v1 升 v2 |
| `007_opcua_deadband.sql` | `sensors` 新增 `opcua_deadband_type` / `opcua_deadband_value`（OPC UA 伺服器端過濾） | 全新部署、v1 升 v2 |
| `008_missing_app_columns.sql` | 補齊「正式庫早就有、但沒有腳本建立」的欄位：`sensors.nickname` / `state_dictionary`、`opcua_servers.resubscribe_requested`、`modbus_scada.unit`、`devices.device_nickname` / `cost` | **全新部署必跑**，否則網頁後台會壞 |
| `009_opcua_server_publish_interval.sql` | `opcua_servers` 新增 `publish_interval_ms`，讓每台 Server 可以有自己的預設訂閱頻率 | 全新部署、想啟用逐 Server 頻率時 |
| `010_cumulative_counter_upload_condition.sql` | 把 63 個**累計型**感測器（累計功耗 / 蒸氣總量 / 流量計積算 / 運轉時數）的 `upload_condition` 從 `threshold_percent` 改成 `on_change`，並把剩餘 threshold 型的 `upload_threshold` 由 NULL 補成顯式的 `1` | **既有部署必跑**（不跑的話累計型點位會長期沒有新資料）；全新部署視感測器命名調整名單 |
| `010_rollback.sql` | 回滾 010（**不是** migration，只有確定要退回舊行為時才手動執行） | ❌ 不要放進批次執行 |
| `011_alarm_management.sql` | 🆕 v3 警報管理：`alarm_rules`（警報規則）、`alarm_events`（警報事件 / 歷史 / 確認），並授權給應用程式帳號 | v3 升級必跑 |
| `012_users_audit_status.sql` | 🆕 v3：`app_users`（多使用者與角色）、`audit_log`（操作稽核）、`service_status`（採集服務心跳），並授權給應用程式帳號 | v3 升級必跑 |
| `014_reading_quality.sql` | 🆕 v3.1：`sensor_readings` 新增 `quality`（正常 / 保持值 / 不確定 / 品質不良 / 通訊中斷）。只改 metadata，幾百萬筆也瞬間完成，已壓縮的 hypertable 同樣適用 | v3.1 升級必跑 |
| `015_modbus_transport.sql` | 🆕 v3.1：`modbus_scada` 新增 `transport`（tcp / rtu_over_tcp / rtu）、`serial_settings`、`enabled` | v3.1 升級必跑（沒用 Modbus 也建議跑） |
| `016_device_templates.sql` | 🆕 v3.2：設備範本（同型設備的感測器 / 點位 / 警報規則） | v3.2 升級必跑 |
| `017_calculated_points.sql` | 🆕 v3.2：計算點（用運算式從其他感測器算出新感測器） | v3.2 升級必跑 |
| `018_report_schedules.sql` | 🆕 v3.2：排程報表（每日 / 每週 / 每月自動寄送） | v3.2 升級必跑 |
| `019_s7_extensions.sql` | 🆕 v3.3：`tia_scada` 新增 `area`（DB/M/I/Q）、`bit_offset`、`rack`、`slot`、`enabled`、`unit` | v3.3 升級必跑（沒用 S7 也建議跑） |
| `020_calc_state.sql` | 🆕 v3.3：`calculated_points.calc_state`（累計函式的狀態，重啟後接續） | v3.3 升級必跑 |
| `021_calc_scripts.sql` | 🆕 v3.4：`calculated_points.kind`（expression / python）、`last_log`（Python 腳本計算點） | v3.4 升級必跑 |
| `022_unique_sensor_binding.sql` | 🆕 `opcua_tags` / `modbus_scada` / `tia_scada` 的 `sensor_id` 加上 UNIQUE（延遲到交易結束才檢查），資料庫層保證一個感測器只綁一個點位 | 建議跑；已有重複綁定時會失敗，先解除重複再跑 |
| `013_timeseries_policy.sql` | 🆕 **選用**：`sensor_readings` 啟用 TimescaleDB 壓縮（超過 30 天的 chunk 自動壓縮，不刪資料）。資料保留政策只寫在註解裡，需要時手動執行 | 確認後再跑，建議離峰時段 |

```bash
# ⚠️ 不要用 for f in sql/0*.sql —— 會把 010_rollback.sql 也一起跑掉（等於撤銷 010），
#    也會跑到選用的 013。請明確列出要執行的檔案：
for f in 000_realtime_tables 001_sensor_hierarchy_and_mapping 006_opcua_upgrade \
         007_opcua_deadband 008_missing_app_columns 009_opcua_server_publish_interval \
         010_cumulative_counter_upload_condition 011_alarm_management 012_users_audit_status \
         014_reading_quality 015_modbus_transport 016_device_templates \
         017_calculated_points 018_report_schedules 019_s7_extensions 020_calc_state 021_calc_scripts \
         022_unique_sensor_binding; do
  psql -h "$DB_HOST" -U <資料表擁有者> -d "$DB_NAME" -v ON_ERROR_STOP=1 \
       -v app_user="$DB_USER" -f "sql/$f.sql" || break
done
```

`011` / `012` 會自動把新表的 `SELECT / INSERT / UPDATE / DELETE` 與序列權限授權給
`-v app_user=<帳號>` 指定的應用程式帳號（省略時預設 `scada`，角色不存在則略過）。

## ⚠️ 編號斷層說明（002 ~ 005 不存在）

`002` ~ `005` **沒有對應的檔案**，不是遺失，是 v1 開發期間那幾版變更當時直接手動在資料庫上執行、沒有留下腳本。編號保留斷層是刻意的：`006` / `007` 這兩個號碼已經寫在程式碼的錯誤訊息（`data_layer/timeseries_writer.py`、`services/opcua_subscription_service.py`）與既有部署筆記裡，重新編號只會讓對照更亂。

因此**全新部署依序跑 `000` → `001` → `006` → `007` → `008` → `009` → `010` → `011` → `012` → `014` → `015` → `016` → `017` → `018` → `019` → `020` → `021` → `022`**，中間跳號直接忽略（`013` 選用，可在任何時候執行，與 014 先後無關）。

## 📌 為什麼會有 008

`008` 是 2026-09-01 對照正式資料庫做 schema 比對後補的。當時發現有一批欄位
「正式庫早就有、程式碼一直在用，但沒有任何 migration 建立它」——都是開發期間
直接在 DB 上手動 `ALTER TABLE` 出來的。結果就是照著文件用 `001` → `006` → `007`
裝一台全新機器，網頁後台會直接壞掉（感測器階層管理、Modbus 新增點位、
OPC UA「立即瀏覽」按鈕全部失效）。

**維持這個目錄可信度的規則：schema 只改在這裡，不要直接在資料庫上 `ALTER TABLE`。**
真的臨時手動改了，記得回頭補一支對應的 migration，否則下一台新機器就會再壞一次。

## 執行權限

migration 需要用**資料表擁有者**執行（目前是 `eric`）。應用程式帳號 `scada`
只有 `SELECT / INSERT / UPDATE`，沒有 `ALTER TABLE` 權限，拿它跑會失敗。

例外：`010` 只有 `UPDATE`、不改 schema，用 `scada` 也跑得動。

`011` / `012` 新增的表由擁有者建立，腳本最後會 `GRANT` 給應用程式帳號（含 `DELETE`：
警報規則與使用者可以在網頁上刪除）。

## 📌 為什麼會有 000

即時層四張表（`tia_scada` / `modbus_scada` / `opcua_servers` / `opcua_tags`）
原本是 v1 時期手動建的，一直沒有建表腳本 —— `001` 只能對「已存在的表」加
`sensor_id`，`008` 也只能補欄位，所以空資料庫沒辦法只靠 `sql/` 重建。
`000` 依 2026-09-01 正式庫的實際定義反推補上，補完之後這個目錄才是
**完整、可重建**的 schema 來源。

## 🧩 欄位的歸屬（同一個欄位只有一個定義來源）

`000` 刻意只建 v1 當時就有的欄位，後續 migration 加的欄位留在各自的檔案：

| 欄位 | 由誰建立 |
| --- | --- |
| 四張表的 `sensor_id`（含索引與 FK） | `001` |
| `modbus_scada.unit`、`opcua_servers.resubscribe_requested` | `008` |
| `sensors.nickname` / `state_dictionary` | `008` |
| `opcua_servers.publish_interval_ms` | `009` |
| `alarm_rules` / `alarm_events` | `011` |
| `app_users` / `audit_log` / `service_status` | `012` |
| `sensor_readings.quality` | `014` |
| `modbus_scada.transport` / `serial_settings` / `enabled` | `015` |
| `device_templates` | `016` |
| `calculated_points` | `017` |
| `report_schedules` | `018` |
| `tia_scada.area` / `bit_offset` / `rack` / `slot` / `enabled` / `unit` | `019` |
| `calculated_points.calc_state` | `020` |
| `calculated_points.kind` / `last_log` | `021` |

這樣同一個欄位不會有兩個定義來源，改的時候不用擔心兩邊不同步。
