# IIoT SCADA 詳細手冊

> 精簡介紹見 [README.md](../README.md)；各版本的升級步驟與行為變化見 [UPGRADE_NOTES_v3.md](../UPGRADE_NOTES_v3.md)。


## 🚀 核心功能

### 1. 多協議支援（可個別開關）

- **OPC UA（主力協議）** — 透過 `asyncua`，採用訂閱推播（Subscription）而非輪詢瀏覽，
  支援逐感測器自訂訂閱頻率與伺服器端 deadband 過濾，詳見下方「OPC UA 訂閱服務」
- **Modbus** — 透過 `pymodbus` 支援 FC01~04 讀取、多位元組資料型態與 Byte/Word Order。
  🆕 v3.1 支援 **Modbus TCP / RTU over TCP / RTU 序列埠** 三種傳輸方式、批次讀取與連線重用（見第 10 節）
- **S7 Protocol (Siemens)** — 透過 `snap7` 連線 S7-1200/1500 PLC，DB 區塊打包讀取

🆕 **Modbus / TIA(S7) 可透過 `.env` 完全屏蔽**：`MODBUS_ENABLED=false` /
`TIA_ENABLED=false` 時，`main.py` 不會啟動該協議的採集執行緒，`admin_app.py`
網頁後台也會完全隱藏對應分頁（不是灰掉，是分頁不存在）。預設值皆為 `true`，
相容既有部署；要屏蔽的話在 `.env` 設為 `false` 即可，不需要改程式碼。

### 2. 併發採集架構

- Modbus、TIA(S7)（若都啟用）在每一輪採集中**併發執行**（`ThreadPoolExecutor`），
  其中一個協議整體卡住不會拖到另一個協議完全沒開始；兩者都停用時主迴圈直接跳過這一步
- 同一協議內，多台 PLC/設備也是併發連線，一台離線只影響自己那條執行緒
- PostgreSQL 連線池使用 `ThreadedConnectionPool`（執行緒安全）

### 3. OPC UA 訂閱服務（Report by Exception + 逐感測器頻率 + 伺服器端過濾）

OPC UA 常駐一個獨立的背景服務，跟主迴圈完全脫鉤：

- **只在必要時才做結構性瀏覽（browse）**：第一次啟動、或使用者於網頁手動觸發重新整理
- **只訂閱已綁定感測器的點位** 🆕：瀏覽發現的所有點位都會存進 `opcua_tags`（供網頁挑選），
  但只有設定了 `sensor_id` 的點位才會實際建立 OPC UA 訂閱，未綁定的點位不佔訂閱資源、
  不消耗頻寬。綁定/解除綁定後最慢 5 秒內自動生效
- **可調訂閱頻率（三層優先序）** 🆕：`sensors.opcua_sampling_interval_ms`（逐感測器）
  > `opcua_servers.publish_interval_ms`（逐 Server）> `.env` 的 `OPCUA_PUBLISH_INTERVAL_MS`
  （全域）。相同頻率的點位會被自動歸進同一組 Subscription（OPC UA 的
  publishing interval 是 Subscription 層級屬性），不同頻率各自獨立 Subscription。
  ⚠️ 部分設備（尤其嵌入式協議轉換盒）的 OPC UA Server 有自己固定的內部更新週期，
  會忽略/覆寫用戶端請求的頻率（連線 log 出現 `Revised values returned differ from
  subscription values` 就是這種情況），此時這個設定對該設備不生效
- **伺服器端 Deadband 過濾** 🆕：`sensors.opcua_deadband_type`（none/percent/absolute）
  + `opcua_deadband_value`，透過 asyncua 的 `Subscription.deadband_monitor()`
  在**伺服器端**就決定要不要把這筆變化透過網路送過來，直接減少不穩定連線上的流量
  （跟 `upload_condition` 是不同層級：deadband 決定「要不要送過來」，
  `upload_condition` 決定「收到之後要不要寫進資料庫」，兩者可疊加使用）。
  `percent` 依賴節點是否設定 EURange，不確定設備有沒有配置時建議用 `absolute`
- 平常靠 Server 端的 Subscription 推播：有變化才通知，本服務收到後先寫進記憶體緩衝區，
  每 2 秒批次更新 `opcua_tags` 即時值，並把已綁定感測器的最新值丟進
  `SensorReadingWriter`（見下方「統一週期性寫入」）
- **統一維護迴圈** 🆕：原本「檢查 resubscribe 旗標」與「定期刷新 sensor_id 綁定」兩個
  背景 task，合併成單一 `_watch_and_maintain()`，每 5 秒檢查一次點位表 / 感測器綁定 /
  取樣頻率 / deadband 設定是否有變化，有變化就整批重建訂閱（犧牲一點效率換取分組邏輯
  單純、不容易在邊界情況留下不一致的訂閱殘留）
- **連線穩定性可調參數** 🆕：`OPCUA_CLIENT_TIMEOUT`（單次請求逾時）、
  `OPCUA_HEARTBEAT_INTERVAL_SEC`（心跳週期）、`OPCUA_HEARTBEAT_TIMEOUT_SEC`
  （單次心跳逾時）、`OPCUA_HEARTBEAT_MAX_FAILURES`（連續失敗幾次才判定斷線），
  網路品質不穩的場域可以調高，容忍偶發逾時，不要動不動就整組重建連線/訂閱；
  同時內建重複訊息抑制，避免 asyncua 斷線時每秒狂噴同一則訊息洗版 log
- 心跳目標優先用已訂閱成功的實際點位（不用系統節點 `i=2259`，因為並非所有
  PLC 內建簡化版 OPC UA Server 都完整實作該子節點）
- 斷線後自動指數退避重連（`RECONNECT_BASE_DELAY` → `RECONNECT_MAX_DELAY`），
  `BadTooManySessions` 則用固定較長的 `SESSION_LIMIT_RETRY_DELAY` 重試

### 4. 感測器階層管理與時序資料

- 雙層資料架構：**即時層**（`modbus_scada` / `tia_scada` / `opcua_tags`，存目前值與連線狀態）
  與 **時序層**（`sensor_readings` hypertable，存歷史數值），透過 `sensor_id` 連結
- 階層架構：廠區 (`sites`) → 產線 (`production_lines`) → 設備 (`devices`) → 感測器 (`sensors`) → 讀值 (`sensor_readings`)
- 網頁「感測器階層管理」分頁可直接維護這整條階層，包含 OPC UA 訂閱頻率、deadband、
  上傳條件等進階設定
- Modbus / TIA / OPC UA 三個點位設定分頁（視 `.env` 開關而定是否顯示）都有「綁定感測器」
  下拉選單，並內建跨協議重複綁定偵測

🆕 **`sensor_readings` 統一週期性寫入**（由 `data_layer/timeseries_writer.py` 的
`SensorReadingWriter` 負責）：

原本各協議在自己那一輪採集結束時各自判斷寫入，現在拆成兩層：

1. **最新值快取**（高頻、純記憶體）：採集端（OPC UA 訂閱服務為主，Modbus/TIA 若啟用
   也共用）每次讀到新值就呼叫 `update_latest()`，只更新記憶體、不碰資料庫
2. **統一寫入排程**（低頻、固定週期，由 `.env` 的 `SENSOR_READING_FLUSH_INTERVAL`
   秒控制）：背景執行緒固定週期醒來，把所有感測器的最新值一次性依各自的
   `sensors.upload_condition` 判斷後批次寫入 `sensor_readings`，四選一：
   - `always`：不判斷，每一輪都寫
   - `on_change`：只有數值與上次「實際寫入」的值不同才寫
   - `threshold_percent`（**新建感測器的預設值**）：變化百分比達到 `upload_threshold`
     才寫，以上次實際寫入的值為基準（`upload_threshold` 填百分比數字，例如 `1` = 1%）；
     上次寫入值為 0 時無法算百分比，退化為「新值不再是 0 就寫」
   - `threshold_absolute`：變化絕對值達到 `upload_threshold` 才寫（適合各感測器量級
     差異大、百分比意義不明確的情境）
