"""
data_layer/bindings.py 的整合測試，需要一個可以亂寫的 PostgreSQL。

    BINDINGS_TEST_DSN="postgresql://postgres:test@127.0.0.1:55432/scada_test" pytest tests/test_bindings.py

沒設定 BINDINGS_TEST_DSN 時整支跳過。⚠️ 不要指向正式庫：測試會在 bindings_test
schema 裡建立 / 刪除資料表（不碰 public），但仍應該用拋棄式的資料庫。
"""

import os
from pathlib import Path

import pytest

from data_layer.bindings import BindingError, bind, swap, unbind

DSN = os.getenv("BINDINGS_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="未設定 BINDINGS_TEST_DSN")

SCHEMA = "bindings_test"
MIGRATION = Path(__file__).resolve().parent.parent / "sql" / "022_unique_sensor_binding.sql"


@pytest.fixture(scope="module")
def conn():
    psycopg2 = pytest.importorskip("psycopg2")
    c = psycopg2.connect(DSN)
    with c.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA}; SET search_path TO {SCHEMA};")
        cur.execute("""
            CREATE TABLE sensors (sensor_id SERIAL PRIMARY KEY, sensor_code TEXT UNIQUE);
            CREATE TABLE opcua_tags   (id SERIAL PRIMARY KEY, node_id TEXT, sensor_id INT REFERENCES sensors);
            CREATE TABLE modbus_scada (id SERIAL PRIMARY KEY, name TEXT,    sensor_id INT REFERENCES sensors);
            CREATE TABLE tia_scada    (id SERIAL PRIMARY KEY, name TEXT,    sensor_id INT REFERENCES sensors);
            CREATE TABLE calculated_points (calc_id SERIAL PRIMARY KEY, sensor_id INT NOT NULL UNIQUE REFERENCES sensors);
            INSERT INTO sensors (sensor_code) SELECT 'S' || g FROM generate_series(1, 6) g;
            INSERT INTO opcua_tags (node_id, sensor_id) VALUES ('n1', 1), ('n2', 2), ('n3', NULL), ('n4', NULL);
            INSERT INTO modbus_scada (name, sensor_id) VALUES ('m1', 3);
            INSERT INTO calculated_points (sensor_id) VALUES (4);
        """)
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
        cur.execute(MIGRATION.read_text(encoding="utf-8"))  # 可重複執行
    c.commit()
    yield c
    with c.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;")
    c.commit()
    c.close()


@pytest.fixture
def cur(conn):
    """每個測試都在自己的交易裡跑，結束時 rollback，互不影響。"""
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {SCHEMA};")
        yield c
    conn.rollback()


def _sid(cur, point_id, table="opcua_tags"):
    cur.execute(f"SELECT sensor_id FROM {table} WHERE id = %s;", (point_id,))
    return cur.fetchone()[0]


def test_bind_free_sensor(cur):
    assert bind(cur, "opcua_tags", 3, 5, expected=None) is None
    assert _sid(cur, 3) == 5


def test_bind_sensor_used_in_same_table_rejected(cur):
    with pytest.raises(BindingError, match="opcua_tags.1"):
        bind(cur, "opcua_tags", 3, 1, expected=None)
    assert _sid(cur, 3) is None


def test_bind_sensor_used_by_other_protocol_rejected(cur):
    with pytest.raises(BindingError, match="modbus_scada.1"):
        bind(cur, "opcua_tags", 3, 3)


def test_bind_sensor_used_by_calculated_point_rejected(cur):
    with pytest.raises(BindingError, match="calculated_points"):
        bind(cur, "opcua_tags", 3, 4)


def test_bind_stale_expected_rejected(cur):
    """畫面上看到未綁定，但其實已經被別人綁了 → 不能蓋掉。"""
    with pytest.raises(BindingError, match="其他人修改過"):
        bind(cur, "opcua_tags", 1, 5, expected=None)
    assert _sid(cur, 1) == 1


def test_rebind_to_same_sensor_is_noop(cur):
    assert bind(cur, "opcua_tags", 1, 1, expected=1) == 1
    assert _sid(cur, 1) == 1


def test_rebind(cur):
    assert bind(cur, "opcua_tags", 1, 6, expected=1) == 1
    assert _sid(cur, 1) == 6


def test_missing_point(cur):
    with pytest.raises(BindingError, match="不存在"):
        bind(cur, "opcua_tags", 999, 5)


def test_unknown_table(cur):
    with pytest.raises(ValueError):
        bind(cur, "sensors; DROP TABLE x", 1, 5)


def test_unbind_many(cur):
    assert unbind(cur, "opcua_tags", {1: 1, 2: 2}) == {1: 1, 2: 2}
    assert _sid(cur, 1) is None and _sid(cur, 2) is None


def test_unbind_stale_rolls_back_whole_batch(conn, cur):
    with pytest.raises(BindingError):
        unbind(cur, "opcua_tags", {1: 1, 2: 99})
    conn.rollback()
    cur.execute(f"SET search_path TO {SCHEMA};")
    assert _sid(cur, 1) == 1


def test_swap(cur):
    swap(cur, "opcua_tags", 1, 2, expected_a=1, expected_b=2)
    cur.execute("SET CONSTRAINTS ALL IMMEDIATE;")
    assert (_sid(cur, 1), _sid(cur, 2)) == (2, 1)


def test_swap_with_unbound(cur):
    swap(cur, "opcua_tags", 1, 3, expected_a=1, expected_b=None)
    cur.execute("SET CONSTRAINTS ALL IMMEDIATE;")
    assert (_sid(cur, 1), _sid(cur, 3)) == (None, 1)


def test_swap_same_point_rejected(cur):
    with pytest.raises(BindingError):
        swap(cur, "opcua_tags", 1, 1)


def test_unique_constraint_blocks_duplicate_at_commit(conn, cur):
    """就算繞過 bindings.py 直接寫 SQL，資料庫也不允許重複綁定。"""
    import psycopg2

    cur.execute("UPDATE opcua_tags SET sensor_id = 1 WHERE id = 3;")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE;")
