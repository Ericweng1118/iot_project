"""SensorReadingWriter 的寫入判斷（純邏輯，不需要資料庫）。"""

from datetime import datetime, timedelta, timezone

import pytest

from data_layer import quality as Q
from data_layer.spool import ReadingSpool
from data_layer.timeseries_writer import SensorReadingWriter

plan = SensorReadingWriter._plan_write
should = SensorReadingWriter._should_write_with

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone(timedelta(hours=8)))


def at(seconds):
    return T0 + timedelta(seconds=seconds)


# ---- _should_write_with：維持 v2 行為（向下相容）----

def test_should_first_sample_always_written():
    assert should("on_change", None, None, 1.0)[0]


def test_should_threshold_percent_below():
    assert not should("threshold_percent", 1.0, (100.0, T0), 100.5)[0]
    assert should("threshold_percent", 1.0, (100.0, T0), 101.0)[0]


def test_should_threshold_percent_zero_base():
    assert should("threshold_percent", 1.0, (0.0, T0), 0.1)[0]
    assert not should("threshold_percent", 1.0, (0.0, T0), 0.0)[0]


def test_should_threshold_absolute_and_legacy():
    assert should("threshold_absolute", 5, (10.0, T0), 15.0)[0]
    assert not should("threshold_absolute", 5, (10.0, T0), 14.9)[0]
    assert should("threshold", 5, (10.0, T0), 15.0)[0]


def test_should_unknown_condition_defaults_to_write():
    assert should("weird", None, (1.0, T0), 1.0)[0]


# ---- _plan_write：v3 時間戳與心跳 ----

def test_plan_first_sample_uses_sample_time():
    t, _ = plan("on_change", None, None, 5.0, at(10), wall_now=at(20))
    assert t == at(10)


def test_plan_change_written_at_sample_time():
    t, _ = plan("on_change", None, (1.0, T0), 2.0, at(30), wall_now=at(60), alive_at=at(30))
    assert t == at(30)


def test_plan_no_change_no_write_before_heartbeat():
    t, _ = plan("on_change", None, (1.0, T0), 1.0, T0, wall_now=at(600), alive_at=at(590),
                heartbeat_interval=3600)
    assert t is None


def test_plan_heartbeat_for_static_value_when_source_alive():
    """OPC UA 數值不變不會推播：樣本時間停在 T0，但來源存活，心跳要以目前時間補寫。"""
    now = at(3700)
    t, reason = plan("on_change", None, (1.0, T0), 1.0, T0, wall_now=now, alive_at=at(3690),
                     heartbeat_interval=3600, alive_window=300)
    assert t == now
    assert "心跳" in reason


def test_plan_heartbeat_skipped_when_source_dead():
    """斷線時不能用舊值補寫，否則會掩蓋斷線。"""
    t, _ = plan("on_change", None, (1.0, T0), 1.0, T0, wall_now=at(3700), alive_at=at(100),
                heartbeat_interval=3600, alive_window=300)
    assert t is None


def test_plan_heartbeat_disabled():
    t, _ = plan("on_change", None, (1.0, T0), 1.0, T0, wall_now=at(99999), alive_at=at(99990),
                heartbeat_interval=0)
    assert t is None


def test_plan_heartbeat_cumulative_counter_under_threshold():
    """2026-09 事故情境：累計值持續增加但未達 1%，心跳仍要補寫（以目前時間）。"""
    now = at(3700)
    t, _ = plan("threshold_percent", 1.0, (1_137_169.0, T0), 1_137_500.0, at(3650),
                wall_now=now, alive_at=at(3650), heartbeat_interval=3600)
    assert t == now


def test_plan_always_new_sample():
    t, _ = plan("always", None, (1.0, T0), 1.0, at(60), wall_now=at(61), alive_at=at(60))
    assert t == at(60)


def test_plan_always_static_value_alive_writes_now():
    t, _ = plan("always", None, (1.0, at(60)), 1.0, at(60), wall_now=at(120), alive_at=at(115))
    assert t == at(120)


def test_plan_always_static_value_dead_skips():
    t, _ = plan("always", None, (1.0, at(60)), 1.0, at(60), wall_now=at(1000), alive_at=at(60),
                alive_window=300)
    assert t is None


def test_plan_old_sample_not_rewritten():
    """樣本時間早於上次寫入（例如重啟後從 DB 載入的較新紀錄），不要重複寫。"""
    t, _ = plan("on_change", None, (1.0, at(100)), 2.0, at(50), wall_now=at(110), alive_at=at(50))
    assert t is None