3. **心跳保底**（由 `.env` 的 `SENSOR_HEARTBEAT_INTERVAL` 秒控制，預設 3600）：
   不論上面判斷結果如何，只要距離上次實際寫入超過這個時間就強制補寫一筆。
   🆕 v3：OPC UA「數值不變就不推播」，v2 的心跳拿「最後收到樣本的時間」來算，
   對長時間不變的點位永遠不會觸發。v3 改用目前時間判斷，並在資料來源確認存活時
   （OPC UA 連線心跳成功 / Modbus、TIA 輪詢成功，`SENSOR_ALIVE_WINDOW` 秒內）
   以目前時間補寫；**斷線時不補寫**，不會用舊值把斷線掩蓋掉

> ⚠️ **累計型計數器不要用 `threshold_percent`。** 累計電表/流量計的基準值可以到
> 百萬等級、日增量卻只有幾百，「變化 1%」要累積四十幾天才達得到，結果就是資料看起來
> 整個斷掉、但採集其實一切正常。這類單調遞增的點位請用 `on_change`（值一跳動就寫，
> 不需要為每支點位猜門檻）或 `threshold_absolute`（想降低資料量時，門檻用工程單位設定）。
> 正式庫的 63 個累計型感測器已由 [`sql/010`](../sql/010_cumulative_counter_upload_condition.sql)
> 統一改成 `on_change`，詳見 [`todo.md`](../todo.md) 待辦 #2 的事故紀錄。

> 💡 心跳保底存在的理由：沒有它，門檻設得不合理的點位會**完全沒有任何紀錄、也不會有
> 任何錯誤 log**，只能靠人工發現。有了心跳，「設備沒有新資料」跟「採集系統掛了」
> 在資料上才能區分開來。除非你有別的斷更偵測機制，否則不建議設成 0（關閉）。

判斷全部在記憶體做，服務啟動時會把每個 sensor 目前資料庫裡最新一筆讀回來當快取；
`upload_condition` / `upload_threshold` 調整後最慢下一個統一寫入週期內生效，
不需要重啟服務。三個協議共用同一個 `sensor_reading_writer` 單例，內部用
`threading.Lock` 保護，多執行緒/背景服務同時呼叫也安全。

> `upload_condition`（決定收到之後要不要寫進資料庫）跟 OPC UA 的
> `opcua_deadband_type`（決定伺服器端要不要把這筆變化送過來）是兩個不同層級的
> 過濾，可以疊加使用：deadband 先在源頭省頻寬，upload_condition 再在寫入端省
> DB 空間跟 I/O。

### 5. 警報管理（🆕 v3，需執行 `sql/011`）

警報引擎（`services/alarm/engine.py`）跑在 `main.py` 裡，每 `ALARM_EVAL_INTERVAL` 秒（預設 5）
直接讀記憶體中的最新值判斷，**不必等 `sensor_readings` 的寫入週期**：

| 規則類型 | 觸發 | 恢復 |
| --- | --- | --- |
| `HH` / `H` | 數值 > 設定值 | 數值 ≤ 設定值 − 遲滯 |
| `L` / `LL` | 數值 < 設定值 | 數值 ≥ 設定值 + 遲滯 |
| `EQ` | 數值 = 設定值（例如故障碼） | 不等於時 |
| `NE` | 數值 ≠ 設定值（例如應為 1 的運轉訊號） | 等於時 |

- **延遲觸發**：條件要連續成立 N 秒才發警報，濾掉瞬間突波；**遲滯**避免在門檻附近反覆跳動
- **四級優先**：1 緊急 / 2 高 / 3 中 / 4 低
- **隱含規則**：`sensors.min_threshold` / `max_threshold` 自動視為 L / H 警報（等級「中」），
  既有設定升級後直接生效（`ALARM_USE_SENSOR_LIMITS=false` 可關閉）
- **通訊中斷警報**：OPC UA Server 斷線、Modbus / TIA 整台 PLC 讀不到，各發**一筆**
  （不會一台設備斷線洗出幾十筆點位警報），斷線持續 `ALARM_COMM_DELAY_SEC` 秒才發
- **確認（ACK）**：ISA-18.2 慣例，「已恢復但沒人確認」的警報仍留在清單上；確認需要操作員以上權限
- **通知**：Webhook（n8n / Slack / Discord / Teams）、Email、Telegram，依等級過濾，失敗自動重試
- 來源已斷線的感測器不做數值判斷（交給通訊警報），避免用舊值反覆觸發 / 恢復

v2 的「異常監控」查詢保留在「警報中心 → 診斷檢查」分頁，不依賴警報引擎，main.py 沒在跑時也能用來排查。

### 6. 數據增量上傳 (Report by Exception)

MQTT 端採用增量發送：快取上一輪的值，只有數值變化時才發送，並定期強制全量心跳上傳。
`MQTT_ENABLED=false` 可完全關閉 MQTT；MQTT 的資料來源查詢也會依 `MODBUS_ENABLED` /
`TIA_ENABLED` 自動跳過停用協議的表。

### 7. PostgreSQL 批量更新

使用 `execute_values` 一次性更新多筆點位；OPC UA 訂閱模式另外提供輕量版
`batch_update_opcua_values`，只更新數值不動結構欄位。
🆕 v3：連線池會丟棄已失效的連線（DB 重啟後自動恢復），並設定連線逾時與 TCP keepalive。

### 8. 監控畫面（🆕 v3）

- **即時總覽**：KPI（採集服務、OPC UA 連線數、感測器狀態、警報數）＋ 依設備分組的卡片 / 表格，
  篩選廠區 / 產線 / 狀態 / 關鍵字，10 ~ 60 秒自動更新（局部重繪，不會清掉篩選條件）
- **歷史趨勢**：最多 8 個感測器，依單位自動分圖；時間跨度長時用 `time_bucket` 聚合
  （平均線＋最小～最大色帶），每個感測器約 1500 點以內；預設**階梯線**
  （`sensor_readings` 是有變化才寫，斜線會誤導）；單一感測器時顯示上下限 / 警報設定值
- **報表匯出**：每小時 / 每日 / 每月（以 `SCADA_TIMEZONE` 當地午夜對齊），
  增量 = 本期期末 − 上期期末，適合累計電表算用量；Excel（報表＋明細兩個工作表）/ CSV

### 9. 使用者、權限與稽核（🆕 v3，需執行 `sql/012`）

| 角色 | 權限 |
| --- | --- |
| 檢視者 viewer | 總覽、趨勢、報表、警報清單、系統狀態 |
| 操作員 operator | ＋ 確認警報 |
| 工程師 engineer | ＋ 點位、感測器階層、警報規則等所有設定 |
| 管理員 admin | ＋ 使用者管理、稽核紀錄 |

- 密碼以 PBKDF2-SHA256（26 萬次迭代、隨機 salt）儲存；連續 5 次登入失敗鎖定 60 秒
- `.env` 的 `ADMIN_USER` / `ADMIN_PASSWORD` 永遠可以用管理員身分登入（救援帳號）
- 登入 / 登出 / 所有設定變更 / 警報確認都寫入 `audit_log`，記錄**改了哪些欄位、舊值 → 新值**
  （密碼欄位一律遮蔽）

### 10. Modbus 採集與線上調適（🆕 v3.1，需執行 `sql/015`）

**採集（`collector/run_modbus_collector.py`）**

| v2 | v3.1 |
| --- | --- |
| 一個點位一次請求 | 同站號、同功能碼、相近位址（間隔 ≤ `MODBUS_MAX_GAP`）合併成一次請求 |
| 每輪重新連線 | 連線跨輪次保留，斷線才重建 |
| 每個 (IP, Port, 站號) 各開一條連線同時連 | 同一個閘道（IP:Port 或序列埠）一條連線，站號依序讀取 —— RS-485 閘道常只允許 1~4 條連線 |
| 只有 Modbus TCP | `transport`：`tcp` / `rtu_over_tcp`（序列閘道透通模式）/ `rtu`（本機序列埠，`plc_ip` 填 `/dev/ttyUSB0`） |
| 某個位址不存在 → 該點位失敗 | 整塊被拒絕時自動逐點讀取，找出壞點位讓它單獨讀；若是空隙裡的未定義暫存器造成，該組改成只合併連續位址 |
| 一個站號沒回應拖慢 / 拖垮整台 | 逾時的站號標記離線，同連線的其他站號照讀；連線本身斷掉才整條標記離線 |
| 每個點位每輪印一行 log | 每輪一行摘要；同一個問題只在發生 / 恢復時各印一次 |
| 讀取失敗時清空數值 | 保留最後數值、只更新連線狀態（與 OPC UA 一致），並寫入通訊中斷品質標記 |

