"""Shared server selections, time boundaries and real input/calculation integration."""
from copy import deepcopy
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import get_db
from app.models import AutoState, Setting, DecisionSourceRevision, CoarseModel
from app.routers.decision import router
from app.services.decision_sources import shared_snapshot, identity, config_view
from app.hourly_brief.inputs import LEVEL, LOCAL
from app.hourly_brief.service import build_snapshot, tick
from app.hourly_brief.models import HourlyBriefRow
from app.telemetry.derived import FOUR_SYSTEM_MEMBERS, DESLIMING_MEMBERS, DENSITY_AVERAGE_MEMBERS
from app.telemetry.domain import iso_time
from app.telemetry.repository import TelemetryRepository
from test_hourly_brief import sessions, config, send, setup_clock, HOUR, RAW_ASH, COAL_AMOUNT, add_manual


@pytest.fixture
def seeded(sessions, monkeypatch):
    setup_clock(monkeypatch, HOUR)
    monkeypatch.setattr('app.services.decision_sources.utc_now', lambda: HOUR)
    monkeypatch.setattr('app.routers.decision.utc_now', lambda: HOUR)
    states = [item for _, item in (*FOUR_SYSTEM_MEMBERS, *DESLIMING_MEMBERS)]
    items = [RAW_ASH, COAL_AMOUNT, LEVEL, *(item for _, item in DENSITY_AVERAGE_MEMBERS),
             'TIA_3_4.TIA_3_4.501/COAL_CAP', 'TIA_3_4.TIA_3_4.502/COAL_CAP', *states]
    cfg = config(items)
    for p in cfg.points:
        if p.item_id in states:
            p.value_type = 'boolean'
    repo = TelemetryRepository(sessions)
    repo.acquire(cfg, 'owner')
    values = [40, 600, 50, 1.42, 1.48, 260, 240, True, True, True, False, True, False]
    send(repo, cfg, HOUR-timedelta(seconds=92), values)
    send(repo, cfg, HOUR-timedelta(seconds=2), values)
    before = (HOUR-timedelta(minutes=1)).astimezone(LOCAL).replace(tzinfo=None)
    with sessions.begin() as db:
        add_manual(db, 8.5, HOUR-timedelta(minutes=3), HOUR-timedelta(minutes=2), 'heavy_clean_coal_ash')
        add_manual(db, 20, HOUR-timedelta(minutes=3), HOUR-timedelta(minutes=2))
        db.add(AutoState(key='floatAshInput', value={'manual':9,'manualAt':(HOUR-timedelta(minutes=2)).timestamp()*1000}, updated_at=before))
        db.add(AutoState(key='amountInputs', value={'floatAmount':{'manual':40,'manualAt':(HOUR-timedelta(minutes=2)).timestamp()*1000}}, updated_at=before))
        model = dict(intercept=3, coefs=[.1,.002,0,0,0,0,0,0,0,.02], metrics={'version':2})
        db.add(AutoState(key='coarseModelVariants', value={'gpt':dict(mlr=model, production='mlr')}, updated_at=before))
    return cfg, repo, values


@pytest.fixture
def client(sessions):
    def database():
        with sessions() as db:
            yield db
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        yield client


def test_live_and_hour_use_identical_gpt_inputs_and_results(sessions, seeded, client):
    with sessions() as db:
        before = list(db.connection().connection.driver_connection.iterdump())
        live = shared_snapshot(db)
        hourly = build_snapshot(db, HOUR)
        assert live['density_label'] == hourly['density_label'] == 'A304'
        assert live['cells']['density']['value'] == 1.48  # Not the former 1.45 mean.
        assert live['coarse_ash']['value'] == pytest.approx(9.2)
        assert live['cells']['coarse_amount']['value'] == 15
        assert live['cells']['coarse_amount']['status'] == 'calculated'
        assert live['heavy_amount']['value'] == 445
        assert live['total_ash']['value'] == pytest.approx(8.561)
        assert live['suggested_density']['value'] == 1.48
        for key, c in hourly['cells'].items():
            assert c == live['cells'][key]
        assert list(db.connection().connection.driver_connection.iterdump()) == before
    response = client.get('/api/v1/decision/current')
    assert response.status_code == 200, response.text
    assert response.json()['cells'] == live['cells']


def test_revision_conflict_noop_and_effective_time(sessions, seeded, client, monkeypatch):
    cfg, _, _ = seeded
    choose = identity(cfg.source_id, 'point3')  # A303
    initial = client.get('/api/v1/decision/sources').json()
    assert initial['revision'] == 0 and initial['density_label'] == 'A304'
    body = {'revision':0, 'choices':{'density-current':choose}}
    changed = client.patch('/api/v1/decision/sources', json=body)
    assert changed.status_code == 200, changed.text
    revision = changed.json()['revision']
    assert changed.json()['density_label'] == 'A303'
    assert client.patch('/api/v1/decision/sources', json=body).status_code == 409
    assert client.patch('/api/v1/decision/sources', json={**body, 'revision':revision}).json()['revision'] == revision
    assert client.get('/api/v1/decision/current').json()['cells']['density']['value'] == 1.42
    later = HOUR+timedelta(seconds=1)
    monkeypatch.setattr('app.routers.decision.utc_now', lambda: later)
    monkeypatch.setattr('app.services.decision_sources.utc_now', lambda: later)
    response = client.patch('/api/v1/decision/sources', json={'revision':revision,
        'choices':{'density-current':identity(cfg.source_id,'point4')}})
    assert response.json()['density_label'] == 'A304'
    with sessions() as db:
        assert build_snapshot(db,HOUR)['cells']['density']['value'] == 1.42
        assert shared_snapshot(db)['cells']['density']['value'] == 1.48
        assert len(list(db.scalars(select(DecisionSourceRevision)))) == 2


