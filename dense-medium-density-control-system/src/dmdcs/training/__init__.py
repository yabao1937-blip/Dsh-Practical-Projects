"""训练层：指标计算 / 时序切分 / 训练管线 / 评估"""
from .metrics import regression_metrics
from .split import TIME_COL, time_split, walk_forward_folds
from .trainer import train_target, train_all
from .evaluate import evaluate_target

__all__ = [
    "regression_metrics",
    "TIME_COL", "time_split", "walk_forward_folds",
    "train_target", "train_all",
    "evaluate_target",
]
