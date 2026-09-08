"""人工处理、审计日志、撤销、插值建议与重新校核联动测试。"""
import pytest

from app import actions
from app.checker import run_checks
from app.database import get_conn
from tests.conftest import hourly, insert_record


def setup_series(db, station="ST", metric="water_level", values=None, times=None):
    conn = get_conn(db)
    try:
        conn.execute("INSERT INTO imports (filename, file_sha256, row_count) VALUES ('t','x',0)")
        ids = {}
        for t, v in zip(times, values):
            ids[t] = insert_record(conn, station, metric, t, v)
        conn.commit()
        return ids
    finally:
        conn.close()


def get_rec(db, rid):
    conn = get_conn(db)
    try:
        return dict(conn.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone())
    finally:
        conn.close()


def get_issue(db, rid, code):
    conn = get_conn(db)
    try:
        row = conn.execute(
            "SELECT * FROM issues WHERE record_id=? AND rule_code=?", (rid, code)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def test_edit_preserves_raw_value_and_logs(db):
    times = hourly("2026-09-01T08:00", 4)
    ids = setup_series(db, values=[12.0, 12.1, 13.5, 13.55], times=times)
    run_checks(db)
    rid = ids[times[2]]

    res = actions.apply_action(rid, "edit", "人工核对后修正为 12.2",
                               new_value=12.2, db_path=db)
    rec = get_rec(db, rid)
    assert rec["raw_value"] == 13.5          # 原始值保留
    assert rec["effective_value"] == 12.2    # 生效值已改

    audit = actions.list_audit(db_path=db)
    assert len(audit) == 1
    assert audit[0]["action"] == "edit"
    assert audit[0]["old_value"] == 13.5
    assert audit[0]["reason"] == "人工核对后修正为 12.2"


def test_invalidate_and_recheck_resolves_spike(db):
    times = hourly("2026-09-01T08:00", 4)
    ids = setup_series(db, values=[12.0, 12.1, 13.5, 13.55], times=times)
    run_checks(db)
    rid = ids[times[2]]
    issue = get_issue(db, rid, "spike")
    assert issue["status"] == "open"

    actions.apply_action(rid, "invalidate", "确认仪器故障，标记无效",
                         issue_id=issue["id"], db_path=db)

    rec = get_rec(db, rid)
    assert rec["effective_status"] == "invalid"
    assert rec["raw_value"] == 13.5
    # 重新校核后该 spike 问题应已解决（失效点不再参与连续性判断）
    assert get_issue(db, rid, "spike")["status"] == "resolved"


def test_keep_marks_issue_ignored_and_persists(db):
    times = hourly("2026-09-01T08:00", 4)
    ids = setup_series(db, values=[12.0, 12.1, 13.5, 13.55], times=times)
    run_checks(db)
    rid = ids[times[2]]
    issue = get_issue(db, rid, "spike")

    actions.apply_action(rid, "keep", "现场确认该时刻有泄洪，突涨属实",
                         issue_id=issue["id"], db_path=db)

    rec = get_rec(db, rid)
    assert rec["effective_value"] == 13.5  # 值不变
    assert get_issue(db, rid, "spike")["status"] == "ignored"
    # 再次校核不应把人工忽略的状态冲掉
    run_checks(db)
    assert get_issue(db, rid, "spike")["status"] == "ignored"


def test_undo_edit_restores_value_and_reopens_issue(db):
    times = hourly("2026-09-01T08:00", 4)
    ids = setup_series(db, values=[12.0, 12.1, 13.5, 13.55], times=times)
    run_checks(db)
    rid = ids[times[2]]
    issue = get_issue(db, rid, "spike")

    res = actions.apply_action(rid, "edit", "误改，测试撤销",
                               new_value=12.2, issue_id=issue["id"], db_path=db)
    assert get_issue(db, rid, "spike")["status"] == "resolved"

    actions.undo(res["batch_id"], db_path=db)
    rec = get_rec(db, rid)
    assert rec["effective_value"] == 13.5
    assert rec["effective_status"] == "valid"
    # 撤销后重新校核，问题重新出现且为 open
    reopened = get_issue(db, rid, "spike")
    assert reopened is not None
    assert reopened["status"] == "open"
    # 审计条目标记为已撤销
    conn = get_conn(db)
    try:
        assert conn.execute("SELECT undone FROM audit_log").fetchone()["undone"] == 1
    finally:
        conn.close()


def test_undo_requires_reason(db):
    times = hourly("2026-09-01T08:00", 3)
    ids = setup_series(db, values=[12.0, 12.1, 12.2], times=times)
    run_checks(db)
    with pytest.raises(ValueError, match="原因不能为空"):
        actions.apply_action(ids[times[1]], "keep", "  ", db_path=db)


def test_short_gap_interpolation_eligible_and_applied(db):
    # 09:00 -> 12:00，缺 10、11 两个点（短缺口，可插值）
    rows = [("2026-09-01T08:00", 12.0), ("2026-09-01T09:00", 12.2),
            ("2026-09-01T12:00", 12.8), ("2026-09-01T13:00", 12.9)]
    setup_series(db, values=[v for _, v in rows], times=[t for t, _ in rows])
    run_checks(db)
    conn = get_conn(db)
    try:
        issue = conn.execute(
            "SELECT * FROM issues WHERE rule_code='missing'").fetchone()
    finally:
        conn.close()

    sug = actions.suggest_interpolation(issue["id"], db_path=db)
    assert sug["eligible"] is True
    assert [p["obs_time"] for p in sug["points"]] == [
        "2026-09-01T10:00:00", "2026-09-01T11:00:00"]
    # 线性插值：12.2 -> 12.8，两点应为 12.4 / 12.6
    assert [p["value"] for p in sug["points"]] == [12.4, 12.6]

    res = actions.apply_interpolation(issue["id"], "经人工确认，短缺口线性插值", db_path=db)
    assert len(res["created_records"]) == 2

    conn = get_conn(db)
    try:
        for rid in res["created_records"]:
            rec = dict(conn.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone())
            assert rec["raw_value"] is None                # 补点没有原始值
            assert rec["effective_status"] == "interpolated"
            assert rec["quality_flag"] == "插值"
        # 缺测问题已解决
        st = conn.execute("SELECT status FROM issues WHERE id=?", (issue["id"],)).fetchone()
        assert st["status"] == "resolved"
    finally:
        conn.close()

    # 撤销插值：补点被删除，缺测问题重新出现
    actions.undo(res["batch_id"], db_path=db)
    conn = get_conn(db)
    try:
        cnt = conn.execute("SELECT COUNT(*) c FROM records WHERE raw_value IS NULL").fetchone()
        assert cnt["c"] == 0
        miss = conn.execute(
            "SELECT status FROM issues WHERE rule_code='missing'").fetchone()
        assert miss["status"] == "open"
    finally:
        conn.close()


def test_long_gap_interpolation_not_suggested(db):
    # 09:00 -> 20:00，缺 10 个点，超过 max_gap_intervals=6 -> 不建议插值
    rows = [("2026-09-01T08:00", 12.0), ("2026-09-01T09:00", 12.1),
            ("2026-09-01T20:00", 12.5), ("2026-09-01T21:00", 12.6)]
    setup_series(db, values=[v for _, v in rows], times=[t for t, _ in rows])
    run_checks(db)
    conn = get_conn(db)
    try:
        issue = conn.execute(
            "SELECT * FROM issues WHERE rule_code='missing'").fetchone()
    finally:
        conn.close()

    sug = actions.suggest_interpolation(issue["id"], db_path=db)
    assert sug["eligible"] is False
    assert "超过配置上限" in sug["reason"]
    with pytest.raises(ValueError, match="不满足插值条件"):
        actions.apply_interpolation(issue["id"], "强行补值", db_path=db)


def test_interpolation_requires_both_valid_ends(db):
    # 缺口内 12:00 存在已标记无效的记录 -> 不能插值
    rows = [("2026-09-01T08:00", 12.0), ("2026-09-01T09:00", 12.1),
            ("2026-09-01T12:00", 12.8), ("2026-09-01T13:00", 12.9)]
    setup_series(db, values=[v for _, v in rows], times=[t for t, _ in rows])
    conn = get_conn(db)
    try:
        conn.execute("UPDATE records SET effective_status='invalid' WHERE obs_time='2026-09-01T12:00'")
        conn.commit()
    finally:
        conn.close()
    run_checks(db)
    conn = get_conn(db)
    try:
        issue = conn.execute(
            "SELECT * FROM issues WHERE rule_code='missing'").fetchone()
    finally:
        conn.close()
    sug = actions.suggest_interpolation(issue["id"], db_path=db)
    assert sug["eligible"] is False
    assert "无效" in sug["reason"]
