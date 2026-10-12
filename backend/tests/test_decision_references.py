"""Authority, reference resolution and retention across process/archive boundaries."""
from copy import deepcopy
from datetime import timedelta
import json
import zlib

import pytest
from sqlalchemy import delete, select, func

from app.decision_hourly.archive import archive_old_hour, read_sample, archived_rows
from app.decision_hourly.models import DecisionCycleSample, DecisionCycleHour, DecisionEvidenceArchive, DecisionTelemetryPin
from app.decision_hourly.references import pack_snapshot, resolve_snapshots, walk, VERSION
from app.decision_hourly.service import record, compact_cell, build_hour
from app.services.decision_sources import shared_snapshot, identity
from app.telemetry.domain import iso_time
from app.telemetry.models import TelemetryReading, TelemetryPoint
from app.models import DecisionSourceRevision
from test_hourly_brief import sessions, HOUR, send
from test_decision_sources import seeded


def test_roundtrip_keeps_outputs_and_types_without_raw_copies(sessions,seeded):
    with sessions.begin() as db:
        original=shared_snapshot(db)
        record(db,HOUR,original)
        row=db.scalar(select(DecisionCycleSample))
        assert row.snapshot['storage_version']==VERSION
        assert '$reading' in row.snapshot['cells']['density']
        assert 'value' not in row.snapshot['cells']['density']['overlay']
        assert '$manual' in row.snapshot['cells']['heavy_ash']
        assert 'level_observation' not in row.snapshot
        slot=row.slot
        stored=deepcopy(row.snapshot)
    # A fresh session must reconstruct exclusively from durable references.
    with sessions() as db:
        view=read_sample(db,slot)['snapshot']
        for key,c in original['cells'].items():
            assert view['cells'][key]==compact_cell(c)
        for key,c in original['inputs'].items():
            assert view['inputs'][key]==compact_cell(c)
        assert type(view['inputs']['sys401']['value']) is int
        assert type(view['cells']['systems']['components']['401']['value']) is bool
        assert view['total_ash']==original['total_ash']
        assert view['suggested_density']==original['suggested_density']
        assert db.get(DecisionCycleSample,slot).snapshot==stored


def test_pruning_preserves_references_even_after_compressed_archive(sessions,seeded,monkeypatch):
    cfg,repo,_=seeded
    with sessions.begin() as db:
        record(db,HOUR)
        row=db.scalar(select(DecisionCycleSample))
        slot=row.slot
        expected=read_sample(db,slot)
        db.add(DecisionCycleHour(start=iso_time(HOUR),end=iso_time(HOUR+timedelta(hours=1)),
            generated_at=iso_time(HOUR+timedelta(hours=1)),snapshot={'frozen':True}))
        db.flush()
        archive_old_hour(db,iso_time(HOUR+timedelta(days=8)))
        assert db.get(DecisionCycleSample,slot) is None
        archive=db.get(DecisionEvidenceArchive,iso_time(HOUR))
        compressed=json.loads(zlib.decompress(archive.payload))
        assert '$reading' in compressed[0]['snapshot']['cells']['density']
        unreferenced=db.scalar(select(TelemetryReading.id).where(~TelemetryReading.id.in_(select(DecisionTelemetryPin.reading_id))))
        assert unreferenced is not None
    monkeypatch.setattr('app.telemetry.repository.utc_now',lambda:HOUR+timedelta(days=30))
    repo.prune(cfg)
    with sessions() as db:
        assert db.get(TelemetryReading,unreferenced) is None
        assert db.scalar(select(func.count()).select_from(DecisionTelemetryPin))>0
        actual=read_sample(db,slot)
        assert actual['snapshot']==expected['snapshot']
        assert archived_rows(db,iso_time(HOUR))[0]['snapshot']==expected['snapshot']


def test_missing_reference_fails_explicitly_without_falling_back(sessions,seeded):
    with sessions.begin() as db:
        record(db,HOUR)
        row=db.scalar(select(DecisionCycleSample))
        slot=row.slot
        reading_id=row.snapshot['cells']['level']['$reading']
        # Simulate corruption outside the application's retention path.
        db.execute(delete(TelemetryReading).where(TelemetryReading.id==reading_id))
    with sessions() as db:
        with pytest.raises(ValueError,match='原始记录缺失'):
            read_sample(db,slot)


def test_zero_and_density_hold_keep_current_and_retained_references(sessions,seeded):
    cfg,repo,values=seeded
    changed=list(values)
    changed[0]=0
    changed[4]=1.0
    send(repo,cfg,HOUR-timedelta(seconds=1),changed)
    with sessions.begin() as db:
        record(db,HOUR)
        row=db.scalar(select(DecisionCycleSample))
        encoded=row.snapshot
        raw=encoded['cells']['raw_ash']
        assert raw['$reading']!=raw['overlay']['retained_sample']['$reading']
        density=encoded['cells']['density']
        assert density['$reading']!=density['overlay']['raw_sample']['$reading']
        view=read_sample(db,row.slot)['snapshot']['cells']
        assert view['raw_ash']['value']==40 and view['raw_ash']['status']=='held'
        assert view['raw_ash']['raw_value']==0
        assert view['density']['value']==1.48 and view['density']['raw_sample']['value']==1.0


def test_average_density_references_both_channels_and_freezes_units(sessions,seeded):
    cfg,_,_=seeded
    with sessions.begin() as db:
        db.add(DecisionSourceRevision(effective_at=iso_time(HOUR-timedelta(seconds=1)),
            config=dict(choices={'density-current':identity(cfg.source_id,'a303_a304_density_average')},target_min=8.5,target_max=9.0)))
    with sessions.begin() as db:
        record(db,HOUR)
        row=db.scalar(select(DecisionCycleSample))
        slot=row.slot
        parts=row.snapshot['cells']['density']['components']
        assert len(parts)==2 and all('$reading' in p for p in parts)
        expected=read_sample(db,slot)['snapshot']
        point=db.get(TelemetryPoint,(cfg.source_id,'point3'))
        point.config=dict(point.config,unit='changed',scale=100)
    with sessions() as db:
        assert read_sample(db,slot)['snapshot']==expected


def test_legacy_inline_stays_readable_and_does_not_get_rewritten(sessions):
    old={'cells':{'density':{'value':1.48,'status':'good'}},'total_ash':{'value':8.8}}
    with sessions() as db:
        assert resolve_snapshots(db,[old])==[old]
        assert 'storage_version' not in old


def test_unpersisted_latest_cache_is_not_authoritative(sessions,seeded):
    cfg,_,_=seeded
    with sessions.begin() as db:
        point=db.get(TelemetryPoint,(cfg.source_id,'point0'))
        point.latest=dict(point.latest,value=99,received_at=iso_time(HOUR-timedelta(seconds=1)))
    with sessions() as db:
        assert shared_snapshot(db)['cells']['raw_ash']['value']==40
        assert build_hour(db,HOUR-timedelta(hours=1),HOUR,HOUR)['cells']['raw_ash']['value']==40


def test_sparse_history_is_not_manufactured_during_reference_packing(sessions,seeded):
    with sessions() as db:
        c=compact_cell(shared_snapshot(db)['cells']['level'])
        c['batch_id']='not-persisted'
        with pytest.raises(ValueError,match='采集历史引用缺失'):
            pack_snapshot(db,{'cells':{'level':c}},123)
