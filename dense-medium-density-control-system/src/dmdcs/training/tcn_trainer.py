"""TCN 训练管线：软测量级联 -> 序列构建 -> 训练 -> 评估 -> 落盘

级联方案（无泄漏）：
  1. 按化验样本时间切分 train/val/test
  2. 仅在 TCN train 段上拟合软测量（XGB+LGBM 集成）
  3. 用该软测量对全部样本生成「当前估计」，作为 TCN 静态输入之一
     （val/test 段的软测量从未见过其真值，无泄漏；train 段为样本内，属 v1 已知简化）
  4. 构造序列 -> 训练 TCN -> 测试集评估 + 持久化基线对比
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from ..config import Config
from ..data.align import build_aligned_dataset
from ..data.sequences import build_sequences
from ..models import EnsemblePredictor, LGBMPredictor, XGBPredictor
from ..utils import ensure_dir, save_json
from .metrics import regression_metrics
from .split import TIME_COL

log = logging.getLogger(__name__)

FEATURE_SUFFIXES = ["__last", "__mean", "__slope"]


def _fit_soft_sensor(cfg: Config, target_key: str,
                     train_aligned: pd.DataFrame) -> Tuple[EnsemblePredictor, List[str]]:
    """在给定训练段上拟合 XGB+LGBM 集成，返回 (集成模型, 特征列)"""
    feature_cols = [c for c in cfg.target_feature_columns(target_key, FEATURE_SUFFIXES)
                    if c in train_aligned.columns]
    n = len(train_aligned)
    cut = max(1, int(n * 0.8))
    tr, va = train_aligned.iloc[:cut], train_aligned.iloc[cut:]
    y_tr = tr[target_key].astype(float)

    xgb_m = XGBPredictor(cfg.settings["model"]["xgboost"],
                         random_state=cfg.settings["random_state"]).fit(tr[feature_cols], y_tr)
    lgb_m = LGBMPredictor(cfg.settings["model"]["lightgbm"],
                          random_state=cfg.settings["random_state"]).fit(tr[feature_cols], y_tr)

    y_va = va[target_key].astype(float)
    r_x = float(np.sqrt(np.mean((xgb_m.predict(va[feature_cols]) - y_va) ** 2)))
    r_l = float(np.sqrt(np.mean((lgb_m.predict(va[feature_cols]) - y_va) ** 2)))

    ens = EnsemblePredictor(method=cfg.settings["model"]["ensemble"]["method"])
    ens.add_member("xgboost", xgb_m, r_x)
    ens.add_member("lightgbm", lgb_m, r_l)
    ens.finalize()
    return ens, feature_cols


def _soft_estimates(cfg: Config, target_key: str, aligned_df: pd.DataFrame,
                    train_aligned: pd.DataFrame) -> pd.DataFrame:
    """对全部样本生成软测量当前估计（级联输入）"""
    ens, feature_cols = _fit_soft_sensor(cfg, target_key, train_aligned)
    est = ens.predict(aligned_df[feature_cols])
    return pd.DataFrame({"timestamp": aligned_df[TIME_COL].values,
                         f"{target_key}_soft": est})


def _split_arrays(*arrays, train_ratio: float, val_ratio: float) -> List[Tuple]:
    """按样本顺序（时间序）切分多个等长数组"""
    n = len(arrays[0])
    i_train = int(n * train_ratio)
    i_val = int(n * (train_ratio + val_ratio))
    return [(a[:i_train], a[i_train:i_val], a[i_val:]) for a in arrays]


def train_tcn(cfg: Config, target_key: str) -> Dict:
    """训练单个目标的 TCN，落盘 artifacts/tcn/<target>/，返回 report"""
    tcn_cfg = cfg.settings["tcn"]
    window_steps = tcn_cfg["window_steps"]
    split_cfg = tcn_cfg["split"]

    # 1. 读取数据（数据库优先，回退 CSV）
    db_path = cfg.resolve(cfg.settings["data"]["db_path"])
    if os.path.exists(db_path):
        from ..data.db_loader import load_lab_from_db, load_realtime_from_db
        realtime = load_realtime_from_db(db_path, cfg.all_feature_names())
        lab = load_lab_from_db(db_path, list(cfg.targets.keys()))
    else:
        from ..data.loader import load_lab, load_realtime
        realtime = load_realtime(cfg.realtime_csv())
        lab = load_lab(cfg.lab_csv())

    # 2. 对齐（软测量需要窗口聚合特征）
    aligned = build_aligned_dataset(
        realtime, lab, cfg.all_feature_names(),
        cfg.settings["feature_window_minutes"], list(cfg.targets.keys()))

    # 3. 化验样本时间切分
    n = len(aligned)
    i_train = int(n * split_cfg["train_ratio"])
    i_val = int(n * (split_cfg["train_ratio"] + split_cfg["val_ratio"]))
    train_aligned = aligned.iloc[:i_train]

    # 4. 软测量级联：仅在 train 段拟合，生成全样本估计
    soft_df = _soft_estimates(cfg, target_key, aligned, train_aligned)

    # 5. 构造序列（含软测量 + 最近化验真值静态通道）
    lab_wide = aligned[[TIME_COL, target_key]]
    X_seq, static, y, timestamps = build_sequences(
        realtime, lab_wide, soft_df, cfg.all_feature_names(), target_key, window_steps)

    # 6. 序列时间切分
    (Xs, Xv, Xt), (Ss, Sv, St), (ys, yv, yt) = _split_arrays(
        X_seq, static, y,
        train_ratio=split_cfg["train_ratio"], val_ratio=split_cfg["val_ratio"])

    # 7. 训练 TCN（早停于验证集）
    from ..models.tcn import TCNForecaster
    forecaster = TCNForecaster(
        window_steps=window_steps, num_filters=tcn_cfg["num_filters"],
        kernel_size=tcn_cfg["kernel_size"], num_layers=tcn_cfg["num_layers"],
        dropout=tcn_cfg["dropout"], lr=tcn_cfg["lr"], epochs=tcn_cfg["epochs"],
        batch_size=tcn_cfg["batch_size"], patience=tcn_cfg["patience"],
        random_state=cfg.settings["random_state"])
    forecaster.feature_names = cfg.all_feature_names()
    forecaster.fit(Xs, Ss, ys, Xv, Sv, yv)

    # 8. 测试集评估 + 持久化基线（上一化验值）与均值基线
    pred = forecaster.predict(Xt, St)
    tcn_metrics = regression_metrics(yt, pred)
    persistence = regression_metrics(yt, St[:, 1])            # 最近化验真值 = 上一值
    mean_baseline = regression_metrics(yt, np.full_like(yt, ys.mean()))

    # 9. 落盘
    target_dir = os.path.join(cfg.artifacts_dir(), "tcn", target_key)
    ensure_dir(target_dir)
    forecaster.save(target_dir)

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meta = {
        "target": target_key, "name": cfg.targets[target_key].name,
        "unit": cfg.targets[target_key].unit,
        "feature_names": cfg.all_feature_names(),
        "static_names": forecaster.static_names,
        "window_steps": window_steps, "horizon_steps": tcn_cfg["horizon_steps"],
        "cascade": "soft_estimate + last_lab",
        "created_at": created_at,
        "config_snapshot": {k: v for k, v in tcn_cfg.items() if k != "split"},
    }
    save_json(meta, os.path.join(target_dir, "meta.json"))

    report = {
        "target": target_key, "name": meta["name"], "unit": meta["unit"],
        "created_at": created_at,
        "n_samples": {"train": len(ys), "val": len(yv), "test": len(yt)},
        "time_range": [str(timestamps.min()), str(timestamps.max())],
        "test_metrics": {"tcn": tcn_metrics},
        "baselines": {"persistence": persistence, "train_mean": mean_baseline},
        "best_val_loss": min(forecaster.history["val_loss"]) if forecaster.history["val_loss"] else None,
        "epochs_run": len(forecaster.history["train_loss"]),
    }
    save_json(report, os.path.join(target_dir, "report.json"))
    log.info("TCN [%s] 训练完成 | test RMSE: tcn=%.4f  persistence=%.4f",
             target_key, tcn_metrics["rmse"], persistence["rmse"])
    return report


def train_tcn_all(cfg: Config) -> Dict[str, Dict]:
    results = {}
    for key in cfg.settings["tcn"]["enabled_targets"]:
        if key in cfg.targets:
            results[key] = train_tcn(cfg, key)
    save_json(results, os.path.join(cfg.artifacts_dir(), "tcn", "summary.json"))
    return results
