"""CSV 解析/校验与重复导入提醒测试。"""
from app.importer import check_duplicate_import, commit_import, parse_csv

GOOD_CSV = (
    "站点编号,监测时间,监测指标,数值,单位\n"
    "ST01,2026-09-01 08:00:00,水位,12.35,m\n"
    "ST01,2026-09-01 09:00:00,水位,12.40,m\n"
)


def test_parse_good_csv():
    r = parse_csv(GOOD_CSV.encode("utf-8"), "good.csv")
    assert r.ok
    assert len(r.rows) == 2
    assert r.rows[0].station_id == "ST01"
    assert r.rows[0].metric == "water_level"


def test_parse_missing_column():
    bad = "站点编号,监测时间,数值\nST01,2026-09-01 08:00,12.3\n"
    r = parse_csv(bad.encode("utf-8"), "bad.csv")
    assert not r.ok
    assert "监测指标" in r.missing_columns or any("监测指标" in e.reason for e in r.errors)


def test_parse_bad_time_and_value_report_rows():
    bad = (
        "站点编号,监测时间,监测指标,数值,单位\n"
        "ST01,not-a-time,水位,12.3,m\n"          # 第 2 行时间错误
        "ST01,2026-09-01 09:00:00,水位,abc,m\n"  # 第 3 行数值错误
    )
    r = parse_csv(bad.encode("utf-8"), "bad.csv")
    assert not r.ok
    by_row = {e.row_no: e.reason for e in r.errors}
    assert 2 in by_row and "时间" in by_row[2]
    assert 3 in by_row and "数值" in by_row[3]


def test_parse_unknown_metric():
    bad = (
        "站点编号,监测时间,监测指标,数值,单位\n"
        "ST01,2026-09-01 08:00:00,含沙量,1.2,kg\n"
    )
    r = parse_csv(bad.encode("utf-8"), "bad.csv")
    assert not r.ok
    assert "监测指标" in r.errors[0].reason


def test_duplicate_rows_in_file_become_warnings_not_errors():
    dup = (
        "站点编号,监测时间,监测指标,数值,单位\n"
        "ST01,2026-09-01 08:00:00,水位,12.35,m\n"
        "ST01,2026-09-01 08:00:00,水位,12.36,m\n"
    )
    r = parse_csv(dup.encode("utf-8"), "dup.csv")
    assert r.ok  # 不阻断
    assert len(r.warnings) == 1 and "重复" in r.warnings[0].reason
    assert len(r.rows) == 2  # 两条都保留，交校核规则处理


def test_duplicate_import_detection(db):
    r = parse_csv(GOOD_CSV.encode("utf-8"), "good.csv")
    assert check_duplicate_import(r.file_sha256, db) is None
    commit_import(r, db)
    dup = check_duplicate_import(r.file_sha256, db)
    assert dup is not None
    assert dup["filename"] == "good.csv"


def test_chinese_and_iso_time_formats():
    data = (
        "站点编号,监测时间,监测指标,数值,单位\n"
        "ST01,2026/9/1 8:00,水位,12.35,m\n"
        "ST01,2026-09-01T09:00:00,流量,45,m3/s\n"
    )
    r = parse_csv(data.encode("utf-8"), "t.csv")
    assert r.ok
    assert r.rows[0].obs_time == "2026-09-01T08:00:00"
    assert r.rows[1].metric == "discharge"
