"""数据库巡检：指标字典 + 各表行数（真实数据入库后的检查工具）

用法：python scripts/inspect_db.py [--config config/settings.yaml]
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from dmdcs.config import Config


def main() -> None:
    ap = argparse.ArgumentParser(description="巡检真实数据数据库")
    ap.add_argument("--config", default=None, help="settings.yaml 路径")
    args = ap.parse_args()
    cfg = Config(args.config)
    db_path = cfg.resolve(cfg.settings["data"]["db_path"])
    if not os.path.exists(db_path):
        print(f"数据库不存在: {db_path}（请先运行 scripts/init_db.py）")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    print(f"数据库: {db_path}\n")

    print("=== 指标字典 ===")
    print(f"{'code':<20}{'name_cn':<16}{'category':<10}{'source':<14}{'period(s)':<10}{'belt':<6}{'lag(s)':<8}")
    for r in conn.execute(
            "SELECT code,name_cn,category,data_source,sample_period_seconds,belt,lag_seconds "
            "FROM indicator_dict ORDER BY category,code"):
        print(f"{r['code']:<20}{r['name_cn']:<16}{r['category']:<10}{r['data_source']:<14}"
              f"{str(r['sample_period_seconds']):<10}{str(r['belt'] or '-'):<6}{str(r['lag_seconds']):<8}")

    print("\n=== 各表行数 ===")
    for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        t = r["name"]
        n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        print(f"  {t:<22} {n} 行")

    print("\n=== 数据时间范围 ===")
    for t in ("realtime_signals", "lab_assays"):
        row = conn.execute(f'SELECT MIN(timestamp), MAX(timestamp) FROM "{t}"').fetchone()
        print(f"  {t:<22} {row[0]} ~ {row[1]}")

    conn.close()


if __name__ == "__main__":
    main()
