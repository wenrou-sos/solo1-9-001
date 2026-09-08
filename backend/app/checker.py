"""校核规则引擎。

五种规则（按 站点+指标 独立计算，水位/流量互不混淆）：
- missing   时间缺测：相邻有效时间间隔 > 标称间隔 × missing_gap_multiplier
- duplicate 同一时间重复记录（不同导入批次可能产生）
- range     数值超出配置的合理范围
- spike     相邻记录变化过大；变化量按时间间隔折算，且长缺测两端不判连续
- flat      连续多记录长时间保持不变

同一条记录可同时命中多种规则，每种规则各产生一条 issue。
校核时原始数据不改动；失效记录不参与跳变/持平的邻接计算，长缺测两端也不直接相连。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .database import get_conn

RULE_LABELS = {
    "missing": "时间缺测",
    "duplicate": "重复记录",
    "range": "超出范围",
    "spike": "变化过大",
    "flat": "长时间不变",
}

# 各指标的默认配置（站点级配置缺省时使用）
DEFAULT_CONFIGS = {
    "water_level": dict(expected_interval_minutes=60.0, missing_gap_multiplier=2.0,
                        max_gap_intervals=6, value_min=0.0, value_max=50.0,
                        max_change_per_interval=0.5, flat_run_minutes=180.0),
    "discharge": dict(expected_interval_minutes=60.0, missing_gap_multiplier=2.0,
                      max_gap_intervals=6, value_min=0.0, value_max=500.0,
                      max_change_per_interval=20.0, flat_run_minutes=180.0),
}


@dataclass
class RuleConfig:
    station_id: str
    metric: str
    expected_interval_minutes: float
    missing_gap_multiplier: float
    max_gap_intervals: int
    value_min: Optional[float]
    value_max: Optional[float]
    max_change_per_interval: Optional[float]
    flat_run_minutes: float

    @property
    def interval(self) -> timedelta:
        return timedelta(minutes=self.expected_interval_minutes)

    @property
    def missing_threshold(self) -> timedelta:
        return timedelta(minutes=self.expected_interval_minutes * self.missing_gap_multiplier)


def load_config(conn, station_id: str, metric: str) -> RuleConfig:
    row = conn.execute(
        "SELECT * FROM rule_configs WHERE station_id = ? AND metric = ?",
        (station_id, metric)).fetchone()
    base = DEFAULT_CONFIGS[metric]
    if row is None:
        return RuleConfig(station_id, metric, **base)
    d = dict(row)
    return RuleConfig(
        station_id, metric,
        expected_interval_minutes=d["expected_interval_minutes"] or base["expected_interval_minutes"],
        missing_gap_multiplier=d["missing_gap_multiplier"] or base["missing_gap_multiplier"],
        max_gap_intervals=d["max_gap_intervals"] or base["max_gap_intervals"],
        value_min=d["value_min"] if d["value_min"] is not None else base["value_min"],
        value_max=d["value_max"] if d["value_max"] is not None else base["value_max"],
        max_change_per_interval=(d["max_change_per_interval"]
                                 if d["max_change_per_interval"] is not None
                                 else base["max_change_per_interval"]),
        flat_run_minutes=d["flat_run_minutes"] or base["flat_run_minutes"],
    )


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _fmt_minutes(td: timedelta) -> str:
    mins = td.total_seconds() / 60
    if mins >= 60 and mins % 60 == 0:
        return f"{int(mins // 60)} 小时"
    return f"{mins:g} 分钟"


def check_series(conn, station_id: str, metric: str, cfg: RuleConfig) -> list[dict]:
    """对一个 站点+指标 序列跑全部规则，返回待写入的 issue dict 列表。"""
    issues: list[dict] = []
    rows = conn.execute(
        """SELECT * FROM records WHERE station_id = ? AND metric = ?
           ORDER BY obs_time, id""", (station_id, metric)).fetchall()
    if not rows:
        return issues

    # ---- duplicate：同一 obs_time 多条记录（跨导入批次） ----
    by_time: dict[str, list] = {}
    for r in rows:
        by_time.setdefault(r["obs_time"], []).append(r)
    for ts, group in by_time.items():
        if len(group) > 1:
            keep = group[0]
            for dup in group[1:]:
                issues.append(dict(
                    station_id=station_id, metric=metric, rule_code="duplicate",
                    severity="warning", record_id=dup["id"], related_record_id=keep["id"],
                    gap_start=None, gap_end=None, expected_count=None,
                    detail=f"监测时间 {ts} 存在 {len(group)} 条记录"
                           f"（本条与第 {keep['id']} 号记录重复），请确认保留哪一条"))

    # ---- range：当前生效值超范围（无效/插值记录不判） ----
    for r in rows:
        if r["effective_status"] in ("invalid",) or r["effective_value"] is None:
            continue
        v = r["effective_value"]
        if cfg.value_min is not None and v < cfg.value_min:
            issues.append(dict(
                station_id=station_id, metric=metric, rule_code="range", severity="warning",
                record_id=r["id"], related_record_id=None, gap_start=None, gap_end=None,
                expected_count=None,
                detail=f"数值 {v:g} 低于合理下限 {cfg.value_min:g}"))
        elif cfg.value_max is not None and v > cfg.value_max:
            issues.append(dict(
                station_id=station_id, metric=metric, rule_code="range", severity="warning",
                record_id=r["id"], related_record_id=None, gap_start=None, gap_end=None,
                expected_count=None,
                detail=f"数值 {v:g} 高于合理上限 {cfg.value_max:g}"))

    # 参与连续性判断的点：有效状态、有生效值、按时间去重（重复点取第一条）
    seen_ts: set[str] = set()
    pts = []
    for r in rows:
        if r["obs_time"] in seen_ts:
            continue
        seen_ts.add(r["obs_time"])
        if r["effective_status"] == "valid" and r["effective_value"] is not None:
            pts.append(r)

    # ---- missing：相邻有效点间隔过大 ----
    for a, b in zip(pts, pts[1:]):
        ta, tb = _parse(a["obs_time"]), _parse(b["obs_time"])
        gap = tb - ta
        if gap > cfg.missing_threshold:
            expected = max(1, int(round(gap / cfg.interval)) - 1)
            issues.append(dict(
                station_id=station_id, metric=metric, rule_code="missing", severity="warning",
                record_id=b["id"], related_record_id=a["id"],
                gap_start=a["obs_time"], gap_end=b["obs_time"], expected_count=expected,
                detail=f"{a['obs_time']} 至 {b['obs_time']} 间隔 {_fmt_minutes(gap)}"
                       f"，超过标称间隔 {_fmt_minutes(cfg.interval)} 的 "
                       f"{cfg.missing_gap_multiplier:g} 倍，疑似缺测约 {expected} 个点"))

    # ---- spike：相邻有效点变化过大（按时间间隔折算；长缺测两端不连续判读） ----
    if cfg.max_change_per_interval is not None:
        for a, b in zip(pts, pts[1:]):
            ta, tb = _parse(a["obs_time"]), _parse(b["obs_time"])
            gap = tb - ta
            if gap > cfg.missing_threshold:
                continue  # 中间有缺测，不当作连续采样比较
            n = max(gap / cfg.interval, 1.0)  # 折算为采样间隔个数
            change = abs(b["effective_value"] - a["effective_value"])
            limit = cfg.max_change_per_interval * n
            if change > limit:
                issues.append(dict(
                    station_id=station_id, metric=metric, rule_code="spike", severity="warning",
                    record_id=b["id"], related_record_id=a["id"],
                    gap_start=None, gap_end=None, expected_count=None,
                    detail=f"数值由 {a['effective_value']:g} 突变为 {b['effective_value']:g}"
                           f"（变化 {change:g}），{_fmt_minutes(gap)}内允许变化约 {limit:g}"
                           f"（每采样间隔 ≤ {cfg.max_change_per_interval:g}）"))

    # ---- flat：连续相同值持续时长超阈值 ----
    run_start = 0
    for i in range(1, len(pts) + 1):
        if i < len(pts) and pts[i]["effective_value"] == pts[run_start]["effective_value"]:
            continue
        if i - run_start >= 2:
            span = _parse(pts[i - 1]["obs_time"]) - _parse(pts[run_start]["obs_time"])
            if span >= timedelta(minutes=cfg.flat_run_minutes):
                for j in range(run_start + 1, i):
                    issues.append(dict(
                        station_id=station_id, metric=metric, rule_code="flat",
                        severity="info", record_id=pts[j]["id"],
                        related_record_id=pts[run_start]["id"],
                        gap_start=None, gap_end=None, expected_count=None,
                        detail=f"数值 {pts[j]['effective_value']:g} 自 "
                               f"{pts[run_start]['obs_time']} 起连续 {i - run_start} 条、"
                               f"历时 {_fmt_minutes(span)} 保持不变，疑似仪器卡死"))
        run_start = i

    return issues


def run_checks(db_path=None, station_id: Optional[str] = None,
               metric: Optional[str] = None) -> dict:
    """重新校核。可限定站点/指标；会先删除对应范围的旧 issue 再重算。

    人工处理保留的问题状态：重新校核后若问题仍存在，保留原 status（resolved/ignored
    不被覆盖回 open）；已消失的问题自动置为 resolved。
    """
    conn = get_conn(db_path)
    try:
        keys = conn.execute(
            """SELECT DISTINCT station_id, metric FROM records
               WHERE (? IS NULL OR station_id = ?) AND (? IS NULL OR metric = ?)""",
            (station_id, station_id, metric, metric)).fetchall()

        total = 0
        for key in keys:
            cfg = load_config(conn, key["station_id"], key["metric"])
            new_issues = check_series(conn, key["station_id"], key["metric"], cfg)

            old = conn.execute(
                "SELECT * FROM issues WHERE station_id = ? AND metric = ?",
                (key["station_id"], key["metric"])).fetchall()
            # 以 (rule_code, record_id, related_record_id, gap_start, gap_end) 为身份
            def ident(it):
                return (it["rule_code"], it["record_id"], it["related_record_id"],
                        it["gap_start"], it["gap_end"])
            old_map = {ident(o): o for o in old}

            for it in new_issues:
                prev = old_map.get(ident(it))
                if prev is None:
                    it["status"] = "open"
                    conn.execute(
                        """INSERT INTO issues (station_id, metric, rule_code, severity,
                           record_id, related_record_id, gap_start, gap_end, expected_count,
                           detail, status)
                           VALUES (:station_id, :metric, :rule_code, :severity, :record_id,
                           :related_record_id, :gap_start, :gap_end, :expected_count,
                           :detail, :status)""",
                        it)
                else:
                    # 问题仍存在：刷新说明文案，保留人工处理过的状态
                    conn.execute(
                        "UPDATE issues SET severity = :severity, detail = :detail, "
                        "expected_count = :expected_count WHERE id = :id",
                        {"severity": it["severity"], "detail": it["detail"],
                         "expected_count": it["expected_count"], "id": prev["id"]})
                total += 1

            # 本轮未再出现且仍处于 open 的旧问题 -> 自动标记已解决（处理生效/数据变化）
            new_idents = {ident(n) for n in new_issues}
            for o in old:
                if ident(o) not in new_idents and o["status"] == "open":
                    conn.execute(
                        "UPDATE issues SET status = 'resolved' WHERE id = ?", (o["id"],))
        conn.commit()
        return {"checked_series": len(keys), "issues_upserted": total}
    finally:
        conn.close()