點位可以個別停用（`enabled`），「Modbus 點位」頁面新增刪除功能與每條連線的採集統計（請求數、耗時、單獨讀取的點位）。

**🔧 Modbus 線上調適（`web/pages/modbus_debug.py`，工程師以上，「工具」選單）**

用跟採集程式完全相同的連線與解碼直接跟設備通訊，調出來的設定存進去就保證採集得到同樣的值：

- 📖 **暫存器讀取**：FC01~04、Modicon 位址（40001）自動換算、連續監看（變動的暫存器標黃）、
  HEX / UINT16 / INT16 / 二進位 / ASCII；多暫存器以 **ABCD / CDAB / BADC / DCBA 四種順序並列解碼**，
  不合理的值（1e-38、1e+30 這種）自動淡化；確認後**一鍵建立點位**（可帶線性換算與感測器綁定）
- 🎯 **點位驗證**：用已建點位自己的設定即時讀一次，比對資料庫目前值；目前順序解出不合理的值時提示可能的正確順序，可直接套用
- 🔍 **站號掃描**：找出 RS-485 匯流排上有回應的站號（回例外碼也算：代表設備存在）
- ✏️ **寫入測試**：FC05 / FC06 / FC16，寫入後自動讀回比對。**預設關閉**（`MODBUS_DEBUG_WRITE_ENABLED=true` 才開），
  需勾選確認，每次寫入都記錄在稽核紀錄
- 📜 **通訊紀錄**：每次請求的結果、回應時間、Modbus 例外碼的中文說明（例如「例外碼 02：位址不存在，檢查是否差 1」）

> 調適工具不受 `MODBUS_ENABLED` 限制：還沒啟用 Modbus 採集時也能先接設備、確認點位。

### 11. 資料可靠性與安全性（🆕 v3.1）

- **本機緩存（`data_layer/spool.py`）**：`sensor_readings` 寫入失敗時存進本機 SQLite（`SPOOL_DIR`），
  資料庫恢復後依寫入順序補寫（每輪最多 10 萬筆）；採集程式在資料庫斷線期間沿用上次的點位設定繼續採集。
  上限 `SPOOL_MAX_ROWS`（預設 200 萬筆，約可撐十幾天），超過丟棄最舊的資料
- **啟動時資料庫連不上**：main.py 每 10 秒重試，不再直接結束
- **品質（`sql/014`、`data_layer/quality.py`）**：0 正常｜1 保持值（心跳補寫）｜2 不確定｜3 品質不良（只記轉變點）｜
  4 通訊中斷（斷線時記一筆）。品質改變時不論上傳條件一律寫一筆；不良 / 中斷期間不參與警報判斷
- **HTTPS**：`.env` 設定 `ADMIN_SSL_CERT` / `ADMIN_SSL_KEY`；`scripts/gen_self_signed_cert.sh <IP>` 產生自簽憑證。
  正式環境若有公司 CA 憑證直接替換即可；也可以改用 nginx / Caddy 反向代理處理 TLS
- **閒置登出**：`ADMIN_IDLE_TIMEOUT_MIN`（預設 30 分鐘）；`ADMIN_IDLE_TIMEOUT_EXEMPT_ROLES`（預設 viewer）不受限，
  控制室的監看螢幕請用檢視者帳號登入

### 12. 工程效率工具（🆕 v3.2，需執行 `sql/016` ~ `sql/018`）

**📥 批次匯入匯出（工具 → 批次匯入匯出，`data_layer/bulk_io.py`）**

| 資料 | 對應鍵 | 說明 |
| --- | --- | --- |
| 設備 | `device_code` | 廠區 / 產線不存在時自動建立 |
| 感測器 | `sensor_code` | 含上傳條件、上下限、deadband、狀態字典 |
| Modbus 點位 | `id`（空白 = 新增） | 用 `sensor_code` 綁定感測器 |
| OPC UA 綁定 | `server_name` + `node_id` | 只改綁定（幾千個點位在 Excel 篩選後批次填 `sensor_code`） |
| 警報規則 | `rule_id`（空白 = 新增） | |

上傳後先驗證：每列的錯誤（必填、格式、設備不存在、感測器已綁定其他點位…）、每筆更新改了哪些欄位；
有任何錯誤就不能套用。套用時在同一個交易裡重新驗證一次，預覽之後資料庫被別人改過也不會誤寫。
CSV 用 UTF-8（含 BOM）或 Big5 都可以；匯出的檔案原封不動匯回去會顯示「全部未變動」。

**🧩 設備範本（設定 → 設備範本，`data_layer/templates.py`）**

1. 手動設定好第一台設備並確認採集正確
2.「新增範本 → 從現有設備」：自動帶出感測器（編號去掉設備前綴當 suffix）、Modbus 點位（位址 / 型態 / 位元組順序 / 換算）
   或 OPC UA 節點樣式（設備代號換成 `{device}`）、警報規則
3.「建立設備」：選範本與產線，用「批次產生」填入編號前綴 / 台數 / IP / 起始站號 → 預覽 → 建立

同型設備位址整體平移時用 `address_offset`；範本可以匯出成 JSON 帶到其他廠區匯入。

**🧮 計算點（設定 → 計算點，`services/calc/`）**

運算式用大括號引用感測器：`{PM01_KW} + {PM02_KW}`；支援四則、次方、比較、位元運算、`and/or/not`、`a if 條件 else b`。
計算引擎（main.py）每 5 秒用記憶體中的最新值計算，結果寫進一般感測器。

| 分類 | 函式 | 典型用途 |
| --- | --- | --- |
| 數學 / 統計 | `abs sign sqrt log log10 exp floor ceil round min max clamp avg sum median spread` | 加總、平均、三相不平衡 |
| 邏輯 | `iff switch between` | 狀態碼對照、範圍判斷 |
| 品質 | `isgood valueor coalesce` | 主備援感測器、斷線時以 0 計，不讓整個計算點斷線 |
| 位元 | `bit bitand bitor bitxor shl shr`、`& \| ^` | 拆 PLC 狀態字 |
| 時間 | `hour minute weekday day month year timeofday` | 時間電價、班別 |
| 累計 | `integral delta ontime count`（週期 hour/day/week/month/never + 生產日起始小時） | kW→kWh、今日 / 本月用電、運轉時數、啟動次數 |
| 動態 | `prev changed rising hold derivative movavg movmin movmax filter` | 需量（15 分鐘平均）、變化率、去雜訊、取樣保持 |

- 輸入斷線 / 品質不良 / 沒有資料 → 暫停計算並標記斷線（不用舊值硬算）；用 `valueor / coalesce / isgood` 可自行處理
- 累計狀態存在 `calculated_points.calc_state`（`sql/020`），重啟後接續；`delta` 的基準值從歷史資料查，試算就是實際用量
- 可以引用其他計算點（依相依順序計算），循環引用會被擋下；網頁「試算」用目前即時值算一次
- 安全：不用 `eval`，運算式轉成 AST 後白名單檢查；次方、視窗長度、運算式長度都有上限

**🐍 Python 腳本計算點（v3.4，`sql/021`，`CALC_SCRIPTS_ENABLED=true`，管理員限定）**

運算式寫不出來的邏輯（迴圈、查表、複雜狀態機）改用 Python 腳本：

```python
kw = value("PM01_KW", 0) + value("PM02_KW", 0)   # value(代碼, 斷線時的預設值)
today = now.strftime("%Y-%m-%d")                  # now：SCADA_TIMEZONE 的目前時間
if state.get("day") != today:                     # state：跨輪保存、重啟後接續（必須能轉 JSON）
    state["day"], state["peak"] = today, 0
state["peak"] = max(state["peak"], kw)
log("功率", kw, "今日尖峰", state["peak"])        # log：顯示在網頁
result = state["peak"]                            # result：這一輪的結果；None = 不更新
```

也可以用 `tags`（所有有效感測器）、`quality("代碼")`、`isgood("代碼")`，import `math statistics datetime json re
itertools functools collections decimal fractions bisect heapq random time`。

