-- ============================================================
-- 024_table_column_comments.sql
-- ============================================================
-- 目的：
--   補齊 sql/000 ~ 023 建立資料表時漏掉的 COMMENT ON（中文說明用途、單位、來源、可用值）：
--     * 沒有表註解的表：sites、production_lines、devices、sensors、sensor_readings、audit_log
--     * 沒有欄位註解的欄位：即時層四張表、階層表、警報、使用者 / 稽核 / 服務狀態、
--       設備範本、計算點、排程報表
--     * 沒有註解的具名索引（idx_* / uq_*）
--   已經有註解的欄位（例如 tia_scada."offset"、modbus_scada.transport、sensors.upload_condition…）
--   不在這裡重寫，避免兩個來源說法不一致。
--
--   另外把 alarm_events、calculated_points 的表註解改寫成「原句 + PME 依賴說明」；
--   app_users 的表註解 sql/023 已經寫了 PME，這裡不動。
--
--   與 PME（能源管理系統）共用的表：sensors、sensor_readings、alarm_events、app_users、
--   audit_log、calculated_points。PME 依賴這些表的欄位名稱與語意，改欄位前依
--   docs/PME_INTERFACE.md 第 5 節先通知 PME。
--
-- 內容只有 COMMENT ON，不改任何結構、不動資料，也不需要授權變更。
--   * COMMENT ON 是覆寫語意，可重複執行（idempotent）。
--   * 整支包在單一交易裡：任何一句失敗（例如欄位不存在）就全部不套用。
--   * 索引註解用 DO 區塊逐一檢查，正式庫若沒有該索引只印 NOTICE 略過，不會報錯。
--
-- 執行方式（需資料表擁有者或 superuser，目前擁有者是 eric）：
--   psql -h <DB_HOST> -U <資料表擁有者> -d <DB_NAME> -f sql/024_table_column_comments.sql
--
-- 回滾：不需要。註解不影響功能；要還原單一註解可用 COMMENT ON ... IS NULL。
-- ============================================================

\set ON_ERROR_STOP on

BEGIN;

-- ------------------------------------------------------------
-- 1. sites：廠區（階層最上層）
-- ------------------------------------------------------------
COMMENT ON TABLE public.sites IS
    '階層最上層：廠區。廠區 → 產線 → 設備 → 感測器。由網頁「感測器階層管理」或批次匯入維護。'
    'PME 以 sensors → devices → production_lines → sites 的 JOIN 取得廠區名稱。';
COMMENT ON COLUMN public.sites.site_id IS '廠區主鍵（流水號，自動產生）';
COMMENT ON COLUMN public.sites.site_name IS '廠區名稱（人看的名稱）';
COMMENT ON COLUMN public.sites.location IS '廠區位置 / 地址說明（選填，自由文字，僅供人閱讀）';

-- ------------------------------------------------------------
-- 2. production_lines：產線
-- ------------------------------------------------------------
COMMENT ON TABLE public.production_lines IS
    '階層第二層：產線，屬於某個廠區（sites）。由網頁「感測器階層管理」或批次匯入維護。PME 讀取其名稱。';
COMMENT ON COLUMN public.production_lines.line_id IS '產線主鍵（流水號，自動產生）';
COMMENT ON COLUMN public.production_lines.site_id IS '所屬廠區，對應 sites.site_id（FK，無 ON DELETE 動作，廠區底下有產線時不可刪）';
COMMENT ON COLUMN public.production_lines.line_name IS '產線名稱（人看的名稱）';

-- ------------------------------------------------------------
-- 3. devices：設備
-- ------------------------------------------------------------
COMMENT ON TABLE public.devices IS
    '階層第三層：設備，屬於某條產線（production_lines），底下掛感測器（sensors）。'
    '由網頁「感測器階層管理」、批次匯入或設備範本套用時建立。PME 讀取 device_code、device_name、device_nickname、line_id。';
COMMENT ON COLUMN public.devices.device_id IS '設備主鍵（流水號，自動產生），sensors.device_id 指向這裡';
COMMENT ON COLUMN public.devices.line_id IS '所屬產線，對應 production_lines.line_id（FK）；NULL = 尚未歸屬產線';
COMMENT ON COLUMN public.devices.device_code IS
    '設備編號（唯一、必填），例如 B03。人工輸入的識別碼，設備範本套用時感測器編號會組成「設備編號_後綴」。';
COMMENT ON COLUMN public.devices.device_name IS '設備名稱（選填，人看的名稱）';
COMMENT ON COLUMN public.devices.device_type IS
    '設備類型（選填，自由文字），例如 power_meter。設備範本會把它與 manufacturer 一起存進範本定義。';
COMMENT ON COLUMN public.devices.manufacturer IS '製造商（選填，自由文字）';
COMMENT ON COLUMN public.devices.install_date IS '安裝日期（選填，DATE）。目前採集程式與網頁沒有讀取，保留給人工 / 外部報表使用。';
COMMENT ON COLUMN public.devices.status IS
    '設備狀態，預設 active。批次匯入的下拉選項為 active / inactive（資料庫沒有 CHECK，實際值以資料為準）。'
    '僅為管理用標記，不影響採集。';

