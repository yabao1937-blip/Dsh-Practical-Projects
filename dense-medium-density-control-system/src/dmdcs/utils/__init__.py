"""通用工具：模型持久化、JSON、日志"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

import joblib


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def save_model(model: Any, path: str) -> None:
    """joblib 持久化模型（含特征列名等状态）"""
    ensure_dir(os.path.dirname(path))
    joblib.dump(model, path)


def load_model(path: str) -> Any:
    return joblib.load(path)


def save_json(obj: Any, path: str, **kwargs: Any) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str, **kwargs)


def load_json(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
