import sys
from pathlib import Path

# 确保 backend/ 在 sys.path（pytest 从任意 cwd 运行也能 import app）
sys.path.insert(0, str(Path(__file__).resolve().parent))
