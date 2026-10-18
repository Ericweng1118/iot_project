# PME（能源管理系統）介面約定

> 本文件是 **SCADA（iot_project）這一側**的約定：PME 可以依賴哪些表與欄位、這些資料的語意、SCADA 對 PME 的承諾。
> PME 這一側（它怎麼使用這些資料、PME 的承諾）見 PME repo 的 `docs/PME_BRIEF_FOR_SCADA.md`（`~/桌面/code/PME`）。兩份文件內容不重複，改介面時兩份一起更新。

## 1. 關係與邊界

- 同一台主機（192.168.20.106）、同一個 `scada` 資料庫。
- **PME 只寫 `ems` schema**（啟動時自動 migration）；`public` 只讀，唯一例外：
  - `audit_log`：PME 寫入，`action` 一律以 `ems.` 為前綴（含前綴不超過 50 字元）。
  - `app_users.last_login`：登入時更新。
- SCADA 不動 `ems`、`energy_ext`、`erp_ext`。

## 2. PME 依賴的 public 表

> 欄位以 `sql/` 的 migration 為準；改欄位名稱、型別前依第 5 節先通知 PME。

### `sensors`
| 欄位 | 說明 |
| --- | --- |
| `sensor_id` | 主鍵。**PME 一律用它當 key**，不要用 `sensor_code` |
| `sensor_code` | 唯一代碼（新增點位時自動編號） |
| `nickname` | 顯示名稱，可為 NULL |
| `sensor_type`、`unit` | 類型、單位 |
| `min_threshold`、`max_threshold` | 警報用上下限 |
| `upload_condition` | `always` / `on_change` / `threshold_percent` / `threshold_absolute`（累計型點位為 `on_change`） |
| `upload_threshold` | 門檻值 |
| `device_id` | 所屬設備 |

廠區 / 產線 / 設備要 JOIN：`sensors.device_id → devices(device_code, device_name, device_nickname, line_id) → production_lines(line_name, site_id) → sites(site_name)`。

### `sensor_readings`（TimescaleDB hypertable）
| 欄位 | 說明 |
| --- | --- |
| `sensor_id`、`reading_time` | 主鍵 `(sensor_id, reading_time)`；寫入用 `ON CONFLICT DO NOTHING` |
| `value` | NUMERIC NOT NULL |
| `quality` | SMALLINT，可為 NULL，定義如下 |
| `reading_id` | BIGSERIAL，不保證與時間順序一致，不要拿來當游標 |

**quality 代碼**（`data_layer/quality.py`）

| 值 | 名稱 | 說明 |
| --- | --- | --- |
| 0 | GOOD | 正常量測 |
| 1 | HELD | 保持值：數值沒變、來源存活，由心跳以目前時間補寫 |
| 2 | UNCERTAIN | 設備回報 Uncertain |
| 3 | BAD | 設備回報 Bad（只記錄轉為不良的那一筆） |
| 4 | COMM_LOST | 通訊中斷標記（`value` 沿用最後一筆） |
| NULL | — | 舊資料（v3.1 之前、2026-10-16 Influx 回補的資料），**視為 GOOD** |

3、4 是「標記」不是量測值，統計時必須排除。有效資料的條件固定寫成：

```sql
(quality IS NULL OR quality < 3)
```

寫成 `quality < 3` 會連 NULL 一起濾掉。

**`reading_time` 的語意**
- 是採集端的牆上時鐘，不是設備時間戳。
- 每 `SENSOR_READING_FLUSH_INTERVAL`（預設 60 秒）批次寫入一次，所以平常就會晚 0–60 多秒才寫進來。
- 資料庫斷線期間資料先存本機 spool（SQLite），恢復後才以**原本的 `reading_time`** 補寫，可能晚好幾小時到好幾天 → 比 PME 游標更早的資料晚到，見第 4 節。
- 計算點（`calculated_points.sensor_id`）的結果也寫在這張表，跟實體點位沒有差別；能源加總時要自行排除，避免重複計算。
- 超過 30 天的 chunk 會壓縮（`sql/013`）；**保留（retention）政策尚未啟用**。

