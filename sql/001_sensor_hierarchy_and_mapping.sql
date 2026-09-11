-- ============================================================
-- 001_sensor_hierarchy_and_mapping.sql
-- 目的：
--   1. 建立 README_DB.md 設計的階層資料表（若尚未建立）
--   2. 在既有的 tia_scada / modbus_scada / opcua_tags 三張「採集設定表」
--      上加一個 sensor_id 欄位，把每個點位掛到 sensors 階層底下
--   3. 之後採集程式會依 sensor_id 把數值寫進 sensor_readings（時序表）
--
-- 可重複執行（皆有 IF NOT EXISTS 防呆），請先在測試環境跑過一次再上正式庫。
-- ============================================================

-- 1. TimescaleDB 擴充套件
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- 2. 階層表：廠區 -> 產線 -> 設備 -> 感測器 -> 感測數據
CREATE TABLE IF NOT EXISTS sites (
    site_id     SERIAL PRIMARY KEY,
    site_name   VARCHAR(100) NOT NULL,
    location    VARCHAR(200)
);

CREATE TABLE IF NOT EXISTS production_lines (
    line_id     SERIAL PRIMARY KEY,
    site_id     INTEGER REFERENCES sites(site_id),
    line_name   VARCHAR(100) NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    device_id       SERIAL PRIMARY KEY,
    line_id         INTEGER REFERENCES production_lines(line_id),
    device_code     VARCHAR(50) UNIQUE NOT NULL,
    device_name     VARCHAR(100),
    device_type     VARCHAR(50),
    manufacturer    VARCHAR(100),
    install_date    DATE,
    status          VARCHAR(20) DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS sensors (
    sensor_id       SERIAL PRIMARY KEY,
    device_id       INTEGER REFERENCES devices(device_id),
    sensor_code     VARCHAR(50) UNIQUE NOT NULL,
    sensor_type     VARCHAR(50) NOT NULL,
    unit            VARCHAR(20),
    min_threshold   NUMERIC,
    max_threshold   NUMERIC
);

CREATE TABLE IF NOT EXISTS sensor_readings (
    reading_id      BIGSERIAL,
    sensor_id       INTEGER NOT NULL REFERENCES sensors(sensor_id),
    reading_time    TIMESTAMPTZ NOT NULL,
    value           NUMERIC NOT NULL,
    PRIMARY KEY (sensor_id, reading_time)
);

-- 轉成 hypertable（若已經是 hypertable，if_not_exists 會直接跳過不報錯）
SELECT create_hypertable(
    'sensor_readings', 'reading_time',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_readings_sensor_time
    ON sensor_readings (sensor_id, reading_time DESC);

-- 3. 把既有三張採集設定表掛上 sensor_id
--    （NULL 代表這個點位還沒對應到 sensors 階層，採集程式會自動略過寫入 sensor_readings）
ALTER TABLE tia_scada
    ADD COLUMN IF NOT EXISTS sensor_id INTEGER REFERENCES sensors(sensor_id);

ALTER TABLE modbus_scada
    ADD COLUMN IF NOT EXISTS sensor_id INTEGER REFERENCES sensors(sensor_id);

ALTER TABLE opcua_tags
    ADD COLUMN IF NOT EXISTS sensor_id INTEGER REFERENCES sensors(sensor_id);

CREATE INDEX IF NOT EXISTS idx_tia_sensor_id    ON tia_scada(sensor_id);
CREATE INDEX IF NOT EXISTS idx_modbus_sensor_id ON modbus_scada(sensor_id);
CREATE INDEX IF NOT EXISTS idx_opcua_sensor_id  ON opcua_tags(sensor_id);

-- ============================================================
-- 注意事項：
-- 1. sensor_readings.value 是 NUMERIC，只能存數字。
--    modbus_scada 裡有些點位透過 state_dictionary 轉成中文狀態字
--    （例如 '待機'/'運轉'），這類點位目前不會寫進 sensor_readings，
--    採集程式會自動偵測並略過（詳見 timeseries_writer.py 的說明）。
-- 2. sensor_id 需要你自己手動維護對應關係：
--    (a) 先在 sites/production_lines/devices/sensors 建好階層資料
--    (b) 再回頭 UPDATE tia_scada / modbus_scada / opcua_tags 的 sensor_id 欄位
--    可以直接下 SQL，也可以在 admin_app.py 的表格編輯器裡改（見下方 Python 檔案）。
-- ============================================================
