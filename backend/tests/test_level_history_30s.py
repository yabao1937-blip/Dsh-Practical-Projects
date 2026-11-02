"""Single-point history policy: synthetic 5s polls and temporary SQLite only."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.gpt_coarse.timing import LEVEL_LAG_SECONDS, aligned_level
from app.hourly_brief.inputs import Inputs
from app.routers.telemetry import history
from app.telemetry import worker
from app.telemetry.config import CollectorConfig, PointConfig
from app.telemetry.domain import iso_time, normalize
from app.telemetry.metadata import install_codec
from app.telemetry.models import TelemetryPoint, TelemetryReading, TelemetrySource
from app.telemetry.realtime import ALIGNMENT_LEVEL_ITEM, RuntimePublisher
from app.telemetry.repository import TelemetryRepository, latest_view
from app.telemetry.spool import DurableSpool


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path/'isolated.db'),
                          connect_args={'check_same_thread': False})
    install_codec(engine)
    Base.metadata.create_all(engine)
    base = datetime(2026, 11, 2, 0, 10, tzinfo=timezone.utc)
    clock = {'now': base}
    monkeypatch.setattr('app.telemetry.repository.utc_now', lambda: clock['now'])
    monkeypatch.setattr('app.telemetry.realtime.utc_now', lambda: clock['now'])

    class FastAsyncio:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def wait_for(self, awaitable, timeout):
            # Only worker.pause uses wait_for. Keep poll_seconds=5 and advance
            # observation clocks explicitly while yielding to the real writer.
            awaitable.close()
            await asyncio.sleep(.005)
            raise asyncio.TimeoutError

    monkeypatch.setattr(worker, 'asyncio', FastAsyncio())
    cfg = CollectorConfig(enabled=True, endpoint='opc.tcp://127.0.0.1:1',
        security='None', allow_insecure=True, poll_seconds=5, history_interval_seconds=60,
        retry_min_seconds=.1, retry_max_seconds=.1,
        points=[PointConfig(key='renamed_level', name='Unrelated display label',
                    node_id='ns=2;s=Level', item_id=ALIGNMENT_LEVEL_ITEM, unit='%', max_age_seconds=120),
                PointConfig(key='coal', name='精磁尾液位', node_id='ns=2;s=Coal',
                    item_id='301/COAL_CAP', unit='t/h', max_age_seconds=120)])
    ctx = SimpleNamespace(cfg=cfg, repo=TelemetryRepository(sessionmaker(engine)),
                          base=base, clock=clock, tmp_path=tmp_path)
    yield ctx
    engine.dispose()


def packet(ctx, offset, status=0):
    when = ctx.base + timedelta(seconds=offset)
    return dict(source_id=ctx.cfg.source_id, batch_id=uuid4().hex,
        samples=[normalize(point, 40+offset/10 if index == 0 else 100+offset, status,
                           when-timedelta(seconds=7), when-timedelta(seconds=2), when)
                 for index, point in enumerate(ctx.cfg.points)])


def level_packet(ctx, offset=30, status=0):
    result = packet(ctx, offset, status)
    result.update(storage_policy='point-history-v1', storage_kind='level-periodic',
                  cadence_seconds=30, point_config=ctx.cfg.points[0].model_dump())
    result['samples'] = [dict(result['samples'][0], storage_kind='level-periodic', cadence_seconds=30)]
    return result


def collect(ctx, monkeypatch, offsets, *, statuses=None, spool=None, on_read=None):
    stop = asyncio.Event()
    reads, envelopes, published = [], [], []
    original_publish = RuntimePublisher.publish

    def publish(self, state, code=None, delay=0, batch=None):
        if batch:
            published.append(deepcopy(batch))
        return original_publish(self, state, code, delay, batch)

    monkeypatch.setattr(RuntimePublisher, 'publish', publish)

    class Spool(DurableSpool):
        def enqueue(self, payload):
            envelopes.append(deepcopy(payload))
            return super().enqueue(payload)

    spool = spool or Spool(ctx.tmp_path/'spool', ctx.cfg.source_id)

    class Reader:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

        async def read(self):
            index = len(reads)
            if on_read:
                await on_read(index)
            offset = offsets[index]
            ctx.clock['now'] = ctx.base + timedelta(seconds=offset)
            current = packet(ctx, offset, (statuses or {}).get(offset, 0))
            reads.append(deepcopy(current))
            if len(reads) == len(offsets):
                stop.set()
            return current['samples']

    result = asyncio.run(worker.run_collector(ctx.cfg, ctx.repo, spool, stop=stop,
                                              reader_factory=lambda _: Reader()))
    return result, reads, envelopes, published, spool


def stored_offsets(ctx, rows, key, kind=None):
    return [int((datetime.fromisoformat(row.received_at.replace('Z', '+00:00'))-ctx.base).total_seconds())
            for row in rows if row.point_key == key and (kind is None or row.sample.get('storage_kind') == kind)]


def test_five_second_polls_store_level_30_and_other_points_60(isolated, monkeypatch):
    ctx = isolated
    result, reads, envelopes, published, spool = collect(ctx, monkeypatch, list(range(0, 121, 5)))
    assert result == 0 and len(reads) == len(published) == 25 and not spool.pending()
    assert all(len(batch['samples']) == 2 for batch in published)
    assert all('storage_policy' not in batch for batch in published)
    full = [batch for batch in envelopes if batch['storage_policy'] == 'periodic-v1']
    extra = [batch for batch in envelopes if batch['storage_policy'] == 'point-history-v1']
    assert [batch['cadence_seconds'] for batch in full] == [60, 60, 60]
    assert len(extra) == 2 and all(len(batch['samples']) == 1 for batch in extra)
    assert [len(batch['alignment_evidence']) for batch in full] == [0, 0, 1]
    assert all('hour_boundary_evidence' in batch for batch in full)
    assert all('alignment_evidence' not in batch and 'hour_boundary_evidence' not in batch for batch in extra)
    with ctx.repo.sessions() as db:
        rows = list(db.scalars(select(TelemetryReading).order_by(TelemetryReading.received_at)))
        assert stored_offsets(ctx, rows, 'renamed_level') == [0, 30, 60, 90, 120]
        assert stored_offsets(ctx, rows, 'coal') == [0, 60, 120]
        # Selected t-90 evidence reuses the supplemental original poll, so it
        # resolves to one row even though two envelopes mention it.
        selected = full[-1]['alignment_evidence'][0]
        evidence = [row for row in rows if row.batch_id == selected['batch_id']]
        assert len(evidence) == 1 and evidence[0].sample['storage_kind'] == 'level-periodic'
        for batch in extra:
            original = next(batch0 for batch0 in published if batch0['batch_id'] == batch['batch_id'])['samples'][0]
            sample = batch['samples'][0]
            assert all(sample[field] == original[field]
                       for field in ('received_at', 'source_at', 'server_at', 'quality', 'value'))
        # All three candidate times can now be read as distinct real events.
        values = []
        for lag in (60, 90, 120):
            target = iso_time(ctx.base+timedelta(seconds=120-lag))
            row = db.scalar(select(TelemetryReading).where(TelemetryReading.point_key == 'renamed_level',
                TelemetryReading.received_at <= target).order_by(TelemetryReading.received_at.desc()).limit(1))
            values.append(row.sample['value'])
        assert values == [46, 43, 40]
        response = history(source_id=ctx.cfg.source_id, point_key='renamed_level', limit=100, before_id=None, db=db)
        assert len(response['items']) == 5
        delayed = aligned_level(Inputs(db, ctx.base+timedelta(seconds=30+LEVEL_LAG_SECONDS)),
                                {'source_id':ctx.cfg.source_id, 'point_key':'renamed_level'})
        assert delayed['value'] == 43 and delayed['observed_at'] == iso_time(ctx.base+timedelta(seconds=30))


@pytest.mark.parametrize('offsets,expected', [([0, 29, 30, 59, 60], [0, 30, 60]),
                                            ([29, 30, 60], [29, 30, 60]),
                                            ([0, 35, 95, 120], [0, 35, 95, 120])])
def test_boundaries_and_missed_polls_preserve_real_observation_times(isolated, monkeypatch, offsets, expected):
    ctx = isolated
    assert collect(ctx, monkeypatch, offsets)[0] == 0
    with ctx.repo.sessions() as db:
        rows = list(db.scalars(select(TelemetryReading).order_by(TelemetryReading.received_at)))
        assert stored_offsets(ctx, rows, 'renamed_level') == expected


def test_quality_events_do_not_move_the_full_periodic_clock(isolated, monkeypatch):
    ctx = isolated
    result, _, envelopes, _, _ = collect(ctx, monkeypatch, [0, 25, 30, 35, 60, 90],
                                        statuses={25:0x80000000, 30:0x80000000})
    assert result == 0
    full = [batch for batch in envelopes if batch['storage_policy'] == 'periodic-v1']
    assert [batch['storage_kind'] for batch in full] == ['periodic', 'quality-event', 'quality-event', 'periodic']
    with ctx.repo.sessions() as db:
        rows = list(db.scalars(select(TelemetryReading).order_by(TelemetryReading.received_at)))
        assert stored_offsets(ctx, rows, 'coal') == [0, 25, 35, 60]
        assert stored_offsets(ctx, rows, 'renamed_level') == [0, 25, 30, 35, 60, 90]
        assert next(row for row in rows if row.received_at == iso_time(ctx.base+timedelta(seconds=30))).sample['quality'] == 'bad'


@pytest.mark.parametrize('base_interval', [5, 15, 30])
def test_finer_existing_global_history_is_preserved(isolated, monkeypatch, base_interval):
    ctx = isolated
    ctx.cfg.history_interval_seconds = base_interval
    result, _, envelopes, _, _ = collect(ctx, monkeypatch, list(range(0, 61, 5)))
    assert result == 0 and all(batch['storage_policy'] == 'periodic-v1' for batch in envelopes)
    with ctx.repo.sessions() as db:
        rows = list(db.scalars(select(TelemetryReading).order_by(TelemetryReading.received_at)))
        assert stored_offsets(ctx, rows, 'renamed_level') == list(range(0, 61, base_interval))
        assert stored_offsets(ctx, rows, 'coal') == list(range(0, 61, base_interval))


@pytest.mark.parametrize('case', ['disabled', 'missing', 'ambiguous'])
def test_disabled_missing_or_ambiguous_semantic_target_keeps_full_batches(isolated, monkeypatch, case):
    ctx = isolated
    if case == 'disabled': ctx.cfg.level_history_interval_seconds = None
    if case == 'missing': ctx.cfg.points[0].item_id = 'other/LEVEL'
    if case == 'ambiguous': ctx.cfg.points[1].item_id = ALIGNMENT_LEVEL_ITEM
    result, _, envelopes, _, _ = collect(ctx, monkeypatch, [0, 30, 60])
    assert result == 0 and len(envelopes) == 2
    assert all(batch['storage_policy'] == 'periodic-v1' for batch in envelopes)


@pytest.mark.parametrize('coefficients', ['defaults', 'integers', 'floats'])
def test_history_only_write_preserves_latest_clocks_density_and_coverage(isolated, monkeypatch, coefficients):
    ctx = isolated
    if coefficients != 'defaults':
        values = (1, 0) if coefficients == 'integers' else (1.0, 0.0)
        ctx.cfg.points[0] = PointConfig.model_validate({**ctx.cfg.points[0].model_dump(),
                                                       'scale':values[0], 'offset':values[1]})
    ctx.repo.acquire(ctx.cfg, 'owner')
    ctx.repo.state(ctx.cfg, 'owner', 'connected')
    original = packet(ctx, 0)
    ctx.repo.persist(ctx.cfg, 'owner', original)
    calls = []
    monkeypatch.setattr('app.telemetry.repository.remember_density', lambda *args: calls.append(args))
    supplement = level_packet(ctx)
    ctx.repo.persist(ctx.cfg, 'owner', supplement)
    ctx.repo.persist(ctx.cfg, 'owner', supplement)
    with ctx.repo.sessions() as db:
        for point in db.scalars(select(TelemetryPoint)):
            assert point.latest['batch_id'] == original['batch_id']
            assert point.last_history_at == iso_time(ctx.base)
        assert db.get(TelemetrySource, ctx.cfg.source_id).last_sample_at == iso_time(ctx.base)
        assert latest_view(db, now=ctx.base+timedelta(seconds=30))['summary'] == {'points':2, 'available':2}
        assert len(list(db.scalars(select(TelemetryReading)))) == 3
    assert calls == []
    # A legacy envelope still sees the ordinary 60s history clock, not t=30.
    ctx.repo.persist(ctx.cfg, 'owner', packet(ctx, 60))
    with ctx.repo.sessions() as db:
        rows = list(db.scalars(select(TelemetryReading).order_by(TelemetryReading.received_at)))
        assert stored_offsets(ctx, rows, 'renamed_level') == [0, 30, 60]
        assert stored_offsets(ctx, rows, 'coal') == [0, 60]


def test_old_supplement_replays_after_key_and_definition_change(isolated):
    ctx = isolated
    ctx.repo.acquire(ctx.cfg, 'owner')
    old = level_packet(ctx)
    ctx.repo.state(ctx.cfg, 'owner', 'stopped', release=True)
    ctx.cfg.points[0].key = 'new_level'
    ctx.cfg.points[0].node_id = 'ns=2;s=ReplacementLevel'
    ctx.cfg.level_history_interval_seconds = None
    ctx.repo.acquire(ctx.cfg, 'new_owner')
    ctx.repo.persist(ctx.cfg, 'new_owner', old)
    ctx.repo.persist(ctx.cfg, 'new_owner', old)
    with ctx.repo.sessions() as db:
        previous = db.get(TelemetryPoint, (ctx.cfg.source_id, 'renamed_level'))
        current = db.get(TelemetryPoint, (ctx.cfg.source_id, 'new_level'))
        assert not previous.enabled and previous.latest is None and current.latest is None
        rows = list(db.scalars(select(TelemetryReading)))
        assert len(rows) == 1 and rows[0].sample['definition_id'] == old['samples'][0]['definition_id']


@pytest.mark.parametrize('damage', ['extra_sample', 'wrong_item', 'wrong_definition', 'wrong_cadence', 'evidence'])
def test_invalid_history_only_envelope_cannot_partially_commit(isolated, damage):
    ctx = isolated
    ctx.repo.acquire(ctx.cfg, 'owner')
    bad = level_packet(ctx)
    if damage == 'extra_sample': bad['samples'].append(deepcopy(bad['samples'][0]))
    if damage == 'wrong_item': bad['point_config']['item_id'] = 'unrelated/LEVEL'
    if damage == 'wrong_definition': bad['samples'][0]['definition_id'] = 'old'
    if damage == 'wrong_cadence': bad['samples'][0]['cadence_seconds'] = 5
    if damage == 'evidence': bad['alignment_evidence'] = [{}]
    with pytest.raises(ValueError):
        ctx.repo.persist(ctx.cfg, 'owner', bad)
    with ctx.repo.sessions() as db:
        assert list(db.scalars(select(TelemetryReading))) == []


def test_busy_supplement_retries_while_original_reader_keeps_running(isolated, monkeypatch):
    ctx = isolated
    original = ctx.repo.persist
    attempts = []

    def persist(cfg, owner, batch):
        if batch.get('storage_policy') == 'point-history-v1':
            attempts.append(batch['batch_id'])
            if len(attempts) == 1:
                error = sqlite3.OperationalError('database is locked')
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise OperationalError('synthetic busy', {}, error)
        return original(cfg, owner, batch)

    monkeypatch.setattr(ctx.repo, 'persist', persist)

    async def wait_for_retry(index):
        if index == 2:
            for _ in range(200):
                if len(attempts) >= 2: return
                await asyncio.sleep(.005)
            pytest.fail('supplement never retried')

    result, reads, _, _, spool = collect(ctx, monkeypatch, [0, 30, 35], on_read=wait_for_retry)
    assert result == 0 and len(reads) == 3 and len(attempts) == 2
    assert attempts[0] == attempts[1] and not spool.pending()
    with ctx.repo.sessions() as db:
        assert len(list(db.scalars(select(TelemetryReading)))) == 3


def test_failed_enqueue_retains_spool_and_restart_uses_fresh_observation(isolated, monkeypatch):
    ctx = isolated

    class FailingSpool(DurableSpool):
        def enqueue(self, batch):
            if batch.get('storage_policy') == 'point-history-v1':
                raise OSError('synthetic disk failure')
            return super().enqueue(batch)

    spool = FailingSpool(ctx.tmp_path/'spool', ctx.cfg.source_id)
    assert collect(ctx, monkeypatch, [0, 30, 35], spool=spool)[0] == 1
    ordinary = DurableSpool(ctx.tmp_path/'spool', ctx.cfg.source_id)
    result, _, envelopes, _, _ = collect(ctx, monkeypatch, [35, 60, 90], spool=ordinary)
    assert result == 0 and not ordinary.pending()
    with ctx.repo.sessions() as db:
        rows = list(db.scalars(select(TelemetryReading).order_by(TelemetryReading.received_at)))
        assert stored_offsets(ctx, rows, 'renamed_level') == [0, 35, 60, 90]
        assert stored_offsets(ctx, rows, 'coal') == [0, 35, 60]


def test_committed_supplement_survives_ack_failure_and_restart_idempotently(isolated, monkeypatch):
    ctx = isolated

    class FailingAck(DurableSpool):
        def ack(self, path):
            if self.read(path).get('storage_policy') == 'point-history-v1':
                raise OSError('synthetic acknowledgement failure')
            return super().ack(path)

    spool = FailingAck(ctx.tmp_path/'spool', ctx.cfg.source_id)
    assert collect(ctx, monkeypatch, [0, 30], spool=spool)[0] == 1
    assert len(spool.pending()) == 1
    saved = spool.read(spool.pending()[0])
    ordinary = DurableSpool(ctx.tmp_path/'spool', ctx.cfg.source_id)
    # A restart retains the existing first-full-read behavior. Its t=35 sample
    # is a new observation, while the original t=30 batch is replayed once.
    assert collect(ctx, monkeypatch, [35, 60, 90], spool=ordinary)[0] == 0
    assert not ordinary.pending()
    with ctx.repo.sessions() as db:
        rows = list(db.scalars(select(TelemetryReading)))
        assert len([row for row in rows if row.batch_id == saved['batch_id']]) == 1
        assert sorted(stored_offsets(ctx, rows, 'renamed_level')) == [0, 30, 35, 60, 90]
