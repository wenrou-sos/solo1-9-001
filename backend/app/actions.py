"""人工处理动作、审计日志、撤销与线性插值建议。

动作类型：
- keep        保留原值，注明原因（问题标记为 ignored）
- edit        手动改值（生效值修改，raw_value 不动）
- invalidate  标记无效（effective_status=invalid）
- interpolate 线性插值补点：对缺测缺口生成新记录，raw_value=NULL，
              effective_status='interpolated'，质量标记为“插值”

撤销：按 audit_log 单条或整个批次（批量插值）反向恢复；插值批次撤销时
删除补出来的记录。撤销后自动重新校核受影响的站点+指标序列。
原始值 raw_value 在任何动作下都不被覆盖。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from .checker import load_config, run_checks
from .database import get_conn


def _get_record(conn, record_id: int) -> dict:
    row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        raise ValueError(f"记录 {record_id} 不存在")
    return dict(row)


def _log(conn, batch_id: str, record_id: int, action: str, reason: str,
         old: dict, new_value, new_status: str, issue_id: Optional[int] = None,
         new_flag: Optional[str] = None) -> None:
    conn.execute(
        """INSERT INTO audit_log
           (batch_id, issue_id, record_id, action, old_value, new_value,
            old_status, new_status, old_flag, new_flag, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (batch_id, issue_id, record_id, action, old.get("effective_value"), new_value,
         old["effective_status"], new_status, old["quality_flag"],
         new_flag if new_flag is not None else old["quality_flag"], reason))


def apply_action(record_id: int, action: str, reason: str,
                 new_value: Optional[float] = None, issue_id: Optional[int] = None,
                 db_path=None) -> dict:
    """对单条记录执行 keep / edit / invalidate。"""
    if action not in ("keep", "edit", "invalidate"):
        raise ValueError(f"不支持的动作：{action}")
    if not reason or not reason.strip():
        raise ValueError("处理原因不能为空")

    conn = get_conn(db_path)
    try:
        rec = _get_record(conn, record_id)
        batch_id = uuid.uuid4().hex
        if action == "keep":
            _log(conn, batch_id, record_id, "keep", reason, rec,
                 rec["effective_value"], rec["effective_status"], issue_id)
            if issue_id:
                conn.execute("UPDATE issues SET status = 'ignored' WHERE id = ?", (issue_id,))
        elif action == "edit":
            if new_value is None:
                raise ValueError("改值必须提供新数值")
            conn.execute(
                "UPDATE records SET effective_value = ? WHERE id = ?",
                (float(new_value), record_id))
            _log(conn, batch_id, record_id, "edit", reason, rec,
                 float(new_value), rec["effective_status"], issue_id)
            if issue_id:
                conn.execute("UPDATE issues SET status = 'resolved' WHERE id = ?", (issue_id,))
        else:  # invalidate
            conn.execute(
                "UPDATE records SET effective_status = 'invalid' WHERE id = ?", (record_id,))
            _log(conn, batch_id, record_id, "invalidate", reason, rec,
                 None, "invalid", issue_id)
            if issue_id:
                conn.execute("UPDATE issues SET status = 'resolved' WHERE id = ?", (issue_id,))
        conn.commit()
        station, metric = rec["station_id"], rec["metric"]
    finally:
        conn.close()

    run_checks(db_path, station_id=station, metric=metric)
    return {"batch_id": batch_id, "action": action, "record_id": record_id}


def suggest_interpolation(issue_id: int, db_path=None) -> dict:
    """为缺测问题计算线性插值建议（不写库）。

    只在缺口两端都有有效数据、且缺口长度（采样间隔个数）不超过配置的
    max_gap_intervals 时给出建议；长缺口不建议插值。
    """
    conn = get_conn(db_path)
    try:
        issue = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if issue is None:
            raise ValueError("问题不存在")
        if issue["rule_code"] != "missing":
            raise ValueError("只有缺测问题可以插值")
        cfg = load_config(conn, issue["station_id"], issue["metric"])

        left = conn.execute(
            """SELECT * FROM records WHERE station_id = ? AND metric = ?
               AND obs_time = ? AND effective_status = 'valid'""",
            (issue["station_id"], issue["metric"], issue["gap_start"])).fetchone()
        right = conn.execute(
            """SELECT * FROM records WHERE station_id = ? AND metric = ?
               AND obs_time = ? AND effective_status = 'valid'""",
            (issue["station_id"], issue["metric"], issue["gap_end"])).fetchone()
        if left is None or right is None:
            return {"eligible": False,
                    "reason": "缺口两端缺少有效数据，不能插值（不自动补值）"}

        gap = datetime.fromisoformat(issue["gap_end"]) - datetime.fromisoformat(issue["gap_start"])
        n_intervals = gap / cfg.interval
        missing_count = int(round(n_intervals)) - 1
        if missing_count < 1:
            return {"eligible": False, "reason": "该缺口没有需要补的点"}
        if missing_count > cfg.max_gap_intervals:
            return {"eligible": False,
                    "reason": f"缺口长度 {missing_count} 个采样间隔，超过配置上限 "
                              f"{cfg.max_gap_intervals}，不建议插值"}

        points = []
        t0 = datetime.fromisoformat(left["obs_time"])
        for k in range(1, missing_count + 1):
            t = t0 + cfg.interval * k
            frac = k / (missing_count + 1)
            v = left["effective_value"] + (right["effective_value"] - left["effective_value"]) * frac
            points.append({"obs_time": t.isoformat(), "value": round(v, 4)})

        # 检查建议时间点是否与已有记录冲突（时间字符串统一规范化后比较）
        existing = {datetime.fromisoformat(r["obs_time"]).isoformat(): r["effective_status"]
                    for r in conn.execute(
                        "SELECT obs_time, effective_status FROM records "
                        "WHERE station_id = ? AND metric = ?",
                        (issue["station_id"], issue["metric"])).fetchall()}
        for p in points:
            if p["obs_time"] in existing:
                if existing[p["obs_time"]] == "invalid":
                    return {"eligible": False,
                            "reason": f"缺口内 {p['obs_time']} 存在已标记无效的记录，"
                                      f"不能插值（请先撤销该无效标记或另行处理）"}
                return {"eligible": False,
                        "reason": f"建议时间点与已有记录冲突：{p['obs_time']}"}

        return {"eligible": True, "issue_id": issue_id,
                "station_id": issue["station_id"], "metric": issue["metric"],
                "missing_count": missing_count, "points": points,
                "reason": f"两端有效，缺口 {missing_count} 个采样间隔（≤ 上限 "
                          f"{cfg.max_gap_intervals}），可线性插值"}
    finally:
        conn.close()


