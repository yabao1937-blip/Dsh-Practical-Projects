"""数据层：demo 生成 / 加载 / 特征工程 / 对齐"""
from .demo import FEATURE_COLUMNS, TARGET_COLUMNS, generate_demo_data
from .features import AGG_SUFFIXES, build_window_features
from .align import build_aligned_dataset
from .loader import load_realtime, load_lab, save_aligned

__all__ = [
    "FEATURE_COLUMNS", "TARGET_COLUMNS", "generate_demo_data",
    "AGG_SUFFIXES", "build_window_features",
    "build_aligned_dataset",
    "load_realtime", "load_lab", "save_aligned",
]