| 保護 | 說明 |
| --- | --- |
| 獨立子程序 | 腳本崩潰、無窮迴圈、吃光記憶體都不會影響採集服務 |
| 逾時 | 每輪最多 `CALC_SCRIPT_TIMEOUT` 秒（預設 2），超時強制終止；之後暫停 30 秒 ~ 10 分鐘（逐次加倍）再試 |
| 記憶體 | 子程序上限 `CALC_SCRIPT_MEMORY_MB`（預設 512 MB） |
| 白名單 | 只開放安全的內建函式與上面列出的模組（不能開檔、不能 import os / subprocess） |
| 權限 | 只有管理員能新增 / 修改，其他角色只能看程式碼；每次修改把完整程式碼記入稽核紀錄 |
| 開關 | `CALC_SCRIPTS_ENABLED=false`（預設）時腳本計算點全部停用 |

> ⚠️ Python 無法做到真正的沙箱，白名單擋得住「不小心」，擋不住「刻意」。這就是為什麼只有管理員能寫、而且預設關閉。

**⏰ 排程報表（報表 → 排程寄送，`services/report_scheduler.py`）**

每日（寄前一天，每小時一列）/ 每週（寄上週一 ~ 週日）/ 每月（寄上個月），寄送時間、範圍（設備 / 感測器）、
統計值（每個一個工作表）、收件人都可設定。Email 沿用警報通知的 SMTP 設定並夾帶 Excel；Webhook 送 JSON 摘要
（小於 2 MB 附 base64 檔案）。採集服務停機錯過的排程只補寄最近一期；寄送失敗每 10 分鐘重試，最多 6 次。
「立即寄送測試」「下載最近一期」可以在建立後馬上確認內容。

### 13. Siemens S7 採集與線上調適（🆕 v3.3，需執行 `sql/019`）

| v2 | v3.3 |
| --- | --- |
| 只能讀 DB | DB / M（旗標）/ I（輸入）/ Q（輸出） |
| BOOL 一律讀第 0 bit | `bit_offset` 0~7，`DB1.DBX10.3` 設得出來 |
| 只認 REAL / DINT / INT / BOOL，其他當 4 bytes（LREAL、STRING 會讀錯） | TIA 全部數值型態 + CHAR / STRING[n]，大小正確 |
| Rack / Slot 寫死 0 / 1（S7-300 連不上） | 每個點位可設 Rack / Slot |
| 一個 DB 從最小讀到最大 offset | 相近位址才合併（`S7_MAX_GAP`），單塊上限 `S7_MAX_BLOCK` |
| 每輪重新連線 | 連線重用，依 PLC（IP + Rack + Slot）分組 |
| 讀取失敗寫入 `{"val": 0.0}` | 保留最後數值、只更新狀態，並寫入通訊中斷品質標記 |
| 錯誤訊息是 snap7 原文 | 中文說明：PUT/GET 沒開、DB 是最佳化存取、Rack/Slot 錯誤… |

**🔧 S7 線上調適（工具 → S7 線上調適，工程師以上）**：PLC 資訊（型號、訂貨號、韌體、RUN/STOP、PDU）與 S7-1200/1500
必要設定檢查表；記憶體讀取（直接輸入 TIA 位址、位元檢視、INT / WORD / DINT / DWORD / REAL / LREAL 並列解碼、連續監看標示變動、
一鍵建立點位）；點位驗證；寫入測試（預設關閉，`S7_DEBUG_WRITE_ENABLED=true`，BOOL 只改那一個位元）；通訊紀錄。
「TIA (S7) 點位」頁面的新增表單也可以直接貼 TIA 位址；批次匯入匯出支援 S7（`address` 欄位填 TIA 位址）。

### 14. 系統健康監控（🆕 v3）

`main.py` 每 10 秒把心跳與執行統計寫進 `service_status`，網頁「系統狀態」頁顯示：
採集服務是否存活（超過 60 秒沒心跳 = 無回應，可分辨正常停止與異常中斷）、
寫入排程統計、每台 OPC UA Server 的監控點數 / 品質不良數 / 最後收到資料時間 / 重連次數、
警報引擎與通知統計、`sensor_readings` 大小與壓縮政策、各 migration 是否已套用。

---

## 📁 專案結構

```
├── core/                         # 🆕 共用基礎（不依賴 Streamlit，可單元測試）
│   ├── config.py                 # .env 讀取、協議開關、SCADA_TIMEZONE
│   ├── auth.py                   # 帳號、PBKDF2 密碼雜湊、角色權限
│   └── audit.py                  # 操作稽核寫入
├── protocols/                    # 協議驅動層（S7 / Modbus / OPC UA）
│   ├── modbus_protocol.py        #     🆕 ModbusConnection：TCP / RTU over TCP / RTU、結構化結果
│   ├── s7_protocol.py            #     🆕 v3.3 S7Connection：DB/M/I/Q、Rack/Slot、CPU 資訊、中文錯誤說明
│   ├── s7_codec.py               #     🆕 v3.3 S7 編解碼、TIA 位址表示法（採集與調適共用）
│   └── modbus_codec.py           #     🆕 Modbus 編解碼、位元組順序、Modicon 位址（採集與調適共用）
├── parsers/                      # 數據解析器（S7 位元組、Modbus 編解碼）
├── data_layer/
│   ├── db_connector.py           # PostgreSQL 連線池（🆕 自動丟棄失效連線）
│   ├── batch_updater.py          # 即時層批量更新
│   ├── timeseries_writer.py      # sensor_readings 統一週期性寫入（來源存活判定、🆕 品質、本機緩存）
│   ├── spool.py                  # 🆕 v3.1 本機緩存（Store-and-Forward，SQLite）
│   ├── bulk_io.py                # 🆕 v3.2 批次匯入匯出（驗證 / 預覽 / 套用）
│   ├── templates.py              # 🆕 v3.2 設備範本
│   └── quality.py                # 🆕 v3.1 品質代碼定義
├── messaging/
│   └── mqtt_publisher.py
├── services/                     # 常駐背景服務（跑在 main.py 裡）
│   ├── opcua_subscription_service.py   # OPC UA 訂閱服務
│   ├── alarm/                    # 🆕 警報子系統
│   │   ├── rules.py              #     判斷邏輯（純函式）
│   │   ├── engine.py             #     AlarmEngine 背景執行緒
│   │   └── notifier.py           #     Webhook / Email / Telegram 通知
│   ├── calc/                     # 🆕 v3.2 計算點（expression.py 安全運算式、engine.py 計算引擎、script_runner.py Python 腳本子程序）
│   ├── reporting.py              # 🆕 v3.2 報表計算（網頁與排程共用）
│   ├── report_scheduler.py       # 🆕 v3.2 排程報表
│   └── status_reporter.py        # 🆕 service_status 心跳回報
├── collector/                    # Modbus / S7 採集、OPC UA 一次性瀏覽測試腳本
│   └── modbus_blocks.py          #     🆕 Modbus 批次讀取規劃
├── web/                          # 🆕 網頁後台（原 admin_app.py 2266 行拆分）
│   ├── common.py                 #     共用查詢 / 權限 / 稽核 / 匯出
│   ├── auth_ui.py                #     登入 / 登出 / 變更密碼
│   ├── nav.py                    #     頁面登錄表（站內連結用）
│   └── pages/                    #     一頁一個模組，各自提供 render()
│       ├── overview.py  trends.py  alarms.py  reports.py          # 監控
│       ├── config_opcua.py  config_modbus.py  config_tia.py       # 點位設定
│       ├── config_hierarchy.py  alarm_rules.py                    # 階層 / 警報規則
│       ├── modbus_debug.py  s7_debug.py  import_export.py     # 🆕 工具：Modbus / S7 線上調適、批次匯入匯出
│       ├── device_templates.py  calculated_points.py           # 🆕 v3.2 設定：設備範本、計算點
│       ├── report_schedules.py   #     🆕 v3.2 排程報表（嵌在報表頁）
│       ├── system.py  users.py  audit_log.py                      # 系統
│       └── diagnostics.py        #     v2「異常監控」（嵌在警報中心）
├── sql/                          # DB migration，執行順序見 sql/README.md
├── tests/                        # 🆕 單元測試（pytest）
├── .streamlit/config.toml        # 🆕 Streamlit 設定（隱藏開發者選單）
├── scripts/gen_self_signed_cert.sh  # 🆕 產生 HTTPS 自簽憑證
├── data/                         # 🆕 執行期資料（本機緩存），不進版控
├── main.py                       # 採集主程式（採集 + 寫入排程 + 警報引擎 + 心跳回報）
├── admin_app.py                  # 網頁後台入口：登入 → 依角色組出導覽選單
├── run_all.py                    # 同時啟動並監護 main.py + admin_app.py（🆕 崩潰自動重啟）
├── requirements.txt / requirements-dev.txt
├── Dockerfile                    # 🆕 HEALTHCHECK
├── env.example                   # .env 範本（唯一的環境變數清單來源）
├── todo.md / README_DB.md / UPGRADE_NOTES_v2.md / UPGRADE_NOTES_v3.md
└── 狀態字典範例.json
```