-- ------------------------------------------------------------
-- 4. sensors：感測器（階層最底層，所有時序資料的 key）
-- ------------------------------------------------------------
COMMENT ON TABLE public.sensors IS
    '感測器：階層最底層，sensor_readings 與警報規則、計算點都以 sensor_id 關聯。'
    '即時層的點位表（tia_scada / modbus_scada / opcua_tags）以 sensor_id 綁定到這裡。'
    '採集上傳條件、OPC UA 取樣 / deadband 設定也在這張表（sql/006、007）。'
    'PME 依賴此表：一律用 sensor_id 當 key（不要用 sensor_code），並讀取 sensor_type、unit、nickname、'
    'min/max_threshold、upload_condition、upload_threshold、device_id；改欄位名稱 / 型別、重編 sensor_id、'
    '換綁設備前須先通知 PME（docs/PME_INTERFACE.md）。';
COMMENT ON COLUMN public.sensors.sensor_id IS
    '感測器主鍵（流水號，自動產生）。sensor_readings 等所有關聯表的 key，PME 也用它當 key。';
COMMENT ON COLUMN public.sensors.device_id IS '所屬設備，對應 devices.device_id（FK）；NULL = 尚未歸屬設備';
COMMENT ON COLUMN public.sensors.sensor_code IS
    '感測器編號（唯一、必填）。網頁新增時自動取「目前最大數字編號 + 1」（data_layer/sensor_codes.py），'
    '設備範本產生的則為「設備編號_後綴」。MQTT Key 與計算點運算式也用它。';
COMMENT ON COLUMN public.sensors.sensor_type IS
    '感測器類型（必填，自由文字，無 CHECK）。網頁 / 匯入範本建議值：temperature、pressure、vibration、current、'
    'voltage、power、energy、flow、level、humidity、speed、torque、position、ph、conductivity、weight、count、'
    'status、gas、liquid、solid；實際值以現有資料為準。';
COMMENT ON COLUMN public.sensors.unit IS
    '工程單位（選填），例如 kW、kWh、°C、bar。sensor_readings.value 的單位以此為準；變更單位須通知 PME 重算。';
COMMENT ON COLUMN public.sensors.min_threshold IS
    '下限（與 value 同單位，選填）。警報引擎把它視為隱含的 L 規則（優先等級「中」）；'
    '.env 的 ALARM_USE_SENSOR_LIMITS=false 可關閉。NULL = 不設下限。資料庫本身不會自動觸發警報。';
COMMENT ON COLUMN public.sensors.max_threshold IS
    '上限（與 value 同單位，選填）。警報引擎把它視為隱含的 H 規則（優先等級「中」）；'
    '.env 的 ALARM_USE_SENSOR_LIMITS=false 可關閉。NULL = 不設上限。資料庫本身不會自動觸發警報。';

-- ------------------------------------------------------------
-- 5. sensor_readings：時序資料（TimescaleDB hypertable）
-- ------------------------------------------------------------
COMMENT ON TABLE public.sensor_readings IS
    '感測器時序讀值，TimescaleDB hypertable（時間欄位 reading_time），主鍵 (sensor_id, reading_time)。'
    '由採集主程式 data_layer/timeseries_writer.py 每 SENSOR_READING_FLUSH_INTERVAL（預設 60 秒）批次寫入，'
    '依 sensors.upload_condition 決定哪些感測器這一輪要寫；寫入用 ON CONFLICT DO NOTHING；'
    '資料庫斷線期間先存本機 spool，恢復後以原本的 reading_time 補寫。'
    '計算點（calculated_points）的結果也寫在這裡。'
    '壓縮：sql/013 啟用後，超過 30 天的 chunk 自動壓縮（segmentby sensor_id）；保留（retention）政策目前未啟用。'
    'PME 依賴此表（唯讀）：統計時有效資料條件為 (quality IS NULL OR quality < 3)；'
    '回補 / 刪除資料前須通知 PME 重算（docs/PME_INTERFACE.md）。';
COMMENT ON COLUMN public.sensor_readings.reading_id IS
    '流水號（BIGSERIAL）。不保證與時間順序一致，不要拿來當增量讀取的游標；請用 reading_time。';
COMMENT ON COLUMN public.sensor_readings.sensor_id IS '感測器，對應 sensors.sensor_id（FK）；與 reading_time 組成主鍵';
COMMENT ON COLUMN public.sensor_readings.reading_time IS
    '讀值時間（TIMESTAMPTZ），hypertable 的分割時間欄位。是採集端的牆上時鐘，不是設備時間戳；'
    '平常會晚 0 ~ 60 多秒才寫進來，spool 補寫時可能晚數小時到數天。';
COMMENT ON COLUMN public.sensor_readings.value IS
    '量測值（NUMERIC，單位依 sensors.unit）。存的是工程值（Modbus：解碼 → 線性換算 → 四捨五入到小數 4 位）；'
    '只能存數字，狀態字典轉出的文字不會寫進來。quality = 4（通訊中斷）時沿用最後一筆的值。';

