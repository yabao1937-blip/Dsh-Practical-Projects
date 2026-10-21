"""Slow observation preparation cannot reserve SQLite's sole writer."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy import delete, select, text

from app.decision_hourly import service
from app.decision_hourly.observation_guard import configuration_revision
from app.decision_hourly.models import DecisionCycleSample, DecisionCycleHour, DecisionTelemetryPin
from app.gpt_density.hourly_anchor import read as read_anchor
from app.models import Setting, AutoState, CoarseModel, ManualMeasurement, DecisionSourceRevision
from app.services.decision_sources import shared_snapshot
from app.services import migrate
from app.services.state import load_store
from app.telemetry.domain import iso_time
from app.telemetry.models import TelemetryPoint
from test_hourly_brief import HOUR, sessions, send
from test_decision_sources import seeded
from test_decision_maintenance import wal


def pause_prepare(monkeypatch):
    started, release = Event(), Event()
    original = service.prepare_record

    def paused(db, *args, **kwargs):
        prepared = original(db, *args, **kwargs)
        started.set()
        assert release.wait(10)
        return prepared

    monkeypatch.setattr(service, 'prepare_record', paused)
    monkeypatch.setattr('app.services.decision_sources.utc_now', lambda: HOUR)
    return started, release


def test_collector_commits_during_prepare_and_old_batch_stays_authoritative(wal, seeded, monkeypatch):
    cfg, repo, values = seeded
    started, release = pause_prepare(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.observation_tick, HOUR, wal)
        try:
            assert started.wait(10)
            later = HOUR + timedelta(seconds=5)
            monkeypatch.setattr('app.telemetry.repository.utc_now', lambda: later)
            send(repo, cfg, later, values)  # A real collector repository transaction, before release.
            with wal() as db:
                assert not list(db.scalars(select(DecisionTelemetryPin)))
        finally:
            release.set()
        assert future.result(timeout=10) == HOUR
    with wal() as db:
        row = db.get(DecisionCycleSample, int(HOUR.timestamp()) // 5)
        assert row.sampled_at == iso_time(HOUR)
        assert list(db.scalars(select(DecisionTelemetryPin)))
        assert service.observation_tick(HOUR, wal) is None


@pytest.mark.parametrize('change', ['target', 'point', 'frozen_hour', 'model', 'manual', 'override', 'source'])
def test_concurrent_configuration_or_frozen_hour_discards_preparation(wal, seeded, monkeypatch, change):
    started, release = pause_prepare(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.observation_tick, HOUR, wal)
        try:
            assert started.wait(10)
            with wal.begin() as db:
                if change == 'target':
                    row = db.get(Setting, 'ash_target')
                    if row:
                        row.value = 9.1
                    else:
                        db.add(Setting(key='ash_target', value=9.1))
                elif change == 'point':
                    row = db.scalar(select(TelemetryPoint))
                    row.config = dict(row.config, scale=2)
                elif change == 'frozen_hour':
                    db.add(DecisionCycleHour(start=iso_time(HOUR), end=iso_time(HOUR+timedelta(hours=1)),
                        generated_at=iso_time(HOUR+timedelta(hours=1)), snapshot={'frozen': True}))
                elif change == 'model':
                    row = db.scalar(select(CoarseModel))
                    if row:
                        row.intercept += 1
                    else:
                        db.add(CoarseModel(method='mlr', intercept=1))
                elif change == 'manual':
                    db.add(ManualMeasurement(metric='heavy_clean_coal_ash', name='heavy assay', value=8.6, unit='%',
                        observed_at=iso_time(HOUR), created_at=iso_time(HOUR), request_hash='concurrent',
                        client_id='concurrent-assay'))
                elif change == 'override':
                    row = db.get(AutoState, 'heavyAshInput')
                    if row:
                        row.value = {'manual': 8.6}
                    else:
                        db.add(AutoState(key='heavyAshInput', value={'manual': 8.6}))
                else:
                    db.add(DecisionSourceRevision(effective_at=iso_time(HOUR), config={'changed': True}))
        finally:
            release.set()
        assert future.result(timeout=10) is None
    with wal() as db:
        assert not list(db.scalars(select(DecisionCycleSample)))
        assert not list(db.scalars(select(DecisionTelemetryPin)))
        assert read_anchor(db, HOUR)['status'] == 'missing'


@pytest.mark.parametrize('key', ['coarseAshEma', 'densityDecisionLog', 'heavyAshBackcalc', 'coarseCalc', 'autoState'])
def test_concurrent_derived_state_does_not_discard_sample_or_anchor(wal, seeded, monkeypatch, key):
    before = (HOUR-timedelta(minutes=1)).replace(tzinfo=None)
    with wal.begin() as db:
        db.add(AutoState(key=key, value={}, updated_at=before))
    started, release = pause_prepare(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.observation_tick, HOUR, wal)
        try:
            assert started.wait(10)
            with wal.begin() as db:
                row = db.get(AutoState, key)
                row.value = ({name: {'v': 9.3, 't': HOUR.timestamp()*1000} for name in
                              ('level_tail', 'ash_502', 'scale_502', 'heavyAsh', 'coarseAsh', 'floatAsh')}
                             if key == 'autoState' else {'screen315': 500, 'waterUnder': 200}
                             if key == 'coarseCalc' else [{'target': 1.48}]
                             if key == 'densityDecisionLog' else {'v': 9.3, 't': HOUR.timestamp()*1000})
        finally:
            release.set()
        assert future.result(timeout=10) == HOUR
    with wal() as db:
        assert db.get(DecisionCycleSample, int(HOUR.timestamp())//5)
        assert read_anchor(db, HOUR)['status'] != 'missing'


@pytest.mark.parametrize('valid_before,valid_after', [(True, True), (False, False), (True, False), (False, True)])
@pytest.mark.parametrize('field,key', [('instrumentInputs', 'level_tail'), ('instrumentInputs', 'ash_502'),
                                     ('instrumentInputs', 'scale_502'), ('heavyAshInput', 'heavyAsh')])
def test_auto_timestamp_only_invalidates_when_manual_validity_changes(wal, seeded, monkeypatch,
                                                                   valid_before, valid_after, field, key):
    manual_at = (HOUR-timedelta(minutes=2)).timestamp()*1000
    before = (HOUR-timedelta(minutes=1)).replace(tzinfo=None)
    with wal.begin() as db:
        manual = {'manual': 55, 'manualAt': manual_at}
        value = {key: manual} if field == 'instrumentInputs' else manual
        db.merge(AutoState(key=field, value=value, updated_at=before))
        db.add(AutoState(key='autoState', value={key: {'v': 50, 't': manual_at+(-1000 if valid_before else 1000)}},
                         updated_at=before))
    started, release = pause_prepare(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.observation_tick, HOUR, wal)
        try:
            assert started.wait(10)
            with wal.begin() as db:
                db.get(AutoState, 'autoState').value = {
                    key: {'v': 60, 't': manual_at+(-500 if valid_after else 2000)}}
        finally:
            release.set()
        expected = HOUR if valid_before == valid_after else None
        assert future.result(timeout=10) == expected
    with wal() as db:
        assert bool(db.get(DecisionCycleSample, int(HOUR.timestamp())//5)) == (expected is not None)
        assert (read_anchor(db, HOUR)['status'] != 'missing') == (expected is not None)


def test_ordinary_mirror_recreates_same_model_without_discarding_sample(wal, seeded, monkeypatch):
    before = (HOUR-timedelta(minutes=1)).replace(tzinfo=None)
    with wal.begin() as db:
        # Existing browser stores carry these neutral defaults too; this mirror
        # changes model metadata rather than introducing configuration keys.
        for key in ('ashInputs', 'instrumentInputs', 'heavyAshInput', 'coarseAshInput'):
            db.add(AutoState(key=key, value={}, updated_at=before))
        for index, method in enumerate(('mlr', 'pls'), start=101):
            db.add(CoarseModel(id=index, method=method, is_current=method == 'mlr',
                train_run_id='old-mirror-metadata', intercept=3,
                coefs=[.1, .002, 0, 0, 0, 0, 0, 0, 0, .02], metrics={'version': 2},
                trained_at=before.isoformat(), created_at=before))
    with wal() as db:
        store = load_store(db)
        old_rows = [(row.id, row.created_at, row.train_run_id) for row in db.scalars(select(CoarseModel))]
    monkeypatch.setattr(migrate, 'SessionLocal', wal)
    started, release = pause_prepare(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.observation_tick, HOUR, wal)
        try:
            assert started.wait(10)
            report = migrate.replace(store, force=False, require_revision=True)
            assert report['ok']
            assert 'coarse_models' in report['owned']
            with wal() as db:
                new_rows = [(row.id, row.created_at, row.train_run_id) for row in db.scalars(select(CoarseModel))]
                assert new_rows != old_rows  # The real PUT path deleted/recreated the rows.
        finally:
            release.set()
        assert future.result(timeout=10) == HOUR
    with wal() as db:
        assert db.get(DecisionCycleSample, int(HOUR.timestamp())//5)
        assert read_anchor(db, HOUR)['status'] != 'missing'


@pytest.mark.parametrize('values', [
    {'instrumentInputs': ['legacy'], 'heavyAshInput': None, 'autoState': ['legacy']},
    {'instrumentInputs': {'level_tail': [], 'ash_502': None, 'scale_502': 'legacy'},
     'coarseAshInput': 1, 'floatAshInput': []},
    {'heavyAshInput': {'manual': 8.5}, 'autoState': {'heavyAsh': ['legacy']}},
])
def test_configuration_guard_tolerates_unused_non_object_legacy_state(wal, values):
    with wal.begin() as db:
        for key, value in values.items():
            db.add(AutoState(key=key, value=value))
    with wal() as db:
        revision = configuration_revision(db)
        assert len(revision) == 64
        assert configuration_revision(db) == revision


def test_prepare_crossing_hour_does_not_request_maintenance(wal, seeded, monkeypatch):
    monkeypatch.setattr('app.services.decision_sources.utc_now', lambda: HOUR)
    times = iter([HOUR, HOUR+timedelta(hours=1)])
    monkeypatch.setattr(service, 'utc_now', lambda: next(times))
    assert service.observation_tick(sessions=wal) is None
    with wal() as db:
        assert not list(db.scalars(select(DecisionCycleSample)))
    monkeypatch.setattr(service, 'observation_tick', lambda *a: None)
    monkeypatch.setattr(service, 'maintenance_tick', lambda *a: pytest.fail('uncommitted observation cannot freeze hours'))
    service.tick(HOUR, wal)


def test_prepared_commit_matches_direct_record_and_keeps_anchor(wal, seeded, monkeypatch):
    monkeypatch.setattr('app.services.decision_sources.utc_now', lambda: HOUR)
    with wal.begin() as db:
        result = shared_snapshot(db)
        store = load_store(db)
        assert service.record(db, HOUR, result, store)
    with wal() as db:
        old = db.get(DecisionCycleSample, int(HOUR.timestamp())//5)
        expected = (old.sampled_at, old.context_key, old.fingerprint, deepcopy(old.snapshot))
        anchor = deepcopy(read_anchor(db, HOUR))
    with wal.begin() as db:
        db.execute(delete(DecisionCycleSample))
    assert service.observation_tick(HOUR, wal) == HOUR
    with wal() as db:
        row = db.get(DecisionCycleSample, int(HOUR.timestamp())//5)
        assert (row.sampled_at, row.context_key, row.fingerprint, row.snapshot) == expected
        assert read_anchor(db, HOUR) == anchor


def test_late_preparation_cannot_rewind_newer_committed_observation(wal, seeded, monkeypatch):
    monkeypatch.setattr('app.services.decision_sources.utc_now', lambda: HOUR)
    with wal() as db:
        db.execute(text('BEGIN'))
        old = service.prepare_record(db, HOUR)
    with wal.begin() as db:
        current = shared_snapshot(db)
        current['generated_at'] = iso_time(HOUR+timedelta(seconds=5))
        assert service.record(db, HOUR, current)
    with wal.begin() as db:
        frozen = deepcopy(read_anchor(db, HOUR))
        assert not service.save_record(db, old)
        assert read_anchor(db, HOUR) == frozen
        assert db.get(DecisionCycleSample, old['slot']) is None
