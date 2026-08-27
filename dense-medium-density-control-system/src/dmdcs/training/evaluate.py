"""评估已训练模型：加载 artifacts，在测试集（时间切分）上输出指标"""
from __future__ import annotations

import os
from typing import Dict

import pandas as pd

from ..config import Config
from ..utils import load_json, load_model
from .metrics import regression_metrics
from .split import time_split


def evaluate_target(cfg: Config, target_key: str, aligned_df: pd.DataFrame) -> Dict:
    """在测试集上评估已落盘模型，返回指标字典"""
    target_dir = os.path.join(cfg.artifacts_dir(), target_key)
    meta_path = os.path.join(target_dir, "meta.json")
    if not os.path.exists(meta_path):
        raise FileNotFoundError(f"目标指标 [{target_key}] 尚未训练，请先运行 scripts/train.py")
    meta = load_json(meta_path)
    feature_cols = meta["feature_columns"]

    xgb_m = load_model(os.path.join(target_dir, "xgboost.joblib"))
    lgb_m = load_model(os.path.join(target_dir, "lightgbm.joblib"))
    ensemble = load_model(os.path.join(target_dir, "ensemble.joblib"))

    _, test_df = time_split(aligned_df, cfg.settings["split"]["train_ratio"])
    X_test, y_test = test_df[feature_cols], test_df[target_key].astype(float)

    return {
        "target": target_key,
        "name": meta.get("name", target_key),
        "unit": meta.get("unit", ""),
        "created_at": meta.get("created_at", ""),
        "n_test": len(test_df),
        "test_metrics": {
            "xgboost": regression_metrics(y_test, xgb_m.predict(X_test)),
            "lightgbm": regression_metrics(y_test, lgb_m.predict(X_test)),
            "ensemble": regression_metrics(y_test, ensemble.predict(X_test)),
        },
    }
