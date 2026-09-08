"""FastAPI 入口：导入预览/提交、校核、查询、人工处理、撤销、导出。"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from . import actions, checker
from .database import get_conn, init_db
from .importer import (TEMPLATE_CSV, check_duplicate_import, commit_import,
                       parse_csv)

app = FastAPI(title="水文监测数据校核工作台", version="1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

METRIC_NAMES = {"water_level": "水位", "discharge": "流量"}


@app.on_event("startup")
def startup() -> None:
    init_db()


# ---------------- 导入 ----------------

@app.get("/api/template")
def download_template() -> Response:
    return Response(
        content="﻿" + TEMPLATE_CSV, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 "attachment; filename=hydro_template.csv"})


@app.post("/api/import/preview")
async def import_preview(file: UploadFile = File(...)) -> dict:
    content = await file.read()
    result = parse_csv(content, file.filename or "")
    duplicate = check_duplicate_import(result.file_sha256)
    preview_rows = [
        {"row_no": r.row_no, "station_id": r.station_id, "obs_time": r.obs_time,
         "metric": r.metric, "value": r.value, "unit": r.unit}
        for r in result.rows[:50]]
    return {
        "filename": result.filename,
        "file_sha256": result.file_sha256,
        "ok": result.ok,
        "total_rows": result.total_rows,
        "valid_rows": len(result.rows),
        "missing_columns": result.missing_columns,
        "errors": [{"row_no": e.row_no, "reason": e.reason} for e in result.errors],
        "warnings": [{"row_no": w.row_no, "reason": w.reason} for w in result.warnings],
        "stations": [{"station_id": s, "metrics": m} for s, m in result.stations.items()],
        "preview": preview_rows,
        "duplicate_import": duplicate,
    }


@app.post("/api/import/commit")
async def import_commit(file: UploadFile = File(...), force: bool = Form(False)) -> dict:
    content = await file.read()
    result = parse_csv(content, file.filename or "")
    if not result.ok:
        raise HTTPException(400, detail={
            "message": "数据存在校验错误，无法导入",
            "errors": [{"row_no": e.row_no, "reason": e.reason} for e in result.errors]})
    duplicate = check_duplicate_import(result.file_sha256)
    if duplicate and not force:
        raise HTTPException(
            409, detail={"message": "该文件已导入过，重复导入会产生重复数据",
                         "duplicate_import": duplicate})
    import_id = commit_import(result)
    stats = checker.run_checks()
    return {"import_id": import_id, "row_count": len(result.rows),
            "stations": result.stations, "check": stats}


# ---------------- 基础数据 ----------------

@app.get("/api/stations")
def list_stations() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT station_id, metric, COUNT(*) AS n,
                      MIN(obs_time) AS start_time, MAX(obs_time) AS end_time
               FROM records GROUP BY station_id, metric ORDER BY station_id, metric"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/configs")
def get_configs(station_id: Optional[str] = None, metric: Optional[str] = None) -> list[dict]:
    conn = get_conn()
    try:
        sql = "SELECT * FROM rule_configs WHERE 1=1"
        params: list = []
        if station_id:
            sql += " AND station_id = ?"; params.append(station_id)
        if metric:
            sql += " AND metric = ?"; params.append(metric)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


@app.put("/api/configs")
def upsert_config(cfg: dict) -> dict:
    required = ["station_id", "metric", "expected_interval_minutes"]
    for k in required:
        if not cfg.get(k):
            raise HTTPException(400, f"缺少必填配置项：{k}")
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO rule_configs (station_id, metric, expected_interval_minutes,
                   missing_gap_multiplier, max_gap_intervals, value_min, value_max,
                   max_change_per_interval, flat_run_minutes)
               VALUES (:station_id, :metric, :expected_interval_minutes,
                   :missing_gap_multiplier, :max_gap_intervals, :value_min, :value_max,
                   :max_change_per_interval, :flat_run_minutes)
               ON CONFLICT(station_id, metric) DO UPDATE SET
                   expected_interval_minutes=excluded.expected_interval_minutes,
                   missing_gap_multiplier=excluded.missing_gap_multiplier,
                   max_gap_intervals=excluded.max_gap_intervals,
                   value_min=excluded.value_min, value_max=excluded.value_max,
                   max_change_per_interval=excluded.max_change_per_interval,
                   flat_run_minutes=excluded.flat_run_minutes,
                   updated_at=datetime('now', 'localtime')""",
            {"station_id": cfg["station_id"], "metric": cfg["metric"],
             "expected_interval_minutes": cfg["expected_interval_minutes"],
             "missing_gap_multiplier": cfg.get("missing_gap_multiplier") or 2.0,
             "max_gap_intervals": cfg.get("max_gap_intervals") or 6,
             "value_min": _num(cfg.get("value_min")),
             "value_max": _num(cfg.get("value_max")),
             "max_change_per_interval": _num(cfg.get("max_change_per_interval")),
             "flat_run_minutes": cfg.get("flat_run_minutes") or 180})
        conn.commit()
    finally:
        conn.close()
    stats = checker.run_checks(station_id=cfg["station_id"], metric=cfg["metric"])
    return {"saved": True, "check": stats}


def _num(v):
    if v is None or v == "":
        return None
    return float(v)


# ---------------- 校核与查询 ----------------

@app.post("/api/checks")
def trigger_checks(station_id: Optional[str] = None, metric: Optional[str] = None) -> dict:
    return checker.run_checks(station_id=station_id, metric=metric)


