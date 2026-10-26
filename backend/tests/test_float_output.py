"""Filter-press plate counters to hourly flotation output: reset-safe positive deltas."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.telemetry import float_output
from app.telemetry.domain import iso_time
from app.telemetry.models import TelemetryPoint, TelemetryReading
from app.telemetry.float_output import (CADENCE, DERIVATION, MAX_STEP_PLATES, MIN_COVERAGE,
                                         PLATE_TONNES, float_rate_stats, floor_hour)

HOUR = datetime(2026, 10, 26, 2, tzinfo=timezone.utc)  # 10:00 Beijing; window 09:00–10:00
ITEMS = {label: item for label, item in float_output.PLATE_MEMBERS}


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'plates.db').as_posix()}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    float_output._cache.clear()
    with sessions() as session:
        yield session
    engine.dispose()
    float_output._cache.clear()


def add_point(session, label):
    session.add(TelemetryPoint(source_id="kep", key=f"tag_{label}", enabled=True,
                               config=dict(item_id=ITEMS[label], name=f"{label}/PLATE_CNT")))


def add_samples(session, label, curve, start=HOUR - timedelta(hours=1), every=5):
    """curve: list of (offset_seconds, value); readings every `every` seconds."""
    key = f"tag_{label}"
    marks = {round(offset / 5) * 5.0: value for offset, value in curve}
    value = curve[0][1]
    for i in range(int(timedelta(hours=1) / CADENCE)):
        offset = (i + 1) * CADENCE.total_seconds()
        if offset % every:
            continue
        value = marks.get(offset, value)
        when = start + timedelta(seconds=offset)
        session.add(TelemetryReading(source_id="kep", point_key=key, batch_id=uuid4().hex,
            received_at=iso_time(when), sample=dict(key=key, value=value, quality="good",
                                                    received_at=iso_time(when))))


def full_curve(increments):
    """Marks at the base value, applying +increments at spread 5-second offsets."""
    curve = [(5.0, 100.0)]
    step = 700.0 / (len(increments) + 1)
    total = 0.0
    for i, rise in enumerate(increments):
        total += rise
        curve.append((round((5.0 + step * (i + 1)) / 5) * 5.0, 100.0 + total))
    return curve


def test_positive_deltas_with_mid_hour_reset(db):
    add_point(db, "A415")
    add_samples(db, "A415", full_curve([13, 5]) + [(500.0, 3.0), (600.0, 8.0)])  # reset at 500s
    db.commit()
    stats = float_rate_stats(db, HOUR)
    assert stats is not None
    # +13 +5, reset drop ignored, +5 after reset: 23 plates -> 299 t.
    assert stats["total_plates"] == pytest.approx(23)
    assert stats["tonnes"] == pytest.approx(23 * PLATE_TONNES)
    assert stats["coverage"] == pytest.approx(719 / 720) and stats["coverage"] >= MIN_COVERAGE


def test_dead_press_contributes_zero_and_stays_usable(db):
    add_point(db, "A415")
    add_point(db, "A417")  # constant zero counter, full coverage
    add_samples(db, "A415", full_curve([13]))
    add_samples(db, "A417", [(5.0, 0.0)])
    db.commit()
    stats = float_rate_stats(db, HOUR)
    assert stats is not None and stats["total_plates"] == pytest.approx(13)
    assert stats["plates"] == {"A415": 13.0}  # zero deltas omitted from the detail


def test_zero_plates_is_a_valid_production_value(db):
    add_point(db, "A415")
    add_samples(db, "A415", [(5.0, 118.0)])
    db.commit()
    stats = float_rate_stats(db, HOUR)
    assert stats is not None and stats["total_plates"] == 0 and stats["tonnes"] == 0


def test_low_coverage_window_is_rejected(db):
    add_point(db, "A415")
    curve = [(5.0 + i * 30, 100.0 + i) for i in range(119)]
    add_samples(db, "A415", curve, every=30)  # one reading per 30 seconds ≈ 17% coverage
    db.commit()
    assert float_rate_stats(db, HOUR) is None
    assert MIN_COVERAGE == 0.8


def test_single_step_spike_marks_the_hour_unusable(db):
    add_point(db, "A415")
    add_samples(db, "A415", full_curve([]) + [(400.0, 100.0 + MAX_STEP_PLATES + 1)])
    db.commit()
    assert float_rate_stats(db, HOUR) is None


def test_boundary_delta_is_attributed_to_this_hour(db):
    add_point(db, "A415")
    start = HOUR - timedelta(hours=1)
    key = "tag_A415"
    for offset, value in ((-10.0, 100.0), (-5.0, 100.0), (5.0, 103.0)):  # +3 crosses the boundary
        when = start + timedelta(seconds=offset) if offset > 0 else start + timedelta(seconds=offset)
        session_stamp = iso_time(when)
        db.add(TelemetryReading(source_id="kep", point_key=key, batch_id=uuid4().hex,
            received_at=session_stamp, sample=dict(key=key, value=value, quality="good",
                                                   received_at=session_stamp)))
    db.commit()
    # Sparse window: coverage gate would reject it, so exercise the internals directly.
    series = float_output._series(db, float_output._plate_points(db), start, HOUR)
    stats = float_output._hour_stats(series, start, HOUR)
    assert stats["plates"] == {"A415": 3.0}


def test_window_is_the_last_complete_hour_and_mid_hour_moments_floor(db):
    assert floor_hour(HOUR) == HOUR
    assert floor_hour(HOUR + timedelta(minutes=23)) == HOUR
    add_point(db, "A415")
    add_samples(db, "A415", full_curve([13]), start=HOUR - timedelta(hours=2))
    db.commit()
    # No samples inside [HOUR-1h, HOUR): nothing active in the window -> fallback.
    assert float_rate_stats(db, HOUR + timedelta(minutes=23)) is None


def test_bad_quality_and_nonnumeric_samples_are_ignored(db):
    add_point(db, "A415")
    start = HOUR - timedelta(hours=1)
    key = "tag_A415"
    for i in range(1, 721):
        when = start + timedelta(seconds=i * 5)
        stamp = iso_time(when)
        value = 100 + (2 if i > 360 else 0)
        quality = "uncertain" if i == 400 else "good"
        db.add(TelemetryReading(source_id="kep", point_key=key, batch_id=uuid4().hex,
            received_at=stamp, sample=dict(key=key, value=value, quality=quality, received_at=stamp)))
    db.commit()
    stats = float_rate_stats(db, HOUR)
    assert stats is not None and stats["total_plates"] == pytest.approx(2)
    assert DERIVATION == "float-plate-count-v1"
