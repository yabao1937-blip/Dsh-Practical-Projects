"""原始数据读取：实时表 + 化验表

真实表就绪后，把导出 CSV（同结构）放入 data/raw 即可无缝替换 demo 数据；
未来接数据库/PLC 时，只需在这里增加对应的 load 函数。
"""

from __future__ import annotations

import os

import pandas as pd

from ..utils import ensure_dir


def load_realtime(path: str) -> pd.DataFrame:
    """高频实时可测变量表（timestamp + 特征列）"""
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values("timestamp").reset_index(drop=True)


def load_lab(path: str) -> pd.DataFrame:
    """低频化验真值表（timestamp + 目标列）"""
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.sort_values("timestamp").reset_index(drop=True)


def save_aligned(df: pd.DataFrame, path: str) -> None:
    ensure_dir(os.path.dirname(path))
    df.to_csv(path, index=False)