@pytest.mark.parametrize('node,source,key', [
    ('system-switches','kepserver-main','point0'), ('desliming','kepserver-main','four_system_status'),
    ('raw-ash','kepserver-main','four_system_status'), ('density-current','kepserver-main','point0'),
    ('density-current','absent','a303_a304_density_average'), ('heavy-ash','manual-measurements','absent'),
    ('heavy-ash','manual-measurements','coarse_slime_ash'),
    ('float-ash','manual-measurements','coarse_slime_ash'),
    ('tail-level','manual-measurements','coarse_slime_ash'),
])
def test_invalid_source_type_is_rejected_without_saving(sessions, seeded, client, node, source, key):
    r = client.patch('/api/v1/decision/sources', json={'revision':0,'choices':{node:identity(source,key)}})
    assert r.status_code == 422, r.text
    with sessions() as db:
        assert db.scalar(select(DecisionSourceRevision)) is None


def test_density_hold_and_offline_do_not_issue_fresh_advice(sessions, seeded):
    cfg, repo, values = seeded
    values[4] = 1
    send(repo,cfg,HOUR-timedelta(seconds=1),values)
    with sessions() as db:
        live = shared_snapshot(db)
        assert live['cells']['density']['value'] == 1.48
        assert live['cells']['density']['raw_value'] == 1
        assert live['cells']['density']['status'] == 'held'
        assert live['suggested_density']['value'] is None
    repo.state(cfg,'owner','stopped')
    with sessions() as db:
        assert shared_snapshot(db)['cells']['raw_ash']['value'] is None
        assert build_snapshot(db,HOUR)['cells']['raw_ash']['value'] == 40


@pytest.mark.parametrize('value', [0, 99])
def test_manual_coarse_assay_cannot_replace_model_or_change_decisions(sessions, seeded, value):
    from app.hourly_brief.inputs import Inputs
    from app.services.decision_sources import selected_cell
    with sessions() as db:
        before = shared_snapshot(db)
        hour_before = build_snapshot(db, HOUR)
    with sessions.begin() as db:
        add_manual(db, value, HOUR-timedelta(seconds=3), HOUR-timedelta(seconds=2), 'coarse_slime_ash')
    with sessions() as db:
        assert shared_snapshot(db) == before
        assert build_snapshot(db, HOUR) == hour_before
        attempted = selected_cell(Inputs(db,HOUR), 'heavy-ash', identity('manual-measurements','coarse_slime_ash'))
        assert attempted['value'] is None and attempted['status'] == 'missing'
        assert '仅作记录' in attempted['reason']


def test_saved_hour_unchanged_after_binding_and_manual_change(sessions, seeded, client, monkeypatch):
    tick(HOUR-timedelta(minutes=1),sessions)
    tick(HOUR,sessions)
    with sessions() as db:
        saved = deepcopy(db.get(HourlyBriefRow,iso_time(HOUR)).snapshot)
    later = HOUR+timedelta(seconds=2)
    monkeypatch.setattr('app.routers.decision.utc_now',lambda:later)
    monkeypatch.setattr('app.services.decision_sources.utc_now',lambda:later)
    client.patch('/api/v1/decision/sources',json={'revision':0,'choices':{'density-current':identity(seeded[0].source_id,'point3')}})
    with sessions.begin() as db:
        add_manual(db, 99, HOUR-timedelta(seconds=1), later, 'heavy_clean_coal_ash')
    tick(later,sessions)
    with sessions() as db:
        assert db.get(HourlyBriefRow,iso_time(HOUR)).snapshot == saved
        assert build_snapshot(db,HOUR)['cells']['heavy_ash']['value'] == 8.5
        assert shared_snapshot(db)['cells']['heavy_ash']['value'] == 99


def test_newer_protection_settings_cannot_unlock_historical_advice(sessions, seeded):
    with sessions.begin() as db:
        db.add(Setting(key='density_guide', value={'maxStep':.02},
                       updated_at=(HOUR+timedelta(seconds=1)).astimezone(LOCAL).replace(tzinfo=None)))
    with sessions() as db:
        result = build_snapshot(db,HOUR)
        assert result['cells']['suggested_density']['value'] is None
        assert '无法还原' in result['cells']['suggested_density']['reason']


def test_recently_imported_model_is_not_known_before_import(sessions, seeded):
    with sessions.begin() as db:
        db.delete(db.get(AutoState,'coarseModelVariants'))
        db.add(CoarseModel(method='mlr',is_current=True,intercept=3,coefs=[0]*10,
            metrics={'version':2},trained_at='2026-10-07 08:00:00',
            created_at=(HOUR+timedelta(seconds=1)).astimezone(LOCAL).replace(tzinfo=None)))
    with sessions() as db:
        assert build_snapshot(db,HOUR)['cells']['coarse_ash']['value'] is None
