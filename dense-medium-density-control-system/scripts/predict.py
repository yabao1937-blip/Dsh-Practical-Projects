"""批量/单条预测：加载已训练模型，对特征宽表输出预测结果

用法（项目根目录）：
    python scripts/predict.py --target clean_ash \
        --input data/processed/aligned.csv \
        --output data/processed/pred_result.csv

输入需包含该目标 meta.json 中 feature_columns 的列（窗口聚合宽表）。
在线场景请改用库接口：SoftSensorPredictor(target, cfg).predict_single(row)
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
from dmdcs.serving.predictor import SoftSensorPredictor


def main() -> None:
    ap = argparse.ArgumentParser(description="软测量批量预测")
    ap.add_argument("--target", required=True, help="目标指标键名")
    ap.add_argument("--input", required=True, help="特征宽表 CSV")
    ap.add_argument("--output", required=True, help="预测结果 CSV 输出路径")
    ap.add_argument("--config", default=None, help="settings.yaml 路径")
    args = ap.parse_args()

    cfg = Config(args.config)
    features = pd.read_csv(args.input)
    print(f"输入: {len(features)} 行, 列: {list(features.columns)[:8]} ...")

    predictor = SoftSensorPredictor(args.target, cfg)
    out = predictor.predict(features)

    keep = [c for c in out.columns if not c.startswith("pred")]
    cols = keep + ["pred_xgb", "pred_lgbm", "prediction"]
    out[cols].to_csv(args.output, index=False)
    print(f"预测完成 -> {args.output}")
    print(out[["pred_xgb", "pred_lgbm", "prediction"]].head(10).to_string())

    # 示例：单条预测
    row = out.iloc[0]
    single = predictor.predict_single(
        {c: float(row[c]) for c in predictor.feature_columns})
    print(f"\n单条预测示例: {single}")


if __name__ == "__main__":
    main()
