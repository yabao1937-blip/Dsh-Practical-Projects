"""TCN 序列构造：实时窗口 + 软测量估计 + 最近化验真值 -> 训练样本

每个样本对应一个化验点 k：
  输入窗口 = [T_k - window_steps*周期, T_k] 的原始可测特征（时序通道）
  静态通道 = [软测量当前估计(T_k), 最近化验真值 y(T_k)]
  目标     = 下一化验值 y(T_{k+1})（horizon = 化验周期）
"""

from __future__ import annotations

from typing import List, Tuple

import numpy as np
import pandas as pd


def build_sequences(realtime_df: pd.DataFrame, lab_df: pd.DataFrame,
                    soft_df: pd.DataFrame, feature_names: List[str],
                    target: str, window_steps: int) -> Tuple[np.ndarray, np.ndarray,
                                                             np.ndarray, pd.DatetimeIndex]:
    """返回 (X_seq [N,W,F], static [N,2], y [N], timestamps)

    lab_df      含 timestamp 与目标真值列（已按时间排序）
    soft_df     含 timestamp 与 {target}_soft 软测量估计列
    """
    rt = realtime_df.sort_values("timestamp")
    rt_ts = rt["timestamp"].to_numpy()

    merged = lab_df.merge(soft_df[["timestamp", f"{target}_soft"]],
                          on="timestamp", how="inner")
    merged = merged.sort_values("timestamp").reset_index(drop=True)

    X_list, S_list, y_list, ts_list = [], [], [], []
    for k in range(len(merged) - 1):
        t_k = merged["timestamp"].iloc[k]
        t_next = merged["timestamp"].iloc[k + 1]
        soft = merged[f"{target}_soft"].iloc[k]
        last_lab = merged[target].iloc[k]
        y_val = merged[target].iloc[k + 1]
        if pd.isna(soft) or pd.isna(last_lab) or pd.isna(y_val):
            continue

        window = rt.loc[rt_ts <= t_k].tail(window_steps)
        if len(window) < window_steps:
            continue

        feat = window[feature_names].to_numpy(dtype=float)   # (W, F)
        X_list.append(feat)
        S_list.append([soft, last_lab])
        y_list.append(y_val)
        ts_list.append(t_next)

    X_seq = np.asarray(X_list, dtype=np.float32)
    static = np.asarray(S_list, dtype=np.float32)
    y = np.asarray(y_list, dtype=np.float32)
    timestamps = pd.DatetimeIndex(ts_list)
    return X_seq, static, y, timestamps