> `sql/` 的編號 002 ~ 005 不存在，不是遺失——那幾版 v1 期間的變更當時是直接在資料庫上手動執行、沒有留下腳本。詳見 [`sql/README.md`](../sql/README.md)。

---

## ⚙️ 安裝與設定

### 1. 建立虛擬環境並安裝依賴

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 建立 `.env` 環境變數檔案

以 `env.example` 為範本複製一份，再填入實際值：

```bash
cp env.example .env
```

`env.example` 是**唯一**的環境變數清單來源（已與程式碼實際讀取的 key 對齊），
以下只列出比較需要留意的幾項：

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `MODBUS_ENABLED` / `TIA_ENABLED` / `OPCUA_ENABLED` | `true` | 設為 `false` 完全屏蔽該協議：`main.py` 不啟動採集執行緒，`admin_app.py` 隱藏對應分頁 |
| `POLL_INTERVAL` | `60.0` | Modbus / TIA(S7) 的採集週期（秒）。OPC UA 走常駐訂閱服務，**不受此值影響** |
| `SENSOR_READING_FLUSH_INTERVAL` | `60` | `sensor_readings` 統一批次寫入的固定週期（秒）。不建議設 <5 秒 |
| `SENSOR_HEARTBEAT_INTERVAL` | `3600` | 心跳保底（秒）：距上次實際寫入超過這個時間就強制補寫一筆，不管 `upload_condition` 判斷結果。設 `0` 關閉，**但不建議** |
| `OPCUA_PUBLISH_INTERVAL_MS` | `1000` | **全域**預設訂閱取樣頻率（毫秒）。優先序：`sensors.opcua_sampling_interval_ms`（逐感測器）> `opcua_servers.publish_interval_ms`（逐 Server）> 本值 |
| `OPCUA_CLIENT_TIMEOUT` | `10` | 單次 OPC UA 請求逾時秒數，網路不穩的場域可調高 |
| `OPCUA_HEARTBEAT_INTERVAL_SEC` / `_TIMEOUT_SEC` / `_MAX_FAILURES` | `15` / `8` / `2` | 心跳週期、單次心跳逾時、連續失敗幾次才判定斷線。調高可容忍偶發逾時，避免頻繁整組重建訂閱 |
| `MQTT_ENABLED` | `true` | 設為 `false` 完全不建立 MQTT 連線 |
| `MQTT_INCLUDE_OPCUA` 🆕 | `false` | MQTT 也上傳 OPC UA 已綁定感測器（Key = `sensor_code`）。純 OPC UA 部署要用 MQTT 時設 `true` |
| `SENSOR_ALIVE_WINDOW` 🆕 | `300` | 來源存活判定窗口（秒），心跳補寫只在這段時間內確認過來源存活時才以目前時間補寫。須大於 `POLL_INTERVAL` |
| `ALARM_ENABLED` 🆕 | `true` | 警報引擎開關；其餘 `ALARM_*` 見 `env.example` |
| `ALARM_WEBHOOK_URLS` / `SMTP_*` / `TELEGRAM_*` 🆕 | — | 警報通知頻道，都不設 = 只在網頁顯示 |
| `SCADA_TIMEZONE` 🆕 | `Asia/Taipei` | 網頁顯示、日期選擇、報表日 / 月邊界使用的時區 |
| `ADMIN_USER` / `ADMIN_PASSWORD` / `ADMIN_PORT` | — | 救援管理員帳密與埠號，**務必改掉預設值**。日常帳號請在「使用者管理」建立 |

> `.env` 已列入 `.gitignore`，請勿提交；`env.example` 只放範例值，不要填入正式環境的密碼。

### 3. 執行資料庫 Migration

**務必明確列出要執行的檔案**，不要用 `for f in sql/0*.sql`（會把 `010_rollback.sql` 一起跑掉、
等於撤銷 010，也會跑到選用的 013）：

```bash
for f in 000_realtime_tables 001_sensor_hierarchy_and_mapping 006_opcua_upgrade \
         007_opcua_deadband 008_missing_app_columns 009_opcua_server_publish_interval \
         010_cumulative_counter_upload_condition 011_alarm_management 012_users_audit_status \
         014_reading_quality 015_modbus_transport 016_device_templates \
         017_calculated_points 018_report_schedules 019_s7_extensions 020_calc_state 021_calc_scripts; do
  psql -h "$DB_HOST" -U <資料表擁有者> -d "$DB_NAME" -v ON_ERROR_STOP=1 \
       -v app_user="$DB_USER" -f "sql/$f.sql" || break
done
```

**既有系統升級到 v3.4**：補跑 `011`、`012`、`014` ~ `021` 即可（全部腳本都是 idempotent，重複執行安全）：

```bash
psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -v app_user=scada -f sql/011_alarm_management.sql
psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -v app_user=scada -f sql/012_users_audit_status.sql
psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -f sql/014_reading_quality.sql
psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -f sql/015_modbus_transport.sql
for f in 016_device_templates 017_calculated_points 018_report_schedules 019_s7_extensions 020_calc_state 021_calc_scripts; do
  psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -v app_user=scada -f sql/$f.sql
done
```

`011` / `012` 會自動授權新表給 `-v app_user` 指定的應用程式帳號。沒跑的話，網頁對應頁面會顯示提示、
警報引擎與心跳回報會自動暫停，**採集本身不受影響**。

`sql/013_timeseries_policy.sql` 是**選用**的壓縮政策（超過 30 天的資料自動壓縮、不刪資料），
建議先在測試環境驗證後於離峰時段執行；資料保留（自動刪除舊資料）是不可逆的決策，只寫在註解裡。

> ⚠️ migration 要用**資料表擁有者**執行。應用程式帳號（`.env` 裡的 `DB_USER`）
> 通常只有 `SELECT / INSERT / UPDATE`，沒有 `ALTER TABLE` 權限，拿它跑會失敗。
> 例外：`010` 只有 `UPDATE`、不改 schema，用應用程式帳號也跑得動。

> 💡 即時層四張表由 `000` 建立，`001` 才對它們加 `sensor_id`，所以 `000` 一定要先跑。
> 既有環境四張表都已存在，全部 `CREATE` 都是 `IF NOT EXISTS`，跑了不會動到現有資料。

### 4. 執行主程式

```bash
source .venv/bin/activate
python main.py          # 只跑採集主服務
# 或
python run_all.py       # 同時跑採集主服務 + 網頁管理後台
```

### 5. Docker 容器化執行

```bash
docker build -t unified_collector:latest .
docker run -d --name unified_collector --env-file .env -p 1118:1118 \
           -v /opt/scada/data:/app/data \
           --stop-timeout 30 unified_collector:latest
# 使用 RS-485 序列埠（transport=rtu）時加上：--device /dev/ttyUSB0
# 使用 HTTPS 時把憑證掛進去：-v /opt/scada/certs:/app/certs:ro（.env 設 ADMIN_SSL_CERT=certs/admin.crt）
```

- 🆕 `/app/data` 是本機緩存（資料庫斷線時暫存資料），**一定要掛 volume**，否則刪除重建容器時尚未補寫的資料會消失

- 🆕 映像檔內建 `HEALTHCHECK`（網頁後台 `/_stcore/health`），`docker ps` 會顯示 healthy / unhealthy
- 🆕 `run_all.py` 收到 `docker stop` 的 SIGTERM 會通知子程序收尾（最後一次寫入、OPC UA 斷線），
  最多約 25 秒，請用 `--stop-timeout 30`（或 compose 的 `stop_grace_period: 30s`）
- 🆕 子程序非預期結束會自動重啟（指數退避 5 → 300 秒）
- 容器沒設 `TZ` 時系統時區是 UTC：寫入 DB 的時間都有帶時區不受影響；網頁顯示 / 報表用
  `.env` 的 `SCADA_TIMEZONE`（預設 `Asia/Taipei`）

### 6. 執行測試

