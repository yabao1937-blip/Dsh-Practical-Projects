from sqlalchemy import JSON, String
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
