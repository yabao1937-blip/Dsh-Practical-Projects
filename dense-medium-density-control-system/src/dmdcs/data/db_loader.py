"""真实数据数据库读取：realtime_signals / lab_assays -> DataFrame

与 CSV 加载器（loader.py）输出同结构，训练管线可无缝切换数据来源：
  - 未建库：走 CSV（demo / 手工填表）
  - 已建库：走 SQLite（真实 PLC/化验数据入库后自动优先）
"""

from __future__ import annotations

import sqlite3
from typing import List

import pandas as pd


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def load_realtime_from_db(db_path: str, feature_names: List[str]) -> pd.DataFrame:
    """读取高频实时表（仅取所需特征列，含 timestamp）"""
    cols = ", ".join(f'"{c}"' for c in ["timestamp"] + feature_names)
    with _connect(db_path) as conn:
        df = pd.read_sql(f"SELECT {cols} FROM realtime_signals ORDER BY timestamp", conn)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.reset_index(drop=True)


def load_lab_from_db(db_path: str, target_columns: List[str]) -> pd.DataFrame:
    """读取低频化验表（仅取所需目标列，含 timestamp）"""
    cols = ", ".join(f'"{c}"' for c in ["timestamp"] + target_columns)
    with _connect(db_path) as conn:
        df = pd.read_sql(f"SELECT {cols} FROM lab_assays ORDER BY timestamp", conn)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.reset_index(drop=True)
