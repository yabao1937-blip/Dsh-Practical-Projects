"""特征工程：以化验时刻为锚点，对高频实时数据做前向窗口聚合

聚合方式（AGG_SUFFIXES）：
  __last  窗口末值（当前状态）
  __mean  窗口均值（平均水平）
  __slope 窗口线性趋势（变化方向/速率）

后续可按需扩展：窗口标准差（波动）、分位数、滞后多期等，只需新增
聚合函数并保持「列名 = {特征}__{后缀}」的约定即可。
"""

from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd

AGG_SUFFIXES: List[str] = ["__last", "__mean", "__slope"]


def _window_slope(values: np.ndarray) -> float:
    """窗口内线性回归斜率（趋势）；样本不足 2 个或恒定时返回 0/NaN"""
    if len(values) < 2:
        return float("nan")
    if np.all(values == values[0]):
        return 0.0
    x = np.arange(len(values), dtype=float)
    slope, _ = np.polyfit(x, values, 1)
    return float(slope)


def build_window_features(realtime_df: pd.DataFrame, feature_names: List[str],
                          window_minutes: int,
                          lab_timestamps: pd.Series) -> pd.DataFrame:
    """对每个化验时刻 t，取 (t-window_minutes, t] 窗口内的实时数据，
    为每个特征计算 末值/均值/斜率，返回特征宽表（timestamp + {特征}__{聚合}）。

    realtime_df   高频实时表（需含 timestamp 与 feature_names 列）
    feature_names 原始特征名清单
    window_minutes 特征窗口长度（分钟）
    lab_timestamps 化验时刻序列（pd.Series of Timestamp）
    """
    rt = realtime_df.sort_values("timestamp")
    rt_ts = rt["timestamp"].to_numpy()
    window = pd.Timedelta(minutes=window_minutes)

    rows: List[dict] = []
    for t in lab_timestamps:
        mask = (rt_ts > t - window) & (rt_ts <= t)
        win = rt.loc[mask]
        row: dict = {"timestamp": t}
        for name in feature_names:
            vals = win[name].to_numpy(dtype=float)
            if len(vals) == 0:
                row[f"{name}__last"] = np.nan
                row[f"{name}__mean"] = np.nan
                row[f"{name}__slope"] = np.nan
            else:
                row[f"{name}__last"] = float(vals[-1])
                row[f"{name}__mean"] = float(vals.mean())
                row[f"{name}__slope"] = _window_slope(vals)
        rows.append(row)

    out = pd.DataFrame(rows)
    out["timestamp"] = pd.to_datetime(out["timestamp"])
    return out
