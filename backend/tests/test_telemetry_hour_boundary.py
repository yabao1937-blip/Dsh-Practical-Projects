from copy import deepcopy
from datetime import timedelta
import pytest
from sqlalchemy import select, func

from test_telemetry_minute_alignment import setup, batch, minute
from app.telemetry.config import PointConfig
from app.telemetry.domain import utc_now, iso_time
from app.telemetry.models import TelemetryReading, TelemetryPoint
from app.telemetry.realtime import RuntimePublisher, select_hour_boundary_evidence


@pytest.fixture
def density_setup(setup):
    cfg, repo=setup
    cfg.points=[PointConfig(key=kind,name=kind,item_id='TIA_4_1.TIA_4_1.A304/'+kind,
        node_id='ns=2;s='+kind,unit='g/cm³') for kind in ('MD','MD_SET')]
    repo.acquire(cfg,'owner')
    return cfg,repo


def packet(density_setup,monkeypatch,age=5,bad=False):
    cfg,repo=density_setup
    boundary=utc_now().replace(minute=0,second=0,microsecond=0)
    now=boundary+timedelta(seconds=5)
    publisher=RuntimePublisher.for_repository(repo,cfg,'owner')
    originals=[]
    for when,status in [(boundary-timedelta(seconds=25),0),(boundary-timedelta(seconds=age),0x80000000 if bad else 0),(now,0)]:
        monkeypatch.setattr('app.telemetry.realtime.utc_now',lambda when=when:when)
        item=batch(cfg,1.48,when,status);originals.append(item);publisher.publish('connected',batch=item)
    entries=select_hour_boundary_evidence(publisher.document,cfg,'owner',now)
    current=minute(cfg,originals[-1],[]);current['hour_boundary_evidence']=entries
    return now,publisher,originals,current


def test_preserves_raw_tail_and_not_every_five_second_sample(density_setup,monkeypatch):
    cfg,repo=density_setup
    now,_,raw,current=packet(density_setup,monkeypatch)
    assert len(current['hour_boundary_evidence'])==2
    repo.persist(cfg,'owner',current);repo.persist(cfg,'owner',current)
    with repo.sessions() as db:
        assert db.scalar(select(func.count()).select_from(TelemetryReading))==4
        for entry in current['hour_boundary_evidence']:
            saved=db.scalar(select(TelemetryReading).where(TelemetryReading.batch_id==raw[1]['batch_id'],TelemetryReading.point_key==entry['point_key']))
            assert saved.sample==entry['sample']
            assert saved.sample['cadence_seconds']==5
            p=db.get(TelemetryPoint,(cfg.source_id,entry['point_key']))
            assert p.latest['received_at']==iso_time(now) and p.last_history_at==iso_time(now)


def test_latest_bad_quality_is_saved_without_returning_older_good(density_setup,monkeypatch):
    cfg,repo=density_setup
    _,_,raw,current=packet(density_setup,monkeypatch,bad=True)
    assert all(e['sample']['quality']=='bad' and e['batch_id']==raw[1]['batch_id'] for e in current['hour_boundary_evidence'])
    repo.persist(cfg,'owner',current)


@pytest.mark.parametrize('cause',['owner','source','definition','missing','retry','split_pair','cold','old'])
def test_no_synthetic_evidence_across_gaps(density_setup,monkeypatch,cause):
    cfg,repo=density_setup
    now,publisher,raw,current=packet(density_setup,monkeypatch)
    if cause=='owner':publisher.document['owner']='other'
    if cause=='source':publisher.document['source_id']='other'
    if cause=='definition':cfg.points[0].scale=2
    if cause=='missing':publisher.document['batches']=[raw[1],raw[2]];raw[1]['samples'].pop()
    if cause=='retry':publisher.publish('retrying','Timeout');publisher.publish('connected',batch=raw[2])
    if cause=='split_pair':publisher.document['batches']=[raw[1],raw[2]];raw[1]['samples'][0]['received_at']=iso_time(now-timedelta(seconds=11))
    if cause=='cold':publisher.document['batches']=[raw[2]]
    if cause=='old':now+=timedelta(minutes=1)
    assert select_hour_boundary_evidence(publisher.document,cfg,'owner',now)==[]


@pytest.mark.parametrize('age,accepted',[(30,True),(30.001,False),(0,False),(-1,False)])
def test_exact_window(density_setup,monkeypatch,age,accepted):
    cfg,_=density_setup
    now,publisher,raw,_=packet(density_setup,monkeypatch,age)
    publisher.document['batches']=raw[1:]
    assert bool(select_hour_boundary_evidence(publisher.document,cfg,'owner',now))==accepted


@pytest.mark.parametrize('cause',['parent_kind','source','time','batch','runtime','definition','pair','duplicate'])
def test_invalid_spool_boundary_evidence_rolls_back(density_setup,monkeypatch,cause):
    cfg,repo=density_setup
    _,_,_,current=packet(density_setup,monkeypatch)
    e=current['hour_boundary_evidence'][0]
    if cause=='parent_kind':current['storage_kind']='quality-change'
    if cause=='source':e['source_id']='other'
    if cause=='time':e['sample']['received_at']=current['samples'][0]['received_at']
    if cause=='batch':e['batch_id']='invalid'
    if cause=='runtime':e['sample']['runtime_only']=True
    if cause=='definition':e['sample']['definition_id']='other'
    if cause=='pair':current['hour_boundary_evidence'].pop()
    if cause=='duplicate':current['hour_boundary_evidence'].append(deepcopy(e))
    with pytest.raises(ValueError):repo.persist(cfg,'owner',current)
    with repo.sessions() as db:
        assert db.scalar(select(func.count()).select_from(TelemetryReading))==0


def test_old_spool_definition_replay_does_not_replace_new_snapshot(density_setup,monkeypatch):
    cfg,repo=density_setup
    now,_,_,old=packet(density_setup,monkeypatch)
    new=cfg.model_copy(deep=True)
    for point in new.points:
        point.item_id=point.item_id.replace('A304','A303')
        point.node_id=point.node_id.replace('ns=2','ns=3')
    repo.acquire(new,'owner')
    current=minute(new,batch(new,1.51,now+timedelta(minutes=1)),[])
    repo.persist(new,'owner',current)
    repo.persist(new,'owner',old)
    with repo.sessions() as db:
        assert db.scalar(select(func.count()).select_from(TelemetryReading))==6
        assert db.get(TelemetryPoint,(cfg.source_id,'MD')).latest['value']==1.51
