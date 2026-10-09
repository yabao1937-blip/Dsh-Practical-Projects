"""Validation and read projections for human feedback; never model inputs."""
import re
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from .models import HourlyBriefFeedback
from ..telemetry.domain import iso_time, parse_time


def canonical_hour(value: str) -> str:
    try:
        moment = parse_time(value)
    except (ValueError, OverflowError):
        raise ValueError("请选择已有简报的整点时间（包含时区）") from None
    if moment.minute or moment.second or moment.microsecond or moment.year <= 1601:
        raise ValueError("请选择已有简报的整点时间（包含时区）")
    return iso_time(moment)


class FeedbackIn(BaseModel):
    hour: str = Field(max_length=64)
    content: str = Field(max_length=2000)
    client_id: UUID

    @field_validator("hour")
    @classmethod
    def validate_hour(cls, value):
        return canonical_hour(value)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value):
        # Keep line breaks; reject invisible control characters unsupported by XLSX.
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", value):
            raise ValueError("反馈包含不支持的控制字符")
        value = value.strip()
        if not value:
            raise ValueError("请填写反馈内容")
        return value


def feedback_view(record):
    return {key: getattr(record, key) for key in ("id", "hour", "content", "created_at", "client_id")}


def feedback_summaries(db, hours):
    if not hours:
        return {}
    latest = select(func.max(HourlyBriefFeedback.id).label("last_id"),
                    func.count().label("total")).where(HourlyBriefFeedback.hour.in_(hours))\
        .group_by(HourlyBriefFeedback.hour).subquery()
    return {record.hour: {"total": total, "latest": feedback_view(record)}
            for record, total in db.execute(select(HourlyBriefFeedback, latest.c.total)
                .join(latest, HourlyBriefFeedback.id == latest.c.last_id))}