-- ------------------------------------------------------------
-- 6. tia_scada：西門子 S7 點位設定 + 即時值
--    （area / bit_offset / rack / slot 與 "offset" 已有註解，不重寫；enabled / unit 在 sql/019 漏了，這裡補上）
-- ------------------------------------------------------------
COMMENT ON COLUMN public.tia_scada.id IS 'S7 點位主鍵（流水號，自動產生）';
COMMENT ON COLUMN public.tia_scada.name IS '點位名稱（必填）。同時是 MQTT Key 與增量上傳比對的基準，請避免重複。';
COMMENT ON COLUMN public.tia_scada.plc_ip IS 'S7 PLC 的 IP 位址（必填）。同一個 IP 的點位視為同一台 PLC，通訊中斷警報以 IP 彙總。';
COMMENT ON COLUMN public.tia_scada.db_number IS 'S7 DB 區塊號碼（必填）。area 不是 DB（M / I / Q）時不使用，網頁寫入 0。';
COMMENT ON COLUMN public.tia_scada.data_type IS
    'S7 資料型別（必填），例如 BOOL、BYTE、INT、WORD、DINT、DWORD、REAL、LREAL；完整清單見 protocols/s7_codec.py。';
COMMENT ON COLUMN public.tia_scada.plc_name IS 'PLC 設備名稱（必填，人看的名稱，用來辨識 / 分組，例如 PLC_MAIN）';
COMMENT ON COLUMN public.tia_scada.current_data IS
    '最新值（JSONB），格式 {"val": 123.45}。由 S7 採集程式回寫；讀取失敗時保留最後一次的值，只更新 plc_state。';
COMMENT ON COLUMN public.tia_scada.plc_state IS
    '連線 / 讀取狀態，由 S7 採集程式每輪回寫：ONLINE 正常、OFFLINE 通訊失敗、ERROR 該點位解析或讀取被拒絕。新增點位時預設 OFFLINE。';
COMMENT ON COLUMN public.tia_scada.last_update IS '最後一次成功讀取（plc_state = ONLINE）的時間；失敗時不更新';
COMMENT ON COLUMN public.tia_scada.enabled IS 'FALSE = 暫停採集這個點位（保留設定），預設 TRUE；S7 採集程式只讀 enabled 的點位';
COMMENT ON COLUMN public.tia_scada.unit IS 'S7 點位的工程單位（選填），例如 kW / °C。僅供顯示，不參與數值換算（與 modbus_scada.unit 一致）。';
COMMENT ON COLUMN public.tia_scada.sensor_id IS
    '綁定的感測器，對應 sensors.sensor_id（FK）。NULL = 未綁定，採集後不會寫進 sensor_readings。'
    '同一張表內一個感測器只能被一個點位綁定（UNIQUE，sql/022）。';

-- ------------------------------------------------------------
-- 7. modbus_scada：Modbus 點位設定 + 即時值
--    （unit / byte_order / word_order / transport / serial_settings / enabled 已有註解，不重寫）
-- ------------------------------------------------------------
COMMENT ON COLUMN public.modbus_scada.id IS 'Modbus 點位主鍵（流水號，自動產生）';
COMMENT ON COLUMN public.modbus_scada.name IS '點位名稱（必填），同時是 MQTT Key';
COMMENT ON COLUMN public.modbus_scada.plc_ip IS
    '設備 IP 位址（必填）。transport = tcp / rtu_over_tcp 時為 IP；transport = rtu 時填本機序列埠路徑（例如 /dev/ttyUSB0）。';
COMMENT ON COLUMN public.modbus_scada.plc_port IS 'TCP Port，預設 502。transport = rtu（序列埠）時不使用。';
COMMENT ON COLUMN public.modbus_scada.slave_id IS 'Modbus 站號 / Unit ID（1 ~ 247）';
COMMENT ON COLUMN public.modbus_scada.function_code IS '讀取用的 Modbus 功能碼：1 = Coils、2 = Discrete Inputs、3 = Holding Registers（預設）、4 = Input Registers';
COMMENT ON COLUMN public.modbus_scada.start_address IS '起始位址（暫存器或線圈）；多暫存器型別（32 / 64 位元）從這裡起連續讀取，位址表示法以設備手冊與採集程式約定為準';
COMMENT ON COLUMN public.modbus_scada.data_type IS
    '資料型別：bool、uint16、int16、uint32、int32、float32、uint64、int64、float64；'
    '舊寫法 word / int / dint / float / real / double 由 protocols/modbus_codec.py 對應到標準名稱。';
COMMENT ON COLUMN public.modbus_scada.raw_min IS '線性換算的原始值下限（設備回傳的數值）。raw_min / raw_max / eng_min / eng_max 任一為 NULL 時不換算；原始值超出範圍會被夾在邊界。';
COMMENT ON COLUMN public.modbus_scada.raw_max IS '線性換算的原始值上限（設備回傳的數值）';
COMMENT ON COLUMN public.modbus_scada.eng_min IS '線性換算的工程值下限（單位同 modbus_scada.unit / sensors.unit），對應 raw_min';
COMMENT ON COLUMN public.modbus_scada.eng_max IS '線性換算的工程值上限（單位同 modbus_scada.unit / sensors.unit），對應 raw_max';
COMMENT ON COLUMN public.modbus_scada.state_dictionary IS
    '狀態字典（選填，JSONB），把數值轉成文字，例如 {"0":"待機","8":"大火燃燒"}。轉出的文字只放在 current_data，不會寫進 sensor_readings。';
COMMENT ON COLUMN public.modbus_scada.current_value IS '最新的純數值（REAL，工程值，四捨五入到小數 4 位）；讀取失敗時保留最後一次的值';
COMMENT ON COLUMN public.modbus_scada.current_data IS
    '最新值（JSONB），格式 {"val": ...}；有對到 state_dictionary 時 val 是狀態文字，否則是數字。讀取失敗時保留最後一次的值。';
