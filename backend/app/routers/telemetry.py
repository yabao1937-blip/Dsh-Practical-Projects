"""实时点位、历史查询及后台只读采集启动；没有修改点表或写 PLC 的入口。"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..database import get_db
from ..auth import require_write
from ..telemetry.control import CollectorStartError, ensure_collector_started
from ..telemetry.models import TelemetryReading
from ..telemetry.repository import latest_view

router = APIRouter(prefix="/telemetry", tags=["工业数据采集"])


@router.post("/reconnect", dependencies=[Depends(require_write)], status_code=202)
def reconnect():
    """启动或复用服务器固定配置的采集进程；实际连接状态由 latest 确认。"""
    try:
        return JSONResponse(ensure_collector_started(), status_code=202)
    except CollectorStartError as exc:
        raise HTTPException(status_code=503, detail={"error_code": exc.code, "message": exc.message}) from None


@router.get("/latest")
def latest(source_id: str | None = Query(None, max_length=64), db: Session = Depends(get_db)):
    if db.bind.dialect.name == "sqlite":
        db.execute(text("BEGIN"))
    return latest_view(db, source_id)


@router.get("/history")
def history(source_id: str = Query(..., min_length=1, max_length=64),
            point_key: str = Query(..., min_length=1, max_length=64),
            limit: int = Query(100, ge=1, le=500), before_id: int | None = Query(None, ge=1),
            db: Session = Depends(get_db)):
    query = select(TelemetryReading).where(TelemetryReading.source_id == source_id,
        TelemetryReading.point_key == point_key)
    if before_id:
        query = query.where(TelemetryReading.id < before_id)
    rows = list(db.scalars(query.order_by(TelemetryReading.id.desc()).limit(limit + 1)))
    return {"items": [{"id": row.id, **row.sample} for row in rows[:limit]],
            "next_before_id": rows[limit - 1].id if len(rows) > limit else None}