### `alarm_events`
`event_id`、`alarm_key`、`source_type`（`sensor_rule` / `sensor_limit` / `server_offline` / `device_offline`）、`sensor_id`（可為 NULL）、`server_id`、`rule_id`、`alarm_type`、`priority`（1 緊急 / 2 高 / 3 中 / 4 低）、`message`、`trigger_value`、`setpoint`、`raised_at`、`cleared_at`、`clear_value`、`acked_at`、`acked_by`、`ack_comment`。
「目前警報」= `cleared_at IS NULL OR acked_at IS NULL`。

### `app_users`
`username`（PK）、`display_name`、`password_hash`、`role`（`viewer` / `operator` / `engineer` / `admin`）、`enabled`、`created_at`、`last_login`。
- `password_hash` 格式 `pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>`（260,000 次，見 `core/auth.py`）。
- `.env` 的 `ADMIN_USER` 救援帳號**不在這張表裡**。

### `audit_log`
`id`、`ts`、`username`（NOT NULL）、`action`（VARCHAR(50)）、`target`（VARCHAR(200)）、`detail`（JSONB）。

## 3. SCADA 發出的事件：`spool.drained`

本機 spool 補寫完成（緩存清空）時，SCADA 寫一筆 `audit_log`。一次斷線即使跨多個寫入週期才補完，也只寫一筆。

| 欄位 | 值 |
| --- | --- |
| `username` | `system` |
| `action` | `spool.drained` |
| `target` | `sensor_readings` |
| `detail.rows` | 這次補寫的筆數（含 COMM_LOST 標記） |
| `detail.sensor_ids` | 受影響的 sensor_id（排序後的陣列） |
| `detail.from` / `detail.to` | 補寫資料的最早 / 最晚 `reading_time`（ISO 8601，含時區） |
| `detail.dropped` | 緩存超過上限（`SPOOL_MAX_ROWS`）而丟棄的筆數，採集程式啟動後累計；> 0 代表有資料永久遺失 |

PME 偵測到這筆事件，就對 `sensor_ids` 的 `from` ~ `to` 重算 `ems`。

限制：補寫摘要只存在記憶體，補寫途中採集程式重啟的話，事件的範圍只涵蓋重啟後補寫的部分；這種情況發生時 SCADA 會另外主動通知 PME（見第 5 節規則 3）。

## 4. 增量讀取建議

- 累計型點位用 `reading_time > cursor` 增量讀即可，晚到的資料只會讓用量分攤到不同時段，不會少算。
- 瞬時型點位若從游標所在區間重新彙總，同樣只有 spool 補寫會漏，靠 `spool.drained` 重算，不需要盲目重掃。
- 對 `sensor_readings` 的大範圍查詢請設 `statement_timeout`（NUC 資源有限，DB 與採集程式共用）。

## 5. 雙方約定

1. **事先通知對方**：改共用表的欄位名稱或型別、`quality` 定義、重編 `sensor_id`、sensor 換綁設備（廠區 / 產線歸屬改變）。SCADA 以後新增 PME 需要讀的 public 表時，對 pme 帳號的 GRANT 寫在 SCADA 自己的 migration 裡。
2. 累計型點位維持 `on_change` 上傳。
3. **需要 PME 重算 `ems` 時**，SCADA 提供 sensor_id 清單與時間範圍，情況包括：回補 `sensor_readings`、刪除 `sensor_readings` 的資料、spool 補寫（用第 3 節的事件）、更改 sensor 的 `unit` 或倍率。
4. SCADA 不動 `ems`；PME 不動 `public`（上述兩個例外除外）與 iot_project 的檔案。重建 timescaledb 容器前先通知。
5. 各自照自己 repo 的分支慣例 commit（PME：`main`；iot_project：`v2-dev`），push 一律由 Eric 做。
6. 訊息格式：目的 / 影響的 sensor_id 或表 / 時間範圍 / 需要對方做什麼 / 完成條件。
7. 啟用 retention、部署 v3、重建容器之前，提前通知並附預計停機時間。

## 6. 規劃中、會影響 PME 的事

- `sensor_readings` 保留年限尚未決定；啟用前會先通知（刪除的資料無法復原）。
- v3 尚未部署到正式容器；部署後寫入程式會換，`quality` 才會開始有值（v2 寫的都是 NULL）。
- PME 專用 DB 帳號（`pme`）的權限調整：討論中，定案後補充於此。
