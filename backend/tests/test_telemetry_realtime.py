"""Five-second live IPC and thirty-second durable sampling, with isolated data."""
import asyncio
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.hourly_brief.inputs import Inputs, LEVEL
from app.gpt_coarse.timing import aligned_level
from app.telemetry.config import CollectorConfig, PointConfig
from app.telemetry.domain import iso_time, normalize, utc_now
from app.telemetry.models import TelemetryPoint, TelemetryReading, TelemetrySource
from app.telemetry.repository import TelemetryRepository, latest_view
from app.telemetry.realtime import (RuntimePublisher, read_runtime, read_runtime_path,
    runtime_folder, MAX_BATCHES, MAX_BYTES)
from app.telemetry.spool import DurableSpool
from app.telemetry.worker import run_collector


@pytest.fixture
def setup(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path/'isolated.db'), connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine)
    cfg = CollectorConfig(enabled=True, endpoint='opc.tcp://127.0.0.1:1', security='None', allow_insecure=True,
        poll_seconds=5, history_interval_seconds=30,
        points=[PointConfig(key='level',name='Level',node_id='ns=2;s=Level',item_id=LEVEL,max_age_seconds=15)])
    repo = TelemetryRepository(sessionmaker(engine))
    repo.acquire(cfg,'owner')
    yield cfg,repo
    engine.dispose()


def batch(cfg, value, when, status=0):
    return dict(source_id=cfg.source_id,batch_id=uuid.uuid4().hex,
        samples=[normalize(cfg.points[0],value,status,when,when,when)])


def publish(monkeypatch, publisher, cfg, value, when, status=0):
    monkeypatch.setattr('app.telemetry.realtime.utc_now',lambda:when)
    data = batch(cfg,value,when,status)
    publisher.publish('connected',batch=data)
    return data


def test_live_five_second_value_does_not_create_history(setup,monkeypatch):
    cfg,repo=setup
    now=utc_now()
    repo.persist(cfg,'owner',batch(cfg,10,now-timedelta(seconds=30)))
    repo.state(cfg,'owner','connected')
    publisher=RuntimePublisher.for_repository(repo,cfg,'owner')
    for second in range(-25,1,5):
        publish(monkeypatch,publisher,cfg,20+second,now+timedelta(seconds=second))
    with repo.sessions() as db:
        view=latest_view(db,now=now)
        assert view['summary']['available']==1
        assert view['items'][0]['sample']['value']==20
        assert view['items'][0]['sample']['runtime_only'] is True
        assert db.scalar(select(func.count()).select_from(TelemetryReading))==1
        historical=Inputs(db,now,live=True,authoritative=True)
        live=Inputs(db,now,live=True,authoritative=True,realtime=True)
        assert historical.telemetry(LEVEL)['value'] is None  # 30-second-old SQL is stale.
        assert live.telemetry(LEVEL)['value']==20


@pytest.mark.parametrize('change',['owner','lease','definition','stale','state'])
def test_runtime_cannot_override_owner_lease_definition_or_freshness(setup,monkeypatch,change):
    cfg,repo=setup
    now=utc_now()
    repo.persist(cfg,'owner',batch(cfg,10,now-timedelta(seconds=30)))
    repo.state(cfg,'owner','connected')
    publisher=RuntimePublisher.for_repository(repo,cfg,'owner')
    publish(monkeypatch,publisher,cfg,20,now)
    with repo.sessions() as db:
        source=db.get(TelemetrySource,cfg.source_id)
        if change=='owner': source.owner='replacement'
        if change=='lease': source.lease_until=iso_time(now-timedelta(seconds=1))
        if change=='definition':
            p=db.get(TelemetryPoint,(cfg.source_id,'level'))
            p.config=dict(p.config,scale=2)
        if change=='state': publisher.publish('retrying','BadTimeout')
        db.commit()
        checked=now+timedelta(seconds=16) if change=='stale' else now
        view=latest_view(db,now=checked)
        assert view['summary']['available']==0


def test_lag_ring_is_live_only_and_applies_ninety_seconds_once(setup,monkeypatch):
    cfg,repo=setup
    now=utc_now()
    repo.persist(cfg,'owner',batch(cfg,11,now-timedelta(seconds=100)))
    repo.state(cfg,'owner','connected')
    publisher=RuntimePublisher.for_repository(repo,cfg,'owner')
    publish(monkeypatch,publisher,cfg,22,now-timedelta(seconds=90))
    publish(monkeypatch,publisher,cfg,99,now)
    with repo.sessions() as db:
        durable=Inputs(db,now,authoritative=True)
        live=Inputs(db,now,authoritative=True,realtime=True)
        current=dict(source_id=cfg.source_id,point_key='level',unit='%')
        old=aligned_level(durable,current)
        realtime=aligned_level(live,current)
        assert old['value']==11
        assert realtime['value']==22
        assert realtime['observed_at']==iso_time(now-timedelta(seconds=90))
        assert realtime['sample']['runtime_only'] is True
        assert realtime['alignment']['offset_seconds']==pytest.approx(0,abs=.001)


