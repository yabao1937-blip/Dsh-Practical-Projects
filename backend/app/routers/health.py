"""系统健康检查"""
from fastapi import APIRouter

from ..schemas import HealthResponse

router = APIRouter(tags=["系统"])


@router.get("/health", response_model=HealthResponse)
def health():
    return {"status": "ok", "modules": ["coal-records"]}
