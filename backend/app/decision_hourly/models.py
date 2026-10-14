from sqlalchemy import JSON, ForeignKey, Integer, String, LargeBinary
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


class DecisionAshSettlement(Base):
    """Retrospective ash only; never replaces the original decision/advice archive."""
    __tablename__ = 'gpt_ash_settlements'
    start: Mapped[str] = mapped_column(String(32), primary_key=True)
    end: Mapped[str] = mapped_column(String(32))
    deadline: Mapped[str] = mapped_column(String(32))
    generated_at: Mapped[str] = mapped_column(String(32))
    snapshot: Mapped[dict] = mapped_column(JSON)


class DecisionTelemetryPin(Base):
    """Keep authoritative readings as long as hot OR compressed decisions cite them."""
    __tablename__ = 'decision_telemetry_pins'
    reading_id: Mapped[int] = mapped_column(Integer, ForeignKey('telemetry_readings.id', ondelete='RESTRICT'), primary_key=True)
    first_slot: Mapped[int] = mapped_column(Integer)


class DecisionEvidenceArchive(Base):
    __tablename__ = 'gpt_decision_evidence_archives'
    start: Mapped[str] = mapped_column(String(32), primary_key=True)
    payload: Mapped[bytes] = mapped_column(LargeBinary)
    checksum: Mapped[str] = mapped_column(String(64))
    sample_count: Mapped[int] = mapped_column(Integer)


class DensityObservation(Base):
    __tablename__ = 'gpt_density_observations'
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    snapshot: Mapped[dict] = mapped_column(JSON)


class DensityAction(Base):
    __tablename__ = 'gpt_density_actions'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    detected_at: Mapped[str] = mapped_column(String(32), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    evidence: Mapped[dict] = mapped_column(JSON)


class DensityActionEntry(Base):
    __tablename__ = 'gpt_density_action_entries'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action_id: Mapped[str] = mapped_column(String(64), ForeignKey('gpt_density_actions.id'), index=True)
    client_id: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[str] = mapped_column(String(32))
    content: Mapped[dict] = mapped_column(JSON)
