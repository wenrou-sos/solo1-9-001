"""校核规则引擎测试：五种规则、多规则命中、间隔折算、长缺测不判连续。"""
from app.checker import run_checks
from app.database import get_conn
from tests.conftest import hourly, insert_record


def issues_for(db, station="ST", metric="water_level"):
    conn = get_conn(db)
    try:
        rows = conn.execute(
            "SELECT rule_code, status, detail FROM issues WHERE station_id=? AND metric=?",
            (station, metric)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def seed_import(db, station, metric, values_with_times, unit="m"):
    conn = get_conn(db)
    try:
        conn.execute("INSERT INTO imports (filename, file_sha256, row_count) VALUES ('t','x',0)")
        ids = {}
        for t, v in values_with_times:
            ids[t] = insert_record(conn, station, metric, t, v, unit)
        conn.commit()
        return ids
    finally:
        conn.close()


def test_clean_series_has_no_issues(db):
    times = hourly("2026-09-01T08:00", 6)
    vals = [12.0, 12.1, 12.2, 12.15, 12.25, 12.3]
    seed_import(db, "ST", "water_level", list(zip(times, vals)))
    stats = run_checks(db)
    assert stats["checked_series"] == 1
    assert issues_for(db) == []


def test_missing_detection(db):
    times = hourly("2026-09-01T08:00", 4)
    # 08,09 正常；删掉 10 点后直接造 13 点 -> 09->13 间隔 4 小时
    rows = [(times[0], 12.0), (times[1], 12.1),
            ("2026-09-01T13:00", 12.4)]
    seed_import(db, "ST", "water_level", rows)
    run_checks(db)
    miss = [i for i in issues_for(db) if i["rule_code"] == "missing"]
    assert len(miss) == 1
    assert "缺测约 3 个点" in miss[0]["detail"]


def test_duplicate_detection(db):
    t = "2026-09-01T10:00"
    conn = get_conn(db)
    try:
        conn.execute("INSERT INTO imports (filename, file_sha256, row_count) VALUES ('t','x',0)")
        insert_record(conn, "ST", "water_level", t, 12.0)
        insert_record(conn, "ST", "water_level", t, 12.5)  # 同站点同指标同时间
        conn.commit()
    finally:
        conn.close()
    run_checks(db)
    dups = [i for i in issues_for(db) if i["rule_code"] == "duplicate"]
    assert len(dups) == 1


def test_range_detection(db):
    times = hourly("2026-09-01T08:00", 4)
    vals = [12.0, 12.1, 99.0, 12.2]   # 99 超上限 50
    seed_import(db, "ST", "water_level", list(zip(times, vals)))
    run_checks(db)
    rng = [i for i in issues_for(db) if i["rule_code"] == "range"]
    assert len(rng) == 1
    assert "高于合理上限" in rng[0]["detail"]


def test_spike_detection(db):
    times = hourly("2026-09-01T08:00", 4)
    vals = [12.0, 12.1, 13.5, 13.55]  # 13.5 相对 12.1 变化 1.4 > 0.5
    seed_import(db, "ST", "water_level", list(zip(times, vals)))
    run_checks(db)
    spikes = [i for i in issues_for(db) if i["rule_code"] == "spike"]
    assert len(spikes) == 1
    assert "突变为 13.5" in spikes[0]["detail"]


def test_spike_scales_with_time_interval(db):
    # 两小时间隔、变化 0.8：折算 0.4/间隔 < 0.5，不应报跳变
    rows = [("2026-09-01T08:00", 12.0), ("2026-09-01T09:00", 12.1),
            ("2026-09-01T11:00", 12.9), ("2026-09-01T12:00", 13.0)]
    seed_import(db, "ST", "water_level", rows)
    run_checks(db)
    # 09->11 是 2 小时间隔（未超 missing 阈值 2h？阈值 = 60*2 = 120 分钟，gap=120 不大于）
    spikes = [i for i in issues_for(db) if i["rule_code"] == "spike"]
    assert spikes == []


def test_long_gap_ends_not_treated_as_continuous(db):
    # 长缺口两端数值差异巨大，但不应判 spike（中间缺测，不是连续采样）
    rows = [("2026-09-01T08:00", 12.0), ("2026-09-01T09:00", 12.1),
            ("2026-09-01T20:00", 30.0), ("2026-09-01T21:00", 30.1)]
    seed_import(db, "ST", "water_level", rows)
    run_checks(db)
    codes = [i["rule_code"] for i in issues_for(db)]
    assert "missing" in codes
    assert "spike" not in codes


def test_flat_detection(db):
    times = hourly("2026-09-01T08:00", 6)
    vals = [12.0, 12.0, 12.0, 12.0, 12.0, 12.3]  # 连续 5 条不变，历时 4 小时
    seed_import(db, "ST", "water_level", list(zip(times, vals)))
    run_checks(db)
    flats = [i for i in issues_for(db) if i["rule_code"] == "flat"]
    assert len(flats) == 4  # 运行内除起点外各点一条


def test_same_record_can_hit_multiple_rules(db):
    times = hourly("2026-09-01T08:00", 4)
    vals = [12.0, 12.1, 99.0, 12.2]   # 99 既超范围又跳变
    seed_import(db, "ST", "water_level", list(zip(times, vals)))
    run_checks(db)
    conn = get_conn(db)
    try:
        rec = conn.execute(
            "SELECT id FROM records WHERE obs_time='2026-09-01T10:00:00'").fetchone()
        codes = {r["rule_code"] for r in conn.execute(
            "SELECT rule_code FROM issues WHERE record_id=?", (rec["id"],))}
    finally:
        conn.close()
    assert codes == {"range", "spike"}


def test_water_level_and_discharge_checked_separately(db):
    times = hourly("2026-09-01T08:00", 3)
    conn = get_conn(db)
    try:
        conn.execute("INSERT INTO imports (filename, file_sha256, row_count) VALUES ('t','x',0)")
        # 水位正常
        for t, v in zip(times, [12.0, 12.1, 12.2]):
            insert_record(conn, "ST", "water_level", t, v, "m")
        # 流量超范围（600 > 500），但不影响水位序列
        for t, v in zip(times, [40.0, 600.0, 41.0]):
            insert_record(conn, "ST", "discharge", t, v, "m3/s")
        conn.commit()
    finally:
        conn.close()
    run_checks(db)
    assert [i["rule_code"] for i in issues_for(db, "ST", "water_level")] == []
    q_issues = issues_for(db, "ST", "discharge")
    assert any(i["rule_code"] == "range" for i in q_issues)


def test_station_specific_config_takes_effect(db):
    # FAST 站使用 30 分钟间隔配置：90 分钟间隔在 1 小时站不算缺测，在 30 分钟站应报
    conn = get_conn(db)
    try:
        conn.execute("INSERT INTO imports (filename, file_sha256, row_count) VALUES ('t','x',0)")
        insert_record(conn, "FAST", "water_level", "2026-09-01T08:00", 8.0)
        insert_record(conn, "FAST", "water_level", "2026-09-01T09:30", 8.1)
        conn.execute(
            """INSERT INTO rule_configs (station_id, metric, expected_interval_minutes,
                   missing_gap_multiplier, max_gap_intervals, value_min, value_max,
                   max_change_per_interval, flat_run_minutes)
               VALUES ('FAST','water_level',30,2,4,0,50,0.3,120)""")
        conn.commit()
    finally:
        conn.close()
    run_checks(db, station_id="FAST", metric="water_level")
    codes = [i["rule_code"] for i in issues_for(db, "FAST", "water_level")]
    assert "missing" in codes


def test_invalid_records_excluded_from_continuity(db):
    # 失效点不参与 spike/flat：12.9 被标记无效后，12.1->13.0 跨越它，不判连续
    times = hourly("2026-09-01T08:00", 4)
    vals = [12.0, 12.1, 12.9, 13.0]
    ids = seed_import(db, "ST", "water_level", list(zip(times, vals)))
    conn = get_conn(db)
    try:
        conn.execute("UPDATE records SET effective_status='invalid' WHERE id=?",
                     (ids[times[2]],))
        conn.commit()
    finally:
        conn.close()
    run_checks(db)
    spikes = [i for i in issues_for(db) if i["rule_code"] == "spike"]
    assert spikes == []
