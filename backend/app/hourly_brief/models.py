from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class HourlyBriefSchedule(Base):
    __tablename__ = "hourly_brief_schedule"
    key: Mapped[str] = mapped_column(String(16), primary_key=True)
    activated_at: Mapped[str] = mapped_column(String(32))
    next_hour: Mapped[str] = mapped_column(String(32))


class HourlyBriefRow(Base):
    __tablename__ = "hourly_brief_rows"
    hour: Mapped[str] = mapped_column(String(32), primary_key=True)
    generated_at: Mapped[str] = mapped_column(String(32))
    snapshot: Mapped[dict] = mapped_column(JSON)


class HourlyBriefFeedback(Base):
    """Append-only operator observations, separate from frozen predictions."""
    __tablename__ = "hourly_brief_feedback"
    __table_args__ = (Index("ix_hourly_brief_feedback_hour_id", "hour", "id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hour: Mapped[str] = mapped_column(String(32), ForeignKey("hourly_brief_rows.hour"))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32))
    client_id: Mapped[str] = mapped_column(String(36), unique=True)
