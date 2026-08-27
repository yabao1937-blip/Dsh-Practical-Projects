"""训练管线：按时间切分训练 XGB/LGBM/集成 -> 评估 -> 落盘 artifacts

每个目标指标一个独立子目录：
  artifacts/<target>/
    ├── xgboost.joblib      XGB 模型
    ├── lightgbm.joblib     LGBM 模型
    ├── ensemble.joblib     集成模型（best|weighted）
    ├── meta.json           特征列、模型清单、配置快照（预测服务据此加载）
    └── report.json         测试集指标 + walk-forward 指标 + 特征重要性
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Dict, List

import numpy as np
import pandas as pd

from ..config import Config
from ..models import EnsemblePredictor, LGBMPredictor, XGBPredictor
from ..utils import ensure_dir, save_json, save_model
from .metrics import regression_metrics
from .split import TIME_COL, time_split, walk_forward_folds

log = logging.getLogger(__name__)

FEATURE_SUFFIXES = ["__last", "__mean", "__slope"]


def train_target(cfg: Config, target_key: str, aligned_df: pd.DataFrame) -> Dict:
    """训练单个目标指标的完整管线，返回 report 字典并落盘 artifacts"""
    target = cfg.targets[target_key]
    feature_cols = [c for c in cfg.target_feature_columns(target_key, FEATURE_SUFFIXES)
                    if c in aligned_df.columns]
    if not feature_cols:
        raise ValueError(f"目标 [{target_key}] 无可用特征列，请检查 targets.yaml 与对齐数据")

    split_cfg = cfg.settings["split"]
    train_df, test_df = time_split(aligned_df, split_cfg["train_ratio"])
    y_train = train_df[target_key].astype(float)

    # ---- 单模型（在全量训练集上训练） ----
    xgb_m = XGBPredictor(cfg.settings["model"]["xgboost"],
                         random_state=cfg.settings["random_state"])
    lgb_m = LGBMPredictor(cfg.settings["model"]["lightgbm"],
                          random_state=cfg.settings["random_state"])
    xgb_m.fit(train_df[feature_cols], y_train)
    lgb_m.fit(train_df[feature_cols], y_train)

    # ---- 集成权重：用训练集内部时间切分(inner)的验证 RMSE 计算 ----
    inner_train, inner_val = time_split(train_df, 0.8)
    xgb_val_rmse = float(np.sqrt(np.mean(
        (xgb_m.predict(inner_val[feature_cols]) - inner_val[target_key].astype(float)) ** 2)))
    lgb_val_rmse = float(np.sqrt(np.mean(
        (lgb_m.predict(inner_val[feature_cols]) - inner_val[target_key].astype(float)) ** 2)))

    ensemble = EnsemblePredictor(method=cfg.settings["model"]["ensemble"]["method"])
    ensemble.add_member("xgboost", xgb_m, xgb_val_rmse)
    ensemble.add_member("lightgbm", lgb_m, lgb_val_rmse)
    ensemble.finalize()

    # ---- 测试集评估 ----
    X_test, y_test = test_df[feature_cols], test_df[target_key].astype(float)
    test_metrics = {
        "xgboost": regression_metrics(y_test, xgb_m.predict(X_test)),
        "lightgbm": regression_metrics(y_test, lgb_m.predict(X_test)),
        "ensemble": regression_metrics(y_test, ensemble.predict(X_test)),
    }

    # ---- walk-forward 滚动验证 ----
    wf_report: Dict = {"enabled": bool(split_cfg["walk_forward"]["enabled"]), "folds": []}
    if wf_report["enabled"]:
        wf_cfg = split_cfg["walk_forward"]
        folds = walk_forward_folds(aligned_df, wf_cfg["n_splits"], wf_cfg["val_fraction"])
        agg: Dict[str, List[dict]] = {"xgboost": [], "lightgbm": [], "ensemble": []}
        for fi, (tr, va) in enumerate(folds):
            mx = XGBPredictor(cfg.settings["model"]["xgboost"],
                              random_state=cfg.settings["random_state"]).fit(
                tr[feature_cols], tr[target_key].astype(float))
            ml = LGBMPredictor(cfg.settings["model"]["lightgbm"],
                               random_state=cfg.settings["random_state"]).fit(
                tr[feature_cols], tr[target_key].astype(float))
            r_x = float(np.sqrt(np.mean(
                (mx.predict(va[feature_cols]) - va[target_key].astype(float)) ** 2)))
            r_l = float(np.sqrt(np.mean(
                (ml.predict(va[feature_cols]) - va[target_key].astype(float)) ** 2)))
            ens = EnsemblePredictor(method=cfg.settings["model"]["ensemble"]["method"])
            ens.add_member("xgboost", mx, r_x)
            ens.add_member("lightgbm", ml, r_l)
            ens.finalize()

            agg["xgboost"].append(regression_metrics(va[target_key].astype(float),
                                                     mx.predict(va[feature_cols])))
            agg["lightgbm"].append(regression_metrics(va[target_key].astype(float),
                                                      ml.predict(va[feature_cols])))
            agg["ensemble"].append(regression_metrics(va[target_key].astype(float),
                                                      ens.predict(va[feature_cols])))
            wf_report["folds"].append({
                "fold": fi + 1,
                "train_rows": len(tr),
                "val_rows": len(va),
                "train_time": [str(tr[TIME_COL].min()), str(tr[TIME_COL].max())],
                "val_time": [str(va[TIME_COL].min()), str(va[TIME_COL].max())],
            })
        wf_report["mean"] = {
            model: {m: float(np.mean([f[m] for f in vals])) for m in vals[0]}
            for model, vals in agg.items()
        }

    # ---- 落盘 ----
    target_dir = os.path.join(cfg.artifacts_dir(), target_key)
    ensure_dir(target_dir)
    save_model(xgb_m, os.path.join(target_dir, "xgboost.joblib"))
    save_model(lgb_m, os.path.join(target_dir, "lightgbm.joblib"))
    save_model(ensemble, os.path.join(target_dir, "ensemble.joblib"))

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meta = {
        "target": target_key,
        "name": target.name,
        "unit": target.unit,
        "range": target.range,
        "kind": target.kind,
        "feature_columns": feature_cols,
        "raw_features": target.features,
        "models": ["xgboost", "lightgbm", "ensemble"],
        "ensemble_method": cfg.settings["model"]["ensemble"]["method"],
        "created_at": created_at,
        "config_snapshot": {
            "xgboost": cfg.settings["model"]["xgboost"],
            "lightgbm": cfg.settings["model"]["lightgbm"],
            "split": cfg.settings["split"],
            "feature_window_minutes": cfg.settings["feature_window_minutes"],
            "sample_period_seconds": cfg.settings["sample_period_seconds"],
        },
    }
    save_json(meta, os.path.join(target_dir, "meta.json"))

    report = {
        "target": target_key,
        "name": target.name,
        "unit": target.unit,
        "created_at": created_at,
        "n_samples": {"train": len(train_df), "test": len(test_df)},
        "time_range": {
            "train": [str(train_df[TIME_COL].min()), str(train_df[TIME_COL].max())],
            "test": [str(test_df[TIME_COL].min()), str(test_df[TIME_COL].max())],
        },
        "test_metrics": test_metrics,
        "walk_forward": wf_report,
        "feature_importance": {
            "xgboost": xgb_m.feature_importance(),
            "lightgbm": lgb_m.feature_importance(),
        },
    }
    save_json(report, os.path.join(target_dir, "report.json"))
    log.info("已训练并保存 [%s]，测试集 RMSE: ensemble=%.4f  xgb=%.4f  lgbm=%.4f",
             target_key, test_metrics["ensemble"]["rmse"],
             test_metrics["xgboost"]["rmse"], test_metrics["lightgbm"]["rmse"])
    return report


def train_all(cfg: Config, aligned_df: pd.DataFrame,
              only: str = "all") -> Dict[str, Dict]:
    """训练全部（或指定）目标指标，返回 {目标键: report}"""
    keys = list(cfg.targets.keys()) if only in ("all", None) else [only]
    results: Dict[str, Dict] = {}
    for key in keys:
        if key not in cfg.targets:
            log.warning("未知目标指标 [%s]，跳过", key)
            continue
        results[key] = train_target(cfg, key, aligned_df)
    save_json(results, os.path.join(cfg.artifacts_dir(), "summary.json"))
    return results
