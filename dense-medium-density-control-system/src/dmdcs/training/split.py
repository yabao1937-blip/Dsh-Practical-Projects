"""时序切分：按时间排序后切分，禁止随机切分（避免时序数据泄漏）"""
from __future__ import annotations

from typing import List, Tuple

import pandas as pd

TIME_COL = "timestamp"


def time_split(df: pd.DataFrame, train_ratio: float) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """按时间顺序：前 train_ratio 训练，后 (1-train_ratio) 测试"""
    df = df.sort_values(TIME_COL).reset_index(drop=True)
    cut = int(len(df) * train_ratio)
    return df.iloc[:cut].copy(), df.iloc[cut:].copy()


def walk_forward_folds(df: pd.DataFrame, n_splits: int = 3,
                       val_fraction: float = 0.1) -> List[Tuple[pd.DataFrame, pd.DataFrame]]:
    """expanding-window 滚动验证

    第 i 折用「开头到 coverage*(i/n_splits)」训练，末尾连续 val_fraction 段验证；
    训练集随折数扩大，模拟真实上线后的滚动重训。
    """
    df = df.sort_values(TIME_COL).reset_index(drop=True)
    n = len(df)
    if n_splits < 1 or n < n_splits + 2:
        raise ValueError("样本量过小，无法执行 walk-forward")
    val_size = max(1, int(n * val_fraction))
    coverage = 1.0 - val_fraction * 0.5  # 预留余量，避免验证段越界
    folds: List[Tuple[pd.DataFrame, pd.DataFrame]] = []
    for i in range(1, n_splits + 1):
        train_end = min(int(n * coverage * (i / n_splits)), n - val_size)
        folds.append((df.iloc[:train_end].copy(),
                      df.iloc[train_end:train_end + val_size].copy()))
    return folds