COMMENT ON COLUMN public.modbus_scada.plc_state IS
    '連線 / 讀取狀態，由 Modbus 採集程式每輪回寫：ONLINE 正常、OFFLINE 通訊失敗、ERROR 該點位被設備拒絕或解碼失敗。新增點位時預設 OFFLINE。';
COMMENT ON COLUMN public.modbus_scada.last_update IS '最後一次成功讀取（plc_state = ONLINE）的時間；失敗時不更新';
COMMENT ON COLUMN public.modbus_scada.sensor_id IS
    '綁定的感測器，對應 sensors.sensor_id（FK）。NULL = 未綁定，採集後不會寫進 sensor_readings。'
    '同一張表內一個感測器只能被一個點位綁定（UNIQUE，sql/022）。';

-- ------------------------------------------------------------
-- 8. opcua_servers：OPC UA Server 清單與連線參數
--    （resubscribe_requested / publish_interval_ms 已有註解，不重寫）
-- ------------------------------------------------------------
COMMENT ON COLUMN public.opcua_servers.id IS 'OPC UA Server 主鍵（流水號，自動產生）；opcua_tags.server_id、alarm_events.server_id 指向這裡';
COMMENT ON COLUMN public.opcua_servers.server_name IS 'Server 名稱（必填、唯一）。也用來組 MQTT Key，故不可重複。';
COMMENT ON COLUMN public.opcua_servers.ip IS 'OPC UA Server 的 IP 位址（必填）';
COMMENT ON COLUMN public.opcua_servers.port IS 'OPC UA Server 的 TCP Port，預設 4840';
COMMENT ON COLUMN public.opcua_servers.username IS '連線帳號；NULL 或空白 = 匿名連線';
COMMENT ON COLUMN public.opcua_servers.password IS
    '連線密碼（敏感資料，目前以明文儲存，採集程式直接讀來連線；username 為空時不使用）。不要在 log、報表、截圖或匯出檔中顯示；'
    '資料庫唯讀帳號 / 對外 API 不應開放此欄位。';
COMMENT ON COLUMN public.opcua_servers.security_policy IS '安全原則，網頁可選 None（預設）或 Basic256Sha256。注意：採集程式目前沒有憑證設定，設成 None 以外只會記警告並以預設方式嘗試連線（protocols/opcua_protocol.py）。';
COMMENT ON COLUMN public.opcua_servers.security_mode IS '訊息安全模式，網頁可選 None（預設）、Sign、SignAndEncrypt；與 security_policy 同樣目前沒有憑證支援，非 None 時只記警告（見 security_policy 說明）。';
COMMENT ON COLUMN public.opcua_servers.root_node_id IS '瀏覽起點的 NodeId，預設 i=85（Objects 資料夾）；指定更精確的節點可縮小瀏覽範圍';
COMMENT ON COLUMN public.opcua_servers.browse_depth IS '從 root_node_id 往下遞迴瀏覽的深度上限，預設 5（網頁允許 1 ~ 20）';
COMMENT ON COLUMN public.opcua_servers.enabled IS '是否啟用，預設 TRUE；FALSE 時訂閱服務不會連這台';
COMMENT ON COLUMN public.opcua_servers.conn_state IS
    '連線狀態，由 OPC UA 採集 / 訂閱服務回寫：ONLINE、OFFLINE、ERROR；剛建立未連線過為 UNKNOWN（預設）。OFFLINE 會觸發 server_offline 警報。';
COMMENT ON COLUMN public.opcua_servers.last_scan IS '最後一次瀏覽（掃描）完成並更新連線狀態的時間';
COMMENT ON COLUMN public.opcua_servers.last_error IS '最近一次連線 / 瀏覽失敗的錯誤訊息；成功時為 NULL。可能含 Server 位址等資訊，對外顯示前請留意。';

-- ------------------------------------------------------------
-- 9. opcua_tags：OPC UA 瀏覽出來的點位與最新值
--    （server_name 已有註解，不重寫）
--    註：scan 清單中的 "references" 不是欄位，是 server_id 的 REFERENCES 子句。
-- ------------------------------------------------------------
COMMENT ON COLUMN public.opcua_tags.id IS 'OPC UA 點位主鍵（流水號，自動產生）';
COMMENT ON COLUMN public.opcua_tags.server_id IS '所屬 OPC UA Server，對應 opcua_servers.id（FK，Server 刪除時點位一併刪除）';
COMMENT ON COLUMN public.opcua_tags.node_id IS
    'OPC UA NodeId（必填），例如 ns=2;s=Temp01。與 server_id 組成唯一鍵，瀏覽時以此 upsert。';
COMMENT ON COLUMN public.opcua_tags.browse_name IS 'OPC UA BrowseName（瀏覽名稱），由瀏覽時寫入';
COMMENT ON COLUMN public.opcua_tags.display_name IS 'OPC UA DisplayName（顯示名稱），由瀏覽時寫入';
COMMENT ON COLUMN public.opcua_tags.data_type IS 'OPC UA 變數型別（VariantType 名稱），由瀏覽時寫入';
COMMENT ON COLUMN public.opcua_tags.current_data IS
    '最新值（JSONB），格式 {"val": 123.45}。未綁定感測器的點位只停留在上次瀏覽的快照，不會持續更新。';
