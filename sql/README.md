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

```bash
for f in sql/0*.sql; do
  psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -f "$f" || break
done
```

## ⚠️ 編號斷層說明（002 ~ 005 不存在）

`002` ~ `005` **沒有對應的檔案**，不是遺失，是 v1 開發期間那幾版變更當時直接手動在資料庫上執行、沒有留下腳本。編號保留斷層是刻意的：`006` / `007` 這兩個號碼已經寫在程式碼的錯誤訊息（`data_layer/timeseries_writer.py`、`services/opcua_subscription_service.py`）與既有部署筆記裡，重新編號只會讓對照更亂。

因此**全新部署依序跑 `000` → `001` → `006` → `007` → `008` → `009` → `010`**，中間跳號直接忽略。

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

這樣同一個欄位不會有兩個定義來源，改的時候不用擔心兩邊不同步。