```bash
pip install -r requirements-dev.txt
python -m pytest tests          # 寫入判斷、警報規則、密碼雜湊、報表計算
python -m pyflakes core services web data_layer main.py admin_app.py
```

---

## 🧠 執行架構說明

### 主迴圈（`main.py`）

```
啟動時：
  ├─ 依 .env 開關決定匯入/啟動哪些協議（MODBUS_ENABLED / TIA_ENABLED / OPCUA_ENABLED）
  ├─ sensor_reading_writer.load_initial_cache() + .start()  # 🆕 啟動統一寫入背景執行緒
  ├─ 初始化 MQTT Publisher（若啟用）
  ├─ 啟動 OPC UA 訂閱服務（若啟用）
  ├─ 🆕 啟動警報引擎（ALARM_ENABLED，每 5 秒判斷記憶體最新值）
  └─ 🆕 啟動心跳回報（每 10 秒寫 service_status）

每輪 POLL_INTERVAL 秒（僅在 Modbus 或 TIA 至少一個啟用時才有意義）：
  ├─ Modbus 採集（若啟用）─┐
  └─ TIA(S7) 採集（若啟用）─┴─ 併發執行，互不阻塞；兩者都停用則跳過
  └─ MQTT 增量上傳（若啟用，資料來源依協議開關自動排除停用的表）

關閉時：
  └─ 依序停止 心跳回報（註記正常停止）→ 警報引擎 → OPC UA 訂閱服務 → sensor_reading_writer → MQTT → DB 連線池
```

OPC UA 完全不在這個迴圈裡，走獨立的常駐背景服務（見下方）。

### OPC UA 訂閱服務（`services/opcua_subscription_service.py`）

```
服務啟動
  └─ 對每一台 enabled=TRUE 的 Server：
        ├─ 連線（連線逾時由 OPCUA_CLIENT_TIMEOUT 控制）
        ├─ 有快取點位表就直接用；沒有才做一次完整 browse 並寫入 opcua_tags
        ├─ 🆕 只對「已綁定 sensor_id」的點位建立訂閱（未綁定的存在 opcua_tags 供瀏覽，
        │      但不佔訂閱資源）；一個都沒綁定時僅維持連線與心跳，不建立任何訂閱
        ├─ 🆕 依每個點位解析出的取樣頻率分組，各組各自 create_subscription()；
        │      同一組內再依 deadband 設定（none/percent/absolute）分別呼叫
        │      subscribe_data_change() 或 deadband_monitor()
        ├─ 背景 task 1：每 2 秒把收到的變化批次 flush 進 opcua_tags 即時值，
        │              並把已綁定感測器的點位丟進 sensor_reading_writer.update_latest()
        │              （不在這裡直接寫 DB，寫入交給統一排程）
        ├─ 背景 task 2（🆕 _watch_and_maintain，取代原本兩個分開的 task）：
        │              每 5 秒檢查一次「立即瀏覽」旗標 / 感測器綁定 / 取樣頻率 /
        │              deadband 設定是否有變化，有變化就整批重建訂閱
        └─ 定期讀心跳目標節點確認連線存活（🆕 逾時 OPCUA_HEARTBEAT_TIMEOUT_SEC、
                連續失敗 OPCUA_HEARTBEAT_MAX_FAILURES 次才判定斷線，容忍偶發逾時）；
                判定斷線 → 退避重連
```

**新增點位 / 移除點位 / 調整訂閱設定的流程：**
使用者在網頁「OPC UA 點位設定」分頁按下「🚀 立即瀏覽並寫入資料庫」，或直接在
「感測器階層管理」分頁調整某感測器的綁定/取樣頻率/deadband，訂閱服務最慢
5 秒內會自動偵測到並重建訂閱，**不需要重啟任何服務**。

**心跳與斷線判定的設計取捨：** 心跳目標優先使用已訂閱成功的實際點位；連線
逾時、心跳逾時、連續失敗容忍次數皆可由 `.env` 調整，網路品質不穩的場域建議
拉高 `OPCUA_CLIENT_TIMEOUT` 與 `OPCUA_HEARTBEAT_MAX_FAILURES`，避免偶發的
回應延遲被誤判成斷線、頻繁整組重建訂閱。真正的硬性斷線（TCP session 已死）
仍會被正確偵測並觸發重連，這組參數只影響「多久 / 多寬容才判定為斷線」，
無法讓真的不穩的網路變穩定。

**Session 數限制的處理：** 首次瀏覽 / 重新整理點位表時，重複使用訂閱服務本身
已經開好的連線去瀏覽，不會另外多開連線；偵測到 `BadTooManySessions` 改用
固定 120 秒等待才重試。

**log 雜訊處理：** `asyncua` 套件內部 logger 等級調到 `WARNING`，並加上
🆕 重複訊息抑制 filter（同一則訊息 10 秒內只印一次），避免斷線時每秒狂噴
`Publish iteration crashed; retrying in 1s` 洗版。

---

## 🖥️ 網頁後台操作說明（`admin_app.py`）

登入後左側選單依**角色**顯示（權限不足的頁面不會出現），Modbus / TIA 頁面另外依 `.env` 開關顯示。
側邊欄隨時顯示目前警報數（點擊直接進警報中心）與採集服務狀態。

| 分組 | 頁面 | 說明 | 最低角色 |
| --- | --- | --- | --- |
| 監控 | 🏭 即時總覽 | 設備卡片 / 表格、KPI、自動更新；卡片的「查看趨勢」直接開啟該設備的趨勢 | 檢視者 |
| 監控 | 📈 歷史趨勢 | 選感測器與時間範圍，下載 CSV（長格式 / 寬格式） | 檢視者 |
| 監控 | 🚨 警報中心 | 目前警報（確認）、警報歷史（統計＋匯出）、診斷檢查 | 檢視者（確認需操作員） |
| 監控 | 📑 報表匯出 | 選設備或感測器、粒度與統計值，匯出 Excel / CSV | 檢視者 |
| 設定 | 📡 OPC UA / Modbus / TIA 點位 | 與 v2 相同：Server 與點位設定、測試連線、綁定感測器 | 工程師 |
| 設定 | 🧬 感測器階層 | 與 v2 相同：廠區 → 產線 → 設備 → 感測器，含上傳條件、deadband、上下限 | 工程師 |
| 設定 | 🧩 設備範本 | 從現有設備建立範本，一次建立多台同型設備 | 工程師 |
| 設定 | 🧮 計算點 | 運算式虛擬感測器、試算、狀態 | 工程師 |
| 工具 | 📥 批次匯入匯出 | 設備 / 感測器 / Modbus / OPC UA 綁定 / 警報規則的 CSV、Excel 匯入匯出 | 工程師 |
| 監控 | ⏰ 排程寄送（報表頁分頁） | 每日 / 每週 / 每月自動寄送 Excel 報表 | 檢視者（編輯需工程師） |
| 工具 | 🔧 S7 線上調適 | CPU 資訊、TIA 位址讀記憶體、多型態解碼、點位驗證、位元寫入、建立點位 | 工程師 |
| 工具 | 🔧 Modbus 線上調適 | 讀暫存器、四種位元組順序對照、站號掃描、點位驗證、寫入測試、一鍵建立點位 | 工程師 |
| 設定 | 🔔 警報規則 | 規則清單（直接編輯 / 刪除）、新增規則（即時顯示觸發條件說明）、通知頻道狀態與測試發送 | 工程師 |
| 系統 | 🩺 系統狀態 | 採集服務心跳、OPC UA 訂閱統計、警報引擎、資料庫大小與政策、migration 檢查 | 檢視者 |
| 系統 | 👥 使用者管理 | 新增帳號、改角色 / 停用、重設密碼、刪除 | 管理員 |
| 系統 | 📜 稽核紀錄 | 依使用者 / 動作 / 日期查詢，匯出 CSV | 管理員 |

> 💡 頁面之間的連結用 `st.page_link` / `st.switch_page`，不會整頁重新載入；
> 請不要在頁面裡用 HTML `<a href>` 連站內頁面，那會開新的 session、使用者得重新登入（見 `web/nav.py`）。

**第一次升級到 v3 之後建議的操作順序**：

