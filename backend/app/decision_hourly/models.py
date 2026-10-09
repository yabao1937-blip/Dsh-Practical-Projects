from sqlalchemy import JSON, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class DecisionCycleContext(Base):
    __tablename__ = 'decision_cycle_contexts'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    definition: Mapped[dict] = mapped_column(JSON)


class DecisionCycleSample(Base):
    __tablename__ = 'decision_cycle_samples'
    slot: Mapped[int] = mapped_column(Integer, primary_key=True)
    sampled_at: Mapped[str] = mapped_column(String(32), index=True)
    context_key: Mapped[str] = mapped_column(String(64), ForeignKey('decision_cycle_contexts.key'))
    fingerprint: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSON)


class DecisionCycleHour(Base):
    __tablename__ = 'decision_cycle_hours'
    start: Mapped[str] = mapped_column(String(32), primary_key=True)
    end: Mapped[str] = mapped_column(String(32))
    generated_at: Mapped[str] = mapped_column(String(32))
    snapshot: Mapped[dict] = mapped_column(JSON)