def apply_interpolation(issue_id: int, reason: str, db_path=None) -> dict:
    """应用插值建议：必须先通过 suggest_interpolation 的资格检查，且需人工确认调用。"""
    suggestion = suggest_interpolation(issue_id, db_path)
    if not suggestion.get("eligible"):
        raise ValueError(f"该缺口不满足插值条件：{suggestion.get('reason', '')}")
    if not reason or not reason.strip():
        raise ValueError("处理原因不能为空")

    conn = get_conn(db_path)
    batch_id = uuid.uuid4().hex
    created_ids = []
    try:
        for p in suggestion["points"]:
            cur = conn.execute(
                """INSERT INTO records
                   (station_id, metric, unit, obs_time, raw_value, effective_value,
                    effective_status, quality_flag, import_id, row_no)
                   VALUES (?, ?, ?, ?, NULL, ?, 'interpolated', '插值', NULL, NULL)""",
                (suggestion["station_id"], suggestion["metric"], None,
                 p["obs_time"], p["value"]))
            rid = cur.lastrowid
            created_ids.append(rid)
            conn.execute(
                """INSERT INTO audit_log
                   (batch_id, issue_id, record_id, action, old_value, new_value,
                    old_status, new_status, old_flag, new_flag, reason)
                   VALUES (?, ?, ?, 'interpolate', NULL, ?, 'interpolated',
                           'interpolated', '', '插值', ?)""",
                (batch_id, issue_id, rid, p["value"], reason))
        conn.execute("UPDATE issues SET status = 'resolved' WHERE id = ?", (issue_id,))
        conn.commit()
        station, metric = suggestion["station_id"], suggestion["metric"]
    finally:
        conn.close()

    run_checks(db_path, station_id=station, metric=metric)
    return {"batch_id": batch_id, "issue_id": issue_id, "created_records": created_ids}


def undo(batch_id: str, db_path=None) -> dict:
    """撤销一个处理批次：反向恢复记录状态；插值批次删除补点。撤销后重新校核。"""
    conn = get_conn(db_path)
    try:
        entries = conn.execute(
            "SELECT * FROM audit_log WHERE batch_id = ? AND undone = 0 ORDER BY id DESC",
            (batch_id,)).fetchall()
        if not entries:
            raise ValueError("该批次不存在或已撤销")

        affected: set[tuple[str, str]] = set()
        for e in entries:
            rec = _get_record(conn, e["record_id"])
            affected.add((rec["station_id"], rec["metric"]))
            if e["action"] == "interpolate":
                # 人工补点：撤销即删除，绝不能留下被当作原始观测。
                # 补点后重新校核产生的、引用补点的问题一并删除；
                # 补点仅因插值而存在，其审计条目随补点删除。
                conn.execute(
                    "DELETE FROM issues WHERE record_id = ? OR related_record_id = ?",
                    (e["record_id"], e["record_id"]))
                conn.execute("DELETE FROM audit_log WHERE id = ?", (e["id"],))
                conn.execute("DELETE FROM records WHERE id = ?", (e["record_id"],))
            else:
                conn.execute(
                    "UPDATE records SET effective_value = ?, effective_status = ?, "
                    "quality_flag = ? WHERE id = ?",
                    (e["old_value"], e["old_status"], e["old_flag"], e["record_id"]))
            # 本批次处理过的问题恢复为 open，随后 run_checks 会按数据现状重新判定
            if e["issue_id"]:
                conn.execute(
                    "UPDATE issues SET status = 'open' WHERE id = ?", (e["issue_id"],))
            conn.execute("UPDATE audit_log SET undone = 1 WHERE id = ?", (e["id"],))
        conn.commit()
    finally:
        conn.close()

    for station, metric in affected:
        run_checks(db_path, station_id=station, metric=metric)
    return {"undone_batch": batch_id, "entries": len(entries)}


def list_audit(record_id: Optional[int] = None, include_undone: bool = False,
               db_path=None) -> list[dict]:
    conn = get_conn(db_path)
    try:
        sql = """SELECT a.*, r.station_id, r.metric, r.obs_time
                 FROM audit_log a JOIN records r ON r.id = a.record_id
                 WHERE (? = 1 OR a.undone = 0)"""
        params: list = [1 if include_undone else 0]
        if record_id is not None:
            sql += " AND a.record_id = ?"; params.append(record_id)
        sql += " ORDER BY a.id DESC"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
