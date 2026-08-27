"""训练管线：构建对齐数据集 -> 逐指标训练 XGB/LGBM/集成 -> 落盘 artifacts

用法（项目根目录）：
    python scripts/train.py [--target clean_ash|all] [--rebuild] [--config config/settings.yaml]

--rebuild：强制重新构建对齐数据集（对齐结果缓存在 data/processed/aligned.csv）
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

# 控制台 UTF-8（避免 Windows GBK 控制台打印 R² 等字符时报 UnicodeEncodeError）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import pandas as pd

from dmdcs.config import Config
from dmdcs.data.align import build_aligned_dataset
from dmdcs.data.loader import load_lab, load_realtime
from dmdcs.training.trainer import train_all
from dmdcs.utils import ensure_dir, setup_logging

log = logging.getLogger("train")


def _print_summary(results: dict) -> None:
    print("\n" + "=" * 78)
    print("训练汇总（测试集指标，按时间切分 train_ratio=0.8）")
    print("=" * 78)
    header = f"{'目标':<18}{'模型':<10}{'R²':>8}{'RMSE':>10}{'MAE':>10}{'MAPE%':>10}"
    print(header)
    print("-" * 78)
    for key, report in results.items():
        for model, m in report["test_metrics"].items():
            print(f"{report['name']:<18}{model:<10}{m['r2']:>8.4f}{m['rmse']:>10.4f}"
                  f"{m['mae']:>10.4f}{m['mape_pct']:>10.3f}")
        print("-" * 78)
    print("模型产物目录: artifacts/<target>/ (xgboost.joblib / lightgbm.joblib / ensemble.joblib / meta.json / report.json)")


def main() -> None:
    ap = argparse.ArgumentParser(description="训练 XGB+LGBM 软测量模型")
    ap.add_argument("--target", default="all", help="目标指标键名或 all")
    ap.add_argument("--rebuild", action="store_true", help="强制重建对齐数据集")
    ap.add_argument("--config", default=None, help="settings.yaml 路径")
    ap.add_argument("--use-db", action="store_true", help="强制从数据库读取（默认自动检测）")
    ap.add_argument("--csv", action="store_true", help="强制从 CSV 读取")
    args = ap.parse_args()

    setup_logging()
    cfg = Config(args.config)
    aligned_csv = cfg.aligned_csv()

    # 数据源选择：数据库存在则优先（--csv 可回退，--use-db 强制）
    db_path = cfg.resolve(cfg.settings["data"]["db_path"])
    use_db = args.use_db or (os.path.exists(db_path) and not args.csv)
    if args.use_db and not os.path.exists(db_path):
        log.warning("未找到数据库 %s，回退 CSV", db_path)
        use_db = False

    if use_db:
        from dmdcs.data.db_loader import load_lab_from_db, load_realtime_from_db
        log.info("数据源：SQLite %s", db_path)
        realtime = load_realtime_from_db(db_path, cfg.all_feature_names())
        lab = load_lab_from_db(db_path, list(cfg.targets.keys()))
    else:
        log.info("数据源：CSV (realtime=%s, lab=%s)", cfg.realtime_csv(), cfg.lab_csv())
        realtime = load_realtime(cfg.realtime_csv())
        lab = load_lab(cfg.lab_csv())

    if args.rebuild or not os.path.exists(aligned_csv):
        log.info("构建对齐数据集 ...")
        aligned = build_aligned_dataset(
            realtime, lab, cfg.all_feature_names(),
            cfg.settings["feature_window_minutes"], list(cfg.targets.keys()))
        ensure_dir(os.path.dirname(aligned_csv))
        aligned.to_csv(aligned_csv, index=False)
        log.info("对齐数据集 %d 行 -> %s", len(aligned), aligned_csv)
    else:
        aligned = pd.read_csv(aligned_csv)
        aligned["timestamp"] = pd.to_datetime(aligned["timestamp"])
        log.info("使用缓存对齐数据集 %d 行 -> %s", len(aligned), aligned_csv)

    results = train_all(cfg, aligned, only=args.target)
    _print_summary(results)


if __name__ == "__main__":
    main()
