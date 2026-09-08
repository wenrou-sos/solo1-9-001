"""SQLite 连接与建表。

所有原始观测值永不被覆盖：人工处理只写入 effective_value / effective_status /
quality_flag，原始值保留在 raw_value；每次处理动作记录到 audit_log，支持撤销。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "hydro.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 每个“站点 + 指标”组合一份校核配置
CREATE TABLE IF NOT EXISTS rule_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    expected_interval_minutes REAL,           -- 标称采样间隔（分钟）
    missing_gap_multiplier REAL DEFAULT 2.0,  -- 缺口超过 interval*该倍数 => 缺测
    max_gap_intervals INTEGER DEFAULT 6,      -- 允许插值的最大缺口（采样间隔个数）
    value_min REAL,                           -- 数值合理范围下限
    value_max REAL,                           -- 数值合理范围上限
    max_change_per_interval REAL,             -- 每个采样间隔允许的最大变化量
    flat_run_minutes REAL DEFAULT 180,        -- 连续不变达到该时长 => 疑似恒定
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE(station_id, metric)
);

CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    unit TEXT,
    obs_time TEXT NOT NULL,                   -- ISO8601，本地时间
    raw_value REAL,                           -- 原始观测值，永不修改；插值补点为 NULL
    effective_value REAL,                     -- 当前生效值（默认=raw_value，无效/插值时变化）
    effective_status TEXT NOT NULL DEFAULT 'valid',  -- valid | invalid | interpolated
    quality_flag TEXT NOT NULL DEFAULT '',    -- 人工质量标记
    import_id INTEGER REFERENCES imports(id),  -- 插值补点无导入批次，为 NULL
    row_no INTEGER
    -- 不设唯一约束：同站点/指标/时间的重复记录允许入库，由 duplicate 校核规则标出
);
CREATE INDEX IF NOT EXISTS idx_records_key ON records(station_id, metric, obs_time);

-- 校核结果：一条记录可同时命中多种规则，每种规则一行
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    rule_code TEXT NOT NULL,                 -- missing | duplicate | range | spike | flat
    severity TEXT NOT NULL DEFAULT 'warning',
    record_id INTEGER REFERENCES records(id),         -- 命中规则的记录（缺测指缺口后一条）
    related_record_id INTEGER REFERENCES records(id), -- 关联记录（如重复对、跳变前一条）
    gap_start TEXT,                                   -- 缺测缺口起点（缺测规则用）
    gap_end TEXT,                                     -- 缺测缺口终点
    expected_count INTEGER,                           -- 缺测：理论应有点数
    detail TEXT NOT NULL,                             -- 命中原因（人话）
    status TEXT NOT NULL DEFAULT 'open',              -- open | resolved | ignored
    UNIQUE(station_id, metric, rule_code, record_id, related_record_id, gap_start, gap_end)
);
CREATE INDEX IF NOT EXISTS idx_issues_key ON issues(station_id, metric, status);

-- 人工处理审计日志：撤销 = 按 action 反向恢复
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,                   -- 同一次操作（如批量插值）共用一个批次号，撤销按批次
    issue_id INTEGER,                         -- 触发该操作的问题（keep 动作需要）
    record_id INTEGER NOT NULL REFERENCES records(id),
    action TEXT NOT NULL,                    -- keep | edit | invalidate | interpolate
    old_value REAL,
    new_value REAL,
    old_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    old_flag TEXT NOT NULL DEFAULT '',
    new_flag TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    undone INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_log(record_id, id);
"""


def get_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
