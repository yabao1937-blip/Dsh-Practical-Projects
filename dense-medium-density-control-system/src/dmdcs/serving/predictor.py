"""软测量预测服务：加载已落盘模型，提供单条/批量预测

供后续模块调用：
  - 异常模型（Isolation Forest / TCN 残差）判断预测可信度
  - 控制模型（PID/MPC）以预测值为输入
  - 大模型（RAG/工具调用）解释预测并生成操作建议
"""

from __future__ import annotations

import os
from typing import Dict

import pandas as pd

from ..config import Config
from ..utils import load_json, load_model


class SoftSensorPredictor:
    """针对单一目标指标的软测量预测器"""

    def __init__(self, target_key: str, cfg: Config):
        self.target_key = target_key
        self.cfg = cfg
        target_dir = os.path.join(cfg.artifacts_dir(), target_key)
        meta_path = os.path.join(target_dir, "meta.json")
        if not os.path.exists(meta_path):
            raise FileNotFoundError(
                f"目标指标 [{target_key}] 尚未训练，请先运行 scripts/train.py")
        self.meta = load_json(meta_path)
        self.feature_columns = self.meta["feature_columns"]
        self.ensemble = load_model(os.path.join(target_dir, "ensemble.joblib"))
        self.xgb = load_model(os.path.join(target_dir, "xgboost.joblib"))
        self.lgbm = load_model(os.path.join(target_dir, "lightgbm.joblib"))

    # ---------------- 批量预测 ----------------
    def predict(self, features_df: pd.DataFrame) -> pd.DataFrame:
        """批量预测。features_df 需包含 meta.feature_columns 中的列
        （即特征工程输出的窗口聚合宽表；在线场景先调用
        dmdcs.data.features.build_window_features 构造）。"""
        missing = [c for c in self.feature_columns if c not in features_df.columns]
        if missing:
            raise ValueError(f"缺少特征列: {missing}")
        X = features_df[self.feature_columns]
        out = features_df.copy()
        out["pred_xgb"] = self.xgb.predict(X)
        out["pred_lgbm"] = self.lgbm.predict(X)
        out["prediction"] = self.ensemble.predict(X)
        return out

    # ---------------- 单条预测 ----------------
    def predict_single(self, feature_row: Dict) -> Dict:
        """单条预测。feature_row 为 {特征列名: 值} 字典。"""
        df = pd.DataFrame([feature_row])
        res = self.predict(df).iloc[0]
        return {
            "target": self.target_key,
            "name": self.meta["name"],
            "unit": self.meta["unit"],
            "prediction": float(res["prediction"]),
            "pred_xgb": float(res["pred_xgb"]),
            "pred_lgbm": float(res["pred_lgbm"]),
        }
