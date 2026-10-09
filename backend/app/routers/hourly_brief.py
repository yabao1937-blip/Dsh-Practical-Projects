from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..auth import require_write
from ..hourly_brief.columns import COLUMNS
from ..hourly_brief.inputs import LOCAL
from ..hourly_brief.models import HourlyBriefFeedback, HourlyBriefRow, HourlyBriefSchedule
from ..hourly_brief.feedback import FeedbackIn, canonical_hour, feedback_summaries, feedback_view
from ..telemetry.domain import iso_time, utc_now
from ..services.decision_sources import config_view

router = APIRouter(prefix="/hourly-brief", tags=["小时推测简报"])


@router.post("/feedback", status_code=201, dependencies=[Depends(require_write)])
def create_feedback(body: FeedbackIn, response: Response, db: Session = Depends(get_db)):
    def existing():
        return db.scalar(select(HourlyBriefFeedback).where(HourlyBriefFeedback.client_id == str(body.client_id)))

    def reuse(record):
        if record.hour != body.hour or record.content != body.content:
            raise HTTPException(409, "提交标识已用于其他反馈，请重新打开后提交")
        response.status_code = 200
        return feedback_view(record)

    record = existing()
    if record:
        return reuse(record)
    if db.get(HourlyBriefRow, body.hour) is None:
        raise HTTPException(404, "该小时简报不存在，无法保存反馈")
    record = HourlyBriefFeedback(hour=body.hour, content=body.content,
                                created_at=iso_time(utc_now()), client_id=str(body.client_id))
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        found = existing()
        if found:
            return reuse(found)
        raise
    db.refresh(record)
    return feedback_view(record)


@router.get("/feedback")
def list_feedback(hour: str = Query(max_length=64), limit: int = Query(20, ge=1, le=100),
                  before_id: int | None = Query(None, ge=1), db: Session = Depends(get_db)):
    try:
        hour = canonical_hour(hour)
    except ValueError:
        raise HTTPException(422, "请选择已有简报的整点时间（包含时区）") from None
    if db.bind.dialect.name == "sqlite":
        db.execute(text("BEGIN"))
    if db.get(HourlyBriefRow, hour) is None:
        raise HTTPException(404, "该小时简报不存在")
    query = select(HourlyBriefFeedback).where(HourlyBriefFeedback.hour == hour)
    if before_id is not None:
        query = query.where(HourlyBriefFeedback.id < before_id)
    records = list(db.scalars(query.order_by(HourlyBriefFeedback.id.desc()).limit(limit + 1)))
    items = records[:limit]
    return {"items": [feedback_view(record) for record in items],
            "total": db.scalar(select(func.count()).select_from(HourlyBriefFeedback).where(HourlyBriefFeedback.hour == hour)),
            "next_before_id": items[-1].id if len(records) > limit else None}


@router.get("/export")
def export_rows(hour: list[str] = Query(min_length=1, max_length=100), db: Session = Depends(get_db)):
    from scripts.generate_hourly_brief_xlsx import generate_workbook
    if db.bind.dialect.name == "sqlite":
        db.execute(text("BEGIN"))
    rows = list(db.scalars(select(HourlyBriefRow).where(HourlyBriefRow.hour.in_(hour))
                          .order_by(HourlyBriefRow.hour.desc())))
    feedback = list(db.scalars(select(HourlyBriefFeedback).where(HourlyBriefFeedback.hour.in_([r.hour for r in rows]))
                              .order_by(HourlyBriefFeedback.id.desc())))
    return Response(generate_workbook(rows, feedback),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="hourly-brief.xlsx"'})


@router.get("")
def list_rows(day: date | None = None, limit: int = Query(100, ge=1, le=500),
              offset: int = Query(0, ge=0, le=1_000_000), db: Session = Depends(get_db)):
    if db.bind.dialect.name == "sqlite":
        db.execute(text("BEGIN"))
    filters = []
    if day:
        begin = datetime.combine(day, time(), tzinfo=LOCAL)
        filters = [HourlyBriefRow.hour >= iso_time(begin), HourlyBriefRow.hour < iso_time(begin + timedelta(days=1))]
    rows = list(db.scalars(select(HourlyBriefRow).where(*filters).order_by(HourlyBriefRow.hour.desc()).limit(limit).offset(offset)))
    feedback = feedback_summaries(db, [row.hour for row in rows])
    schedule = db.get(HourlyBriefSchedule, "main")
    return {
        "columns": [dict(key=key, label=label, unit=unit) for key, label, unit in COLUMNS],
        "rows": [dict(hour=row.hour, generated_at=row.generated_at, **row.snapshot,
                      feedback=feedback.get(row.hour, {"total": 0, "latest": None})) for row in rows],
        "total": db.scalar(select(func.count()).select_from(HourlyBriefRow).where(*filters)),
        "limit": limit, "offset": offset, "server_time": iso_time(utc_now()),
        "activated_at": schedule.activated_at if schedule else None,
        "next_hour": schedule.next_hour if schedule else None,
        "shared_sources": config_view(db),
        "policy": "从启用后的下一整点开始；取整点前已收到的最近记录；手工值缺失时使用已配置仪表默认值。",
    }
