"""WAL concurrency: slow hourly calculations must not block five-second writes."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, Barrier

import pytest
from sqlalchemy import select, text

from app.decision_hourly import archive, service, worker
from app.decision_hourly.models import DecisionCycleContext, DecisionCycleSample, DecisionCycleHour, DecisionEvidenceArchive
from app.telemetry.domain import iso_time
from test_hourly_brief import sessions, HOUR

START = HOUR-timedelta(hours=1)


@pytest.fixture
def wal(sessions):
    with sessions() as db:
        db.execute(text('PRAGMA journal_mode=WAL'))
        db.execute(text('CREATE TABLE write_probe (value INTEGER)'))
        db.add(DecisionCycleContext(key='test',definition={}))
        db.commit()
    return sessions


def probe(sessions):
    with sessions() as db:
        db.execute(text('PRAGMA busy_timeout=200'))
        db.execute(text('BEGIN IMMEDIATE'))
        db.execute(text('INSERT INTO write_probe VALUES (1)'))
        db.commit()


def seed_archive(sessions):
    with sessions.begin() as db:
        db.add(DecisionCycleHour(start=iso_time(START),end=iso_time(HOUR),generated_at=iso_time(HOUR),snapshot={'frozen':1}))
        db.add(DecisionCycleSample(slot=int(START.timestamp())//5,sampled_at=iso_time(START),
            context_key='test',fingerprint='first',snapshot={'evidence':'original'}))


def test_slow_hour_summary_allows_collector_write(wal,monkeypatch):
    started,release=Event(),Event()
    def slow(db,*args):
        db.scalar(select(DecisionCycleContext))  # Pin the read snapshot.
        started.set()
        assert release.wait(5)
        return {'frozen':'original'}
    monkeypatch.setattr(service,'build_hour',slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(service.summary_tick,HOUR,wal)
        try:
            assert started.wait(5)
            probe(wal)  # Must commit while the summary is still paused.
        finally:
            release.set()
        future.result(timeout=5)
    with wal() as db:
        assert db.get(DecisionCycleHour,iso_time(START)).snapshot=={'frozen':'original'}


def test_concurrent_summaries_do_not_overwrite_frozen_result(wal,monkeypatch):
    barrier=Barrier(2)
    def prepare(db,*args):
        barrier.wait(timeout=5)
        return {'source':id(db)}
    monkeypatch.setattr(service,'build_hour',prepare)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _:service.summary_tick(HOUR,wal),range(2)))
    with wal() as db:
        saved=db.get(DecisionCycleHour,iso_time(START)).snapshot
        assert len(list(db.scalars(select(DecisionCycleHour))))==1
    monkeypatch.setattr(service,'build_hour',lambda *args:pytest.fail('Frozen hour recomputed'))
    service.summary_tick(HOUR,wal)
    with wal() as db:
        assert db.get(DecisionCycleHour,iso_time(START)).snapshot==saved


def test_archive_compression_does_not_lock_out_writes(wal,monkeypatch):
    seed_archive(wal)
    started,release=Event(),Event()
    compress=archive.zlib.compress
    def slow(raw):
        started.set()
        assert release.wait(5)
        return compress(raw)
    monkeypatch.setattr(archive.zlib,'compress',slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(archive.archive_tick,iso_time(HOUR),wal)
        try:
            assert started.wait(5)
            probe(wal)
        finally:
            release.set()
        future.result(timeout=5)
    with wal() as db:
        assert not list(db.scalars(select(DecisionCycleSample)))
        assert archive.read_sample(db,int(START.timestamp())//5)['snapshot']=={'evidence':'original'}


@pytest.mark.parametrize('change',['added','changed'])
def test_archive_rechecks_rows_before_deleting(wal,change):
    seed_archive(wal)
    with wal() as db:
        prepared=archive.prepare_archive(db,iso_time(HOUR))
    with wal.begin() as db:
        if change=='added':
            db.add(DecisionCycleSample(slot=int(START.timestamp())//5+1,
                sampled_at=iso_time(START+timedelta(seconds=5)),context_key='test',fingerprint='new',snapshot={}))
        else:
            db.get(DecisionCycleSample,int(START.timestamp())//5).fingerprint='changed'
    with wal.begin() as db:
        archive.commit_archive(db,prepared)
    with wal() as db:
        assert len(list(db.scalars(select(DecisionCycleSample))))==(2 if change=='added' else 1)
        assert db.get(DecisionEvidenceArchive,iso_time(START)) is None


def test_archive_repeat_is_idempotent_and_corruption_retains_rows(wal):
    seed_archive(wal)
    with wal() as db:
        prepared=archive.prepare_archive(db,iso_time(HOUR))
    with wal.begin() as db:
        archive.commit_archive(db,prepared)
    with wal.begin() as db:
        archive.commit_archive(db,prepared)
        db.add(DecisionCycleSample(slot=int(START.timestamp())//5,sampled_at=iso_time(START),
            context_key='test',fingerprint='first',snapshot={'evidence':'original'}))
        db.get(DecisionEvidenceArchive,iso_time(START)).payload=b'corrupt'
    with pytest.raises(Exception):
        archive.archive_tick(iso_time(HOUR),wal)
    with wal() as db:
        assert db.get(DecisionCycleSample,int(START.timestamp())//5) is not None


def test_worker_records_during_slow_maintenance_and_waits_on_shutdown(monkeypatch):
    started,release=Event(),Event()
    calls=[]
    def observe():
        calls.append('observation')
        return HOUR
    def maintain(now):
        started.set()
        assert release.wait(5)
        calls.append('maintenance finished')
    monkeypatch.setattr(worker,'observation_tick',observe)
    monkeypatch.setattr(worker,'maintenance_tick',maintain)
    monkeypatch.setattr(worker,'next_delay',lambda _:0.01)
    async def scenario():
        stop=asyncio.Event()
        task=asyncio.create_task(worker.run(stop))
        try:
            assert await asyncio.to_thread(started.wait,5)
            for _ in range(200):
                if calls.count('observation')>=3:
                    break
                await asyncio.sleep(.01)
            assert calls.count('observation')>=3
            stop.set()
            await asyncio.sleep(.03)
            assert not task.done()  # Finishes the active maintenance before exiting.
        finally:
            stop.set()
            release.set()
            await asyncio.wait_for(task,5)
        assert calls[-1]=='maintenance finished'
    asyncio.run(scenario())


def test_worker_requires_committed_observation_and_survives_maintenance_failure(monkeypatch):
    entered,release,maintained=Event(),Event(),Event()
    observations=[]
    maintenance=[]
    def observe():
        entered.set()
        assert release.wait(5)
        observations.append(HOUR)
        return HOUR
    def maintain(now):
        assert observations
        maintenance.append(now)
        if len(maintenance)==1:
            raise RuntimeError('isolated maintenance failure')
        maintained.set()
    monkeypatch.setattr(worker,'observation_tick',observe)
    monkeypatch.setattr(worker,'maintenance_tick',maintain)
    monkeypatch.setattr(worker,'next_delay',lambda _:0.01)
    async def scenario():
        stop=asyncio.Event();task=asyncio.create_task(worker.run(stop))
        try:
            assert await asyncio.to_thread(entered.wait,5)
            assert not maintenance
            release.set()
            assert await asyncio.to_thread(maintained.wait,5)
            assert len(observations)>=2
        finally:
            release.set();stop.set()
            await asyncio.wait_for(task,5)
    asyncio.run(scenario())
