"""数据对齐：高频实时表 + 低频化验表 -> 训练用特征宽表（含真值）

真实场景：化验结果滞后数小时才出，软测量要的是「化验时刻对应的特征窗口」，
因此以化验时刻为锚点聚合特征，与化验真值对齐成监督学习的 (X, y) 样本。
"""

from __future__ import annotations

from typing import List

import pandas as pd

from .features import build_window_features


def build_aligned_dataset(realtime_df: pd.DataFrame, lab_df: pd.DataFrame,
                          feature_names: List[str], window_minutes: int,
                          target_columns: List[str]) -> pd.DataFrame:
    """返回宽表：timestamp + {特征}__{聚合} 列 + 目标真值列

    窗口内无实时数据（如长时间停机）导致的特征缺失行会被剔除。
    """
    feat = build_window_features(realtime_df, feature_names, window_minutes,
                                 lab_df["timestamp"])
    merged = feat.merge(lab_df, on="timestamp", how="inner")
    merged = merged.dropna().sort_values("timestamp").reset_index(drop=True)
    return merged