COMMENT ON COLUMN public.opcua_tags.quality IS 'OPC UA 資料品質：GOOD、UNCERTAIN、BAD（BAD 不計入來源存活，也不參與警報判斷）';
COMMENT ON COLUMN public.opcua_tags.plc_state IS
    '連線狀態：ONLINE / OFFLINE，預設 OFFLINE。只有「已綁定 sensor_id」的點位會隨 Server 連線狀態更新。';
COMMENT ON COLUMN public.opcua_tags.last_update IS '最後一次寫入最新值的時間（瀏覽快照或訂閱回寫）';
COMMENT ON COLUMN public.opcua_tags.sensor_id IS
    '綁定的感測器，對應 sensors.sensor_id（FK）。NULL = 未綁定：不會建立訂閱，也不會寫進 sensor_readings。'
    '同一張表內一個感測器只能被一個點位綁定（UNIQUE，sql/022）；瀏覽 upsert 刻意不覆蓋此欄位。';

-- ------------------------------------------------------------
-- 10. alarm_rules：警報規則
-- ------------------------------------------------------------
COMMENT ON COLUMN public.alarm_rules.rule_id IS '警報規則主鍵（流水號，自動產生）；alarm_events.rule_id 指向這裡';
COMMENT ON COLUMN public.alarm_rules.sensor_id IS '規則所屬感測器，對應 sensors.sensor_id（FK，感測器刪除時規則一併刪除）';
COMMENT ON COLUMN public.alarm_rules.alarm_type IS
    '警報類型：HH / H（數值 > setpoint 觸發）、L / LL（數值 < setpoint 觸發）、'
    'EQ（數值 = setpoint 觸發，例如故障碼）、NE（數值 ≠ setpoint 觸發，例如應為 1 的運轉訊號）';
COMMENT ON COLUMN public.alarm_rules.setpoint IS '設定值 / 門檻（單位同 sensors.unit）';
COMMENT ON COLUMN public.alarm_rules.deadband IS '遲滯（≥ 0，單位同 sensors.unit）：H / HH 要降到 setpoint - deadband 以下才恢復，L / LL 反之；避免在門檻附近反覆跳動';
COMMENT ON COLUMN public.alarm_rules.on_delay_sec IS '延遲觸發（秒，≥ 0）：條件要連續成立這麼多秒才發出警報，濾掉瞬間突波；0 = 立即';
COMMENT ON COLUMN public.alarm_rules.priority IS '優先等級（1 ~ 4）：1 緊急、2 高（預設）、3 中、4 低';
COMMENT ON COLUMN public.alarm_rules.message IS '附加訊息（選填，最長 200 字），例如處置方式；會帶入警報事件的 message';
COMMENT ON COLUMN public.alarm_rules.enabled IS '是否啟用，預設 TRUE；FALSE 時警報引擎略過此規則';
COMMENT ON COLUMN public.alarm_rules.created_at IS '建立時間';
COMMENT ON COLUMN public.alarm_rules.updated_at IS '最後修改時間（由網頁儲存時更新，沒有 trigger）';

-- ------------------------------------------------------------
-- 11. alarm_events：警報事件（表註解改寫為「原句 + PME 依賴說明」）
-- ------------------------------------------------------------
COMMENT ON TABLE public.alarm_events IS
    '警報事件。目前警報 = cleared_at IS NULL OR acked_at IS NULL。'
    '由警報引擎（services/alarm/engine.py）寫入，網頁「警報中心」確認（ACK）。'
    'PME 依賴此表（唯讀），欄位名稱 / 語意變更前須先通知 PME（docs/PME_INTERFACE.md）。';
COMMENT ON COLUMN public.alarm_events.event_id IS '警報事件主鍵（流水號，自動產生）';
COMMENT ON COLUMN public.alarm_events.alarm_key IS
    '警報來源的唯一識別：rule:<rule_id>、limit:<sensor_id>:H（或 L，來自 sensors 上下限）、server:<id>:offline、'
    'plc:<資料表>:<ip>:offline。同一個 alarm_key 同時只能有一筆未恢復事件（uq_alarm_events_active_key）。';
COMMENT ON COLUMN public.alarm_events.source_type IS '警報來源：sensor_rule（alarm_rules 規則）、sensor_limit（sensors 上下限）、server_offline（OPC UA Server 斷線）、device_offline（S7 / Modbus 設備通訊中斷）';
COMMENT ON COLUMN public.alarm_events.sensor_id IS '相關感測器，對應 sensors.sensor_id（FK，感測器刪除時設為 NULL）；通訊類警報為 NULL';
COMMENT ON COLUMN public.alarm_events.server_id IS '相關 OPC UA Server，對應 opcua_servers.id（FK，Server 刪除時設為 NULL）；僅 server_offline 有值';
COMMENT ON COLUMN public.alarm_events.rule_id IS '觸發的規則，對應 alarm_rules.rule_id（FK，規則刪除時設為 NULL）；僅 source_type = sensor_rule 有值';
COMMENT ON COLUMN public.alarm_events.alarm_type IS '警報類型：HH / H / L / LL / EQ / NE（見 alarm_rules.alarm_type），通訊類警報為 OFFLINE';
COMMENT ON COLUMN public.alarm_events.priority IS '優先等級：1 緊急、2 高、3 中、4 低；通訊類警報預設 2（.env ALARM_COMM_PRIORITY）';
COMMENT ON COLUMN public.alarm_events.message IS '警報訊息（給人看的文字）';
COMMENT ON COLUMN public.alarm_events.trigger_value IS '觸發當下的量測值（單位同 sensors.unit）；通訊類警報為 NULL';
COMMENT ON COLUMN public.alarm_events.setpoint IS '觸發當下的設定值 / 門檻（單位同 sensors.unit）；通訊類警報為 NULL';
COMMENT ON COLUMN public.alarm_events.raised_at IS '警報發生時間';
COMMENT ON COLUMN public.alarm_events.cleared_at IS '恢復正常的時間；NULL = 仍在發生';
COMMENT ON COLUMN public.alarm_events.clear_value IS '恢復當下的量測值（單位同 sensors.unit）';
COMMENT ON COLUMN public.alarm_events.acked_at IS '確認（ACK）時間；NULL = 尚未確認。已恢復但未確認的警報仍留在目前警報清單（ISA-18.2）。';
COMMENT ON COLUMN public.alarm_events.acked_by IS '確認者的帳號（app_users.username 或 .env 的救援帳號）';
COMMENT ON COLUMN public.alarm_events.ack_comment IS '確認時填的備註（選填）';

