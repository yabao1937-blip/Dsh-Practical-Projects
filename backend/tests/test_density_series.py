from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json

import pytest
from fastapi import HTTPException

from app.decision_hourly import density_series as series
from app.decision_hourly import density_policy_view as presentation
from app.telemetry.domain import iso_time
from test_density_candidate import evidence
from test_density_candidate_api import client,sessions
from test_hourly_quality_advice import HOUR


def sample_and_point():
    e=evidence()
    sample=dict(slot=int(HOUR.timestamp())//5,sampled_at=iso_time(HOUR),context_key='frozen',
        cadence_seconds=60,storage_version='authoritative-readings-v1',
        provenance=dict(reading_ids=[1,2,3],manual_ids=[],future_input_times=[]),
        snapshot=dict(density_channels=dict(A304=dict(setpoint=e['current_setpoint'],actual=e['actual_density'])),
            inputs=dict(is_stoppage=dict(value=0,status='good',observed_at=iso_time(HOUR))),target=e['reference']['target']))
    point=deepcopy(e['reference'])
    point.pop('identity');point.pop('availability')
    point.update(at=point.pop('generated_at'),apply_start=iso_time(HOUR))
    point['basis']['captured_at']=iso_time(HOUR)
    return sample,point


def test_real_sample_replay_does_not_use_advice_generated_three_seconds_later():
    s,p=sample_and_point()
    p['at']=iso_time(HOUR+timedelta(seconds=3))
    r=series.sample_candidate(s,[p])
    assert r['candidate']['value'] is None
    assert r['candidate']['reason_codes']==['reference_provenance']
    assert r['source']['reading_ids']==[1,2,3]


def test_target_edit_reuses_only_known_ash_and_unchanged_frozen_basis():
    s,p=sample_and_point()
    s['snapshot']['target']=dict(min=9,max=9.5)
    r=series.sample_candidate(s,[p])
    assert r['reference']['target_recomputed']
    assert r['reference']['value']==1.48
    assert r['basis']['ash']==p['ash']
    assert r['candidate']['value']==1.48 and r['candidate']['status']=='hold'


def test_future_basis_or_received_input_or_incompatible_context_is_not_reused():
    for issue in ('basis','received','context'):
        s,p=sample_and_point()
        if issue=='basis':p['basis']['captured_at']=iso_time(HOUR+timedelta(seconds=1))
        if issue=='received':s['provenance']['future_input_times']=[dict(field='received_at',at=iso_time(HOUR+timedelta(seconds=1)))]
        r=series.sample_candidate(s,[p],context_ok=issue!='context',context_reason='frozen mismatch' if issue=='context' else None)
        assert r['candidate']['value'] is None
        assert r['reference']['status']=='blocked'


def test_gap_markers_use_full_real_cadence_and_keep_both_edges():
    points=[]
    for i in range(200):
        when=HOUR+timedelta(seconds=60*i+(300 if i>=100 else 0))
        points.append(dict(at=iso_time(when),value=1.48,status='hold',reason_codes=['target_band'],
            quality=dict(level='provisional'),source=dict(cadence_seconds=60),input_evidence={}))
    marked=series.with_gap_markers(points)
    assert sum(p['break_before'] for p in marked)==1 and marked[100]['sample_gap_seconds']==360
    selected=series.plot_indices(marked,maximum=24)
    assert 99 in selected and 100 in selected
    assert not any(p.get('break_before') for p in points)


def test_actual_display_does_not_draw_good_source_placeholder_as_valid():
    s,p=sample_and_point()
    s['snapshot']['density_channels']['A304']['actual']['value']=1.0
    r=series.compact_result(series.sample_candidate(s,[p]))
    plotted=series.channel_points([r],'actual')[0]
    assert plotted['value'] is None and plotted['raw_value']==1.0
    assert plotted['source_status']=='good' and plotted['status']=='invalid'


@pytest.mark.parametrize('status,value',[('adjust',1.47),('wait',1.48),('unavailable',None)])
def test_test_default_views_replace_only_density_and_always_mark_new_policy(client,monkeypatch,status,value):
    monkeypatch.setenv('DMCS_DENSITY_EXPERIMENT','1')
    raw=dict(coarse_ash=dict(value=12.3),total_ash=dict(value=8.8),target=dict(min=8.5,max=9),
        suggested_density=dict(value=1.599),cells=dict(other=dict(value=5),suggested_density=dict(value=1.599)))
    from app.gpt_density.candidate import evaluate_candidate
    candidate=evaluate_candidate(evidence())
    candidate['candidate'].update(status=status,value=value,delta=None if value is None else round(value-1.48,6))
    monkeypatch.setattr(presentation,'load_views',lambda db:(deepcopy(raw),deepcopy(raw),HOUR))
    monkeypatch.setattr(presentation,'current',lambda *a,**k:deepcopy(candidate))
    for path in ('/api/v1/decision/current','/api/v1/decision/hourly'):
        response=client.get(path)
        assert response.status_code==200,response.text
        r=response.json()
        assert r['density_policy']=='density-candidate-v1'
        assert r['suggested_density']['value']==value
        assert r['suggested_density']['status']==status
        assert r['cells']['suggested_density']==r['suggested_density']
        assert r['coarse_ash']==raw['coarse_ash'] and r['total_ash']==raw['total_ash']
        assert raw['suggested_density']['value']==1.599


