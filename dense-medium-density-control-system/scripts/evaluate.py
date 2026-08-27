"""评估已训练模型：加载 artifacts，在测试集（时间切分）上输出指标

用法（项目根目录）：
    python scripts/evaluate.py [--target clean_ash|all] [--config config/settings.yaml]
"""

import argparse
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
from dmdcs.training.evaluate import evaluate_target


def main() -> None:
    ap = argparse.ArgumentParser(description="评估已训练模型")
    ap.add_argument("--target", default="all", help="目标指标键名或 all")
    ap.add_argument("--config", default=None, help="settings.yaml 路径")
    args = ap.parse_args()

    cfg = Config(args.config)
    aligned_csv = cfg.aligned_csv()
    if not os.path.exists(aligned_csv):
        print(f"找不到对齐数据集 {aligned_csv}，请先运行 scripts/train.py（或 --rebuild）")
        return
    aligned = pd.read_csv(aligned_csv)
    aligned["timestamp"] = pd.to_datetime(aligned["timestamp"])

    keys = list(cfg.targets.keys()) if args.target == "all" else [args.target]
    print("\n" + "=" * 80)
    print("模型评估（测试集，时间切分）")
    print("=" * 80)
    header = f"{'目标':<18}{'模型':<10}{'R²':>8}{'RMSE':>10}{'MAE':>10}{'MAPE%':>10}"
    print(header)
    print("-" * 80)
    for key in keys:
        if key not in cfg.targets:
            print(f"未知目标指标 [{key}]")
            continue
        try:
            res = evaluate_target(cfg, key, aligned)
        except FileNotFoundError as e:
            print(e)
            continue
        for model, m in res["test_metrics"].items():
            print(f"{res['name']:<18}{model:<10}{m['r2']:>8.4f}{m['rmse']:>10.4f}"
                  f"{m['mae']:>10.4f}{m['mape_pct']:>10.3f}")
        print("-" * 80)


if __name__ == "__main__":
    main()
