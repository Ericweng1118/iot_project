# v2 → v3 升級說明

v3 把系統從「資料採集器 + 設定後台」補成完整的 SCADA：即時總覽、歷史趨勢、
警報管理與通知、報表匯出、多使用者權限、操作稽核、系統健康監控。
採集核心（OPC UA 訂閱、Modbus / TIA 輪詢、統一寫入排程）的架構不變。

---

## 1. 升級步驟

```bash
# 1) 資料庫：用資料表擁有者補跑兩支 migration（idempotent，可重複執行）
psql -h <DB_HOST> -U <擁有者> -d <DB_NAME> -v app_user=scada -f sql/011_alarm_management.sql
psql -h <DB_HOST> -U <擁有者> -d <DB_NAME> -v app_user=scada -f sql/012_users_audit_status.sql

# 2) .env：對照 env.example 補上需要的新設定（全部都有預設值，不補也能跑）
#    建議至少設定一種警報通知（ALARM_WEBHOOK_URLS / SMTP_* / TELEGRAM_*）

# 3) 重建映像檔（新增 openpyxl 套件）並重新部署
docker build -t unified_collector:<日期> .
docker stop -t 30 unified_collector && docker rm unified_collector
docker run -d --name unified_collector --env-file .env -p 1118:1118 \
           --stop-timeout 30 unified_collector:<日期>
```

升級後：

1. 用 `.env` 的 `ADMIN_USER` 登入 →「使用者管理」為每位使用者建立個人帳號
2.「警報規則 → 通知頻道」按「發送測試通知」確認通知設定
3.「系統狀態」確認採集服務運作中、migration 檢查全部 ✅（013 為選用）

> migration 沒跑也能啟動：警報引擎、心跳回報會自動暫停，網頁對應頁面顯示提示，採集不受影響。

---

## 2. 行為變化（請留意）

### 2.1 心跳補寫改用「目前時間」，數值不變的點位每小時會多一筆

v2 的心跳保底用「最後收到樣本的時間」判斷，OPC UA 是「有變化才推播」，數值長時間
不變的點位樣本時間永遠不前進 → **心跳從來不會觸發**，這些點位在 `sensor_readings` 裡
看起來像斷線，網頁「資料斷更」會誤報。

v3 改為：距上次寫入超過 `SENSOR_HEARTBEAT_INTERVAL`，且資料來源在 `SENSOR_ALIVE_WINDOW`
（預設 300 秒）內確認過還活著，就以**目前時間**補寫一筆。「確認存活」的來源：

- OPC UA：每次連線心跳成功（`OPCUA_HEARTBEAT_INTERVAL_SEC`，預設 15 秒）
- Modbus / TIA：每次輪詢讀取成功

**斷線時不補寫**，而且 OPC UA 一判定斷線就立即取消存活狀態，不會用舊值把斷線掩蓋掉。

影響：數值不變的點位每 `SENSOR_HEARTBEAT_INTERVAL` 秒會多一筆資料（預設每小時），
`always` 條件的點位在數值不變時也會每個寫入週期補一筆（這才是 `always` 原本的語意）。

### 2.2 品質不良（BAD）的 OPC UA 數值不再寫入歷史

設備回報 StatusCode 不是 Good 的值，只更新即時層 `opcua_tags`（`quality='BAD'`），
不寫進 `sensor_readings`、不參與警報判斷、不算「來源存活」。總覽頁會顯示「品質不良」。

### 2.3 感測器上下限會產生警報

`sensors.min_threshold` / `max_threshold` 會被警報引擎視為 L / H 規則（等級「中」）。
有設上下限的感測器，升級後超限會出現在警報中心，若有設定通知也會發送。
不想要這個行為：`.env` 設 `ALARM_USE_SENSOR_LIMITS=false`。

⚠️ 舊版「新增感測器」表單預設填入下限 0 / 上限 100，很多感測器的上下限只是表單預設值，並沒有警報意義
（正式庫實測：172 個感測器有上下限，累計電表 / 流量計的上限 100,000 升級後全部觸發警報）。
升級前請先檢查並清空不需要的上下限，或直接停用隱含規則。v3.4 起新增表單的上下限改為選填、預設空白。

### 2.4 網頁後台改為多頁導覽、需要角色權限

原本 5 個分頁改成左側選單（監控 / 設定 / 系統）。`.env` 的救援帳號是管理員，看得到全部頁面；
新建立的帳號依角色顯示。原本各點位設定頁的操作方式完全不變，只是多了稽核紀錄。

