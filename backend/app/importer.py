"""CSV 解析、校验、预览与导入。

CSV 必填列（中文表头）：站点编号, 监测时间, 监测指标, 数值, 单位
监测指标支持：水位(ZW/m)、流量(LL/m³/s)，同时接受英文 water_level / discharge。
时间支持 ISO 格式与常见中文格式（2026/9/1 8:00、2026-09-01 8:00:00）。
"""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .database import get_conn

REQUIRED_COLUMNS = ["站点编号", "监测时间", "监测指标", "数值", "单位"]

METRIC_ALIASES = {
    "水位": "water_level",
    "water_level": "water_level",
    "zw": "water_level",
    "流量": "discharge",
    "discharge": "discharge",
    "ll": "discharge",
}

DEFAULT_UNIT = {"water_level": "m", "discharge": "m3/s"}

TIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
]


@dataclass
class RowError:
    row_no: int          # CSV 行号（含表头，从 2 开始）
    reason: str


@dataclass
class RowWarning:
    row_no: int
    reason: str


@dataclass
class ParsedRow:
    row_no: int
    station_id: str
    obs_time: str        # 规范化后的 ISO 时间
    metric: str
    value: float
    unit: str


@dataclass
class ParseResult:
    ok: bool
    errors: list[RowError] = field(default_factory=list)
    warnings: list[RowWarning] = field(default_factory=list)
    rows: list[ParsedRow] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    stations: dict[str, list[str]] = field(default_factory=dict)  # station -> metrics
    total_rows: int = 0
    file_sha256: str = ""
    filename: str = ""


def parse_time(text: str) -> Optional[datetime]:
    text = text.strip()
    if not text:
        return None
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_csv(content: bytes, filename: str = "") -> ParseResult:
    """解析并逐行校验，不写库。错误定位到具体行号与原因。"""
    result = ParseResult(ok=False, filename=filename,
                         file_sha256=hashlib.sha256(content).hexdigest())
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        result.errors.append(RowError(0, "文件编码不是 UTF-8，请另存为 UTF-8 后重试"))
        return result

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        result.errors.append(RowError(0, "文件为空"))
        return result

    header = [h.strip() for h in header]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        result.missing_columns = missing
        result.errors.append(
            RowError(1, f"缺少必需列：{', '.join(missing)}；实际表头：{', '.join(header)}"))
        return result
    idx = {c: header.index(c) for c in REQUIRED_COLUMNS}

    seen: dict[tuple, int] = {}  # (站点, 指标, 时间) -> 首次出现行号（文件内重复）
    for line_no, raw in enumerate(reader, start=2):
        result.total_rows += 1
        if not any(cell.strip() for cell in raw):
            continue  # 空行跳过
        if len(raw) < len(header):
            result.errors.append(RowError(line_no, f"列数不足（应为 {len(header)} 列）"))
            continue

        station = raw[idx["站点编号"]].strip()
        time_raw = raw[idx["监测时间"]].strip()
        metric_raw = raw[idx["监测指标"]].strip()
        value_raw = raw[idx["数值"]].strip()
        unit = raw[idx["单位"]].strip()

        if not station:
            result.errors.append(RowError(line_no, "站点编号为空"))
            continue

        metric = METRIC_ALIASES.get(metric_raw.lower())
        if metric is None:
            result.errors.append(RowError(
                line_no, f"监测指标无法识别：'{metric_raw}'（应为 水位/water_level 或 流量/discharge）"))
            continue

        dt = parse_time(time_raw)
        if dt is None:
            result.errors.append(RowError(line_no, f"监测时间解析失败：'{time_raw}'"))
            continue

        try:
            value = float(value_raw)
        except ValueError:
            result.errors.append(RowError(line_no, f"数值不合法：'{value_raw}'"))
            continue

        if not unit:
            unit = DEFAULT_UNIT[metric]

        key = (station, metric, dt.isoformat())
        if key in seen:
            # 重复记录不阻断导入：入库后由 duplicate 校核规则标出，人工确认保留哪条
            result.warnings.append(RowWarning(
                line_no, f"与第 {seen[key]} 行重复（同站点、同指标、同时间 {dt.isoformat()}），"
                         f"导入后将在校核结果中标出"))
        else:
            seen[key] = line_no

        parsed = ParsedRow(line_no, station, dt.isoformat(), metric, value, unit)
        result.rows.append(parsed)
        result.stations.setdefault(station, [])
        if metric not in result.stations[station]:
            result.stations[station].append(metric)

    result.ok = len(result.errors) == 0
    return result


def check_duplicate_import(file_sha256: str, db_path=None) -> Optional[dict]:
    """同一份文件（按内容哈希）是否已导入过。"""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, filename, row_count, imported_at FROM imports WHERE file_sha256 = ?",
            (file_sha256,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def commit_import(result: ParseResult, db_path=None) -> int:
    """把解析通过的数据写入库，返回 import_id。调用前应已确认重复导入提醒。"""
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO imports (filename, file_sha256, row_count) VALUES (?, ?, ?)",
            (result.filename, result.file_sha256, len(result.rows)))
        import_id = cur.lastrowid
        conn.executemany(
            """INSERT INTO records
               (station_id, metric, unit, obs_time, raw_value,
                effective_value, effective_status, import_id, row_no)
               VALUES (?, ?, ?, ?, ?, ?, 'valid', ?, ?)""",
            [(r.station_id, r.metric, r.unit, r.obs_time, r.value, r.value,
              import_id, r.row_no) for r in result.rows])
        conn.commit()
        return import_id
    finally:
        conn.close()


TEMPLATE_CSV = (
    "站点编号,监测时间,监测指标,数值,单位\n"
    "ST01,2026-09-01 08:00:00,水位,12.35,m\n"
    "ST01,2026-09-01 08:00:00,流量,45.2,m3/s\n"
    "ST02,2026-09-01 08:00:00,水位,8.12,m\n"
)
