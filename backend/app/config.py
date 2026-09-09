"""全局配置：数据库 URL（SQLite dev / 可切 MySQL·PG）、前端静态目录、灰分模型参数路径。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 环境变量可覆盖（conftest 用它把 pytest 隔离到临时库，绝不触碰真实数据）
DATABASE_URL = os.environ.get("DMCS_DATABASE_URL") or f"sqlite:///{(DATA_DIR / 'dense_medium.db').as_posix()}"

# 粗精煤泥灰分预测模型参数文件（当前为占位经验公式，后期替换为训练模型）
MODEL_DIR = BASE_DIR / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)
ASH_MODEL_PATH = MODEL_DIR / "coarse_slime_ash_model.json"

# 前端静态目录（相对后端定位：workspace 根的 frontend/，随仓库整体移动自动适配）
FRONTEND_DIR = BASE_DIR.parent / "frontend"