@pytest.fixture
def writer(tmp_path):
    return SensorReadingWriter(flush_interval_seconds=60, heartbeat_interval_seconds=3600,
                               spool=ReadingSpool(tmp_path / "spool.sqlite3"))


def test_writer_confirm_alive_and_snapshot(writer):
    w = writer
    w.update_latest(7, "3.5")
    w.update_latest(8, "not-a-number")
    w.update_latest(None, 1)
    snap = w.snapshot_latest()
    assert set(snap) == {7}
    value, _ts, alive, quality = snap[7]
    assert value == 3.5 and alive is not None and quality == Q.GOOD
    w.confirm_alive([7, 99])
    assert w.snapshot_latest()[7][2] >= alive
    assert 99 not in w.snapshot_latest()


def test_writer_mark_unavailable_drops_alive_and_queues_one_marker(writer):
    w = writer
    w.update_latest(1, 10)
    w.mark_unavailable([1, 2])
    w.mark_unavailable([1])          # 重複呼叫只排一筆標記
    snap = w.snapshot_latest()[1]
    assert snap[2] is None and snap[3] == Q.COMM_LOST
    assert list(w._pending_markers) == [1]
    assert w._pending_markers[1][2] == Q.COMM_LOST


# ---- v3.1 品質 ----

def test_plan_quality_change_forces_write():
    t, reason = plan("on_change", None, (1.0, T0, Q.GOOD), 1.0, at(10), wall_now=at(20),
                     alive_at=at(10), quality=Q.UNCERTAIN)
    assert t == at(10) and "品質" in reason


def test_plan_bad_quality_only_transition_is_written():
    t, _ = plan("on_change", None, (1.0, T0, Q.GOOD), 0.0, at(10), wall_now=at(20), quality=Q.BAD)
    assert t == at(10)
    t, _ = plan("on_change", None, (0.0, at(10), Q.BAD), 5.0, at(30), wall_now=at(40), quality=Q.BAD)
    assert t is None


def test_plan_recovery_after_comm_lost_is_written_even_if_same_value():
    t, _ = plan("on_change", None, (1.0, at(10), Q.COMM_LOST), 1.0, at(100), wall_now=at(110),
                alive_at=at(100), quality=Q.GOOD)
    assert t == at(100)


def test_plan_comm_lost_latest_with_old_sample_not_rewritten():
    """斷線後 latest 標成 COMM_LOST 但樣本時間沒變，不能把舊樣本再寫一次。"""
    t, _ = plan("on_change", None, (1.0, at(50), Q.COMM_LOST), 1.0, at(10), wall_now=at(60),
                quality=Q.COMM_LOST)
    assert t is None


def test_bad_quality_does_not_count_as_alive(writer):
    writer.update_latest(3, 1.0)
    writer.update_latest(3, 0.0, quality=Q.BAD)
    writer.confirm_alive([3])
    assert writer.snapshot_latest()[3][2] is None


# ---- v3.1 本機緩存（模擬資料庫中斷）----

def test_flush_spools_when_db_down_and_drains_after_recovery(writer, monkeypatch):
    inserted = []
    state = {"db_up": False}

    def fake_insert(rows):
        if not state["db_up"]:
            raise RuntimeError("connection refused")
        inserted.extend(rows)

    monkeypatch.setattr(writer, "_insert", fake_insert)
    writer._upload_config = {1: ("on_change", None), 2: ("on_change", None)}
    writer.update_latest(1, 10.0)
    writer.update_latest(2, 20.0)
    assert writer.flush() == 0
    assert writer.spool.count() == 2 and inserted == []

    writer.mark_unavailable([1])
    writer.flush()
    assert writer.spool.count() == 3            # 斷線標記也進緩存

    state["db_up"] = True
    writer.update_latest(2, 25.0)
    writer.flush()
    assert writer.spool.count() == 0
    qualities = sorted((s, q) for s, _t, _v, q in inserted)
    assert (1, Q.COMM_LOST) in qualities and (2, Q.GOOD) in qualities
    assert writer.get_stats()["drained_total"] == 3


def test_spool_survives_reopen_and_caps_size(tmp_path):
    path = tmp_path / "s.sqlite3"
    sp = ReadingSpool(path, max_rows=3)
    sp.append([(1, at(i), float(i), 0) for i in range(5)])
    assert sp.count() == 3 and sp.dropped_total == 2
    sp.close()
    sp2 = ReadingSpool(path, max_rows=3)
    rows = sp2.peek(10)
    assert [r[3] for r in rows] == [2.0, 3.0, 4.0]       # 保留最新的
    assert rows[0][2] == at(2)                            # 時間含時區原樣還原
    sp2.delete_up_to(rows[1][0])
    assert sp2.count() == 1