1. 用 `.env` 的救援帳號登入 →「使用者管理」為每位使用者建立個人帳號（稽核紀錄才分得出是誰）
2.「警報規則」展開「通知頻道」，確認 `.env` 設定的通知方式後按「發送測試通知」
3. 為重要感測器建立警報規則（HH / LL、延遲、遲滯）；只需要簡單上下限的，維持在感測器階層設定即可
4.「系統狀態」確認採集服務運作中、各 OPC UA Server 監控點數正確

---

## 📊 資料庫表格設計（僅列出 v2 新增/變更欄位，完整欄位見 SQL migration 檔）

### `sensors`（🆕 v2 新增欄位）

| 欄位 | 型態 | 說明 |
| --- | --- | --- |
| `opcua_sampling_interval_ms` | INTEGER | OPC UA 訂閱該點位的取樣頻率（毫秒），NULL = 沿用 Server 層級預設值 |
| `opcua_deadband_type` | VARCHAR(20) | `none` / `percent` / `absolute`，伺服器端 DataChangeFilter 判斷方式 |
| `opcua_deadband_value` | NUMERIC | 搭配 `opcua_deadband_type` 使用的門檻值 |
| `upload_condition` | VARCHAR(20) | `always` / `on_change` / `threshold_percent` / `threshold_absolute`，新建感測器預設 `threshold_percent`。**累計型計數器請用 `on_change`**，見上方第 4 節的警告 |
| `upload_threshold` | NUMERIC | 搭配 `upload_condition` 使用的門檻值（百分比數字或絕對值）。`always` / `on_change` 時不生效；為 NULL 時程式會 fallback（`threshold_percent` → 1%、`threshold_absolute` → 0），`010` 已把門檻型的 NULL 補成顯式的 `1` |

---

## 🎛️ Modbus TCP 參數解析

### Byte Order / Word Order 速查表

| 工業俗稱 | `byte_order` | `word_order` | 備註 |
| --- | --- | --- | --- |
| Big Endian (ABCD) | BIG | BIG | Modbus 官方標準 |
| Word Swap (CDAB) | BIG | LITTLE | 🌟 台灣電表最常見！ |
| Byte Swap (BADC) | LITTLE | BIG | - |
| Little Endian (DCBA) | LITTLE | LITTLE | 部分歐美設備 |

---

## 🔧 模組說明

| 模組 | 說明 |
| --- | --- |
| `collector/run_modbus_collector.py` | Modbus 資料採集主程式，多設備併發連線 |
| `collector/run_s7_collector.py` | TIA/S7 資料採集主程式，多 PLC 併發連線 |
| `collector/run_opcua_collector.py` | OPC UA 一次性完整瀏覽採集（獨立測試腳本，`main.py` 平時走訂閱服務） |
| `services/opcua_subscription_service.py` | 🆕 OPC UA 常駐訂閱服務：逐感測器取樣頻率分組、伺服器端 deadband、只訂閱已綁定點位、統一維護迴圈、可調連線穩定性參數 |
| `protocols/s7_protocol.py` | 西門子 S7 協議封裝（`snap7`） |
| `protocols/modbus_protocol.py` | Modbus TCP 協議封裝（`pymodbus`） |
| `protocols/opcua_protocol.py` | 🆕 OPC UA 協議封裝（`asyncua`），連線逾時可由 `OPCUA_CLIENT_TIMEOUT` 調整 |
| `parsers/plc_parser.py` | S7 位元組解析、線性縮放 |
| `parsers/encoder.py` | Modbus Big/Little Endian 編解碼 |
| `data_layer/db_connector.py` | PostgreSQL 連線池（`ThreadedConnectionPool`） |
| `data_layer/batch_updater.py` | 即時層批量寫入（`execute_values` UPDATE/UPSERT） |
| `data_layer/timeseries_writer.py` | 🆕 `SensorReadingWriter` 單例：統一週期性寫入排程 + 逐感測器 `upload_condition` 判斷，三協議共用，執行緒安全 |
| `messaging/mqtt_publisher.py` | MQTT 發送器，增量比對、心跳全量上傳、自動重連 |
| `services/alarm/engine.py` | 🆕 警報引擎：讀記憶體最新值判斷規則、通訊中斷偵測、寫入 `alarm_events` |
| `services/alarm/rules.py` | 🆕 警報判斷純邏輯（HH/H/L/LL/EQ/NE、遲滯、延遲觸發、訊息格式） |
| `services/alarm/notifier.py` | 🆕 Webhook / Email / Telegram 通知（背景佇列、重試） |
| `services/status_reporter.py` | 🆕 `service_status` 心跳與執行統計回報 |
| `core/config.py` / `core/auth.py` / `core/audit.py` | 🆕 .env 讀取、帳號與角色、操作稽核 |
| `web/` | 🆕 網頁後台各頁面（見上方「網頁後台操作說明」） |
| `main.py` | 主程式入口，依 `.env` 開關決定啟動哪些協議，並啟動警報引擎與心跳回報 |
| `admin_app.py` | 網頁後台入口（Streamlit）：登入、依角色組出導覽選單、側邊欄狀態 |
| `run_all.py` | 同時啟動並監護 `main.py` 與 `admin_app.py`（崩潰自動重啟、SIGTERM 正確收尾） |
| `sql/` | 資料庫 migration，執行順序與編號斷層說明見 [`sql/README.md`](../sql/README.md) |

---

## 🩺 常見問題排查

