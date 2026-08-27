"""TCN 前向预测：给定实时数据窗口 + 最近化验值，预测下一次化验值

用法（项目根目录）：
    python scripts/predict_tcn.py --target clean_ash \
        --realtime data/raw/realtime.csv --last-lab 10.85

说明：
  - 取实时表最后 window_steps 行作为 TCN 时序输入
  - 软测量当前估计由已训练的 XGB/LGBM 集成计算（级联输入）
  - 静态输入 = [软测量估计, 最近化验真值]
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

import numpy as np
import pandas as pd

from dmdcs.config import Config
from dmdcs.data.features import build_window_features
from dmdcs.data.loader import load_realtime
from dmdcs.models.tcn import TCNForecaster
from dmdcs.serving.predictor import SoftSensorPredictor


def main() -> None:
    ap = argparse.ArgumentParser(description="TCN 预测下一次化验值")
    ap.add_argument("--target", required=True, help="目标指标键名")
    ap.add_argument("--realtime", required=True, help="实时表 CSV")
    ap.add_argument("--last-lab", type=float, required=True, help="最近一次化验真值")
    ap.add_argument("--config", default=None, help="settings.yaml 路径")
    args = ap.parse_args()

    cfg = Config(args.config)
    target_dir = os.path.join(cfg.artifacts_dir(), "tcn", args.target)
    if not os.path.exists(os.path.join(target_dir, "meta.json")):
        print(f"目标 [{args.target}] 的 TCN 尚未训练，请先运行 scripts/train_tcn.py")
        return

    forecaster = TCNForecaster.load(target_dir)
    realtime = load_realtime(args.realtime)

    if len(realtime) < forecaster.window_steps:
        print(f"实时数据不足 {forecaster.window_steps} 步，无法预测")
        return

    # 软测量当前估计（级联输入）：取实时表末尾窗口聚合
    now = realtime["timestamp"].max()
    feat = build_window_features(realtime, cfg.all_feature_names(),
                                 cfg.settings["feature_window_minutes"],
                                 pd.Series([now]))
    soft_est = float(SoftSensorPredictor(args.target, cfg).predict(feat)["prediction"].iloc[0])

    # TCN 时序输入：最后 window_steps 行的原始特征
    window = realtime.tail(forecaster.window_steps)
    X_seq = window[forecaster.feature_names].to_numpy(dtype=float)[None, :, :]
    static = np.array([[soft_est, args.last_lab]], dtype=float)

    pred = float(forecaster.predict(X_seq, static)[0])
    print("=" * 56)
    print(f"TCN 预测（{cfg.targets[args.target].name}）")
    print("=" * 56)
    print(f"预测基准时刻: {now}")
    print(f"软测量当前估计: {soft_est:.3f}")
    print(f"最近化验真值: {args.last_lab:.3f}")
    print(f"下一化验值预测: {pred:.3f} {cfg.targets[args.target].unit}")


if __name__ == "__main__":
    main()
