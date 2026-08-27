"""配置加载：settings.yaml + targets.yaml -> 指标字典（Config）

- settings.yaml：数据源路径、采样/化验/窗口占位值、切分方式、模型超参
- targets.yaml ：指标字典（特征清单 + 软测量目标指标定义）
真实数据表就绪后，只需更新 settings.yaml 中的数据路径与占位值，代码无需改动。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List

import yaml

# src/dmdcs/config.py -> 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")


@dataclass
class TargetSpec:
    """单个软测量目标指标的定义（来自 targets.yaml）"""

    key: str            # 指标键名（对应数据表中的列名）
    name: str           # 中文名
    unit: str           # 单位
    range: List[float]  # 合理取值范围 [min, max]
    kind: str           # soft_sensor（不可在线测量，需软测量推断）等
    features: List[str] # 该指标使用的原始特征名（未聚合）


class Config:
    """统一配置入口：settings（运行/模型配置）+ targets（指标字典）"""

    def __init__(self, settings_path: str | None = None, targets_path: str | None = None):
        self.settings_path = settings_path or os.path.join(CONFIG_DIR, "settings.yaml")
        self.targets_path = targets_path or os.path.join(CONFIG_DIR, "targets.yaml")
        with open(self.settings_path, encoding="utf-8") as f:
            self.settings: Dict[str, Any] = yaml.safe_load(f)
        with open(self.targets_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        self.features: List[Dict[str, Any]] = raw["features"]
        self.targets: Dict[str, TargetSpec] = {
            key: TargetSpec(key=key, **spec) for key, spec in raw["targets"].items()
        }

    # ---------------- 路径 ----------------
    def resolve(self, path: str) -> str:
        """相对路径按项目根目录解析"""
        return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)

    def realtime_csv(self) -> str:
        return self.resolve(self.settings["data"]["realtime_csv"])

    def lab_csv(self) -> str:
        return self.resolve(self.settings["data"]["lab_csv"])

    def aligned_csv(self) -> str:
        return self.resolve(self.settings["data"]["aligned_csv"])

    def processed_dir(self) -> str:
        return self.resolve(self.settings["data"]["processed_dir"])

    def artifacts_dir(self) -> str:
        return self.resolve(self.settings["artifacts_dir"])

    # ---------------- 特征 / 目标 ----------------
    def all_feature_names(self) -> List[str]:
        """全部原始特征名（targets.yaml 的 features 清单）"""
        return [f["name"] for f in self.features]

    def target_feature_columns(self, target_key: str, suffixes: List[str]) -> List[str]:
        """目标指标对应的聚合后特征列名（如 raw_ash__last）"""
        target = self.targets[target_key]
        return [f"{name}{suffix}" for name in target.features for suffix in suffixes]
