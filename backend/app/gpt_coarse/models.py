"""Append-only GPT artifact versions, independent of the legacy model/store mirror."""
from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class GptCoarseRevision(Base):
    __tablename__ = 'gpt_coarse_model_revisions'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    activated_at: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    artifact: Mapped[dict] = mapped_column(JSON, nullable=False)