def test_runtime_hour_boundary_never_reads_a_future_batch(setup,monkeypatch):
    cfg,repo=setup
    boundary=utc_now().replace(minute=0,second=0,microsecond=0)
    publisher=RuntimePublisher.for_repository(repo,cfg,'owner')
    publish(monkeypatch,publisher,cfg,17,boundary-timedelta(seconds=3))
    publish(monkeypatch,publisher,cfg,29,boundary+timedelta(seconds=2))
    with repo.sessions() as db:
        inputs=Inputs(db,boundary,authoritative=True,realtime=True)
        assert inputs.telemetry(LEVEL)['value']==17
        assert inputs.telemetry(LEVEL)['observed_at']==iso_time(boundary-timedelta(seconds=3))


def test_runtime_is_bounded_and_database_path_isolated(setup,monkeypatch,tmp_path):
    cfg,repo=setup
    now=utc_now()
    publisher=RuntimePublisher.for_repository(repo,cfg,'owner')
    for index in range(150):
        publish(monkeypatch,publisher,cfg,index,now-timedelta(seconds=(149-index)*.1))
    path=runtime_folder(repo.sessions.kw['bind'])/(cfg.source_id+'.json')
    data=json.loads(path.read_text(encoding='utf-8'))
    assert len(data['batches'])<=MAX_BATCHES
    assert path.stat().st_size<=MAX_BYTES
    assert read_runtime_path(tmp_path/'another.db',now)=={}
    assert read_runtime_path(tmp_path/'isolated.db',now)[cfg.source_id]['batches'][-1]['samples'][0]['value']==149
    path.write_text('[]',encoding='utf-8')
    with repo.sessions() as db:
        assert read_runtime(db,now)=={}


def test_authenticated_remote_documents_are_still_fenced_by_sql_owner(setup,monkeypatch):
    cfg,repo=setup
    now=utc_now()
    publisher=RuntimePublisher.for_repository(repo,cfg,'owner')
    publish(monkeypatch,publisher,cfg,20,now)
    documents=read_runtime_path(repo.sessions.kw['bind'].url.database,now)
    with repo.sessions() as db:
        db.info['telemetry_runtime']=documents
        assert read_runtime(db,now)
        documents[cfg.source_id]['owner']='other'
        assert read_runtime(db,now)=={}


def test_inputs_check_remote_cache_on_prediction_clock_not_laptop_clock(setup,monkeypatch):
    cfg,repo=setup
    host_now=utc_now()
    repo.state(cfg,'owner','connected')
    publisher=RuntimePublisher.for_repository(repo,cfg,'owner')
    publish(monkeypatch,publisher,cfg,42,host_now)
    documents=read_runtime_path(repo.sessions.kw['bind'].url.database,host_now)
    monkeypatch.setattr('app.telemetry.realtime.utc_now',lambda:host_now+timedelta(hours=1))
    with repo.sessions() as db:
        db.info['telemetry_runtime']=documents
        live=Inputs(db,host_now,live=True,authoritative=True,realtime=True)
        assert live.telemetry(LEVEL)['value']==42


def test_collector_saves_thirty_second_buckets_but_reads_every_five(setup,tmp_path):
    cfg,repo=setup
    repo.state(cfg,'owner','stopped',release=True)
    cfg.poll_seconds=.1  # Fast test clock; observation timestamps still advance five seconds.
    stop=asyncio.Event()
    base=utc_now().replace(second=0,microsecond=0)-timedelta(minutes=1)
    reads=[]
    class Reader:
        async def __aenter__(self): return self
        async def __aexit__(self,*_): pass
        async def read(self):
            when=base+timedelta(seconds=5*len(reads))
            reads.append(when)
            if len(reads)==13: stop.set()
            return batch(cfg,len(reads),when)['samples']
    assert asyncio.run(run_collector(cfg,repo,DurableSpool(tmp_path/'spool',cfg.source_id),
        stop=stop,reader_factory=lambda _:Reader()))==0
    with repo.sessions() as db:
        stored=list(db.scalars(select(TelemetryReading).order_by(TelemetryReading.received_at)))
        assert len(reads)==13
        assert len(stored)==3
        assert [r.sample['value'] for r in stored]==[1,7,13]
        assert all(r.sample['cadence_seconds']==30 for r in stored)


def test_quality_changes_are_saved_as_short_events_not_thirty_second_coverage(setup,tmp_path):
    cfg,repo=setup
    repo.state(cfg,'owner','stopped',release=True)
    cfg.poll_seconds=.1
    stop=asyncio.Event()
    base=utc_now().replace(second=0,microsecond=0)-timedelta(minutes=1)
    reads=[]
    class Reader:
        async def __aenter__(self): return self
        async def __aexit__(self,*_): pass
        async def read(self):
            index=len(reads)
            reads.append(index)
            if len(reads)==4: stop.set()
            return batch(cfg,index,base+timedelta(seconds=index*5),0x80000000 if index==1 else 0)['samples']
    assert asyncio.run(run_collector(cfg,repo,DurableSpool(tmp_path/'spool',cfg.source_id),
        stop=stop,reader_factory=lambda _:Reader()))==0
    with repo.sessions() as db:
        stored=list(db.scalars(select(TelemetryReading).order_by(TelemetryReading.received_at)))
        assert [r.sample['storage_kind'] for r in stored]==['periodic','quality-event','quality-event']
        assert [r.sample['cadence_seconds'] for r in stored]==[30,.1,.1]
