"""localStorage 一次性迁移"""
from fastapi import APIRouter

from ..schemas import MigrateReport
from ..services import migrate

router = APIRouter(prefix="/migrate", tags=["迁移"])


@router.post("/preview", response_model=MigrateReport)
def preview(store: dict):
    """dry-run：返回各表规划条数 + 未识别键，不写库。"""
    return migrate.preview(store)


@router.post("/localstorage", response_model=MigrateReport)
def apply_migration(store: dict):
    """把前端导出的 dmcs_store JSON 完整迁入库。"""
    return migrate.apply(store)
