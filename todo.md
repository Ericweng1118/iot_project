# 待辦事項與已知技術債

> 建立於 2026-09-01，來源是一次對照正式資料庫（`scada@192.168.20.106/scada`）
> 做的 schema 比對與程式碼稽核。已完成的項目留在下方「已完成」區備查。

---

## 🔴 待處理

### 0. v3 上線（2026-10-01 完成開發，尚未部署）

v3 的程式碼與 migration 只在**獨立測試資料庫**驗證過，正式庫與正式容器都還沒動。上線步驟見
[`UPGRADE_NOTES_v3.md`](UPGRADE_NOTES_v3.md)：

- [x] 正式庫執行 `sql/011`、`012`、`014` ~ `021`（2026-10-02 以 eric 身分執行完成，執行前備份在 `.backup/`）
- [ ] 部署時掛上本機緩存目錄 `-v /opt/scada/data:/app/data`（v3.1）
- [ ] 決定要不要啟用 HTTPS（`scripts/gen_self_signed_cert.sh 192.168.20.106`）
- [ ] `.env` 設定至少一種警報通知（Webhook 可以直接接現有的 n8n）
- [ ] 重建映像檔、`docker stop -t 30` 後重新部署
- [ ] 建立個人帳號，之後避免共用 `.env` 救援帳號
- [ ] 檢查有設 `min_threshold` / `max_threshold` 的感測器：升級後會自動產生 L / H 警報

### 1. `sensor_readings` 資料量成長 —— 壓縮與保留政策尚未啟用

> 🆕 2026-10-01：壓縮已寫成選用的 [`sql/013_timeseries_policy.sql`](sql/013_timeseries_policy.sql)
> （只壓縮、不刪資料，已在測試庫驗證可重複執行、壓縮後仍可查詢與寫入）。
> **保留年限仍待決定**，所以保留政策只寫在 013 的註解裡，沒有自動啟用。
> 網頁「系統狀態」會顯示目前大小與是否已啟用政策。

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

### 4. 綁定工作台套用到 Modbus / S7 頁面（2026-10-04 OPC UA 已改完並試用）

OPC UA 點位頁已改用「綁定工作台」（`web/binding_workbench.py` + `data_layer/bindings.py`），
Modbus 與 TIA (S7) 頁面還是舊的 `st.data_editor` + `SelectboxColumn` 逐列下拉，仍有舊問題：
選單會列出畫面上其他列已綁定的感測器、同一次儲存裡兩列選同一個感測器時檢查不到、
兩個點位互換感測器會被誤判成衝突、每次儲存都重寫所有列。

- [ ] `config_modbus.py`：表格的 `sensor_label` 欄改唯讀，下方接 `render_binding_workbench(PointSpec(table="modbus_scada", …))`
- [ ] `config_tia.py`：同上（`table="tia_scada"`）
- [ ] 兩頁的「新增點位」表單與 `modbus_debug.py` / `s7_debug.py` 的「一鍵建立點位」改用 `bindings.bind()` 寫入綁定
- [ ] 改完後 `web/common.py` 的 `_sensor_select_options` / `_binding_filter_caption` / `_find_binding_conflict` 若已無人使用就移除
- [ ] 正式庫執行 `sql/022_unique_sensor_binding.sql`（2026-10-04 唯讀檢查過正式庫沒有重複綁定，可以直接跑）

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

## 🧭 下一階段建議（v3 之後）

> 2026-10-01：「A. 可靠性」階段已完成（v3.1：本機緩存、歷史品質、HTTPS、閒置登出），
> 另外 Modbus 採集改寫並新增線上調適工具。以下為尚未開始的項目，依對現場的價值排序。
> 還沒做的可靠性項目：採集程式啟動時資料庫就連不上的情況，因為點位設定都在資料庫裡，
> 只能等資料庫恢復才開始採集（執行中斷線則不受影響）；警報事件在資料庫斷線期間不會被記錄。

> 2026-10-01：「B. 工程效率」已完成（v3.2）。下一步是「C. 控制與畫面」。

1. **寫入控制（Setpoint / 開關命令）**：目前是純監視系統。下達命令到 PLC 有安全風險，需要先定義
   哪些點位可寫、上下限、二次確認、權限（建議只開放給 engineer 並寫入 audit_log）再實作
2. **連續聚合（continuous aggregate）**：`sensor_readings` 建 1 小時 / 1 天的 continuous aggregate，
   趨勢與報表查長區間（一年以上）時會快很多。目前資料量下 `time_bucket` 即時聚合仍夠快
3. **HMI 畫面**：依製程圖（P&ID）擺放即時值的圖形化畫面，目前總覽是卡片 / 表格
4. **OPC UA 安全連線**：`security_policy` / 憑證管理目前只支援 None + 帳密
5. **警報抑制（shelving）**：暫時擱置特定警報（設備維修期間），目前只能停用規則
6. **opcua_tags 舊資料清理**：Server 端移除的點位目前只停止訂閱、資料列不會刪除（已知限制）

---

## ✅ 已完成（2026-10-04）

- OPC UA 點位頁改用「綁定工作台」：未綁定點位 × 未綁定感測器配對、已綁定可解除 / 改綁 / 互換，
  每個動作一個交易（advisory lock + 樂觀鎖），不會再重複綁定（`web/binding_workbench.py`、`data_layer/bindings.py`）
