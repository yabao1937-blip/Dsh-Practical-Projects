"""新采集表不属于 /state 镜像，化验和模型表不会被采集覆盖。"""
from sqlalchemy import JSON, Boolean, Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class TelemetrySource(Base):
    __tablename__ = "telemetry_sources"
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(24))
    owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[str | None] = mapped_column(String(32), nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_sample_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_seconds: Mapped[float] = mapped_column(Float, default=0)


class TelemetryPoint(Base):
    __tablename__ = "telemetry_points"
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    config: Mapped[dict] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    latest: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_history_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class TelemetryReading(Base):
    __tablename__ = "telemetry_readings"
    __table_args__ = (
        UniqueConstraint("source_id", "point_key", "batch_id", name="uq_telemetry_batch_point"),
        Index("ix_telemetry_point_time", "source_id", "point_key", "received_at"),
        Index("ix_telemetry_retention", "received_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64))
    point_key: Mapped[str] = mapped_column(String(64))
    batch_id: Mapped[str] = mapped_column(String(36))
    received_at: Mapped[str] = mapped_column(String(32))
    sample: Mapped[dict] = mapped_column(JSON)
