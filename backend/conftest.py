"""pytest 全局配置：backend/ 加入 sys.path + 数据库隔离。

隔离规则（重要）：
- 必须在 import app.* 之前设置 DMCS_DATABASE_URL（config 在 import 时读取），
  把整轮测试锁进临时 SQLite，绝不读写 data/dense_medium.db；
- 既有用例隐含「库中已有种子数据」的假设（test_state 会重新 PUT 种子，
  此前依赖真实 DB 恰好保存着上一轮种子），因此在临时库上会话级播种一次。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

_TMP_DIR = tempfile.mkdtemp(prefix="dmcs_pytest_")
os.environ["DMCS_DATABASE_URL"] = f"sqlite:///{(Path(_TMP_DIR) / 'test.db').as_posix()}"

from app import models  # noqa: E402
from app.database import engine  # noqa: E402
from app.services import migrate  # noqa: E402

models.Base.metadata.create_all(bind=engine)
SEED = json.loads((BACKEND_DIR / "data" / "seed_store.json").read_text(encoding="utf-8"))
migrate.replace(SEED)