### 2.5 `run_all.py` 會自動重啟崩潰的子程序

原本 main.py 掛掉只印一行警告。現在會以 5 → 10 → 20 … 最多 300 秒的間隔自動重啟。
`docker stop` 送的 SIGTERM 也會正確轉給子程序收尾（請用 `-t 30`）。

### 2.6 MQTT 可以上傳 OPC UA 點位（預設關閉）

`MQTT_INCLUDE_OPCUA=true` 時，OPC UA 已綁定感測器（品質 GOOD）以 `sensor_code` 為 Key 一併上傳。
預設 `false`，既有下游收到的資料格式不變。

---

## 3. 新增的資料表

| 表 | Migration | 用途 |
| --- | --- | --- |
| `alarm_rules` | 011 | 警報規則 |
| `alarm_events` | 011 | 警報事件、確認紀錄、歷史 |
| `app_users` | 012 | 網頁使用者與角色 |
| `audit_log` | 012 | 操作稽核 |
| `service_status` | 012 | 採集服務心跳與執行統計 |

`013_timeseries_policy.sql`（選用）：`sensor_readings` 超過 30 天自動壓縮，不刪資料。

---

## 4. 回滾

程式碼回到 v2 即可，v3 新增的表 v2 不會讀取，留著不影響。若要完全移除：

```sql
DROP TABLE IF EXISTS alarm_events, alarm_rules, audit_log, app_users, service_status;
-- 013 的壓縮政策：
SELECT remove_compression_policy('sensor_readings', if_exists => true);
```

---

## 5. 驗證方式（v3 開發時的實測）

在獨立的測試資料庫（TimescaleDB 2.29、刻意設成 UTC 時區）＋ 本機 OPC UA 模擬器上驗證：

- 數值永遠不變的點位：每個心跳間隔以目前時間補寫一筆 ✅；模擬器停止後不再補寫 ✅
- 累計型計數器變化未達 1% 門檻：心跳照常補寫 ✅
- HH 規則 5 秒延遲觸發、遲滯恢復；EQ 狀態警報；上限隱含警報 ✅
- OPC UA Server 停止 → 已綁定點位 OFFLINE、通訊中斷警報（延遲後發出）→ 恢復後自動解除 ✅
- Webhook 通知發生 / 恢復 ✅
- SIGTERM 正常收尾、`service_status` 註記正常停止 ✅
- 網頁 13 個頁面無例外；登入失敗計數、角色權限阻擋、ACK、建立帳號、弱密碼阻擋 ✅
- `tests/` 單元測試 44 項通過

---

## 6. v3.1（2026-10-01）：資料可靠性、安全性、Modbus

### 6.1 升級步驟（v3 → v3.1）

```bash
psql -h <DB_HOST> -U <擁有者> -d <DB_NAME> -f sql/014_reading_quality.sql
psql -h <DB_HOST> -U <擁有者> -d <DB_NAME> -f sql/015_modbus_transport.sql
# 重建映像檔（新增 pyserial），部署時掛上本機緩存目錄：
docker run -d ... -v /opt/scada/data:/app/data --stop-timeout 30 unified_collector:<日期>
```

`.env` 新增的設定都有預設值，可選擇性加上：`SPOOL_*`、`MODBUS_*`、`ADMIN_SSL_*`、`ADMIN_IDLE_*`（見 `env.example`）。

### 6.2 行為變化

| 項目 | 變化 |
| --- | --- |
| 資料庫斷線 | 採集照常，`sensor_readings` 存本機緩存，恢復後補寫；main.py 啟動時連不上資料庫會重試而不是結束 |
| `sensor_readings` | 多一個 `quality` 欄位；斷線時多一筆 quality=4 的標記；OPC UA 品質不良從「完全不寫」改成「記錄轉為不良的那一筆」 |
| 趨勢圖 / 報表 | 品質 3 / 4 的標記不畫進線條、不算進統計，趨勢圖以 ▼ 標示 |
| 網頁 | 非檢視者角色閒置 30 分鐘自動登出；可設定 HTTPS；登出狀態打開深層連結，登入後會回到原本的頁面 |
| Modbus | 批次讀取、連線重用、依實體連線分組（同閘道多站號共用一條連線）；讀取失敗時保留最後數值；不再每輪強制寫入 `sensor_readings`（統一由寫入排程處理） |
| S7 | 同上：讀取失敗時通知寫入排程（通訊中斷標記）、不再每輪強制寫入、資料庫斷線時沿用上次的點位設定 |
| 新頁面 | 「工具 → Modbus 線上調適」（工程師以上） |

