"""One-minute SQL batches retain only the original level evidence needed at t-90."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import uuid

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.gpt_coarse.timing import aligned_level
from app.hourly_brief.inputs import Inputs, LEVEL
from app.decision_hourly.references import pack_snapshot, resolve_snapshots
from app.telemetry.config import CollectorConfig, PointConfig
from app.telemetry.domain import iso_time, normalize, utc_now
from app.telemetry.models import TelemetryPoint, TelemetryReading, TelemetrySource
from app.telemetry.realtime import RuntimePublisher, select_alignment_evidence
from app.telemetry.repository import TelemetryRepository
from app.telemetry.spool import DurableSpool
from app.telemetry.worker import run_collector
from app.telemetry.metadata import install_codec


@pytest.fixture
def setup(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path/'isolated.db'),
                           connect_args={'check_same_thread': False})
    install_codec(engine)
    Base.metadata.create_all(engine)
    cfg = CollectorConfig(enabled=True, endpoint='opc.tcp://127.0.0.1:1', security='None',
        allow_insecure=True, poll_seconds=5, history_interval_seconds=60,
        points=[PointConfig(key='level', name='Level', item_id=LEVEL, node_id='ns=2;s=Level', unit='%'),
                PointConfig(key='coal', name='Coal', item_id='301/COAL_CAP', node_id='ns=2;s=Coal', unit='t/h')])
    repo = TelemetryRepository(sessionmaker(engine))
    repo.acquire(cfg, 'owner')
    yield cfg, repo
    engine.dispose()


def batch(cfg, value, when, status=0):
    return dict(source_id=cfg.source_id, batch_id=uuid.uuid4().hex,
                samples=[normalize(point, value, status, when, when, when) for point in cfg.points])


def minute(cfg, current, evidence):
    result = deepcopy(current)
    result.update(storage_policy='periodic-v1', storage_kind='periodic', cadence_seconds=60,
                  alignment_evidence=evidence)
    result['samples'] = [dict(sample, storage_kind='periodic', cadence_seconds=60)
                         for sample in result['samples']]
    return result


def packet(setup, monkeypatch, *, status=0):
    cfg, repo = setup
    now = utc_now().replace(second=5, microsecond=0)
    publisher = RuntimePublisher.for_repository(repo, cfg, 'owner')
    originals = []
    for seconds, value, quality in [(-95, 22, 0), (-90, 33, status), (0, 88, 0)]:
        when = now + timedelta(seconds=seconds)
        monkeypatch.setattr('app.telemetry.realtime.utc_now', lambda when=when: when)
        reading = batch(cfg, value, when, quality)
        originals.append(reading)
        publisher.publish('connected', batch=reading)
    evidence = select_alignment_evidence(publisher.document, cfg, 'owner', now)
    return now, publisher, originals, minute(cfg, originals[-1], evidence)


def test_selects_one_original_level_event_without_copying_all_five_second_history(setup, monkeypatch):
    cfg, _ = setup
    now, _, originals, current = packet(setup, monkeypatch)
    evidence = current['alignment_evidence']
    assert len(evidence) == 1
    assert evidence[0]['point_key'] == 'level'
    assert evidence[0]['batch_id'] == originals[1]['batch_id']
    assert evidence[0]['prediction_at'] == iso_time(now)
    assert evidence[0]['target_at'] == iso_time(now-timedelta(seconds=90))
    assert evidence[0]['sample'] == dict(originals[1]['samples'][0],
                                       storage_kind='alignment-evidence', cadence_seconds=5)
    cfg.history_interval_seconds = 30
    assert select_alignment_evidence({'state': 'connected'}, cfg, 'owner', now) == []


@pytest.mark.parametrize('cause', ['owner', 'source', 'retry', 'definition', 'missing', 'too_old'])
def test_alignment_never_crosses_owner_connection_definition_or_missing_window(setup, monkeypatch, cause):
    cfg, _ = setup
    now, publisher, _, _ = packet(setup, monkeypatch)
    if cause == 'owner': publisher.document['owner'] = 'other'
    if cause == 'source': publisher.document['source_id'] = 'other'
    if cause == 'retry':
        publisher.publish('retrying', 'BadTimeout')
        publisher.publish('connected', batch=batch(cfg, 99, now))
        assert len(publisher.document['batches']) == 1
    if cause == 'definition': cfg.points[0].scale = 2
    if cause == 'missing': publisher.document['batches'] = []
    if cause == 'too_old': now += timedelta(seconds=125)
    assert select_alignment_evidence(publisher.document, cfg, 'owner', now) == []


def test_bad_latest_level_is_captured_and_rejected_instead_of_using_older_good(setup, monkeypatch):
    cfg, repo = setup
    now, _, originals, current = packet(setup, monkeypatch, status=0x80000000)
    assert current['alignment_evidence'][0]['batch_id'] == originals[1]['batch_id']
    assert current['alignment_evidence'][0]['sample']['quality'] == 'bad'
    repo.persist(cfg, 'owner', current)
    with repo.sessions() as db:
        result = aligned_level(Inputs(db, now, authoritative=True),
                               dict(source_id=cfg.source_id, point_key='level', unit='%'))
        assert result['value'] is None
        assert result['sample']['batch_id'] == originals[1]['batch_id']
        assert result['sample']['quality'] == 'bad'


def test_auxiliary_and_periodic_commit_together_without_rewinding_current_state(setup, monkeypatch):
    cfg, repo = setup
    now, _, originals, current = packet(setup, monkeypatch)
    repo.persist(cfg, 'owner', current)
    repo.persist(cfg, 'owner', current)  # Exact durable spool replay is idempotent.
    with repo.sessions() as db:
        readings = list(db.scalars(select(TelemetryReading)))
        assert len(readings) == len(cfg.points)+1
        assert sum(r.sample['storage_kind'] == 'periodic' for r in readings) == len(cfg.points)
        assert sum(r.sample['storage_kind'] == 'alignment-evidence' for r in readings) == 1
        point = db.get(TelemetryPoint, (cfg.source_id, 'level'))
        source = db.get(TelemetrySource, cfg.source_id)
        assert point.latest['received_at'] == point.last_history_at == source.last_sample_at == iso_time(now)
        assert point.latest['value'] == 88
        assert point.latest['batch_id'] == current['batch_id']
        assert source.owner == 'owner'
        result = aligned_level(Inputs(db, now, authoritative=True),
                               dict(source_id=cfg.source_id, point_key='level', unit='%'))
        assert result['value'] == 33
        assert result['sample']['batch_id'] == originals[1]['batch_id']
        assert result['observed_at'] == iso_time(now-timedelta(seconds=90))
        assert result['alignment']['offset_seconds'] == 0
        packed = pack_snapshot(db, {'cells': {'level': result}, 'inputs': {}}, 1, pins=set())
        assert '$reading' in packed['cells']['level']['sample']
        assert resolve_snapshots(db, [packed])[0]['cells']['level']['value'] == 33


@pytest.mark.parametrize('field', ['source', 'point', 'definition', 'node', 'kind', 'cadence',
                                  'prediction', 'target', 'future', 'expired', 'runtime', 'multiple'])
def test_invalid_evidence_cannot_commit_any_part_of_the_minute(setup, monkeypatch, field):
    cfg, repo = setup
    now, _, _, current = packet(setup, monkeypatch)
    evidence = current['alignment_evidence'][0]
    sample = evidence['sample']
    if field == 'source': evidence['source_id'] = 'other'
    if field == 'point': evidence['point_key'] = sample['key'] = 'coal'
    if field == 'definition': sample['definition_id'] = 'other'
    if field == 'node': sample['node_id'] = 'other'
    if field == 'kind': sample['storage_kind'] = 'periodic'
    if field == 'cadence': sample['cadence_seconds'] = 60
    if field == 'prediction': evidence['prediction_at'] = iso_time(now-timedelta(seconds=1))
    if field == 'target': evidence['target_at'] = iso_time(now-timedelta(seconds=120))
    if field == 'future': sample['received_at'] = iso_time(now-timedelta(seconds=89))
    if field == 'expired': sample['received_at'] = iso_time(now-timedelta(seconds=121))
    if field == 'runtime': sample['runtime_only'] = True
    if field == 'multiple': current['alignment_evidence'].append(deepcopy(evidence))
    with pytest.raises(ValueError): repo.persist(cfg, 'owner', current)
    with repo.sessions() as db:
        assert db.scalar(select(func.count()).select_from(TelemetryReading)) == 0
        assert db.get(TelemetryPoint, (cfg.source_id, 'level')).latest is None
        assert db.get(TelemetrySource, cfg.source_id).last_sample_at is None


def test_failure_after_auxiliary_insert_rolls_back_auxiliary_and_main_rows(setup, monkeypatch):
    cfg, repo = setup
    _, _, _, current = packet(setup, monkeypatch)
    current['samples'].append(dict(current['samples'][-1], key='unknown'))
    with pytest.raises(ValueError): repo.persist(cfg, 'owner', current)
    with repo.sessions() as db:
        assert db.scalar(select(func.count()).select_from(TelemetryReading)) == 0


def test_replay_preserves_old_evidence_without_replacing_a_new_definition(setup, monkeypatch):
    cfg, repo = setup
    now, _, _, old = packet(setup, monkeypatch)
    repo.state(cfg, 'owner', 'stopped', release=True)
    cfg.points[0].scale = 2
    repo.acquire(cfg, 'new-owner')
    new = minute(cfg, batch(cfg, 50, now+timedelta(minutes=1)), [])
    repo.persist(cfg, 'new-owner', new)
    repo.persist(cfg, 'new-owner', old)
    with repo.sessions() as db:
        point = db.get(TelemetryPoint, (cfg.source_id, 'level'))
        assert point.latest['value'] == 100
        assert point.latest['batch_id'] == new['batch_id']
        assert point.last_history_at == iso_time(now+timedelta(minutes=1))
        assert db.get(TelemetrySource, cfg.source_id).last_sample_at == iso_time(now+timedelta(minutes=1))
        assert db.scalar(select(func.count()).select_from(TelemetryReading)) == 5


def test_spool_replay_uses_its_original_evidence_after_the_live_ring_changes(setup, monkeypatch, tmp_path):
    cfg, repo = setup
    now, publisher, originals, current = packet(setup, monkeypatch)
    spool = DurableSpool(tmp_path/'replay', cfg.source_id)
    saved = spool.enqueue(current)
    publisher.document['batches'] = [batch(cfg, 99, now-timedelta(seconds=90))]
    repo.persist(cfg, 'owner', spool.read(saved))
    with repo.sessions() as db:
        row = db.scalar(select(TelemetryReading).where(
            TelemetryReading.point_key == 'level', TelemetryReading.batch_id == originals[1]['batch_id']))
        assert row.sample['value'] == 33
        assert row.sample['received_at'] == originals[1]['samples'][0]['received_at']


def test_worker_minute_spool_contains_only_periodic_points_and_one_lag_event(setup, monkeypatch, tmp_path):
    cfg, repo = setup
    repo.state(cfg, 'owner', 'stopped', release=True)
    cfg.poll_seconds = .1  # Accelerated waits; raw observation clocks still advance five seconds.
    stop = asyncio.Event()
    base = utc_now().replace(second=0, microsecond=0)-timedelta(minutes=3)
    reads = []
    envelopes = []

    class Spool(DurableSpool):
        def enqueue(self, payload):
            envelopes.append(deepcopy(payload))
            return super().enqueue(payload)

    class Reader:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def read(self):
            when = base+timedelta(seconds=5*len(reads))
            monkeypatch.setattr('app.telemetry.realtime.utc_now', lambda: when)
            reads.append(when)
            if len(reads) == 37: stop.set()
            return batch(cfg, len(reads), when)['samples']

    assert asyncio.run(run_collector(cfg, repo, Spool(tmp_path/'spool', cfg.source_id),
                                     stop=stop, reader_factory=lambda _: Reader())) == 0
    assert len(reads) == 37
    full = [p for p in envelopes if p['storage_policy'] == 'periodic-v1']
    supplemental = [p for p in envelopes if p['storage_policy'] == 'point-history-v1']
    assert len(full) == 4 and len(supplemental) == 3
    assert [len(p['alignment_evidence']) for p in full] == [0, 0, 1, 1]
    assert all(len(p['samples']) == 2 for p in full)
    assert all(len(p['samples']) == 1 for p in supplemental)
    with repo.sessions() as db:
        rows = list(db.scalars(select(TelemetryReading)))
        periodic = [row for row in rows if row.sample['storage_kind'] == 'periodic']
        extra = [row for row in rows if row.sample['storage_kind'] == 'level-periodic']
        assert len(periodic) == 8
        assert len(extra) == 3
        assert all(row.sample['cadence_seconds'] == 60 for row in periodic)
        assert all(row.sample['cadence_seconds'] == 30 for row in extra)
        # Lag evidence reuses the same original poll already saved by the 30s
        # policy, without a second SQL row or changing the minute envelope.
        for envelope in full:
            for evidence in envelope['alignment_evidence']:
                matching = [row for row in rows if row.batch_id == evidence['batch_id']
                            and row.point_key == evidence['point_key']]
                assert len(matching) == 1
                assert matching[0].received_at == evidence['sample']['received_at']