| 現象 | 可能原因 |
| --- | --- |
| 設定 `MODBUS_ENABLED=false` 後網頁還看得到 Modbus 分頁 | 確認用的是新版 `admin_app.py`（分頁清單依開關動態組出），並確認 `.env` 真的被載入（`ADMIN_PORT` 等其他變數有沒有生效可以交叉驗證） |
| `sensor_readings` 好像變比較慢才有新資料 | 這是預期行為：v2 改成 `SENSOR_READING_FLUSH_INTERVAL` 固定週期統一寫入（預設 60 秒），不是採集到就馬上寫；調小這個值可以縮短延遲，但太小意義不大且會增加 DB 負擔 |
| 感測器調了 `upload_condition` 沒有立即生效 | 最慢下一個 `SENSOR_READING_FLUSH_INTERVAL` 週期才會套用新設定（背景排程每輪都會重新讀一次 `sensors` 表），不是即時的 |
| **某個感測器完全沒有新資料，但 OPC UA 連線正常、`opcua_tags.current_data` 也一直在變** | 先查它的 `upload_condition`。若是**累計型計數器**套用 `threshold_percent`，基準值百萬等級時「變化 1%」要累積四十幾天，判斷永遠不成立，而且不會有任何錯誤 log。改成 `on_change`（或跑 `sql/010`）。驗證方式：`SELECT upload_condition, upload_threshold FROM sensors WHERE sensor_id=<id>;` 再比對 `sensor_readings` 最後一筆的值與 `opcua_tags` 目前值差幾 %。心跳保底生效後，這種點位至少仍會每 `SENSOR_HEARTBEAT_INTERVAL` 有一筆，不會完全消失 |
| **資料庫裡的時間戳比實際時間少 8 小時**（`opcua_tags.last_update` 看起來像早就停止更新） | 容器沒有設定 `TZ` 時跑在 UTC，程式若用 naive 的 `datetime.now()` 寫進 `timestamptz` 欄位，PostgreSQL 會照 session 的 `+08` 解讀，結果整批偏移。程式端已全部改用 `datetime.now().astimezone()`；若自行新增寫入時間的程式碼，**務必也帶時區**。快速確認：`docker exec <容器> date` 跟主機 `date` 對一下 |
| `sensors.upload_threshold` / `opcua_deadband_value` 存檔報欄位不存在 | 尚未執行 `sql/006_opcua_upgrade.sql` / `sql/007_opcua_deadband.sql`，補跑 migration |
| 在網頁上改感測器的上傳條件，存檔報 `violates check constraint "chk_sensors_upload_condition"` | 資料庫套用的是 `006` 的舊版本，該版 CHECK 只允許 `always` / `on_change` / `threshold`，擋掉了現在使用的 `threshold_percent` / `threshold_absolute`。重跑最新版 `sql/006_opcua_upgrade.sql` 即可（會自動把遺留的 `threshold` 轉成 `threshold_absolute` 再換上新的 constraint） |
| OPC UA log 出現 `Revised values returned differ from subscription values` | 正常訊息，代表伺服器端修改了實際生效的訂閱參數（常見於固定內部更新週期的設備，例如收到 `RevisedPublishingInterval=1000.0` 代表該設備不管你要求多慢，都用自己的 1 秒週期）。這種設備調整 `opcua_sampling_interval_ms` 不會真的降低它的負載，改用 `opcua_deadband_type=absolute` 從伺服器端過濾雜訊才有實際效果 |
| OPC UA 連線頻繁斷線重連，但重連都很快成功 | 通常是網路品質問題，不是程式邏輯錯誤；可以調高 `OPCUA_CLIENT_TIMEOUT` / `OPCUA_HEARTBEAT_MAX_FAILURES` 減少誤判次數、降低 log 噪音，但無法讓底層網路本身變穩定；若斷線頻率有規律性（例如每隔固定分鐘），較可能是網路設備（NAT timeout 等）問題 |
| 設定了 `opcua_deadband_type=percent` 但沒有效果 | Percent deadband 依賴節點是否設定 EURange（工程量測範圍），很多設備（尤其協議轉換盒）不會主動配置，導致此設定被忽略；改用 `absolute` |
| 未綁定感測器的 OPC UA 點位，`current_data` 都不會更新 | v2 起，只有已綁定 `sensor_id` 的點位才會持續訂閱更新數值，這是為了省頻寬的預期行為；未綁定的點位可以按「🚀 立即瀏覽並寫入資料庫」手動刷新一次快照，或直接綁定它 |
| OPC UA Server 剛新增/剛啟動，log 顯示「沒有已綁定感測器的點位，暫不建立訂閱」 | 正常訊息，不是錯誤；到「感測器階層管理」把該 Server 底下至少一個點位綁定 `sensor_id`，最慢 5 秒內會自動建立訂閱 |
| `permission denied for table sensor_readings` | DB 使用者權限不足，用管理員帳號補 `GRANT`，並設定 `ALTER DEFAULT PRIVILEGES`；PostgreSQL 的 GRANT 對表和序列 (sequence) 是分開的 |
| `run_all.py` 啟動報 `FileNotFoundError: ... 'streamlit'` | 虛擬環境本身沒裝 streamlit，或沒用 `sys.executable -m streamlit` 啟動 |
| 🆕 網頁顯示「採集服務 無回應」 | `main.py` 超過 60 秒沒寫入 `service_status` 心跳：程序已停止 / 當機，或與資料庫斷線。查容器狀態與 log；`run_all.py` 會自動重啟崩潰的 main.py |
| 🆕 警報中心顯示「尚未建立警報資料表」 | 尚未執行 `sql/011_alarm_management.sql`；跑完後重啟 main.py，警報引擎會自動開始運作 |
| 🆕 設定了警報規則但一直沒有警報 | 依序檢查：「系統狀態」警報引擎規則數是否包含它（規則改完約 15 秒套用）；該感測器的點位是否已綁定且連線正常（來源斷線時不做數值判斷）；有設「延遲觸發」的話，條件要**連續**成立那麼久 |
| 🆕 收不到警報通知 | 「警報規則 → 通知頻道」按「發送測試通知」看錯誤訊息；確認 `ALARM_NOTIFY_MIN_PRIORITY` 有涵蓋該警報等級；`.env` 改完要重啟 main.py |
| 🆕 從總覽點「查看趨勢」後要求重新登入 | 不應發生；若自行修改頁面，請用 `st.page_link` / `st.switch_page` 而不是 HTML 連結（見 `web/nav.py`） |
| 🆕 v3.1 側邊欄出現「📦 本機緩存 N 筆待補寫」 | 資料庫寫不進去（斷線、磁碟滿、權限），資料暫存在本機，恢復後自動補寫，不需要處理。數量一直增加代表資料庫一直寫不進去，查「系統狀態」的最近錯誤 |
| 🆕 v3.1 趨勢圖出現紅色 ▼、線條斷開 | 那個時間點通訊中斷或設備回報品質不良（`sensor_readings.quality` 3 / 4），斷開的區間沒有可信資料。滑鼠移到 ▼ 上看是哪一種 |
| 🆕 v3.1 Modbus 讀取回「例外碼 02：位址不存在」 | 最常見是位址差 1：手冊寫 40001，協議位址是 0。用「Modbus 線上調適」開啟 Modicon 位址輸入直接打 40001 試；也可能是讀取數量跨到設備沒有定義的暫存器 |
| 🆕 v3.1 Modbus 數值很怪（1e-38、上億） | 位元組順序設錯。到「Modbus 線上調適 → 點位驗證」，看四種順序哪一欄是合理的值，直接套用；台灣電表最常見是 CDAB |
| 🆕 v3.1 Modbus 某個站號一直「無回應」 | 站號錯誤、設備關機、RS-485 A/B 接反或鮑率 / 同位設定不同。用「站號掃描」確認匯流排上實際有哪些站號 |
| 🆕 v3.4 Python 計算點顯示「Python 腳本未啟用」 | `.env` 設定 `CALC_SCRIPTS_ENABLED=true` 並重新啟動服務（main.py 與網頁都要） |
| 🆕 v3.4 Python 計算點「執行逾時」 | 腳本超過 `CALC_SCRIPT_TIMEOUT` 秒；檢查迴圈。逾時後會暫停 30 秒起再重試，修改腳本儲存後約 15 秒套用 |
| 🆕 v3.3 S7「CPU 拒絕存取（function refused）」 | S7-1200/1500 沒開 PUT/GET：TIA Portal → CPU 屬性 → 防護與安全 → 連線機制 → 勾選「允許來自遠端物件的 PUT/GET 通訊存取」並下載 |
| 🆕 v3.3 S7「位址超出範圍」或讀到的值不對 | DB 是「最佳化的區塊存取」（沒有固定 offset）：DB 屬性取消勾選後重新編譯下載；用「S7 線上調適 → 記憶體讀取」對照 TIA 的 offset |
| 🆕 v3.3 S7-300 連不上 | Slot 要設 2（S7-1200/1500 是 1） |
| 🆕 v3.3 計算點的今日用量（delta）數字怪怪的 | delta 以週期開始時「之前最後一筆」歷史值當基準；計數器有歸零或換表時，當期會出現負值或跳動，屬於資料本身的問題 |
| 🆕 v3.2 計算點一直是「⚫ 斷線」 | 訊息欄會寫是哪個輸入感測器沒資料 / 斷線 / 品質不良。計算點不會用舊值硬算，輸入恢復後自動恢復 |
| 🆕 v3.2 排程報表沒有寄出 | 「報表 → 排程寄送」看「上一期」狀態與錯誤；按「立即寄送測試」可直接看到 SMTP / Webhook 的錯誤訊息。排程由 main.py 執行，採集服務要在運作中 |
| 🆕 v3.2 匯入時「感測器已綁定其他點位」 | 同一個感測器只能綁一個點位（含計算點）。要搬移綁定，在同一個檔案裡把原本的點位清空、新點位填上即可（同一次匯入內允許搬移） |
| 🆕 v3.1 閒置登出太頻繁 | 調整 `ADMIN_IDLE_TIMEOUT_MIN`；控制室監看螢幕請用「檢視者」帳號登入（預設不受閒置登出限制） |
| 🆕 數值不變的感測器在「資料斷更」裡出現 | v2 的已知問題（OPC UA 數值不變不會推播，心跳永遠不觸發），v3 已修正：連線正常時每 `SENSOR_HEARTBEAT_INTERVAL` 秒會以目前時間補寫一筆 |

---

## 📚 文件導覽

| 文件 | 內容 |
| --- | --- |
| [`README.md`](../README.md) | 精簡介紹與快速開始 |
| **docs/MANUAL.md**（本檔） | 功能細節、安裝設定、執行架構、網頁操作、常見問題排查 |
| [`README_DB.md`](../README_DB.md) | 資料庫 schema 參考：即時層四張表的完整欄位定義、時序層階層表、寫入規則 |
| [`sql/README.md`](../sql/README.md) | migration 的執行順序、各檔用途、編號 002~005 斷層的原因 |
| [`UPGRADE_NOTES_v2.md`](../UPGRADE_NOTES_v2.md) | v1 → v2 的行為變化、部署步驟、已知取捨 |
| [`UPGRADE_NOTES_v3.md`](../UPGRADE_NOTES_v3.md) | 🆕 v2 → v3 的新功能、行為變化、升級步驟 |
| [`todo.md`](../todo.md) | 待辦事項、已知技術債、下一階段建議 |