-- ------------------------------------------------------------
-- 12. app_users：網頁後台使用者（表註解 sql/023 已有，這裡只補欄位）
-- ------------------------------------------------------------
COMMENT ON COLUMN public.app_users.username IS
    '登入帳號（主鍵，最長 50）。區分大小寫；只允許英數與 _ . -，且不得等於 .env 的 ADMIN_USER。建立後不可改名。';
COMMENT ON COLUMN public.app_users.display_name IS '顯示名稱（選填，最長 100）';
COMMENT ON COLUMN public.app_users.password_hash IS
    '密碼雜湊（敏感資料），格式 pbkdf2_sha256$<迭代次數>$<salt_b64>$<hash_b64>，由 core/auth.py 產生；不存明文。不得對外顯示。';
COMMENT ON COLUMN public.app_users.role IS
    '角色，預設 viewer（CHECK：viewer / operator / engineer / admin）。由低到高：viewer 檢視、operator 加警報確認、'
    'engineer 加所有設定、admin 加使用者管理與稽核紀錄。';
COMMENT ON COLUMN public.app_users.enabled IS '帳號是否啟用，預設 TRUE；停用請設 FALSE（不刪除帳號）';
COMMENT ON COLUMN public.app_users.created_at IS '帳號建立時間（預設 now()）';
COMMENT ON COLUMN public.app_users.last_login IS '最後一次成功登入時間，登入流程更新（SCADA 與 PME 都會寫）；從未登入為 NULL';

-- ------------------------------------------------------------
-- 13. audit_log：操作稽核
-- ------------------------------------------------------------
COMMENT ON TABLE public.audit_log IS
    '操作稽核紀錄：誰、什麼時間、做了什麼、改了哪個對象（寫入端 core/audit.py，只新增不修改）。'
    '網頁設定變更、登入 / 登出、警報確認都會寫；採集主程式以 username = system 寫系統事件（如 spool.drained）。'
    'PME 共用此表：PME 寫入的 action 一律以 ems. 為前綴，並讀取 SCADA 的 spool.drained（自動重算）與設定變更事件（只標示）'
    '（docs/PME_INTERFACE.md）。';
COMMENT ON COLUMN public.audit_log.id IS '稽核紀錄主鍵（流水號，自動產生）';
COMMENT ON COLUMN public.audit_log.ts IS '事件發生時間（預設 now()）；設定變更事件的 ts 即設定生效時間（採集程式下次重載後才套用）';
COMMENT ON COLUMN public.audit_log.username IS '操作者帳號；系統事件為 system；登入失敗時為輸入的帳號（空白時為 (空白)）';
COMMENT ON COLUMN public.audit_log.action IS
    '動作代碼（最長 50），例如 login、login.failed、logout.idle、sensor.update、modbus.update、tia.update、'
    'alarm.ack、user.create、import.<entity>、spool.drained。PME 寫入的一律以 ems. 開頭（例如 ems.user.create）。';
COMMENT ON COLUMN public.audit_log.target IS '操作對象（選填，最長 200），例如 sensor:12、alarm_event:345、user:alice、sensor_readings';
COMMENT ON COLUMN public.audit_log.detail IS '事件細節（JSONB，選填）；設定變更通常是 {欄位: [舊值, 新值]}，格式依 action 而異（見 docs/PME_INTERFACE.md 第 3 節）';

-- ------------------------------------------------------------
-- 14. service_status：採集服務心跳
-- ------------------------------------------------------------
COMMENT ON COLUMN public.service_status.service_name IS '服務名稱（主鍵）；目前只有 collector（main.py）';
COMMENT ON COLUMN public.service_status.host IS '服務所在主機名稱（容器的 hostname）';
COMMENT ON COLUMN public.service_status.pid IS '服務的作業系統行程 ID';
COMMENT ON COLUMN public.service_status.version IS '採集程式版本（core/config.py 的 APP_VERSION，例如 3.4.0）';
COMMENT ON COLUMN public.service_status.started_at IS '本次啟動時間';
COMMENT ON COLUMN public.service_status.last_heartbeat IS '最後一次回報心跳的時間（約每 10 秒更新）；超過 60 秒沒更新，網頁視為採集服務停止';
COMMENT ON COLUMN public.service_status.info IS
    '各子系統執行統計（JSONB）：protocols、writer（寫入排程）、opcua（各 Server）、alarm（警報引擎）、main_loop 等，格式見 services/status_reporter.py；供網頁顯示用，鍵值可能隨版本增減。';

