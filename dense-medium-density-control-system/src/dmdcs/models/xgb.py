"""XGBoost 回归预测器（CPU 版；device 可在 settings.yaml 中切换 cuda）"""

from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd
import xgboost as xgb

from .base import BasePredictor


class XGBPredictor(BasePredictor):
    name = "xgboost"

    def _fit_impl(self, X: pd.DataFrame, y: pd.Series) -> None:
        params = {**self.params}
        params.setdefault("random_state", self.random_state)
        self.model = xgb.XGBRegressor(**params, n_jobs=-1)
        self.model.fit(X, y)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return self.model.predict(X)

    def feature_importance(self) -> Dict[str, float]:
        return dict(zip(self.feature_names,
                        self.model.feature_importances_.astype(float).tolist()))
