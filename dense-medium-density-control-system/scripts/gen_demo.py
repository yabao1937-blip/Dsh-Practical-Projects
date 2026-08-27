"""生成合成 demo 数据：realtime.csv + lab.csv

真实数据表就绪前，用本脚本按 targets.yaml 指标字典生成模拟数据跑通全管线。
用法（项目根目录）：
    python scripts/gen_demo.py [--days 45] [--config config/settings.yaml]
"""

import argparse
import os
import sys

# 使 src 包可导入
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

# 控制台 UTF-8（避免 Windows GBK 控制台打印中文/特殊字符时报 UnicodeEncodeError）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from dmdcs.config import Config
from dmdcs.data.demo import generate_demo_data


def main() -> None:
    ap = argparse.ArgumentParser(description="生成合成 demo 数据")
    ap.add_argument("--config", default=None, help="settings.yaml 路径")
    ap.add_argument("--days", type=int, default=45, help="模拟天数")
    args = ap.parse_args()

    cfg = Config(args.config)
    n_rt, n_lab = generate_demo_data(
        cfg.realtime_csv(), cfg.lab_csv(),
        sample_period_seconds=cfg.settings["sample_period_seconds"],
        lab_period_hours=cfg.settings["lab_period_hours"],
        days=args.days,
        random_state=cfg.settings["random_state"],
    )
    print(f"已生成 demo 数据：")
    print(f"  实时表 {n_rt} 行 -> {cfg.realtime_csv()}")
    print(f"  化验表 {n_lab} 行 -> {cfg.lab_csv()}")


if __name__ == "__main__":
    main()