### 6.3 驗證方式（實測）

測試資料庫（含已壓縮的 chunk）＋ Modbus 模擬器（站號 1 稀疏位址、站號 2、不存在的站號 3）：

- `014` 在已壓縮的 hypertable 上執行成功、可重複執行，壓縮後仍可寫入帶品質的資料 ✅
- Modbus 7 個點位：第一輪整塊被拒絕（空隙有未定義暫存器）→ 自動逐點讀取 → 該組改為只合併連續位址；
  不存在的位址單獨隔離；站號 3 逾時不影響站號 2；之後每輪 5 次請求 ✅
- **資料庫停機 40 秒**：採集持續、25 筆存入本機緩存、恢復後 8 秒內補寫完成，`sensor_readings` 最大間隔 5.5 秒（= 採集週期）✅
- Modbus 設備斷線：寫入一筆 quality=4 標記、斷線期間不補寫、恢復後正常寫入；log 只在斷線與恢復各一行 ✅
- 線上調適：讀取、Modicon 位址換算、例外碼中文說明、站號掃描、點位驗證、建立點位、寫入＋讀回＋稽核 ✅
- HTTPS（自簽憑證、SAN 含區網 IP）、閒置登出、檢視者豁免、深層連結登入後保留 ✅
- 單元測試 94 項通過

---

## 7. v3.2（2026-10-01）：工程效率

### 7.1 升級步驟（v3.1 → v3.2）

```bash
for f in 016_device_templates 017_calculated_points 018_report_schedules; do
  psql -h <DB_HOST> -U <擁有者> -d <DB_NAME> -v app_user=scada -f sql/$f.sql
done
# 重建映像檔、重新部署（沒有新增套件）
```

沒跑 migration 時，對應頁面顯示提示，計算引擎與排程報表待命，其他功能不受影響。

### 7.2 新功能

| 功能 | 位置 | 說明 |
| --- | --- | --- |
| 批次匯入匯出 | 工具 → 批次匯入匯出 | 設備 / 感測器 / Modbus 點位 / OPC UA 綁定 / 警報規則；預覽後才套用，單一交易 |
| 設備範本 | 設定 → 設備範本 | 從現有設備建立範本，一次建立多台同型設備（感測器 + 點位 / 綁定 + 警報規則） |
| 計算點 | 設定 → 計算點 | 運算式虛擬感測器，main.py 每 5 秒計算，結果與實體感測器同樣處理 |
| 排程報表 | 報表 → 排程寄送 | 每日 / 每週 / 每月自動寄送上一期 Excel（Email / Webhook） |

### 7.3 行為變化

- 報表計算移到 `services/reporting.py`（網頁與排程共用），數字與 v3.1 相同
- 側邊欄頁面變多，導覽選單改為全部展開
- 計算點的結果感測器不能再綁定實體點位（綁定下拉選單會自動排除）；從設備建立範本時也不會帶入計算點的結果感測器

### 7.4 驗證方式（實測）

- 匯入：用匯入功能從零建立設備（自動建廠區 / 產線）→ 感測器 → Modbus 點位與綁定 → 警報規則 → OPC UA 綁定；
  每種資料匯出後原封不動匯回皆為「全部未變動」；Excel 修改只更新被改的欄位；跨表綁定衝突被擋下；
  預覽後資料庫被改過時拒絕套用 ✅
- 範本：從設備產生範本（點位名稱、CDAB、警報規則保留）→ 建立 3 台設備（9 感測器、3 點位、3 警報）；
  同時綁 Modbus 與 OPC UA 的設備拒絕產生範本 ✅
- 計算點：加總、引用其他計算點（依相依順序）、除以零、循環引用、輸入無資料，各自正確標記 ✅
- 排程報表：本機 SMTP 收到信件與 Excel 附件（每日增量合計 287.5 與種子資料吻合）、Webhook 收到摘要 ✅
- 網頁：所有頁面三種角色無例外；瀏覽器實測 CSV 上傳預覽 ✅；單元測試 129 項通過

---

## 8. v3.3（2026-10-02）：Siemens S7 與計算點

### 8.1 升級步驟（v3.2 → v3.3）

```bash
psql -h <DB_HOST> -U <擁有者> -d <DB_NAME> -f sql/019_s7_extensions.sql
psql -h <DB_HOST> -U <擁有者> -d <DB_NAME> -f sql/020_calc_state.sql
# 重建映像檔、重新部署（沒有新增套件）
```

### 8.2 行為變化

