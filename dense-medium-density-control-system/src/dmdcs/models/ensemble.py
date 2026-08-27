"""模型集成：best（选最优单模型）| weighted（按验证 RMSE 倒数加权）

集成对象持有各单模型与权重，输出加权平均预测；
后续 TCN 加入时，直接 add_member 即可扩展为三模型集成。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from .base import BasePredictor


class EnsemblePredictor:
    """软测量集成预测器"""

    def __init__(self, method: str = "weighted"):
        self.method = method  # best | weighted
        # 成员：(name, predictor, val_rmse) -> finalize 后第三项变为权重
        self.members: List[Tuple[str, BasePredictor, float]] = []

    def add_member(self, name: str, predictor: BasePredictor,
                   val_rmse: float | None = None) -> None:
        self.members.append((name, predictor, val_rmse))

    def finalize(self) -> None:
        """根据各成员验证 RMSE 计算权重（在训练集内部切分上得到，避免用测试集）"""
        if not self.members:
            return
        rmses = [m[2] if m[2] is not None and m[2] > 0 else 1e-9 for m in self.members]
        if self.method == "best":
            best_idx = int(np.argmin(rmses))
            weights = [1.0 if i == best_idx else 0.0 for i in range(len(self.members))]
        else:  # weighted：权重 ∝ 1/RMSE
            inv = [1.0 / r for r in rmses]
            total = float(sum(inv))
            weights = [w / total for w in inv]
        self.members = [(name, model, w)
                        for (name, model, _), w in zip(self.members, weights)]

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        if not self.members:
            raise RuntimeError("集成模型为空，请先 add_member + finalize")
        preds = [model.predict(X) * w for _, model, w in self.members]
        return np.sum(preds, axis=0)

    def feature_importance(self) -> Dict[str, float]:
        """成员重要性按权重平均"""
        names = self.members[0][1].feature_names
        imp = np.zeros(len(names))
        for _, model, w in self.members:
            imp += w * np.array(list(model.feature_importance().values()))
        return dict(zip(names, imp.tolist()))