- `sql/022`：`opcua_tags` / `modbus_scada` / `tia_scada` 的 `sensor_id` 加 UNIQUE（DEFERRABLE），尚未在正式庫執行
- 新增感測器的 `sensor_code` 改為自動流水號（`data_layer/sensor_codes.py`）：階層管理表單不再手動輸入、
  計算點留空自動編號、批次匯入 `sensor_code` 留空 = 自動編號
- 批次匯入匯出新增 Excel 匯入範本（`data_layer/import_templates.py`）：必填欄位標色、標題註解、
  下拉選單（設備、感測器「編號｜設備｜暱稱」、各種選項）；OPC UA 綁定範本預先列出未綁定點位

## ✅ 已完成（2026-10-02，v3.4）

- **Python 腳本計算點**（`sql/021`）：獨立子程序、逾時 / 退避、記憶體上限、白名單、管理員限定、`CALC_SCRIPTS_ENABLED` 開關
- **正式庫 migration**：011、012、014 ~ 021 已套用（備份在 `.backup/`）

---

## ✅ 已完成（2026-10-02，v3.3）

- **S7 採集改寫**：DB/M/I/Q、BOOL 位元、正確型態大小、Rack/Slot、區塊切分、連線重用、失敗保留最後值、中文錯誤說明（`sql/019`）
- **S7 線上調適**頁面；TIA 點位頁可貼 TIA 位址、刪除、採集統計；批次匯入匯出支援 S7
- **計算點函式擴充**：品質、時間、位元、統計、累計（integral / delta / ontime / count）、動態（movavg / filter…），
  累計狀態持久化（`sql/020`）
- **已知限制**：計算點只輸出數值；計算結果寫回 PLC 屬於下一階段（寫入控制）。（任意腳本已於 v3.4 以 Python 腳本計算點提供）

---

## ✅ 已完成（2026-10-01，v3.2）

- **批次匯入匯出**：設備 / 感測器 / Modbus / OPC UA 綁定 / 警報規則，預覽（逐列錯誤、欄位差異）後單一交易套用
- **設備範本**（`sql/016`）：從現有設備建立、批次產生設備清單、Modbus 位址平移、OPC UA 節點樣式、JSON 匯出匯入
- **計算點**（`sql/017`）：安全運算式（AST 白名單）、相依順序、循環偵測、輸入斷線時暫停計算、試算
- **排程報表**（`sql/018`）：每日 / 每週 / 每月，Email 附 Excel、Webhook；錯過只補最近一期、失敗重試
- 報表計算移到 `services/reporting.py`，網頁與排程共用

---

## ✅ 已完成（2026-10-01，v3.1）

- **本機緩存（Store-and-Forward）**：`data_layer/spool.py`，資料庫寫入失敗存本機 SQLite、恢復後補寫；
  Modbus / S7 採集在資料庫斷線期間沿用上次的點位設定；main.py 啟動時連不上資料庫改為重試
- **歷史資料品質**：`sql/014`，正常 / 保持值 / 不確定 / 品質不良 / 通訊中斷；趨勢圖斷線處斷開並標示，報表排除
- **HTTPS**（`ADMIN_SSL_CERT` / `ADMIN_SSL_KEY`、`scripts/gen_self_signed_cert.sh`）與**閒置自動登出**
- **Modbus 採集改寫**：批次讀取、連線重用、依實體連線分組、RTU over TCP / RTU 序列埠（`sql/015`）、
  自動隔離壞點位、站號逾時不互相影響、log 去重；設定頁新增刪除、停用、採集統計
- **Modbus 線上調適**頁面（讀取 / 監看 / 四種順序對照 / 站號掃描 / 點位驗證 / 寫入測試 / 建立點位）
- **修正**：總覽頁在 OPC UA 停用時 SQL 錯誤（UNION 欄位名稱）；深層連結登入後被導回首頁；
  系統狀態頁在資料庫恢復後仍顯示過時錯誤；資料庫斷線期間 log 每幾秒洗版

---

## ✅ 已完成（2026-10-01，v3）

- **修正心跳保底對數值不變的 OPC UA 點位無效**：v2 用最後收到樣本的時間判斷心跳，OPC UA
  數值不變不推播 → 心跳永遠不觸發，「資料斷更」誤報。改為目前時間＋來源存活判定，
  斷線時不補寫（`SensorReadingWriter._plan_write()`，有單元測試）
- **品質不良的 OPC UA 數值不寫入歷史**、不參與警報
- **`run_all.py`**：處理 SIGTERM（`docker stop` 時 main.py 能做最後一次寫入）、子程序崩潰自動重啟
- **DB 連線池**：丟棄失效連線（DB 重啟後自動恢復）、連線逾時、TCP keepalive
- **文件錯誤**：README / sql/README 的 `for f in sql/0*.sql` 會把 `010_rollback.sql` 一起執行
  （等於撤銷 010），改成明確列出檔名
- **診斷查詢效能**：「從未寫入」查詢原本 LEFT JOIN 整張 hypertable，改用 NOT EXISTS
- **MQTT 可上傳 OPC UA 點位**（`MQTT_INCLUDE_OPCUA`，預設關閉）
- **新功能**：即時總覽、歷史趨勢、警報引擎 / 規則 / 通知 / 確認 / 歷史、報表匯出、多使用者與角色、
  操作稽核、系統狀態頁（`sql/011`、`sql/012`）
- **`admin_app.py` 拆分**：2266 行單檔拆成 `web/pages/` 一頁一模組；既有設定頁邏輯原封不動搬移
- **單元測試**：`tests/` 44 項（寫入判斷、警報規則、密碼雜湊、報表計算）

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