-- ------------------------------------------------------------
-- 15. device_templates：設備範本
-- ------------------------------------------------------------
COMMENT ON COLUMN public.device_templates.template_id IS '範本主鍵（流水號，自動產生）';
COMMENT ON COLUMN public.device_templates.name IS '範本名稱（必填、唯一）';
COMMENT ON COLUMN public.device_templates.description IS '範本說明（選填）';
COMMENT ON COLUMN public.device_templates.protocol IS '範本協議（CHECK）：none 只建設備與感測器、modbus 同時建 Modbus 點位、opcua 依節點樣式綁定已瀏覽的 OPC UA 點位；預設 none';
COMMENT ON COLUMN public.device_templates.definition IS '範本內容（JSONB，必填）：device、modbus、sensors（含 point 與 alarms），格式見 data_layer/templates.py';
COMMENT ON COLUMN public.device_templates.created_by IS '建立者帳號（選填）';
COMMENT ON COLUMN public.device_templates.created_at IS '建立時間';
COMMENT ON COLUMN public.device_templates.updated_at IS '最後修改時間（由網頁儲存時更新，沒有 trigger）';

-- ------------------------------------------------------------
-- 16. calculated_points：計算點（表註解改寫為「原句 + PME 依賴說明」）
--     （calc_state / kind / last_log 已有註解，不重寫）
-- ------------------------------------------------------------
COMMENT ON TABLE public.calculated_points IS
    '計算點：以運算式從其他感測器的即時值算出新感測器的值。'
    '結果寫進 sensor_id 指向的一般感測器（sensor_readings），與實體點位無差別。'
    'PME 依賴此表（唯讀）：自動判斷計量點時不納入計算點；已手動設為計量點的保留（例 829 / 830 熱量）；欄位變更前須先通知 PME。';
COMMENT ON COLUMN public.calculated_points.calc_id IS '計算點主鍵（流水號，自動產生）';
COMMENT ON COLUMN public.calculated_points.sensor_id IS
    '結果感測器，對應 sensors.sensor_id（FK，UNIQUE：一個感測器只能是一個計算點的結果；感測器刪除時計算點一併刪除）。此感測器不應再綁定實體點位。';
COMMENT ON COLUMN public.calculated_points.expression IS '運算式（kind = expression，語法見 services/calc/expression.py）或 Python 腳本原始碼（kind = python）';
COMMENT ON COLUMN public.calculated_points.description IS '說明（選填）';
COMMENT ON COLUMN public.calculated_points.enabled IS '是否啟用，預設 TRUE；FALSE 時計算引擎略過';
COMMENT ON COLUMN public.calculated_points.current_value IS '最新計算結果（DOUBLE PRECISION，單位同結果感測器 sensors.unit）；由計算引擎每輪回寫，計算失敗時保留最後一次的值';
COMMENT ON COLUMN public.calculated_points.quality IS '結果品質：GOOD / UNCERTAIN（與 opcua_tags.quality 同寫法），由計算引擎依輸入品質回寫';
COMMENT ON COLUMN public.calculated_points.state IS '計算狀態：ONLINE 正常、OFFLINE 輸入無資料、ERROR 運算式 / 腳本錯誤；預設 OFFLINE，由計算引擎回寫';
COMMENT ON COLUMN public.calculated_points.last_error IS '最近一次計算錯誤訊息；正常時為 NULL';
COMMENT ON COLUMN public.calculated_points.last_update IS '最近一次回寫計算結果的時間';
COMMENT ON COLUMN public.calculated_points.created_by IS '建立者帳號（選填）';
COMMENT ON COLUMN public.calculated_points.created_at IS '建立時間';
COMMENT ON COLUMN public.calculated_points.updated_at IS '最後修改時間（由網頁儲存時更新，沒有 trigger）';

