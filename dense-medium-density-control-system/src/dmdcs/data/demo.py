"""合成 demo 数据生成

在真实数据表就绪前，按 targets.yaml 特征字典生成：
  - data/raw/realtime.csv：高频实时可测变量（软测量输入，每 5 分钟一行）
  - data/raw/lab.csv     ：低频化验真值（每 2 小时一行）

生成逻辑：先构造特征序列（随机游走 + 系统启停 regime），再由「特征 -> 真值」
的确定性工艺关系 + 噪声得到软测量目标真值；化验值 = 真值 + 化验噪声。

真实表就绪后，用相同表结构（timestamp + 特征列 / timestamp + 目标列）的
CSV 替换即可无缝接入 loader，无需改代码。
"""

from __future__ import annotations

import os
from typing import List

import numpy as np
import pandas as pd

# 与 targets.yaml 的 features 清单保持一致
FEATURE_COLUMNS: List[str] = [
    "raw_ash", "coal_amount", "sysA", "sysB", "sys401", "sys402",
    "desliming473", "desliming474", "is_stoppage", "level",
]

# 与 targets.yaml 的 targets 键保持一致
TARGET_COLUMNS: List[str] = [
    "clean_ash", "medium_viscosity", "clean_moisture",
    "suspension_density", "recovery",
]


def _random_walk(rng: np.random.Generator, n: int, start: float,
                 step: float, lo: float, hi: float) -> np.ndarray:
    """有界随机游走（工艺量缓慢漂移）"""
    x = np.empty(n, dtype=float)
    x[0] = start
    for i in range(1, n):
        nxt = x[i - 1] + rng.normal(0, step)
        x[i] = float(np.clip(nxt, lo, hi))
    return x


def _regime_series(rng: np.random.Generator, n: int, n_change_points: int,
                   on_prob: float = 0.7) -> np.ndarray:
    """系统启停信号：随机切换点的 0/1 序列（模拟 401/402/A/B 系统运行）"""
    change_points = sorted(rng.choice(np.arange(1, n), size=n_change_points, replace=False))
    out = np.empty(n, dtype=int)
    state = 1 if rng.random() < on_prob else 0
    start = 0
    for cp in list(change_points) + [n]:
        out[start:cp] = state
        state = 1 - state
        start = cp
    return out


def generate_demo_data(realtime_path: str, lab_path: str,
                       sample_period_seconds: int = 300,
                       lab_period_hours: int = 2,
                       days: int = 45,
                       random_state: int = 42):
    """生成 demo 实时表与化验表，返回 (实时行数, 化验行数)"""
    rng = np.random.default_rng(random_state)
    n = days * 24 * 3600 // sample_period_seconds
    ts = pd.date_range("2025-01-01 00:00:00", periods=n, freq=f"{sample_period_seconds}s")
    df = pd.DataFrame({"timestamp": ts})

    # ---------- 特征（软测量输入） ----------
    df["raw_ash"] = _random_walk(rng, n, 20.0, 0.12, 15.0, 30.0)
    df["coal_amount"] = _random_walk(rng, n, 400.0, 3.0, 250.0, 520.0)
    df["sysA"] = _regime_series(rng, n, 40)
    df["sysB"] = _regime_series(rng, n, 40)
    df["sys401"] = _regime_series(rng, n, 40)
    df["sys402"] = _regime_series(rng, n, 40)
    df["desliming473"] = _regime_series(rng, n, 8, on_prob=0.92)
    df["desliming474"] = _regime_series(rng, n, 8, on_prob=0.92)
    # 停机（低负荷）：少数几段长时段
    df["is_stoppage"] = 0
    for s in rng.choice(np.arange(n - 60), size=6, replace=False):
        df.loc[s:s + int(rng.integers(12, 60)), "is_stoppage"] = 1
    df["level"] = _random_walk(rng, n, 1.05, 0.01, 0.85, 1.40)

    # ---------- 软测量真值（由特征经确定性工艺关系 + 噪声得到） ----------
    raw = df["raw_ash"].to_numpy()
    coal = df["coal_amount"].to_numpy()
    level = df["level"].to_numpy()
    stop = df["is_stoppage"].to_numpy()
    a, b = df["sysA"].to_numpy(), df["sysB"].to_numpy()
    c4, c4b = df["sys401"].to_numpy(), df["sys402"].to_numpy()
    d473, d474 = df["desliming473"].to_numpy(), df["desliming474"].to_numpy()

    df["clean_ash"] = (10.6 + 0.35 * (raw - 20.0) + 2.4 * (level - 1.0)
                       + 0.30 * c4 + 0.25 * c4b - 0.20 * a - 0.20 * b
                       + rng.normal(0, 0.22, n))
    df["medium_viscosity"] = (3.0 + 3.2 * (level - 1.0) + 0.08 * (raw - 20.0)
                              + 0.20 * c4 + 0.20 * c4b + 0.15 * a + 0.15 * b
                              + rng.normal(0, 0.10, n))
    df["clean_moisture"] = (8.6 + 0.08 * (raw - 20.0) - 0.15 * (coal - 400.0) / 100.0
                            + 0.9 * (1 - d473) + 0.9 * (1 - d474)
                            + rng.normal(0, 0.15, n))
    df["suspension_density"] = (1.450 + 0.010 * (raw - 20.0) / 10.0 + 0.015 * (level - 1.0)
                                + 0.004 * c4 + 0.004 * c4b
                                + rng.normal(0, 0.003, n))
    df["recovery"] = (80.0 + 0.45 * (20.0 - raw) - 3.0 * (level - 1.0) - 6.0 * stop
                      + rng.normal(0, 1.2, n))

    # ---------- 实时表：只保留可测特征 ----------
    realtime = df[["timestamp"] + FEATURE_COLUMNS].copy()
    realtime["timestamp"] = realtime["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")

    # ---------- 化验表：每 lab_period_hours 采样一次真值 + 化验噪声 ----------
    step = int(lab_period_hours * 3600 // sample_period_seconds)
    lab_idx = np.arange(step // 2, n, step)  # 中段偏移，避免首行边界
    lab = df.iloc[lab_idx].copy()
    lab_noise = pd.DataFrame({
        "clean_ash": rng.normal(0, 0.08, len(lab)),
        "medium_viscosity": rng.normal(0, 0.04, len(lab)),
        "clean_moisture": rng.normal(0, 0.08, len(lab)),
        "suspension_density": rng.normal(0, 0.001, len(lab)),
        "recovery": rng.normal(0, 0.5, len(lab)),
    })
    for col in TARGET_COLUMNS:
        lab[col] = (lab[col].to_numpy() + lab_noise[col].to_numpy()).round(3)
    lab = lab[["timestamp"] + TARGET_COLUMNS].copy()
    lab["timestamp"] = lab["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")

    os.makedirs(os.path.dirname(realtime_path), exist_ok=True)
    realtime.to_csv(realtime_path, index=False)
    lab.to_csv(lab_path, index=False)
    return len(realtime), len(lab)
