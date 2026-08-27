"""训练 TCN 时序预测模型（预测下一次化验值，级联+混合输入）

用法（项目根目录）：
    python scripts/train_tcn.py                     # 训练 tcn.enabled_targets 全部指标
    python scripts/train_tcn.py --target clean_ash  # 只训练指定指标
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from dmdcs.config import Config
from dmdcs.training.tcn_trainer import train_tcn, train_tcn_all
from dmdcs.utils import setup_logging


def _print_summary(results: dict) -> None:
    print("\n" + "=" * 84)
    print("TCN 训练汇总（测试集指标；基线 = 上一化验值/训练均值）")
    print("=" * 84)
    header = f"{'目标':<18}{'模型':<10}{'R²':>8}{'RMSE':>10}{'MAE':>10}{'MAPE%':>10}"
    print(header)
    print("-" * 84)
    for key, report in results.items():
        for model, m in [("tcn", report["test_metrics"]["tcn"]),
                         ("persist", report["baselines"]["persistence"]),
                         ("mean", report["baselines"]["train_mean"])]:
            print(f"{report['name']:<18}{model:<10}{m['r2']:>8.4f}{m['rmse']:>10.4f}"
                  f"{m['mae']:>10.4f}{m['mape_pct']:>10.3f}")
        print("-" * 84)


def main() -> None:
    ap = argparse.ArgumentParser(description="训练 TCN 时序预测模型")
    ap.add_argument("--target", default="all", help="目标指标键名或 all")
    ap.add_argument("--config", default=None, help="settings.yaml 路径")
    args = ap.parse_args()

    setup_logging()
    cfg = Config(args.config)

    if args.target == "all":
        results = train_tcn_all(cfg)
    else:
        results = {args.target: train_tcn(cfg, args.target)}
    _print_summary(results)


if __name__ == "__main__":
    main()