-- ------------------------------------------------------------
-- 17. report_schedules：排程報表
-- ------------------------------------------------------------
COMMENT ON COLUMN public.report_schedules.schedule_id IS '排程主鍵（流水號，自動產生）';
COMMENT ON COLUMN public.report_schedules.name IS '排程名稱（必填），會出現在報表通知中';
COMMENT ON COLUMN public.report_schedules.enabled IS '是否啟用，預設 TRUE';
COMMENT ON COLUMN public.report_schedules.frequency IS '頻率（CHECK）：daily 每天寄前一天、weekly 每週寄上週一 ~ 週日、monthly 每月寄上個月';
COMMENT ON COLUMN public.report_schedules.send_time IS '寄送時間（TIME，預設 07:00），為 .env SCADA_TIMEZONE 的當地時間';
COMMENT ON COLUMN public.report_schedules.weekday IS 'weekly 用：星期幾寄送，0 = 週一 ~ 6 = 週日（CHECK 0 ~ 6）；其他頻率不使用';
COMMENT ON COLUMN public.report_schedules.day_of_month IS 'monthly 用：每月幾號寄送（CHECK 1 ~ 28，避開月底差異）；其他頻率不使用';
COMMENT ON COLUMN public.report_schedules.granularity IS '報表時間粒度（CHECK）：auto（daily 用 1 hour、weekly / monthly 用 1 day）、1 hour、1 day、1 month';
COMMENT ON COLUMN public.report_schedules.metrics IS '報表統計項目（TEXT 陣列，預設 {avg}）：avg / min / max / last / delta';
COMMENT ON COLUMN public.report_schedules.device_codes IS '報表涵蓋的設備編號清單（TEXT 陣列，對應 devices.device_code），預設空陣列；與 sensor_codes 合併決定涵蓋的感測器';
COMMENT ON COLUMN public.report_schedules.sensor_codes IS '報表涵蓋的感測器編號清單（TEXT 陣列，對應 sensors.sensor_code），預設空陣列；與 device_codes 合併決定涵蓋的感測器';
COMMENT ON COLUMN public.report_schedules.recipients IS '收件人 Email 清單（TEXT 陣列）；空陣列 = 使用 .env 的 ALARM_EMAIL_TO';
COMMENT ON COLUMN public.report_schedules.send_email IS '是否以 Email 寄送，預設 TRUE（SMTP 設定沿用警報通知）';
COMMENT ON COLUMN public.report_schedules.send_webhook IS '是否送到 Webhook，預設 FALSE';
COMMENT ON COLUMN public.report_schedules.last_run_at IS '最後一次「排定」的執行時間（不是實際寄出時間）；排程器據此判斷下一期是否到期，也用來避免重複寄送';
COMMENT ON COLUMN public.report_schedules.last_period IS '最近一次處理的報表期間文字，例如 2026-10-01 或 2026-10-01 ~ 2026-10-07';
COMMENT ON COLUMN public.report_schedules.last_status IS '最近一次結果：OK 已寄出、RETRYING 失敗重試中（每 10 分鐘一次）、ERROR 重試達上限後放棄這一期';
COMMENT ON COLUMN public.report_schedules.last_error IS '最近一次失敗的錯誤訊息；成功時為 NULL';
COMMENT ON COLUMN public.report_schedules.created_by IS '建立者帳號（選填）';
COMMENT ON COLUMN public.report_schedules.created_at IS '建立時間';
COMMENT ON COLUMN public.report_schedules.updated_at IS '最後修改時間（由網頁儲存時更新，沒有 trigger）';

-- ------------------------------------------------------------
-- 18. 具名索引（idx_* / uq_*）
--     用 DO 區塊逐一檢查：索引不存在只印 NOTICE 略過。
--     主鍵 / UNIQUE constraint 自動產生的索引（*_pkey、*_key）不加註解，見其所屬的表與欄位註解。
-- ------------------------------------------------------------
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
            ('idx_opcua_tags_server_id',   '依 Server 查詢 / 刪除其底下點位（opcua_tags.server_id）'),
            ('idx_opcua_tags_name',        '依 server_name 查詢 opcua_tags（組 MQTT Key 時使用）'),
            ('idx_tia_sensor_id',          '由 sensor_id 反查 S7 點位（tia_scada.sensor_id）'),
            ('idx_modbus_sensor_id',       '由 sensor_id 反查 Modbus 點位（modbus_scada.sensor_id）'),
            ('idx_opcua_sensor_id',        '由 sensor_id 反查 OPC UA 點位（opcua_tags.sensor_id）'),
            ('idx_readings_sensor_time',   '趨勢 / 報表「依感測器查時間區間」，(sensor_id, reading_time DESC)；與主鍵 (sensor_id, reading_time) 前綴重疊，主要差別是時間倒序'),
            ('idx_sensors_nickname',       '感測器下拉選單依暱稱（nickname）排序 / 搜尋'),
            ('idx_alarm_rules_sensor',     '依感測器載入警報規則（alarm_rules.sensor_id）'),
            ('uq_alarm_events_active_key', '部分唯一索引（WHERE cleared_at IS NULL）：同一個 alarm_key 同時只能有一筆未恢復事件，警報引擎重複判斷時 ON CONFLICT DO NOTHING 依此略過'),
            ('idx_alarm_events_raised',    '警報歷史依發生時間倒序瀏覽（raised_at DESC）'),
            ('idx_alarm_events_sensor',    '查詢單一感測器的警報歷史（sensor_id, raised_at DESC）'),
            ('idx_alarm_events_unacked',   '部分索引（WHERE acked_at IS NULL）：快速列出尚未確認的警報'),
            ('idx_audit_log_ts',           '稽核紀錄依時間倒序瀏覽（ts DESC）'),
            ('idx_audit_log_user',         '稽核紀錄依操作者查詢（username, ts DESC）'),
            ('uq_opcua_tags_sensor_id',    '保證一個感測器只被一個 OPC UA 點位綁定（UNIQUE sensor_id，延遲到交易結束才檢查；NULL 不受限），sql/022'),
            ('uq_modbus_scada_sensor_id',  '保證一個感測器只被一個 Modbus 點位綁定（UNIQUE sensor_id，延遲到交易結束才檢查；NULL 不受限），sql/022'),
            ('uq_tia_scada_sensor_id',     '保證一個感測器只被一個 S7 點位綁定（UNIQUE sensor_id，延遲到交易結束才檢查；NULL 不受限），sql/022')
        ) AS t(idx_name, idx_comment)
    LOOP
        IF to_regclass(format('public.%I', r.idx_name)) IS NOT NULL THEN
            EXECUTE format('COMMENT ON INDEX public.%I IS %L', r.idx_name, r.idx_comment);
        ELSE
            RAISE NOTICE '索引 % 不存在，略過註解', r.idx_name;
        END IF;
    END LOOP;
END $$;

COMMIT;