| 項目 | 變化 |
| --- | --- |
| S7 讀取失敗 | 保留最後數值、`plc_state` 改為 OFFLINE / ERROR（v2 會把 `current_data` 寫成 0） |
| S7 BOOL | 依 `bit_offset` 讀取；既有點位 bit_offset 預設 0，與 v2 相同 |
| S7 型態大小 | LREAL / LINT / STRING 等改用正確大小；v2 因讀取長度不足而 PARSE_ERROR 的點位會開始有值 |
| S7 採集 | 不再每輪強制寫入 `sensor_readings`（交給統一寫入排程，與 Modbus / OPC UA 一致） |
| 計算點 | 新函式不影響既有運算式；運算式內容改變時累計狀態自動重置 |

### 8.3 計算點與付費 SCADA 的比較

| 能力 | v3.3 | Ignition（Expression Tag） | WinCC / AVEVA（Script） |
| --- | --- | --- | --- |
| 四則 / 邏輯 / 條件 / 數學 | ✅ | ✅ | ✅ |
| 時間函式（時間電價、班別） | ✅ | ✅ | ✅ |
| 位元運算（拆狀態字） | ✅ | ✅ | ✅ |
| 品質處理（備援、斷線預設值） | ✅ isgood / valueor / coalesce | ✅ isGood / coalesce | ✅（自行寫） |
| 累計（積分、期間用量、運轉時數、啟動次數） | ✅ 內建、狀態持久化 | ◐ 需 Tag History 或腳本 | ◐ 需腳本 / 歷史庫 |
| 移動平均 / 濾波 / 變化率 | ✅ | ◐ 部分需腳本 | ◐ 需腳本 |
| 文字運算 / 字串輸出 | ❌（sensor_readings 只存數值） | ✅ | ✅ |
| 任意腳本（Python / VBS / C） | ❌ 刻意不提供（安全與穩定性） | ✅ runScript | ✅ |
| 計算結果寫回 PLC | ❌（屬於下一階段「寫入控制」） | ✅ | ✅ |

### 8.4 驗證方式（實測）

S7 模擬器（snap7 server）＋ 測試資料庫：

- 10 個點位（含 LREAL、DBX18.3、M 區、I 區、STRING[20]、遠處 offset）全部正確，4 次請求 / 2 ms ✅
- PLC 斷線：數值保留最後值、狀態 OFFLINE、中文錯誤訊息、計算點標記斷線並說明原因 ✅
- S7 線上調適：CPU 資訊、TIA 位址讀取、點位驗證、M30.5 寫入只改第 5 位元、建立點位 ✅
- 批次匯入匯出 S7：匯出後原封不動匯回 = 全部未變動 ✅
- 計算點：delta 以前一天最後一筆為基準算出今日用量、count / ontime / integral / coalesce / bit / 時間電價正確；
  重啟後 integral 與 count 接續累計（不歸零）✅
- 單元測試 178 項通過

---

## 9. v3.4（2026-10-02）：Python 腳本計算點

### 9.1 升級步驟

```bash
psql -h <DB_HOST> -U <擁有者> -d <DB_NAME> -f sql/021_calc_scripts.sql
# .env 加上（預設關閉）：
CALC_SCRIPTS_ENABLED=true
```

> 2026-10-02 已在正式庫（scada）以 eric 身分執行 011、012、014 ~ 021；執行前的備份在
> `.backup/scada_schema_before_v3_*.sql`、`.backup/scada_config_data_before_v3_*.sql`。

### 9.2 設計重點

- 腳本在獨立子程序（multiprocessing spawn）執行：逾時強制終止 + 退避、記憶體上限（RLIMIT_AS）、較低優先權
- 內建函式與 import 白名單；只有管理員能新增 / 修改，修改記入稽核；`CALC_SCRIPTS_ENABLED` 預設關閉
- 腳本與運算式共用相依排序（腳本裡寫死的 `value("X")` 會被解析成相依），可以互相引用
- 腳本引用的感測器斷線時標記「斷線」（與運算式一致），程式錯誤 / 逾時標記「錯誤」並附行號

### 9.3 驗證方式（實測）

- 腳本讀取 S7 即時值並輸出 log、運算式引用腳本結果、state 跨重啟接續 ✅
- `import os` 被擋、無窮迴圈 2 秒被終止且之後退避（25 秒內只重啟一次）、引用不存在的感測器標記斷線 ✅
- 管理員可新增 / 試算 / 修改；工程師只能看程式碼；開關關閉時管理員也只能看 ✅
- 單元測試 189 項通過（含子程序逾時、記憶體上限、崩潰隔離）

