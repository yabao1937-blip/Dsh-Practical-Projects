"""初始化真实数据数据库：建表 + 种子指标字典（与 config/targets.yaml 同步）

用法（项目根目录）：
    python scripts/init_db.py                        # 建库 + 指标字典
    python scripts/init_db.py --import-realtime data/raw/realtime.csv \
                              --import-lab data/raw/lab.csv   # 可选：CSV 导入

真实数据到位后流程：
  1) 本脚本建库（data/db/dmdcs.db）
  2) PLC/仪表采集写入 realtime_signals，化验/LIMS 写入 lab_assays
  3) scripts/train.py 检测到数据库后自动改从数据库读取（或加 --use-db 强制）
"""

import argparse
import csv
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

# 控制台 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from dmdcs.config import Config, PROJECT_ROOT

SCHEMA_SQL = os.path.join(PROJECT_ROOT, "db", "schema.sql")

# 特征补充信息（采集源/皮带/系统/周期/滞后补偿），与 targets.yaml 的 features 合并
FEATURE_META = {
    "raw_ash":       dict(data_source="online_gauge", belt=None, system_tag=None,
                          sample_period_seconds=60, lag_seconds=0,
                          remark="原煤在线灰分仪"),
    "coal_amount":   dict(data_source="belt_scale", belt=None, system_tag=None,
                          sample_period_seconds=60, lag_seconds=0,
                          remark="小时带煤量（皮带秤）"),
    "sysA":          dict(data_source="plc", belt="502", system_tag="A",
                          sample_period_seconds=60, lag_seconds=0,
                          remark="A系统合介泵运行信号"),
    "sysB":          dict(data_source="plc", belt="502", system_tag="B",
                          sample_period_seconds=60, lag_seconds=0,
                          remark="B系统合介泵运行信号"),
    "sys401":        dict(data_source="plc", belt="501", system_tag="401",
                          sample_period_seconds=60, lag_seconds=0,
                          remark="401系统合介泵运行信号"),
    "sys402":        dict(data_source="plc", belt="501", system_tag="402",
                          sample_period_seconds=60, lag_seconds=0,
                          remark="402系统合介泵运行信号"),
    "desliming473":  dict(data_source="plc", belt="501", system_tag=None,
                          sample_period_seconds=60, lag_seconds=0,
                          remark="473脱粉筛运行信号"),
    "desliming474":  dict(data_source="plc", belt="501", system_tag=None,
                          sample_period_seconds=60, lag_seconds=0,
                          remark="474脱粉筛运行信号"),
    "is_stoppage":   dict(data_source="derived", belt=None, system_tag=None,
                          sample_period_seconds=60, lag_seconds=0,
                          remark="停机/低负荷（煤量<阈值或泵停，规则派生）"),
    "level":         dict(data_source="plc", belt="501", system_tag=None,
                          sample_period_seconds=60, lag_seconds=-90,
                          remark="精磁尾液位（采样滞后补偿 -1分30秒）"),
}

# 目标补充信息（化验周期按 settings.yaml lab_period_hours 覆盖）
TARGET_META = {
    "clean_ash":          dict(data_source="lab", belt="501", system_tag=None,
                               sample_period_seconds=7200, lag_seconds=0,
                               remark="精煤真实灰分（国标化验，采样时刻非出结果时刻）"),
    "medium_viscosity":   dict(data_source="lab", belt=None, system_tag=None,
                               sample_period_seconds=7200, lag_seconds=0,
                               remark="介质粘度（化验）"),
    "clean_moisture":     dict(data_source="lab", belt="501", system_tag=None,
                               sample_period_seconds=7200, lag_seconds=0,
                               remark="精煤水分（化验）"),
    "suspension_density": dict(data_source="lab", belt=None, system_tag=None,
                               sample_period_seconds=7200, lag_seconds=0,
                               remark="悬液密度（软测量目标，化验/定期标定）"),
    "recovery":           dict(data_source="derived", belt=None, system_tag=None,
                               sample_period_seconds=7200, lag_seconds=0,
                               remark="回收率/产率（由产率报表计算）"),
}


def _seed_dictionary(cursor: sqlite3.Cursor, cfg: Config) -> int:
    """从 targets.yaml + 补充信息生成指标字典记录"""
    rows = []
    for f in cfg.features:
        name = f["name"]
        meta = FEATURE_META.get(name, {})
        used_by = [k for k, t in cfg.targets.items() if name in t.features]
        rows.append((
            name, f.get("desc", ""), "feature", meta.get("data_source", "plc"),
            f.get("unit", ""), None, None,
            meta.get("sample_period_seconds", 60), meta.get("lag_seconds", 0),
            meta.get("belt"), meta.get("system_tag"), ",".join(used_by), 1,
            meta.get("remark", ""),
        ))
    for key, t in cfg.targets.items():
        meta = TARGET_META.get(key, {})
        period = meta.get("sample_period_seconds",
                          cfg.settings.get("lab_period_hours", 2) * 3600)
        vmin, vmax = t.range if t.range else (None, None)
        rows.append((
            key, t.name, t.kind, meta.get("data_source", "lab"), t.unit,
            vmin, vmax, period, meta.get("lag_seconds", 0),
            meta.get("belt"), meta.get("system_tag"), "", 1,
            meta.get("remark", ""),
        ))
    cursor.executemany(
        """INSERT OR REPLACE INTO indicator_dict
           (code, name_cn, category, data_source, unit, valid_min, valid_max,
            sample_period_seconds, lag_seconds, belt, system_tag,
            used_by_targets, enabled, remark)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    return len(rows)


def _import_csv(cursor: sqlite3.Cursor, table: str, path: str, columns: list) -> int:
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        n = 0
        for row in reader:
            vals = [row.get(c) for c in columns]
            ph = ",".join("?" * len(columns))
            cursor.execute(
                f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) VALUES ({ph})",
                vals)
            n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="初始化真实数据数据库")
    ap.add_argument("--config", default=None, help="settings.yaml 路径")
    ap.add_argument("--import-realtime", default=None, help="导入实时表 CSV")
    ap.add_argument("--import-lab", default=None, help="导入化验表 CSV")
    args = ap.parse_args()

    cfg = Config(args.config)
    db_path = cfg.resolve(cfg.settings["data"]["db_path"])
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    with open(SCHEMA_SQL, encoding="utf-8") as f:
        ddl = f.read()
    conn = sqlite3.connect(db_path)
    conn.executescript(ddl)
    n_dict = _seed_dictionary(conn.cursor(), cfg)

    if args.import_realtime:
        cols = ["timestamp"] + cfg.all_feature_names() + ["quality", "source"]
        n = _import_csv(conn.cursor(), "realtime_signals", args.import_realtime, cols)
        print(f"实时表导入 {n} 行")
    if args.import_lab:
        cols = ["timestamp"] + list(cfg.targets.keys()) + ["quality", "source"]
        n = _import_csv(conn.cursor(), "lab_assays", args.import_lab, cols)
        print(f"化验表导入 {n} 行")

    conn.commit()
    conn.close()
    print(f"数据库初始化完成: {db_path}")
    print(f"指标字典 {n_dict} 条（features={len(cfg.features)} + targets={len(cfg.targets)}）")
    print("表: indicator_dict / realtime_signals / lab_assays / production_batch / model_versions")


if __name__ == "__main__":
    main()