@app.get("/api/issues")
def list_issues(station_id: Optional[str] = None, metric: Optional[str] = None,
                status_filter: Optional[str] = None,
                start: Optional[str] = None, end: Optional[str] = None) -> list[dict]:
    conn = get_conn()
    try:
        sql = """SELECT i.*, r.obs_time AS record_time, r.effective_value,
                        r.effective_status, rr.obs_time AS related_time
                 FROM issues i
                 LEFT JOIN records r ON r.id = i.record_id
                 LEFT JOIN records rr ON rr.id = i.related_record_id
                 WHERE 1=1"""
        params: list = []
        if station_id:
            sql += " AND i.station_id = ?"; params.append(station_id)
        if metric:
            sql += " AND i.metric = ?"; params.append(metric)
        if status_filter:
            sql += " AND i.status = ?"; params.append(status_filter)
        if start:
            sql += " AND COALESCE(i.gap_start, r.obs_time) >= ?"; params.append(start)
        if end:
            sql += " AND COALESCE(i.gap_end, r.obs_time) <= ?"; params.append(end)
        sql += " ORDER BY COALESCE(i.gap_start, r.obs_time), i.rule_code"
        issues = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for it in issues:
            it["rule_label"] = checker.RULE_LABELS.get(it["rule_code"], it["rule_code"])
            it["metric_name"] = METRIC_NAMES.get(it["metric"], it["metric"])
        return issues
    finally:
        conn.close()


@app.get("/api/records")
def list_records(station_id: str, metric: str,
                 start: Optional[str] = None, end: Optional[str] = None) -> dict:
    conn = get_conn()
    try:
        sql = """SELECT r.*, (
                    SELECT GROUP_CONCAT(rule_code || ':' || status)
                    FROM issues i WHERE i.record_id = r.id
                 ) AS issue_codes
                 FROM records r WHERE r.station_id = ? AND r.metric = ?"""
        params: list = [station_id, metric]
        if start:
            sql += " AND r.obs_time >= ?"; params.append(start)
        if end:
            sql += " AND r.obs_time <= ?"; params.append(end)
        sql += " ORDER BY r.obs_time, r.id"
        records = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for r in records:
            r["metric_name"] = METRIC_NAMES.get(r["metric"], r["metric"])
            r["is_interpolated"] = r["raw_value"] is None
        return {"station_id": station_id, "metric": metric, "records": records}
    finally:
        conn.close()


# ---------------- 人工处理 ----------------

@app.post("/api/actions")
def do_action(body: dict) -> dict:
    try:
        return actions.apply_action(
            record_id=int(body["record_id"]), action=body["action"],
            reason=body.get("reason", ""), new_value=body.get("new_value"),
            issue_id=body.get("issue_id"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/issues/{issue_id}/interpolation")
def interpolation_suggestion(issue_id: int) -> dict:
    try:
        return actions.suggest_interpolation(issue_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/issues/{issue_id}/interpolate")
def do_interpolate(issue_id: int, body: dict) -> dict:
    try:
        return actions.apply_interpolation(issue_id, body.get("reason", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/audit")
def get_audit(record_id: Optional[int] = None) -> list[dict]:
    return actions.list_audit(record_id)


@app.post("/api/undo")
def do_undo(body: dict) -> dict:
    try:
        return actions.undo(body["batch_id"])
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------- 导出 ----------------

@app.get("/api/export")
def export_data(station_id: str, metric: Optional[str] = None,
                start: Optional[str] = None, end: Optional[str] = None,
                version: str = "revised") -> Response:
    conn = get_conn()
    try:
        sql = "SELECT * FROM records WHERE station_id = ?"
        params: list = [station_id]
        if metric:
            sql += " AND metric = ?"; params.append(metric)
        if start:
            sql += " AND obs_time >= ?"; params.append(start)
        if end:
            sql += " AND obs_time <= ?"; params.append(end)
        sql += " ORDER BY metric, obs_time, id"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    buf = io.StringIO()
    if version == "raw":
        buf.write("站点编号,监测时间,监测指标,数值,单位\n")
        for r in rows:
            if r["raw_value"] is None:
                continue  # 人工插值补点不属于原始观测，原始导出不含
            buf.write(f"{r['station_id']},{r['obs_time']},{METRIC_NAMES.get(r['metric'], r['metric'])},"
                      f"{r['raw_value']:g},{r['unit'] or ''}\n")
        filename = f"{station_id}_原始数据.csv"
    else:
        buf.write("站点编号,监测时间,监测指标,数值,单位,质量标记,处理原因\n")
        audit = {a["record_id"]: a for a in actions.list_audit()}
        for r in rows:
            reason = ""
            if r["effective_status"] == "invalid":
                value, mark = "", "无效"
            elif r["raw_value"] is None or r["effective_status"] == "interpolated":
                value, mark = f"{r['effective_value']:g}", "插值(人工补值,非原始观测)"
            elif r["effective_value"] != r["raw_value"]:
                value, mark = f"{r['effective_value']:g}", "人工修订"
            elif (a := audit.get(r["id"])) and a["action"] == "keep":
                value, mark = f"{r['effective_value']:g}", "保留原值(人工确认)"
            else:
                value, mark = f"{r['effective_value']:g}", "原始"
            if mark != "原始":
                reason = audit.get(r["id"], {}).get("reason", "")
            buf.write(f"{r['station_id']},{r['obs_time']},{METRIC_NAMES.get(r['metric'], r['metric'])},"
                      f"{value},{r['unit'] or ''},{mark},{reason}\n")
        filename = f"{station_id}_修订数据.csv"

    data = "﻿" + buf.getvalue()
    ascii_name = f"{station_id}_{'raw' if version == 'raw' else 'revised'}.csv"
    utf8_name = quote(filename)
    return Response(
        content=data.encode("utf-8"), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename={ascii_name}; filename*=UTF-8''{utf8_name}"})