def test_pending_error_marker_never_returns_legacy_density(client,monkeypatch):
    monkeypatch.setenv('DMCS_DENSITY_EXPERIMENT','1')
    def unavailable(db):raise HTTPException(503,'busy')
    monkeypatch.setattr(presentation,'load_views',unavailable)
    for path in ('/api/v1/decision/current','/api/v1/decision/hourly'):
        response=client.get(path)
        assert response.status_code==503
        body=response.json()['detail']
        assert body['density_policy']=='density-candidate-v1'
        assert body['candidate']['value'] is None


def test_density_history_failure_keeps_other_predictions_without_old_fallback(client,monkeypatch):
    monkeypatch.setenv('DMCS_DENSITY_EXPERIMENT','1')
    raw=dict(coarse_ash=dict(value=12.3),suggested_density=dict(value=1.599))
    monkeypatch.setattr(presentation,'load_views',lambda db:(raw,raw,HOUR))
    def unavailable(*a,**k):raise HTTPException(503,'history query failed')
    monkeypatch.setattr(presentation,'current',unavailable)
    body=client.get('/api/v1/decision/current').json()
    assert body['density_policy']=='density-candidate-v1'
    assert body['suggested_density']['value'] is None
    assert body['coarse_ash']==raw['coarse_ash']


def test_series_get_reads_only_verified_files_and_end_is_exclusive(client,tmp_path,monkeypatch):
    monkeypatch.setenv('DMCS_DENSITY_EXPERIMENT','1')
    monkeypatch.setattr('app.config.BASE_DIR',tmp_path/'backend')
    root=tmp_path/'output/density-optimization-db7-20261104';(root/'raw').mkdir(parents=True)
    s,p=sample_and_point()
    rows=[series.compact_result(series.sample_candidate(s,[p]))]
    rows[0]['at']=iso_time(HOUR-timedelta(minutes=1))
    raw=json.dumps(rows).encode();(root/'raw/recalculated-one.json').write_bytes(raw)
    payload=dict(density_policy='density-candidate-v1',policy='density-candidate-v1',experimental=True,
        period=dict(start=iso_time(HOUR-timedelta(days=7)),end=iso_time(HOUR)),ash=[],source={},suggestions=[])
    encoded=json.dumps(payload).encode();(root/'series.json').write_bytes(encoded)
    manifest=dict(series=dict(sha256=sha256(encoded).hexdigest()),
        artifacts=[dict(path='recalculated-one.json',sha256=sha256(raw).hexdigest())])
    (root/'manifest.json').write_text(json.dumps(manifest))
    response=client.get('/api/v1/decision/evidence/density-series')
    assert response.status_code==200,response.text
    result=response.json()
    assert result['summary']['total_samples']==1
    assert result['period']['end_exclusive'] is True
    assert result['suggestions'][0]['at']<result['period']['end']
    assert client.get('/api/v1/decision/evidence/density-series',params={'limit':720}).status_code==422


def _write_snapshot(root,suffix,period_end):
    """Minimal checksum-valid snapshot dir; returns its series payload period end."""
    directory=root/f'output/density-optimization-db7-{suffix}'
    (directory/'raw').mkdir(parents=True,exist_ok=True)
    payload=dict(density_policy='density-candidate-v1',policy='density-candidate-v1',experimental=True,
        period=dict(start=iso_time(period_end-timedelta(days=7)),end=iso_time(period_end)),ash=[],source={},suggestions=[])
    encoded=json.dumps(payload).encode();(directory/'series.json').write_bytes(encoded)
    (directory/'manifest.json').write_text(json.dumps(dict(
        series=dict(sha256=sha256(encoded).hexdigest()),artifacts=[])))
    return directory


def test_series_serves_newest_snapshot_and_names_it(client,tmp_path,monkeypatch):
    """2026-11-06：read_frozen 取最新 dated 快照目录，并在 source.snapshot 标明来源。"""
    monkeypatch.setenv('DMCS_DENSITY_EXPERIMENT','1')
    monkeypatch.setattr('app.config.BASE_DIR',tmp_path/'backend')
    older_end=HOUR-timedelta(days=2); newer_end=HOUR
    _write_snapshot(tmp_path,'20261104',older_end)
    _write_snapshot(tmp_path,'20261106',newer_end)
    result=client.get('/api/v1/decision/evidence/density-series').json()
    assert result['period']['end']==iso_time(newer_end)          # 新窗口生效
    assert result['source']['snapshot']=='density-optimization-db7-20261106'


def test_series_falls_back_when_newest_snapshot_is_corrupt(client,tmp_path,monkeypatch):
    """最新快照校验失败时回退到旧快照，而不是 503。"""
    monkeypatch.setenv('DMCS_DENSITY_EXPERIMENT','1')
    monkeypatch.setattr('app.config.BASE_DIR',tmp_path/'backend')
    older_end=HOUR-timedelta(days=2); newer_end=HOUR
    _write_snapshot(tmp_path,'20261104',older_end)
    broken=_write_snapshot(tmp_path,'20261106',newer_end)
    (broken/'series.json').write_bytes(b'{"tampered":true}')      # 与 manifest 校验和不符
    result=client.get('/api/v1/decision/evidence/density-series').json()
    assert result['period']['end']==iso_time(older_end)
    assert result['source']['snapshot']=='density-optimization-db7-20261104'
