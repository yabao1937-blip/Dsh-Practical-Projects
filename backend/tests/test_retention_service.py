"""End-to-end original-evidence retention on disposable databases only."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import zlib

import pytest
from sqlalchemy import select

from app.decision_hourly import ash_settlement
from app.decision_hourly.archive import read_sample
from app.decision_hourly.references import VERSION, pin_readings
from app.decision_hourly.models import (DecisionCycleHour, DecisionCycleSample,
    DecisionEvidenceArchive, DecisionTelemetryPin)
from app.storage_retention import service
from app.storage_retention.bundle import read_table, restore_bundle
from app.storage_retention.models import RetentionArchive, RetentionHourBasis, RetentionState
from app.storage_retention.policy import RetentionPolicy, storage_root
from app.telemetry.domain import iso_time
from app.telemetry.models import TelemetryReading
from test_ash_settlement import START, END, sample, assay
from test_hourly_brief import sessions

NOW = END+timedelta(days=31)


def seed(sessions, *, pins=False):
    with sessions.begin() as db:
        row = sample(db, START+timedelta(seconds=10))
        db.add(DecisionCycleHour(start=iso_time(START), end=iso_time(END),
            generated_at=iso_time(END), snapshot={'original':True}))
        db.add(TelemetryReading(source_id='kep', point_key='level', batch_id='old',
            received_at=iso_time(START+timedelta(seconds=1)), sample={'value':50,'quality':'good'}))
        db.flush()
        original = db.scalar(select(TelemetryReading))
        original_id = original.id
        if pins:
            db.add(DecisionTelemetryPin(reading_id=original.id, first_slot=row.slot,
                last_slot=row.slot))
        # A newer high-water row remains online even if the source stops for months.
        db.add(TelemetryReading(source_id='kep', point_key='level', batch_id='new',
            received_at=iso_time(NOW), sample={'value':51}))
        return row.slot, original_id


def drain(sessions, now=NOW, maximum=20):
    outputs = []
    for _ in range(maximum):
        value = service.run_tick(sessions, now)
        outputs.append(value)
        if value['phase'] == 'idle':
            return outputs
    raise AssertionError(outputs)


def test_roundtrip_then_small_cleanup_preserves_hour_chemistry_and_raw_highwater(sessions, tmp_path):
    slot, old_id = seed(sessions, pins=True)
    with sessions() as db:
        expected = deepcopy(read_sample(db, slot))
        expected_settlement = ash_settlement.build_settlement(db, START, NOW)
    results = drain(sessions)
    assert any(r['phase'] == 'archived' for r in results)
    with sessions() as db:
        archived = db.get(RetentionArchive, iso_time(START))
        assert archived.status == 'online-removed'
        assert db.get(TelemetryReading, old_id) is None
        assert db.scalar(select(TelemetryReading.id)) > old_id
        assert db.get(DecisionCycleSample, slot) is None
        assert db.get(DecisionCycleHour, iso_time(START)).snapshot == {'original':True}
        assert db.get(RetentionHourBasis, iso_time(START))
        assert ash_settlement.build_settlement(db, START, NOW) == expected_settlement
        path = storage_root(db.bind)/archived.manifest
        restored_rows = list(read_table(path, 'decision_resolved'))
        assert json.loads(restored_rows[0]['snapshot']) == expected['snapshot']
        assert list(read_table(path, 'retention_hour_bases'))[0]['payload']
        restored = restore_bundle(path, tmp_path/'isolated.sqlite')
        assert restored.is_file()


def test_corrupt_archive_preserves_remaining_raw(sessions):
    _, old_id = seed(sessions)
    for _ in range(10):
        if service.run_tick(sessions, NOW)['phase'] == 'archived':
            break
    with sessions() as db:
        row = db.get(RetentionArchive, iso_time(START))
        path = storage_root(db.bind)/row.manifest
        manifest = json.loads(path.read_bytes())
        member = path.parent/manifest['tables'][0]['path']
        member.write_bytes(member.read_bytes()[:-2])
    with pytest.raises(ValueError, match='checksum'):
        service.run_tick(sessions, NOW)
    with sessions() as db:
        assert db.get(TelemetryReading, old_id)


def test_restore_failure_never_deletes_online_evidence(sessions, monkeypatch):
    slot, old_id = seed(sessions)
    service.run_tick(sessions, NOW)
    service.run_tick(sessions, NOW)
    monkeypatch.setattr(service.ArchiveBundleWriter,'finish',lambda self: (_ for _ in ()).throw(ValueError('restore failed')))
    with pytest.raises(ValueError, match='restore failed'):
        service.run_tick(sessions, NOW)
    with sessions() as db:
        assert db.get(DecisionCycleSample, slot)
        assert db.get(TelemetryReading, old_id)
        assert db.get(RetentionArchive, iso_time(START)) is None


def test_thirty_day_boundary_future_evidence_and_readonly_are_preserved(sessions, monkeypatch):
    slot, old_id = seed(sessions)
    boundary = END+timedelta(days=30)-timedelta(microseconds=1)
    drain(sessions, boundary)
    with sessions() as db:
        assert db.get(DecisionCycleSample, slot)
        assert db.get(TelemetryReading, old_id)
    monkeypatch.setattr('app.config.REMOTE_READONLY', {'server_url':'http://invalid'})
    with pytest.raises(PermissionError):
        service.run_tick(sessions, NOW)


def test_detail_expiry_keeps_permanent_late_assay_inputs(sessions):
    slot, old_id = seed(sessions)
    drain(sessions)
    with sessions.begin() as db:
        late = assay(db, 9, observed=START+timedelta(minutes=47), created=NOW)
        late.time_basis = 'production-time-v1'
        db.flush()
        expected = ash_settlement.build_settlement(db, START, NOW)
    expire_now = END+timedelta(days=366)
    drain(sessions, expire_now)
    with sessions() as db:
        row = db.get(RetentionArchive, iso_time(START))
        assert row.status == 'expired'
        assert not (storage_root(db.bind)/row.manifest).exists()
        assert db.get(RetentionHourBasis, iso_time(START))
        assert ash_settlement.build_settlement(db, START, NOW) == expected


def test_future_slot_is_backfilled_and_shared_raw_is_not_deleted(sessions):
    _, old_id = seed(sessions)
    future = NOW+timedelta(days=20)
    with sessions.begin() as db:
        db.add(DecisionTelemetryPin(reading_id=old_id, first_slot=int(START.timestamp())//5, last_slot=None))
        db.add(DecisionCycleSample(slot=int(future.timestamp())//5, sampled_at=iso_time(future),
            context_key='ctx', fingerprint='future', snapshot={'storage_version':VERSION,
                'cells':{'level':{'$reading':old_id,'fields':['value'],'overlay':{}}}}))
    for _ in range(12):
        service.run_tick(sessions, NOW)
    with sessions() as db:
        pin = db.get(DecisionTelemetryPin,old_id)
        assert pin.last_slot == int(future.timestamp())//5
        assert db.get(TelemetryReading,old_id)
        assert db.get(DecisionCycleSample,int(future.timestamp())//5)


def test_concurrent_decision_insert_aborts_online_removal(sessions, monkeypatch):
    slot, old_id = seed(sessions)
    service.run_tick(sessions,NOW)
    service.run_tick(sessions,NOW)
    original = service.ArchiveBundleWriter.finish
    def finish(writer):
        path = original(writer)
        with sessions.begin() as db:
            sample(db,START+timedelta(seconds=20))
        return path
    monkeypatch.setattr(service.ArchiveBundleWriter,'finish',finish)
    with pytest.raises(ValueError,match='Hour changed'):
        service.run_tick(sessions,NOW)
    with sessions() as db:
        assert db.get(DecisionCycleSample,slot)
        assert db.get(TelemetryReading,old_id)
        assert db.get(RetentionArchive,iso_time(START)) is None


def test_changed_raw_is_preserved(sessions):
    slot, old_id = seed(sessions)
    for _ in range(10):
        if service.run_tick(sessions,NOW)['phase']=='archived':
            break
    with sessions.begin() as db:
        db.get(TelemetryReading,old_id).sample={'value':999}
    with pytest.raises(ValueError,match='Raw row changed'):
        service.run_tick(sessions,NOW)
    with sessions() as db:
        assert db.get(TelemetryReading,old_id).sample['value']==999


def test_expiry_resumes_after_members_removed(sessions):
    seed(sessions)
    drain(sessions)
    with sessions.begin() as db:
        row=db.get(RetentionArchive,iso_time(START))
        row.status='expiring'
        path=storage_root(db.bind)/row.manifest
        for file in (path.parent/'tables').iterdir():
            file.unlink()
        (path.parent/'tables').rmdir()
        (path.parent/'manifest.sha256').unlink()
    result=service.run_tick(sessions,END+timedelta(days=366))
    with sessions() as db:
        assert db.get(RetentionArchive,iso_time(START)).status=='expired'
        assert db.get(DecisionCycleHour,iso_time(START))


def test_expired_tracking_window_reports_insufficient_without_losing_action(sessions):
    from types import SimpleNamespace
    from app.decision_hourly.tracking import assess_window
    seed(sessions)
    drain(sessions)
    drain(sessions,END+timedelta(days=366))
    action=SimpleNamespace(evidence={'before':{'sampled_at':iso_time(START+timedelta(seconds=5))},
                                    'after':{'sampled_at':iso_time(START+timedelta(seconds=15))}})
    with sessions() as db:
        result=assess_window(db,action,START+timedelta(seconds=10),START+timedelta(seconds=20))
        assert result['status']=='insufficient'
        assert '一年' in result['reason']
        assert db.get(RetentionHourBasis,iso_time(START))


def test_raw_cleanup_uses_small_batches_and_keeps_last_id(sessions):
    seed(sessions)
    with sessions.begin() as db:
        db.add_all([TelemetryReading(source_id='kep',point_key='level',batch_id='batch-'+str(i),
            received_at=iso_time(START+timedelta(seconds=1)),sample={'value':i}) for i in range(601)])
    results=drain(sessions)
    deletes=[r['deleted'] for r in results if r['phase']=='raw-cleanup']
    assert deletes and max(deletes)==500 and all(0<=n<=500 for n in deletes)
    with sessions() as db:
        rows=list(db.scalars(select(TelemetryReading)))
        assert len(rows)==2  # Newer receipt plus the highest integer id sentinel.


def test_missing_original_hour_cannot_be_archived(sessions):
    slot,old_id=seed(sessions)
    with sessions.begin() as db:
        db.delete(db.get(DecisionCycleHour,iso_time(START)))
    service.run_tick(sessions,NOW)
    service.run_tick(sessions,NOW)
    with pytest.raises(ValueError,match='hourly summary'):
        service.run_tick(sessions,NOW)
    with sessions() as db:
        assert db.get(DecisionCycleSample,slot)
        assert db.get(TelemetryReading,old_id)
