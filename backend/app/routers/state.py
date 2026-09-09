"""整库快照（GET /state 读取、PUT /state 整体重写，供前端数据层切换）"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import migrate
from ..services.state import load_store

router = APIRouter(prefix="/state", tags=["状态"])


@router.get("")
def get_state(db: Session = Depends(get_db)):
    """返回前端 App.store 等价结构（DB → store 桥接）"""
    return load_store(db)


@router.put("")
def put_state(store: dict):
    """整体重写库（前端 saveStore 提交整库快照）"""
    return migrate.replace(store)
