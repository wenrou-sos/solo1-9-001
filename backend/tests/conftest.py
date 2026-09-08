"""pytest 公共夹具：每个用例一个临时 SQLite 库。"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import get_conn, init_db  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    yield path


def insert_record(conn, station, metric, time_str, value, unit="m",
                  import_id=1, status="valid", raw_value=None):
    cur = conn.execute(
        """INSERT INTO records (station_id, metric, unit, obs_time, raw_value,
           effective_value, effective_status, import_id, row_no)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (station, metric, unit, time_str,
         raw_value if raw_value is not None else value, value, status, import_id, 0))
    return cur.lastrowid


def hourly(start: str, n: int, step_min: int = 60):
    t0 = datetime.fromisoformat(start)
    return [(t0 + timedelta(minutes=step_min * i)).isoformat() for i in range(n)]
