"""Original minute envelopes remain traceable through cold retention and expiry."""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4
import pytest

from sqlalchemy import select

from app.decision_hourly.archive import archive_old_hour, archived_rows, read_sample
from app.decision_hourly.ash_settlement import build_settlement
from app.decision_hourly.models import (DecisionCycleHour, DecisionCycleSample,
    DecisionEvidenceArchive, DecisionTelemetryPin)
from app.decision_hourly.references import walk
from app.decision_hourly.service import build_hour, prepare_record, save_record
from app.hourly_brief.inputs import LEVEL
from app.services.decision_sources import shared_snapshot
from app.storage_retention.models import RetentionArchive, RetentionHourBasis
from app.storage_retention.service import run_tick
from app.telemetry.domain import iso_time, normalize
from app.telemetry.models import TelemetryReading
from app.telemetry.realtime import RuntimePublisher, select_alignment_evidence
from app.telemetry.spool import DurableSpool
from test_ash_settlement import assay
from test_decision_sources import seeded
from test_hourly_brief import HOUR, sessions


@pytest.fixture(autouse=True)
def compact_metadata_storage(sessions, tmp_path):
    from point_metadata import Catalog
    from app.telemetry.metadata import install_codec
    install_codec(sessions.kw['bind'], catalog=Catalog(tmp_path/'point-metadata'))


def _advance(sessions, now, maximum=24):
    results = []
    for _ in range(maximum):
        result = run_tick(sessions, now)
        results.append(result)
        if result['phase'] == 'idle':
            return results
    raise AssertionError(results)


def test_minute_spool_to_original_decision_cold_archive_and_permanent_hour(sessions, seeded, monkeypatch, tmp_path):
    cfg, repo, values = seeded
    cfg.history_interval_seconds = 60
    now = HOUR+timedelta(seconds=5)
    start, end = HOUR, HOUR+timedelta(hours=1)
    monkeypatch.setattr('app.telemetry.repository.utc_now', lambda: now)
    publisher = RuntimePublisher.for_repository(repo, cfg, 'owner')
    originals = []
    for offset, level in ((-95, 22), (-90, 33), (0, 88)):
        when = now+timedelta(seconds=offset)
        samples = [normalize(point, level if point.item_id == LEVEL else value, 0,
            when, when, when) for point, value in zip(cfg.points, values)]
        packet = dict(source_id=cfg.source_id, batch_id=uuid4().hex, samples=samples)
        originals.append(packet)
        monkeypatch.setattr('app.telemetry.realtime.utc_now', lambda when=when: when)
        publisher.publish('connected', batch=packet)
    evidence = select_alignment_evidence(publisher.document, cfg, 'owner', now)
    assert len(evidence) == 1 and evidence[0]['sample']['value'] == 33
    minute = dict(deepcopy(originals[-1]), storage_policy='periodic-v1',
        storage_kind='periodic', cadence_seconds=60, alignment_evidence=evidence)
    minute['samples'] = [dict(sample, storage_kind='periodic', cadence_seconds=60)
                         for sample in minute['samples']]
    spool = DurableSpool(tmp_path/'original-spool', cfg.source_id)
    pending = spool.enqueue(minute)
    # The persistent envelope, not a later cache, owns its historical evidence.
    publisher.document['batches'] = [deepcopy(originals[-1])]
    replay = spool.read(pending)
    repo.persist(cfg, 'owner', replay)
    repo.persist(cfg, 'owner', replay)
    spool.ack(pending)

    with sessions.begin() as db:
        auxiliary = db.scalar(select(TelemetryReading).where(
            TelemetryReading.batch_id == evidence[0]['batch_id'],
            TelemetryReading.point_key == evidence[0]['point_key']))
        auxiliary_id = auxiliary.id
        assert auxiliary.sample['value'] == 33
        assert auxiliary.received_at == iso_time(now-timedelta(seconds=90))
        assert auxiliary.sample['cadence_seconds'] == 5
        snapshot = shared_snapshot(db, now, live=True, realtime=False)
        assert snapshot['cells']['level']['value'] == 33
        assert snapshot['level_observation']['value'] == 88
        assert snapshot['cells']['level']['observed_at'] == auxiliary.received_at
        prepared = prepare_record(db, now, snapshot, cadence_seconds=60)
        assert prepared is not None
        assert auxiliary_id in {node['$reading'] for node in walk(prepared['packed']) if '$reading' in node}
        assert save_record(db, prepared)
        slot = prepared['slot']
        assert db.get(DecisionTelemetryPin, auxiliary_id).last_slot == slot
        original = deepcopy(read_sample(db, slot))
        assert original['snapshot']['cadence_seconds'] == 60
        frozen = build_hour(db, start, end, end)
        db.add(DecisionCycleHour(start=iso_time(start), end=iso_time(end),
            generated_at=iso_time(end), snapshot=frozen))
        db.flush()
        first_settlement = build_settlement(db, start, end+timedelta(hours=2))
        # Exercise both the existing in-database compressed tier and the new
        # self-contained cold tier, with the same original source references.
        archive_old_hour(db, iso_time(end))
        db.flush()
        assert db.get(DecisionCycleSample, slot) is None
        assert db.get(DecisionEvidenceArchive, iso_time(start)).sample_count == 1
        assert read_sample(db, slot)['snapshot'] == original['snapshot']
        # Keep a newer integer ID so every minute/auxiliary source row can retire.
        db.add(TelemetryReading(source_id=cfg.source_id, point_key=cfg.points[0].key,
            batch_id='highwater-'+uuid4().hex, received_at=iso_time(end+timedelta(days=1)),
            sample={'value':40, 'quality':'good'}))

    migrated = _advance(sessions, end+timedelta(days=31))
    assert any(result['phase'] == 'archived' for result in migrated)
    with sessions() as db:
        assert db.get(DecisionCycleSample, slot) is None
        assert db.get(DecisionEvidenceArchive, iso_time(start)) is None
        assert db.get(TelemetryReading, auxiliary_id) is None
        cold = read_sample(db, slot)
        assert cold['snapshot'] == original['snapshot']
        assert cold['sampled_at'] == original['sampled_at']
        assert db.get(DecisionCycleHour, iso_time(start)).snapshot == frozen
        assert db.get(RetentionHourBasis, iso_time(start))
        assert build_settlement(db, start, end+timedelta(hours=2)) == first_settlement

    late_now = end+timedelta(days=32)
    with sessions.begin() as db:
        late = assay(db, 10, observed=start+timedelta(minutes=47), created=late_now)
        late.time_basis = 'production-time-v1'
        db.flush()
        revised = build_settlement(db, start, late_now)
        assert revised['item']['value'] != first_settlement['item']['value']
        assert revised['item']['recalculated_count'] == 1
        assert db.get(DecisionCycleHour, iso_time(start)).snapshot == frozen

    _advance(sessions, end+timedelta(days=366))
    with sessions() as db:
        assert db.get(RetentionArchive, iso_time(start)).status == 'expired'
        assert db.get(RetentionHourBasis, iso_time(start))
        assert build_settlement(db, start, late_now) == revised
        assert db.get(DecisionCycleHour, iso_time(start)).snapshot == frozen
        # Raw-only boundary hours never had decision details to expire.
        assert archived_rows(db, iso_time(start-timedelta(hours=1))) == []
