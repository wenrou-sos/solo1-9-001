"""生成三个站点的模拟水文数据，写入 samples/ 并导入数据库。

运行：
    python -m app.seed            # 生成样例 CSV 并导入（已导入的文件会自动跳过）
    python -m app.seed --reset    # 清空数据库后重新生成导入

三个站点的埋点（启动后即可逐条验证）：
- ST01 青溪站（水位/流量，1 小时间隔）：
    水位 14:00 突跳(spike)、20:00-23:00 连续 4 条不变(flat)、
    次日 02:00 数值 99 同时命中 范围+跳变、10:00 一条重复记录；
    流量 次日 05:00 数值 600 超范围。
- ST02 临江站（水位/流量，1 小时间隔）：
    水位 12:00-13:00 短缺口（2 点，可插值）、20:00-次日05:00 长缺口（10 点，不可插值）、
    09:00 重复记录；流量 15:00-18:00 连续不变。
- ST03 望湖站（水位/流量，30 分钟间隔，演示按站点配置）：
    水位 13:00 突跳、18:00-20:30 连续 6 条不变、09:30 重复；
    流量 15:30 出现负值(超范围)、23:00 突跳。
"""
from __future__ import annotations

import argparse
import csv
import math
from datetime import datetime, timedelta
from pathlib import Path

from . import checker
from .database import DB_PATH, get_conn, init_db
from .importer import check_duplicate_import, commit_import, parse_csv

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"
START = datetime(2026, 9, 1, 8, 0, 0)


def gen_series(count: int, interval_min: int, base: float, amp: float,
               seed: float) -> list[tuple[datetime, float]]:
    out = []
    for i in range(count):
        t = START + timedelta(minutes=interval_min * i)
        v = base + amp * math.sin(i * 0.45 + seed) + 0.03 * math.sin(i * 2.7 + seed)
        out.append((t, round(v, 2)))
    return out


def build_rows() -> dict[str, list[tuple]]:
    """返回 {文件名: [(station, time, metric, value, unit), ...]}"""
    files: dict[str, list[tuple]] = {}

    # ---- ST01 青溪站，1 小时，25 个点 ----
    st01 = []
    wl = gen_series(25, 60, 12.30, 0.15, 1.0)
    wl[6] = (wl[6][0], 13.80)           # 14:00 突跳
    for i in range(12, 16):             # 20:00-23:00 卡死不变
        wl[i] = (wl[i][0], 12.10)
    wl[18] = (wl[18][0], 99.00)         # 次日 02:00 超范围 + 跳变
    for t, v in wl:
        st01.append(("ST01", t, "水位", v, "m"))
    st01.append(("ST01", wl[2][0], "水位", 12.62, "m"))  # 重复 10:00

    q = gen_series(25, 60, 45.0, 6.0, 2.0)
    q[21] = (q[21][0], 600.0)           # 次日 05:00 流量超范围
    for t, v in q:
        st01.append(("ST01", t, "流量", v, "m3/s"))
    files["ST01_青溪站.csv"] = st01

    # ---- ST02 临江站，1 小时，25 个点 ----
    st02 = []
    wl2 = gen_series(25, 60, 9.80, 0.12, 3.0)
    for i in [4, 5] + list(range(12, 22)):  # 短缺口(12-13点) + 长缺口(20点-次日05点)
        wl2[i] = None
    for item in wl2:
        if item is not None:
            st02.append(("ST02", item[0], "水位", item[1], "m"))
    st02.append(("ST02", gen_series(25, 60, 9.8, 0, 3)[1][0], "水位", 9.55, "m"))  # 重复 09:00

    q2 = gen_series(25, 60, 38.0, 5.0, 4.0)
    for i in range(7, 11):              # 15:00-18:00 卡死不变
        q2[i] = (q2[i][0], 33.50)
    for t, v in q2:
        st02.append(("ST02", t, "流量", v, "m3/s"))
    files["ST02_临江站.csv"] = st02

    # ---- ST03 望湖站，30 分钟，49 个点 ----
    st03 = []
    wl3 = gen_series(49, 30, 8.10, 0.10, 5.0)
    wl3[10] = (wl3[10][0], 9.50)        # 13:00 突跳
    for i in range(20, 26):             # 18:00-20:30 卡死（150 分钟）
        wl3[i] = (wl3[i][0], 8.05)
    for t, v in wl3:
        st03.append(("ST03", t, "水位", v, "m"))
    st03.append(("ST03", wl3[3][0], "水位", 8.22, "m"))  # 重复 09:30

    q3 = gen_series(49, 30, 120.0, 10.0, 6.0)
    q3[15] = (q3[15][0], -4.0)          # 15:30 负值超范围
    q3[30] = (q3[30][0], 420.0)         # 23:00 突跳
    for t, v in q3:
        st03.append(("ST03", t, "流量", v, "m3/s"))
    files["ST03_望湖站.csv"] = st03

    return files


def write_csv(path: Path, rows: list[tuple]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["站点编号", "监测时间", "监测指标", "数值", "单位"])
        for station, t, metric, v, unit in rows:
            w.writerow([station, t.strftime("%Y-%m-%d %H:%M:%S"), metric, f"{v:g}", unit])


def seed(reset: bool = False) -> None:
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    init_db()

    files = build_rows()
    imported = 0
    for name, rows in files.items():
        path = SAMPLES_DIR / name
        write_csv(path, rows)
        content = path.read_bytes()
        result = parse_csv(content, name)
        dup = check_duplicate_import(result.file_sha256)
        if dup:
            print(f"跳过（已导入过）：{name}")
            continue
        commit_import(result)
        imported += 1
        print(f"已导入：{name}（{len(result.rows)} 行，"
              f"警告 {len(result.warnings)} 条）")

    # ST03 为 30 分钟采样，写入站点级配置
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO rule_configs (station_id, metric, expected_interval_minutes,
                   missing_gap_multiplier, max_gap_intervals, value_min, value_max,
                   max_change_per_interval, flat_run_minutes)
               VALUES ('ST03','water_level',30,2,4,0,50,0.3,120)
               ON CONFLICT(station_id, metric) DO NOTHING""")
        conn.execute(
            """INSERT INTO rule_configs (station_id, metric, expected_interval_minutes,
                   missing_gap_multiplier, max_gap_intervals, value_min, value_max,
                   max_change_per_interval, flat_run_minutes)
               VALUES ('ST03','discharge',30,2,4,0,500,25,120)
               ON CONFLICT(station_id, metric) DO NOTHING""")
        conn.commit()
    finally:
        conn.close()

    stats = checker.run_checks()
    print(f"\n初始化完成：新导入 {imported} 个文件，"
          f"校核覆盖 {stats['checked_series']} 个站点+指标序列，"
          f"当前问题 {stats['issues_upserted']} 条。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成并导入水文模拟数据")
    parser.add_argument("--reset", action="store_true", help="清空数据库后重新生成")
    args = parser.parse_args()
    seed(reset=args.reset)
