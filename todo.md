# 待辦事項與已知技術債

> 建立於 2026-09-01，來源是一次對照正式資料庫（`scada@192.168.20.106/scada`）
> 做的 schema 比對與程式碼稽核。已完成的項目留在下方「已完成」區備查。

---

## 🔴 待處理

### 1. `sensor_readings` 資料量成長 —— 壓縮與保留政策尚未啟用

**現況（2026-09-01 實測）**

| 項目 | 數值 |
| --- | --- |
| 總筆數 | 4,420,965 |
| Chunk 數 | 5 |
| 時間範圍 | 2026-08-05 ～ 2026-09-01（約 27 天） |
| 壓縮政策 | ❌ 未啟用 |
| 保留政策 | ❌ 未啟用 |

不到一個月累積 442 萬筆，換算約 **16 萬筆/天**。目前查詢還很快，但沒有任何
自動清理機制，磁碟會單調成長。

**建議做法**：確認要保留多久之後，寫成 `sql/010_timeseries_policy.sql`，
內容大致如下（**先在測試環境驗證再上正式庫**）：

```sql
ALTER TABLE sensor_readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'sensor_id'
);
SELECT add_compression_policy('sensor_readings', INTERVAL '30 days');
SELECT add_retention_policy('sensor_readings', INTERVAL '1 year');
```

**決策前要先確認的事**：

- 歷史資料要保留多久？（保留政策會**永久刪除**超期資料，不可逆）
- 壓縮後的 chunk 無法直接 `UPDATE` / `DELETE`，若有回頭修資料的需求要先評估
- `compress_segmentby = 'sensor_id'` 適合「依單一感測器查時間區間」的查詢樣式，
  若主要查詢是跨感測器聚合，segmentby 要重新選

---

## 🟡 觀察中

### 2. ~~`sensors.upload_threshold` 全部是 NULL~~ → 已由 `sql/010` 處理

**2026-09-11 追記：這個「觀察中」的項目其實已經在正式庫造成事故了。**

當時的判斷是「`threshold_percent` + threshold 為 None 會 fallback 到 1%，
目前不影響運作」。實際上對**累計型計數器**是致命的：累計電表的基準值可達百萬
等級、日增量只有幾百，變化 1% 要累積四十幾天才會寫進一筆。

排查起點是 sensor 126（`B06累計功耗`）從 2026-09-03 16:50 之後就沒有新資料，
但 OPC UA 連線正常、訂閱正常、`opcua_tags` 的即時值也一直在跳：

| | |
| --- | --- |
| 上次寫入 | `1,137,169`（2026-09-03 16:50） |
| 8 天後的即時值 | `1,139,354`（+2,185，**0.1921%**） |
| 觸發 1% 需要 | +11,371.69，約還要 41 天 |

全庫掃描後發現 126 個 `threshold_percent` 感測器裡有 **47 個**超過一天沒有新值，
**每一個**的變化量都低於 1%，沒有例外。停止寫入的時間點與 v2 改版部署
（2026-09-03 16:56）吻合。

**已處理**：`sql/010_cumulative_counter_upload_condition.sql`
把 63 個累計型感測器改成 `on_change`，並把剩餘 threshold 型的門檻由 NULL
補成顯式的 `1`（行為不變，只是把設定寫進資料）。

---

### 3. `devices.device_nickname` / `devices.cost` 沒有任何程式碼在讀

兩個欄位存在於正式庫，但 `grep` 整個專案找不到任何使用點。
`sql/008` 為了讓腳本能重建現況而保留了它們。

若確認沒有外部報表 / BI 工具在讀，可以另開一支 migration 移除；
若有外部使用者，建議在 `README_DB.md` 註明是誰在用。

---

## ✅ 已完成（2026-09-11）

- **補回統一寫入排程的心跳保底**：`SENSOR_HEARTBEAT_INTERVAL`（.env 早就有、
  預設 3600 秒）在 v2 改版時被漏掉，沒有任何程式碼在讀它。v1 本來就有「值沒變
  也每小時補寫一筆」的保護，少了它，門檻設錯的點位會**完全沒有資料、也不會有
  任何錯誤 log**，只能靠人工發現 —— 上面的待辦 #2 就是這樣才拖了 8 天才被發現。
  現在 `_should_write_with()` 會在超過心跳間隔時無條件補寫，讓「沒有新資料」
  跟「系統掛了」在資料上可以區分開來。
- **`sql/010`**：63 個累計型感測器改用 `on_change`（詳見待辦 #2）
- **修正寫入資料庫的時間戳少 8 小時**：`batch_updater.py`、`run_s7_collector.py`、
  `run_modbus_collector.py` 用 naive 的 `datetime.now()` 寫進 `timestamptz` 欄位。
  容器沒有設 `TZ` 所以跑在 UTC，Postgres 卻照 session 的 `+08` 解讀，結果
  `opcua_tags.last_update` 全部顯示慢 8 小時 —— 排查時看起來像「所有 OPC UA
  Server 都在 8 小時前就停止更新」，其實是活的，差點誤導方向。S7 / Modbus 更嚴重：
  它們把同一個 naive 時間當作 `reading_time` 傳進 `sensor_reading_writer.stage()`，
  等於歷史資料的時間軸整個偏移。四處全部改成 `datetime.now().astimezone()`。

---

## ✅ 已完成（2026-09-01）

- **修正 `sql/006`**：CHECK constraint 補上 `threshold_percent` / `threshold_absolute`
  （原本只允許 `always` / `on_change` / `threshold`），並加上遺留值轉換與 backfill
- **補寫 `sql/007`**：deadband 欄位的 migration 原本從未被寫出來，只有程式碼的
  錯誤訊息在提到它
- **新增 `sql/008`**：補齊 `sensors.nickname` / `state_dictionary`、
  `opcua_servers.resubscribe_requested`、`modbus_scada.unit` 等
  「程式碼在用但沒有腳本建立」的欄位
- **新增 `sql/009` + 程式碼**：把 `opcua_servers.publish_interval_ms` 從
  「文件有寫但實際不存在」做成真功能（migration + `load_opcua_servers()` 加撈
  + 網頁 Server 清單可編輯），訂閱頻率變成三層優先序
- **`006` ~ `009` 全部在 TEMP 資料表上驗證過**：可重複執行，且產出的 schema
  與正式庫逐欄位比對一致
- **新增 `sql/000`**：即時層四張表原本完全沒有建表腳本，依正式庫實際定義補上
  （含 UNIQUE / FK `ON DELETE CASCADE` / 索引）。`sql/` 目錄現在是完整可重建的
  schema 來源，空資料庫可以只靠 `000` → `001` → `006` → `007` → `008` → `009` 重建
- **文件與現實對齊**：`README_DB.md` 的欄位定義依實際 schema 校正、
  `env.example` 與程式碼實際讀取的 key 交叉比對一致、`sql/README.md`
  補上編號斷層與執行權限說明
