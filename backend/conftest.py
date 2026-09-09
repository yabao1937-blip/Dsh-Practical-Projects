"""pytest 全局配置：把 backend/ 加入 sys.path，使任意 cwd 下都能 import app。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
