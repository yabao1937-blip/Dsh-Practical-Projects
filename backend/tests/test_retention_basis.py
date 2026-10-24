"""Permanent chemistry survives detail expiry without replaying a model."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace
import zlib

import pytest
from sqlalchemy import delete, event, select, text

from app.decision_hourly import ash_settlement
from app.decision_hourly.evidence import weights
from app.decision_hourly.models import (DecisionAshSettlement, DecisionCycleHour,
    DecisionCycleSample, DecisionEvidenceArchive)
from app.gpt_runtime.settlement_rows import load_rows as projected_rows
from app.storage_retention.basis import (prepare_basis, save_basis,
    load_basis_rows, load_basis_stopped)
from app.storage_retention.models import RetentionHourBasis
from app.telemetry.domain import iso_time, normalize
from app.telemetry.models import TelemetryReading
from test_ash_settlement import START, END, NOW, PUBLISH, sample, assay
from test_decision_sources import seeded
from test_hourly_brief import sessions
from test_settlement_archive_projection import referenced_sample, freeze_and_archive


def _remove_details(db):
    for model in (DecisionCycleSample, DecisionEvidenceArchive, TelemetryReading):
        db.execute(delete(model))
    db.flush()


def _repack(prepared, edit):
    value = json.loads(zlib.decompress(prepared['payload']))
    edit(value)
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return {**prepared, 'payload': zlib.compress(raw), 'checksum': hashlib.sha256(raw).hexdigest()}


def test_basis_survives_reference_and_decision_expiry_late_assay_and_minutes(sessions):
    with sessions.begin() as db:
        slot, _ = referenced_sample(db, START+timedelta(seconds=59))
        referenced_sample(db, START+timedelta(minutes=30), rate=100)
        freeze_and_archive(db)
        old_hour = deepcopy(db.get(DecisionCycleHour, iso_time(START)).snapshot)
        frames = ash_settlement.load_rows(db, START)
        before_frames = deepcopy([vars(row) for row in frames])
        earlier = assay(db, 7, observed=START-timedelta(minutes=1), created=START)
        earlier.time_basis = 'production-time-v1'
        db.flush()
        estimated = ash_settlement.build_settlement(db, START, PUBLISH)
        prepared = prepare_basis(db, START, frames, NOW)
        assert [vars(row) for row in frames] == before_frames
        save_basis(db, prepared)
        late = assay(db, 8, observed=START+timedelta(minutes=47), created=NOW)
        late.time_basis = 'production-time-v1'
        db.flush()
        expected = ash_settlement.build_settlement(db, START, NOW, rows=frames)
        expected_interval = ash_settlement.read_settled_interval(
            db, START+timedelta(minutes=1), START+timedelta(minutes=31), NOW)
        _remove_details(db)
        actual = ash_settlement.build_settlement(db, START, NOW)
        assert actual == expected
        assert actual['item']['value'] != estimated['item']['value']
        assert actual['item']['recalculated_count'] == 2
        assert ash_settlement.read_settled_interval(
            db, START+timedelta(minutes=1), START+timedelta(minutes=31), NOW) == expected_interval
        assert db.get(DecisionCycleHour, iso_time(START)).snapshot == old_hour
        loaded = load_basis_rows(db, START)
        assert loaded[0].slot == slot
        assert loaded[0].snapshot['coarse_ash']['value'] == 20
        assert 'model' not in loaded[0].snapshot
        assert '$reading' not in json.dumps([vars(row) for row in loaded])
        assert [vars(row) for row in projected_rows(db, START)] == [vars(row) for row in loaded]


def test_mixed_cadence_bad_quality_and_stopped_intervals_are_frozen(sessions, seeded, monkeypatch):
    cfg, _, _ = seeded
    with sessions.begin() as db:
        for second, cadence in ((0, 5), (5, 30), (35, 30), (50, 5), (3590, 30)):
            original = sample(db, START+timedelta(seconds=second))
            stored = db.get(DecisionCycleSample, original.slot)
            snapshot = deepcopy(stored.snapshot)
            snapshot['cadence_seconds'] = cadence
            for key, index in (('belt501', 5), ('belt502', 6)):
                snapshot['cells'][key].update(source_id=cfg.source_id,
                    point_key=cfg.points[index].key, cadence_seconds=cadence)
            stored.snapshot = snapshot
        when = START+timedelta(seconds=15)
        p = cfg.points[5]
        bad = normalize(p, 200, 0x80000000, when, when, when)
        bad.update(storage_kind='quality-event', cadence_seconds=5)
        db.add(TelemetryReading(source_id=cfg.source_id, point_key=p.key,
            batch_id='bad-15', received_at=iso_time(when), sample=bad))
        for second in (100, 130):
            for index in (1, 5, 6):
                p, when = cfg.points[index], START+timedelta(seconds=second)
                zero = normalize(p, 0, 0, when, when, when)
                zero['cadence_seconds'] = 30
                db.add(TelemetryReading(source_id=cfg.source_id, point_key=p.key,
                    batch_id='zero-'+str(second), received_at=iso_time(when), sample=zero))
        db.add(DecisionCycleHour(start=iso_time(START), end=iso_time(END),
            generated_at=iso_time(END), snapshot={'immutable': True}))
        db.flush()
        rows = ash_settlement.load_rows(db, START)
        original_weights = weights(rows, END, db=db)
        assert original_weights[rows[1].slot]['seconds'] == 10
        original = ash_settlement.build_settlement(db, START, NOW)
        assert original['item']['stopped_seconds'] == 60
        prepared = prepare_basis(db, START, rows, NOW)
        save_basis(db, prepared)
        _remove_details(db)
        monkeypatch.setattr('app.decision_hourly.evidence.quality_boundaries',
                            lambda *a, **k: pytest.fail('Raw quality must not be read after expiry'))
        monkeypatch.setattr('app.decision_hourly.ash_settlement.stopped_intervals',
                            lambda *a, **k: pytest.fail('Raw stops must not be read after expiry'))
        loaded = load_basis_rows(db, START)
        assert weights(loaded, END, db=db) == original_weights
        assert load_basis_stopped(db, START) == original['stopped_intervals']
        assert ash_settlement.build_settlement(db, START, NOW) == original
        # A caller may clip a returned weight without mutating permanent evidence.
        returned = weights(loaded, END, db=db)
        returned[loaded[0].slot]['seconds'] = 0
        assert weights(loaded, END, db=db) == original_weights


def test_basis_uses_saved_original_stops_after_point_definition_changed(sessions, monkeypatch):
    with sessions.begin() as db:
        sample(db)
        original = ash_settlement.build_settlement(db, START, NOW, stopped=[
            [START.timestamp()+100, START.timestamp()+120]])
        db.add(DecisionAshSettlement(start=iso_time(START), end=iso_time(END),
            generated_at=iso_time(NOW), deadline=iso_time(PUBLISH), snapshot=original))
        db.flush()
        monkeypatch.setattr('app.decision_hourly.production_coverage.stopped_intervals',
                            lambda *a, **k: pytest.fail('Original saved stops must win'))
        prepared = prepare_basis(db, START, ash_settlement.load_rows(db, START), NOW)
        save_basis(db, prepared)
        assert load_basis_stopped(db, START) == original['stopped_intervals']


def test_idempotent_save_does_not_overwrite_different_basis(sessions):
    with sessions.begin() as db:
        sample(db)
        prepared = prepare_basis(db, START, ash_settlement.load_rows(db, START), NOW)
        first = save_basis(db, prepared)
        assert save_basis(db, {**prepared, 'created_at': iso_time(NOW+timedelta(days=1))}).checksum == first.checksum
        changed = _repack(prepared, lambda data: data['rows'][0]['snapshot']['coarse_ash'].update(value=99))
        with pytest.raises(ValueError, match='不同内容'):
            save_basis(db, changed)
        assert load_basis_rows(db, START)[0].snapshot['coarse_ash']['value'] == 20


@pytest.mark.parametrize('mutate', [
    lambda value: value.update(end=iso_time(END+timedelta(hours=1))),
    lambda value: value['rows'][0].update(sampled_at=iso_time(END)),
    lambda value: value['rows'][0]['weight'].update(seconds=-1),
    lambda value: value['rows'][0]['weight'].update(duration_seconds=3600),
    lambda value: value['rows'][0]['snapshot']['cells'].update(float_ash={'$manual': 1}),
    lambda value: value.update(stopped_intervals=[[START.timestamp()-1, START.timestamp()+1]]),
])
def test_invalid_basis_refuses_save(sessions, mutate):
    with sessions.begin() as db:
        sample(db)
        prepared = prepare_basis(db, START, ash_settlement.load_rows(db, START), NOW)
        with pytest.raises(ValueError):
            save_basis(db, _repack(prepared, mutate))
        assert db.scalar(select(RetentionHourBasis)) is None


def test_corrupted_basis_fails_closed_and_does_not_fall_back_to_hot(sessions):
    with sessions.begin() as db:
        sample(db)
        prepared = prepare_basis(db, START, ash_settlement.load_rows(db, START), NOW)
        save_basis(db, prepared)
        saved = db.get(RetentionHourBasis, iso_time(START))
        saved.payload = saved.payload[:-3]
        db.flush()
        for loader in (load_basis_rows, load_basis_stopped, ash_settlement.load_rows, projected_rows):
            with pytest.raises(ValueError):
                loader(db, START)


def test_readonly_reader_does_not_write_and_writer_is_blocked(sessions, monkeypatch):
    with sessions.begin() as db:
        sample(db)
        prepared = prepare_basis(db, START, ash_settlement.load_rows(db, START), NOW)
        save_basis(db, prepared)
    monkeypatch.setattr('app.config.REMOTE_READONLY', {'server_url': 'http://example.invalid'})
    with sessions() as db:
        statements = []
        def capture(conn, cursor, statement, parameters, context, many):
            statements.append(statement)
        event.listen(db.bind, 'before_cursor_execute', capture)
        try:
            assert load_basis_rows(db, START)
            assert load_basis_stopped(db, START) == []
            assert projected_rows(db, START)
            with pytest.raises(ValueError, match='只读'):
                save_basis(db, prepared)
        finally:
            event.remove(db.bind, 'before_cursor_execute', capture)
        assert statements and all(s.lstrip().upper().startswith('SELECT') for s in statements)


def test_old_host_without_basis_table_keeps_original_loaders(sessions):
    with sessions.begin() as db:
        sample(db)
        db.execute(text('DROP TABLE retention_hour_bases'))
        assert load_basis_rows(db, START) is None
        assert load_basis_stopped(db, START) is None
        assert len(ash_settlement.load_rows(db, START)) == 1
        assert len(projected_rows(db, START)) == 1


def test_pure_thirty_second_projected_rows_keep_lagged_quality_event_clipping(sessions, seeded):
    cfg, _, _ = seeded
    with sessions.begin() as db:
        for second in (0, 30):
            original = sample(db, START+timedelta(seconds=second))
            stored = db.get(DecisionCycleSample, original.slot)
            snapshot = deepcopy(stored.snapshot)
            snapshot['cadence_seconds'] = 30
            snapshot['quality_sources'] = [dict(source_id=cfg.source_id,
                point_key=cfg.points[3].key, lag_seconds=90)]
            for key, index in (('belt501', 5), ('belt502', 6)):
                snapshot['cells'][key].update(source_id=cfg.source_id,
                    point_key=cfg.points[index].key, cadence_seconds=30)
            stored.snapshot = snapshot
        p, when = cfg.points[3], START-timedelta(seconds=75)
        bad = normalize(p, 50, 0x80000000, when, when, when)
        bad.update(storage_kind='quality-event', cadence_seconds=5)
        db.add(TelemetryReading(source_id=cfg.source_id, point_key=p.key,
            batch_id='lagged-bad', received_at=iso_time(when), sample=bad))
        db.flush()
        normal, projected = ash_settlement.load_rows(db, START), projected_rows(db, START)
        assert [vars(row) for row in projected] == [vars(row) for row in normal]
        assert weights(projected, END, db=db) == weights(normal, END, db=db)
        assert weights(projected, END, db=db)[normal[0].slot]['seconds'] == 15
        assert ash_settlement.build_settlement(db, START, NOW, rows=projected) == ash_settlement.build_settlement(
            db, START, NOW, rows=normal)


def test_five_thirty_sixty_basis_preserves_clipping_and_late_assay_after_raw_expiry(sessions, seeded):
    cfg, _, _ = seeded
    cadences = ((0, 5), (5, 30), (35, 60), (95, 60), (155, 5), (3590, 60))
    with sessions.begin() as db:
        for second, cadence in cadences:
            original = sample(db, START+timedelta(seconds=second))
            stored = db.get(DecisionCycleSample, original.slot)
            snapshot = deepcopy(stored.snapshot)
            snapshot['cadence_seconds'] = cadence
            snapshot['quality_sources'] = [dict(source_id=cfg.source_id,
                point_key=cfg.points[3].key, lag_seconds=90)]
            for key, index in (('belt501', 5), ('belt502', 6)):
                snapshot['cells'][key].update(source_id=cfg.source_id,
                    point_key=cfg.points[index].key, cadence_seconds=cadence)
            stored.snapshot = snapshot
        # Both direct and 90-second-lagged bad events cut the minute frame at 60s.
        for index, second in ((5, 60), (3, -30)):
            p, when = cfg.points[index], START+timedelta(seconds=second)
            bad = normalize(p, 200 if index == 5 else 50, 0x80000000, when, when, when)
            bad.update(storage_kind='quality-event', cadence_seconds=5)
            db.add(TelemetryReading(source_id=cfg.source_id, point_key=p.key,
                batch_id='bad-'+str(index), received_at=iso_time(when), sample=bad))
        for second in (200, 260):
            for index in (1, 5, 6):
                p, when = cfg.points[index], START+timedelta(seconds=second)
                zero = normalize(p, 0, 0, when, when, when)
                zero['cadence_seconds'] = 60
                db.add(TelemetryReading(source_id=cfg.source_id, point_key=p.key,
                    batch_id='zero-'+str(second), received_at=iso_time(when), sample=zero))
        db.add(DecisionCycleHour(start=iso_time(START), end=iso_time(END),
            generated_at=iso_time(END), snapshot={'advice': 'original unchanged'}))
        earlier = assay(db, 7, observed=START-timedelta(minutes=1), created=START)
        earlier.time_basis = 'production-time-v1'
        db.flush()
        rows = ash_settlement.load_rows(db, START)
        original_weights = weights(rows, END, db=db)
        assert [original_weights[row.slot]['duration_seconds'] for row in rows] == [5, 30, 25, 60, 5, 10]
        estimated = ash_settlement.build_settlement(db, START, PUBLISH)
        assert estimated['item']['stopped_seconds'] == 120
        late = assay(db, 8, observed=START+timedelta(minutes=47), created=NOW)
        late.time_basis = 'production-time-v1'
        db.flush()
        expected = ash_settlement.build_settlement(db, START, NOW)
        expected_interval = ash_settlement.read_settled_interval(db,
            START+timedelta(minutes=1), START+timedelta(minutes=3), NOW)
        prepared = prepare_basis(db, START, rows, NOW)
        saved_bytes = prepared['payload']
        assert json.loads(zlib.decompress(saved_bytes))['policy'] == 'permanent-hour-chemistry-v1'
        save_basis(db, prepared)
        _remove_details(db)
        loaded = load_basis_rows(db, START)
        assert [row.snapshot['cadence_seconds'] for row in loaded] == [cadence for _, cadence in cadences]
        assert weights(loaded, END, db=db) == original_weights
        actual = ash_settlement.build_settlement(db, START, NOW)
        assert actual == expected
        assert actual['item']['value'] != estimated['item']['value']
        assert actual['item']['recalculated_count'] == len(cadences)
        assert ash_settlement.read_settled_interval(db,
            START+timedelta(minutes=1), START+timedelta(minutes=3), NOW) == expected_interval
        assert db.get(RetentionHourBasis, iso_time(START)).payload == saved_bytes
        assert db.get(DecisionCycleHour, iso_time(START)).snapshot == {'advice': 'original unchanged'}


@pytest.mark.parametrize('cadence,seconds', [(5, 6), (30, 31), (60, 61)])
def test_basis_weight_cannot_extend_original_cadence_or_exceed_sixty(sessions, cadence, seconds):
    with sessions.begin() as db:
        sample(db)
        prepared = prepare_basis(db, START, ash_settlement.load_rows(db, START), NOW)
        def extend(data):
            frame = data['rows'][0]
            frame['snapshot']['cadence_seconds'] = cadence
            frame['weight'].update(seconds=seconds, duration_seconds=seconds)
        with pytest.raises(ValueError, match='权重异常'):
            save_basis(db, _repack(prepared, extend))
        assert db.scalar(select(RetentionHourBasis)) is None
