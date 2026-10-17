"""Lossless archive defaults and bounded ash-only reference hydration."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import zlib

import pytest
from sqlalchemy import delete, event, select

from app.decision_hourly import archive, ash_settlement
from app.decision_hourly.models import DecisionCycleHour, DecisionCycleSample, DecisionEvidenceArchive
from app.decision_hourly.references import VERSION
from app.telemetry.domain import iso_time
from app.telemetry.models import TelemetryReading
from test_ash_settlement import START, END, PUBLISH, NOW, assay, sample
from test_hourly_brief import sessions


def ash_fields(snapshot):
    """Independent minimal consumer contract; default archive reads stay complete."""
    return {
        **{key: snapshot[key] for key in ('storage_version', 'evidence_policy', 'sources',
            'coarse_ash', 'heavy_amount', 'total_ash') if key in snapshot},
        'cells': {key: value for key, value in snapshot.get('cells', {}).items()
                  if key in ('belt501', 'belt502', 'float_ash', 'float_amount', 'coarse_amount')},
    }


def referenced_sample(db, when=START, rate=400, choice=None):
    """Packed production flow plus unrelated display evidence in the same archive."""
    row = sample(db, when, rate=rate, choice=choice)
    stored = db.get(DecisionCycleSample, row.slot)
    snapshot = deepcopy(stored.snapshot)
    snapshot['cells']['density'] = dict(value=1.48, status='good', unit='g/cm³')
    ids = {}
    for key in ('belt501', 'density'):
        cell = snapshot['cells'][key]
        reading = TelemetryReading(source_id=cell.get('source_id', 'archive-test'), point_key=key,
            batch_id=cell.get('batch_id', str(row.slot)), received_at=iso_time(when), sample=deepcopy(cell))
        db.add(reading)
        db.flush()
        ids[key] = reading.id
        snapshot['cells'][key] = {'$reading': reading.id, 'fields': list(cell), 'overlay': {}}
    snapshot['storage_version'] = VERSION
    snapshot['model'] = {'name': 'frozen-model', 'unneeded': 'x' * 5000}
    snapshot['density_channels'] = [snapshot['cells']['density']]
    stored.snapshot = snapshot
    db.flush()
    return row.slot, ids


def freeze_and_archive(db):
    if not db.get(DecisionCycleHour, iso_time(START)):
        db.add(DecisionCycleHour(start=iso_time(START), end=iso_time(END),
            generated_at=iso_time(END), snapshot={'immutable': True}))
    db.flush()
    archive.archive_old_hour(db, iso_time(END))
    db.flush()


def test_default_readback_remains_complete_but_projection_queries_only_ash_references(sessions):
    with sessions.begin() as db:
        slot, ids = referenced_sample(db)
        expected = archive.read_sample(db, slot)['snapshot']
        freeze_and_archive(db)

    queried = []
    engine = sessions.kw['bind']

    def capture(conn, cursor, statement, parameters, context, executemany):
        if 'FROM telemetry_readings' in statement and 'telemetry_readings.id IN' in statement:
            queried.extend(parameters)

    event.listen(engine, 'before_cursor_execute', capture)
    try:
        with sessions() as db:
            complete = archive.archived_rows(db, iso_time(START))[0]
            assert complete['snapshot'] == expected
            assert set(queried) == set(ids.values())
        queried.clear()
        with sessions() as db:
            projected = archive.archived_rows(db, iso_time(START), project_snapshot=ash_settlement.settlement_snapshot)[0]
            assert projected['snapshot'] == ash_fields(expected)
            assert set(queried) == {ids['belt501']}
            assert projected['slot'] == complete['slot']
            assert projected['sampled_at'] == complete['sampled_at']
            assert projected['context_key'] == complete['context_key']
            assert projected['fingerprint'] == complete['fingerprint']
            assert archive.read_sample(db, slot)['snapshot'] == expected
    finally:
        event.remove(engine, 'before_cursor_execute', capture)


@pytest.mark.parametrize('choice', [None,
    json.dumps(['manual-measurements', 'heavy_clean_coal_ash']),
    json.dumps(['kep', 'online-heavy-ash'])])
def test_archive_settlement_late_assays_and_minute_mass_match_hot_and_full_readback(sessions, monkeypatch, choice):
    with sessions.begin() as db:
        referenced_sample(db, START + timedelta(seconds=59), choice=choice)
        referenced_sample(db, START + timedelta(minutes=30), rate=100, choice=choice)
        db.add(DecisionCycleHour(start=iso_time(START), end=iso_time(END),
            generated_at=iso_time(END), snapshot={'immutable': True}))
        earlier = assay(db, 7, observed=START - timedelta(minutes=1), created=START)
        earlier.time_basis = 'production-time-v1'
        db.flush()
        before = ash_settlement.build_settlement(db, START, PUBLISH)
        late = assay(db, 8, observed=START + timedelta(minutes=47), created=NOW)
        late.time_basis = 'production-time-v1'
        db.flush()
        hot = ash_settlement.build_settlement(db, START, NOW)
        hot_interval = ash_settlement.read_settled_interval(db, START, END, NOW)
        freeze_and_archive(db)

    # The actual load_rows path may select archive projection; full forensic
    # hydration remains an independent result oracle for the same stored bytes.
    with sessions() as db:
        projected = ash_settlement.build_settlement(db, START, NOW)
        assert projected == hot
        if choice != json.dumps(['kep', 'online-heavy-ash']):
            assert projected['item']['value'] != before['item']['value']
            assert projected['item']['recalculated_count'] == 2
        else:
            assert projected['item']['value'] == before['item']['value']
            assert projected['item']['recalculated_count'] == 0
        full = ash_settlement.read_settled_interval(db, START, END, NOW)
        left = ash_settlement.read_settled_interval(db, START, START + timedelta(minutes=1), NOW)
        right = ash_settlement.read_settled_interval(db, START + timedelta(minutes=1), END, NOW)
        assert full == hot_interval
        assert left['summary']['mass_t'] + right['summary']['mass_t'] == pytest.approx(full['summary']['mass_t'])
        assert left['summary']['valid_seconds'] + right['summary']['valid_seconds'] == 10
        assert sum(part['summary']['value'] * part['summary']['mass_t'] for part in (left, right)) == pytest.approx(
            full['summary']['value'] * full['summary']['mass_t'])

    full_reader = archive.archived_rows
    monkeypatch.setattr(ash_settlement, 'archived_rows', lambda db, start, **kwargs: full_reader(db, start))
    with sessions() as db:
        assert ash_settlement.build_settlement(db, START, NOW) == projected
        assert ash_settlement.read_settled_interval(db, START, END, NOW) == hot_interval


def test_projection_cannot_bypass_checksum_of_unneeded_fields(sessions):
    with sessions.begin() as db:
        referenced_sample(db)
        freeze_and_archive(db)
        saved = db.get(DecisionEvidenceArchive, iso_time(START))
        data = json.loads(zlib.decompress(saved.payload))
        data[0]['snapshot']['model']['unneeded'] = 'tampered unused evidence'
        changed = json.dumps(data).encode()
        assert hashlib.sha256(changed).hexdigest() != saved.checksum
        saved.payload = zlib.compress(changed)

    calls = []
    with sessions() as db, pytest.raises(ValueError, match='归档校验失败'):
        archive.archived_rows(db, iso_time(START), project_snapshot=lambda s: calls.append(s) or {})
    assert calls == []


def test_projection_requires_all_ash_references_while_full_readback_requires_all_fields(sessions):
    with sessions.begin() as db:
        _, ids = referenced_sample(db)
        freeze_and_archive(db)
        db.execute(delete(TelemetryReading).where(TelemetryReading.id == ids['density']))
    with sessions() as db:
        assert archive.archived_rows(db, iso_time(START), project_snapshot=ash_settlement.settlement_snapshot)
        with pytest.raises(ValueError, match='原始记录缺失'):
            archive.archived_rows(db, iso_time(START))
    with sessions.begin() as db:
        db.execute(delete(TelemetryReading).where(TelemetryReading.id == ids['belt501']))
    with sessions() as db, pytest.raises(ValueError, match='原始记录缺失'):
        archive.archived_rows(db, iso_time(START), project_snapshot=ash_settlement.settlement_snapshot)
