from sqlalchemy import JSON, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class RetentionHourBasis(Base):
    """Permanent chemistry-only evidence, not a reconstructed model prediction."""
    __tablename__ = 'retention_hour_bases'
    start: Mapped[str] = mapped_column(String(32), primary_key=True)
    end: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(32))
    checksum: Mapped[str] = mapped_column(String(64))
    payload: Mapped[bytes] = mapped_column(LargeBinary)


class RetentionArchive(Base):
    __tablename__ = 'retention_archives'
    start: Mapped[str] = mapped_column(String(32), primary_key=True)
    end: Mapped[str] = mapped_column(String(32), index=True)
    archive_id: Mapped[str] = mapped_column(String(160), unique=True)
    manifest: Mapped[str] = mapped_column(String(512))
    checksum: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24))
    sample_count: Mapped[int] = mapped_column(Integer)
    raw_count: Mapped[int] = mapped_column(Integer)
    raw_min_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    raw_max_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    point_ranges: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Temporary deletion receipts; cleared once the matching raw rows are gone.
    receipts: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)


class RetentionState(Base):
    __tablename__ = 'retention_state'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
