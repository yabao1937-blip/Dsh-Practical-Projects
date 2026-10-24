"""An unpinned raw quality event remains evidence for the following hour.

Minute decisions can be cut short by a level failure received in the previous
hour, because that input is delayed by 90 seconds. The event need not itself
be a referenced input value. Archiving the preceding sparse/raw-only hour must
not erase this boundary before the following hour's permanent weights freeze.
"""
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.decision_hourly.ash_settlement import build_settlement, load_rows
from app.decision_hourly.evidence import weights
from app.decision_hourly.models import (DecisionCycleHour, DecisionCycleSample,
                                       DecisionTelemetryPin)
from app.storage_retention.basis import load_basis_rows
from app.storage_retention.models import RetentionArchive, RetentionHourBasis
from app.storage_retention.service import run_tick
from app.telemetry.domain import iso_time
from app.telemetry.models import TelemetryReading
from test_ash_settlement import START, END, sample
from test_hourly_brief import sessions


@pytest.mark.parametrize('cadence', [30, 60])
@pytest.mark.parametrize('lag', [0, 90])
def test_sparse_preceding_hour_keeps_unpinned_quality_until_next_basis(sessions, cadence, lag):
    now = END + timedelta(days=31)
    when = START + timedelta(seconds=15-lag)
    with sessions.begin() as db:
        original = sample(db, START)
        saved = db.get(DecisionCycleSample, original.slot)
        snapshot = deepcopy(saved.snapshot)
        snapshot.update(cadence_seconds=cadence, quality_sources=[
            dict(source_id='kep', point_key='delayed-level', lag_seconds=lag)])
        saved.snapshot = snapshot
        db.add(DecisionCycleHour(start=iso_time(START), end=iso_time(END),
            generated_at=iso_time(END), snapshot={'original': True}))
        # The former hour is deliberately sparse and has no decision/hour row.
        # Its entire raw set fits in a single cleanup batch, exposing ordering.
        db.add(TelemetryReading(source_id='kep', point_key='unrelated',
            batch_id='preceding-sparse', received_at=iso_time(START-timedelta(seconds=80)),
            sample={'value': 1, 'quality': 'good', 'storage_kind': 'periodic'}))
        event = TelemetryReading(source_id='kep', point_key='delayed-level',
            batch_id='quality-boundary', received_at=iso_time(when), sample={
                'value': 30, 'quality': 'bad', 'storage_kind': 'quality-event',
                'cadence_seconds': 5})
        db.add(event)
        db.flush()
        event_id = event.id
        # Keep an actually newer high-water row so the event is not retained
        # accidentally by the separate integer-PK reuse protection.
        db.add(TelemetryReading(source_id='kep', point_key='delayed-level',
            batch_id='newest', received_at=iso_time(now), sample={'value': 31, 'quality': 'good'}))

    with sessions() as db:
        rows = load_rows(db, START)
        expected_weights = weights(rows, END, db=db)
        expected_settlement = build_settlement(db, START, now)
        assert expected_weights[original.slot]['seconds'] == 15
        assert db.get(DecisionTelemetryPin, event_id) is None

    for _ in range(4):
        result = run_tick(sessions, now)
        if result.get('phase') == 'archived':
            break
    assert result == dict(phase='archived', start=iso_time(START-timedelta(hours=1)),
                          samples=0, cleanup=None)
    with sessions() as db:
        assert db.get(TelemetryReading, event_id) is not None
        assert db.get(RetentionHourBasis, iso_time(START)) is None

    # This tick tries to clean the sparse former hour *before* freezing this
    # hour. The delayed event must survive that early cleanup step.
    result = run_tick(sessions, now)
    assert result['phase'] == 'archived' and result['start'] == iso_time(START)
    with sessions() as db:
        assert db.get(TelemetryReading, event_id) is not None
        basis_rows = load_basis_rows(db, START)
        assert weights(basis_rows, END, db=db) == expected_weights
        assert build_settlement(db, START, now) == expected_settlement

    # Once the next hour is durably self-contained, the dependency may expire.
    for _ in range(8):
        run_tick(sessions, now)
        with sessions() as db:
            if db.get(TelemetryReading, event_id) is None:
                break
    with sessions() as db:
        assert db.get(TelemetryReading, event_id) is None
        assert db.get(DecisionCycleHour, iso_time(START)).snapshot == {'original': True}
        assert db.get(RetentionArchive, iso_time(START)).status == 'online-removed'
        assert weights(load_basis_rows(db, START), END, db=db) == expected_weights
        assert build_settlement(db, START, now) == expected_settlement
