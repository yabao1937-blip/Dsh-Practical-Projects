"""预测器抽象基类

统一接口（fit / predict / feature_importance / name / params），
XGB、LGBM 以及后续步骤的 TCN 均实现此接口，便于在训练与集成中互换。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List

import numpy as np
import pandas as pd


class BasePredictor(ABC):
    """软测量单模型预测器接口"""

    name: str = "base"

    def __init__(self, params: Dict[str, Any], random_state: int = 42):
        self.params = dict(params)
        self.random_state = random_state
        self.feature_names: List[str] = []
        self.model: Any = None

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "BasePredictor":
        """统一 fit 入口：记录特征列名后交给子类实现"""
        self.feature_names = list(X.columns)
        self._fit_impl(X, y)
        return self

    @abstractmethod
    def _fit_impl(self, X: pd.DataFrame, y: pd.Series) -> None:
        """子类实现实际训练"""

    @abstractmethod
    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """返回预测值数组"""

    @abstractmethod
    def feature_importance(self) -> Dict[str, float]:
        """返回 {特征列名: 重要性} 字典"""
