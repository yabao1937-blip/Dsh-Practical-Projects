from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..hourly_brief.columns import COLUMNS
from ..hourly_brief.inputs import LOCAL
from ..hourly_brief.models import HourlyBriefRow, HourlyBriefSchedule
from ..telemetry.domain import iso_time, utc_now
from ..services.decision_sources import config_view

router = APIRouter(prefix="/hourly-brief", tags=["小时推测简报"])


@router.get("/export")
def export_rows(hour: list[str] = Query(min_length=1, max_length=100), db: Session = Depends(get_db)):
    from scripts.generate_hourly_brief_xlsx import generate_workbook
    rows = list(db.scalars(select(HourlyBriefRow).where(HourlyBriefRow.hour.in_(hour))
                          .order_by(HourlyBriefRow.hour.desc())))
    return Response(generate_workbook(rows),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="hourly-brief.xlsx"'})


@router.get("")
def list_rows(day: date | None = None, limit: int = Query(100, ge=1, le=500),
              offset: int = Query(0, ge=0, le=1_000_000), db: Session = Depends(get_db)):
    filters = []
    if day:
        begin = datetime.combine(day, time(), tzinfo=LOCAL)
        filters = [HourlyBriefRow.hour >= iso_time(begin), HourlyBriefRow.hour < iso_time(begin + timedelta(days=1))]
    rows = db.scalars(select(HourlyBriefRow).where(*filters).order_by(HourlyBriefRow.hour.desc()).limit(limit).offset(offset))
    schedule = db.get(HourlyBriefSchedule, "main")
    return {
        "columns": [dict(key=key, label=label, unit=unit) for key, label, unit in COLUMNS],
        "rows": [dict(hour=row.hour, generated_at=row.generated_at, **row.snapshot) for row in rows],
        "total": db.scalar(select(func.count()).select_from(HourlyBriefRow).where(*filters)),
        "limit": limit, "offset": offset, "server_time": iso_time(utc_now()),
        "activated_at": schedule.activated_at if schedule else None,
        "next_hour": schedule.next_hour if schedule else None,
        "shared_sources": config_view(db),
        "policy": "从启用后的下一整点开始；取整点前已收到的最近记录；手工值缺失时使用已配置仪表默认值。",
    }
