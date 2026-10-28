"""Shared server selections, time boundaries and real input/calculation integration."""
from copy import deepcopy
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import get_db
from app.decision_hourly import models as _decision_hourly_models  # noqa: F401  (register tables before create_all)
from app.models import AutoState, Setting, DecisionSourceRevision, CoarseModel
from app.routers.decision import router
from app.services.decision_sources import shared_snapshot, identity, config_view
from app.hourly_brief.inputs import LEVEL, LOCAL
from legacy_hourly_reference import build_snapshot, tick
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
             'TIA_3_4.TIA_3_4.501/COAL_CAP', 'TIA_3_4.TIA_3_4.502/COAL_CAP', *states,
             *(item+'_SET' for _, item in DENSITY_AVERAGE_MEMBERS)]
    cfg = config(items)
    for p in cfg.points:
        if p.item_id in states:
            p.value_type = 'boolean'
    repo = TelemetryRepository(sessions)
    repo.acquire(cfg, 'owner')
    values = [40, 600, 50, 1.42, 1.48, 260, 240, True, True, True, False, True, False, 1.42, 1.48]
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


def test_shared_snapshot_never_computes_the_unused_density_mean(sessions, seeded, monkeypatch):
    from app.hourly_brief.inputs import Inputs

    def explode(self):
        raise AssertionError("共享快照不应无条件计算 A303/A304 平均")

    monkeypatch.setattr(Inputs, "density", explode)
    with sessions() as db:
        live = shared_snapshot(db)
    assert live["density_label"] == "A304"
    assert live["cells"]["density"]["value"] == 1.48


def test_inputs_collect_keeps_density_for_full_callers(sessions, seeded):
    from app.hourly_brief.inputs import Inputs
    with sessions() as db:
        cells = Inputs(db, HOUR, live=True, authoritative=True).collect()
    assert cells["density"]["value"] == 1.45


def test_snapshot_survives_a_retired_density_channel(sessions, seeded):
    from app.telemetry.models import TelemetryPoint
    from app.telemetry.repository import latest_view
    with sessions.begin() as db:
        assert db.get(TelemetryPoint, ("kepserver-main", "point3")) is not None
        db.delete(db.get(TelemetryPoint, ("kepserver-main", "point3")))
    with sessions() as db:
        live = shared_snapshot(db)
        view = latest_view(db)
    assert live["density_label"] == "A304"
    assert live["cells"]["density"]["value"] == 1.48
    average = next(item for item in view["derived_items"] if item.get("kind") == "numeric_average")
    assert average["available"] is False and average["sample"]["value"] is None


def test_live_and_hour_use_identical_gpt_inputs_and_results(sessions, seeded, client):
    with sessions() as db:
        before = list(db.connection().connection.driver_connection.iterdump())
        live = shared_snapshot(db)
        hourly = build_snapshot(db, HOUR)
        assert live['density_label'] == hourly['density_label'] == 'A304'
        assert live['cells']['density']['value'] == 1.48  # Not the former 1.45 mean.
        assert live['coarse_ash']['value'] == pytest.approx(9.2)
        assert live['cells']['coarse_amount']['value'] == 25
        assert live['cells']['coarse_amount']['status'] == 'calculated'
        assert live['heavy_amount']['value'] == 435
        assert live['total_ash']['value'] == pytest.approx((8.5*435+9*40+9.2*25)/500)
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


def test_target_patch_is_shared_atomic_and_preserves_historical_goal(sessions, seeded, client, monkeypatch):
    before = client.get('/api/v1/decision/current').json()
    later = HOUR+timedelta(seconds=1)
    monkeypatch.setattr('app.routers.decision.utc_now',lambda:later)
    monkeypatch.setattr('app.services.decision_sources.utc_now',lambda:later)
    body = dict(revision=0,target_min=7.5,target_max=8)
    response = client.patch('/api/v1/decision/sources',json=body)
    assert response.status_code == 200
    config = response.json()
    after = client.get('/api/v1/decision/current').json()
    assert after['target'] == dict(min=7.5,max=8)
    assert after['sources']['resolved_choices'] == before['sources']['resolved_choices']
    assert after['coarse_ash'] == before['coarse_ash']
    assert after['total_ash'] == before['total_ash']
    assert after['suggested_density']['value'] == 1.45
    assert before['suggested_density']['value'] == 1.48
    assert client.patch('/api/v1/decision/sources',json=body).status_code == 409
    assert client.patch('/api/v1/decision/sources',json={**body,'revision':config['revision']}).json()['revision'] == config['revision']
    for invalid in [dict(target_min=9,target_max=8),dict(target_min=-1),dict(target_max=101)]:
        assert client.patch('/api/v1/decision/sources',json={'revision':config['revision'],**invalid}).status_code == 422
    assert client.get('/api/v1/decision/sources').json() == config
    with sessions() as db:
        assert build_snapshot(db,HOUR)['target'] == before['target']


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


@pytest.mark.parametrize('key,value', [('densityLastMoveAt',HOUR.timestamp()*1000),
    ('densityActionLatch',dict(key='old-action')),('densityDecisionLog',[])])
def test_newer_action_state_does_not_erase_historical_advice(sessions, seeded, key, value):
    with sessions() as db:
        before = build_snapshot(db,HOUR)
    with sessions.begin() as db:
        db.add(AutoState(key=key,value=value,
            updated_at=(HOUR+timedelta(seconds=1)).astimezone(LOCAL).replace(tzinfo=None)))
    with sessions() as db:
        assert build_snapshot(db,HOUR)['cells']['suggested_density'] == before['cells']['suggested_density']
        assert before['cells']['suggested_density']['value'] is not None


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


def float_belt_row(when, ash, amount=None):
    from app.models import CoalRecord
    local = when.astimezone(LOCAL).replace(tzinfo=None, microsecond=0)
    return CoalRecord(category='float', ts=local.strftime('%Y-%m-%d %H:%M:%S'), system='default',
                      source='manual', ash_content=ash, coal_amount=amount, created_at=local)


def seed_plates(db, plates=2, label='A415'):
    """One counter point whose hour window rises by `plates` plates at 5-second cadence."""
    from uuid import uuid4
    from app.telemetry import float_output
    from app.telemetry.models import TelemetryPoint, TelemetryReading
    item = dict(float_output.PLATE_MEMBERS)[label]
    db.add(TelemetryPoint(source_id='kepserver-main', key='tag_plates', enabled=True,
                          config=dict(item_id=item, name=f'{label}/PLATE_CNT')))
    start = HOUR-timedelta(hours=1)
    for i in range(719):
        stamp = iso_time(start+timedelta(seconds=(i+1)*5))
        db.add(TelemetryReading(source_id='kepserver-main', point_key='tag_plates', batch_id=uuid4().hex,
            received_at=stamp, sample=dict(key='tag_plates', value=100+(plates if i >= 360 else 0),
                                           quality='good', received_at=stamp)))


def test_plate_derived_float_amount_feeds_decision(sessions, seeded):
    from app.telemetry import float_output
    from app.telemetry.float_output import DERIVATION
    float_output._cache.clear()
    with sessions.begin() as db:
        seed_plates(db, plates=2)
    with sessions() as db:
        live = shared_snapshot(db)
        cell = live['cells']['float_amount']
        assert cell['value'] == 26.0 and cell['status'] == 'calculated'
        assert cell['derivation'] == DERIVATION and cell['source'] == '压滤机板数折算'
        assert cell['plates'] == {'A415': 2.0} and cell['coverage'] >= 0.8
        assert live['heavy_amount']['value'] == 449  # 260+240−26−25 replaces the fixed 40.
        assert live['total_ash']['value'] == pytest.approx((8.5*449+9*26+9.2*25)/500)
        assert build_snapshot(db, HOUR)['cells']['float_amount'] == live['cells']['float_amount']


def test_belt_record_cannot_mask_plate_float_amount(sessions, seeded):
    from app.telemetry import float_output
    from app.hourly_brief.inputs import Inputs
    from app.services.decision_sources import collect_shared, config_view
    float_output._cache.clear()
    with sessions.begin() as db:
        seed_plates(db, plates=1)
        db.add(float_belt_row(HOUR-timedelta(seconds=30), None, amount=77.7))
    with sessions() as db:
        inputs = Inputs(db, HOUR, live=True, authoritative=True)
        cells = collect_shared(inputs, config_view(db, inputs.hour, inputs))
        assert cells['float_amount']['derivation'] == float_output.DERIVATION
        assert cells['float_amount']['value'] == 13.0


def test_belt_record_still_feeds_float_amount_without_plate_data(sessions, seeded):
    from app.telemetry import float_output
    from app.hourly_brief.inputs import Inputs
    from app.services.decision_sources import collect_shared, config_view
    float_output._cache.clear()
    with sessions.begin() as db:
        db.add(float_belt_row(HOUR-timedelta(seconds=30), None, amount=77.7))
    with sessions() as db:
        inputs = Inputs(db, HOUR, live=True, authoritative=True)
        cells = collect_shared(inputs, config_view(db, inputs.hour, inputs))
        # The fresher belt record wins by observation time; no plate derivation.
        assert cells['float_amount']['value'] == 77.7
        assert not cells['float_amount'].get('derivation')


def collect_float_cells(db):
    from app.hourly_brief.inputs import Inputs
    from app.services.decision_sources import collect_shared, config_view
    inputs = Inputs(db, HOUR, live=True, authoritative=True)
    return inputs, collect_shared(inputs, config_view(db, inputs.hour, inputs))


def test_manual_float_ash_participates_in_total_ash(sessions, seeded):
    # Baseline: the seeded store manual float value 9 feeds the three-part total ash.
    with sessions() as db:
        live = shared_snapshot(db)
        assert live['cells']['float_ash']['value'] == 9
        assert live['total_ash']['value'] == pytest.approx((8.5*435+9*40+9.2*25)/500)
    # A newer manual flotation assay replaces the older store value in the decision.
    with sessions.begin() as db:
        add_manual(db, 12.5, HOUR-timedelta(minutes=1), HOUR-timedelta(seconds=30), 'float_clean_coal_ash')
    with sessions() as db:
        live = shared_snapshot(db)
        cell = live['cells']['float_ash']
        assert cell['value'] == 12.5 and cell['status'] == 'manual' and cell['metric'] == 'float_clean_coal_ash'
        assert live['total_ash']['value'] == pytest.approx((8.5*435+12.5*40+9.2*25)/500)


def test_manual_float_ash_competes_with_belt_records_by_time(sessions, seeded, monkeypatch):
    with sessions.begin() as db:
        db.delete(db.get(AutoState, 'floatAshInput'))
    # Older manual assay + belt record: the belt record stays visible (pre-change rule).
    with sessions.begin() as db:
        add_manual(db, 12.5, HOUR-timedelta(minutes=5), HOUR-timedelta(minutes=4), 'float_clean_coal_ash')
        db.add(float_belt_row(HOUR-timedelta(seconds=20), 11.2))
    with sessions() as db:
        inputs, cells = collect_float_cells(db)
        assert cells['float_ash']['source'] == '历史浮精记录' and cells['float_ash']['value'] == 11.2
        assert inputs.collect()['float_ash']['value'] == 11.2
    # A manual assay newer than the belt record wins; the older record cannot mask it.
    with sessions.begin() as db:
        add_manual(db, 13.8, HOUR-timedelta(seconds=10), HOUR-timedelta(seconds=5), 'float_clean_coal_ash')
    with sessions() as db:
        inputs, cells = collect_float_cells(db)
        assert cells['float_ash']['value'] == 13.8 and cells['float_ash']['metric'] == 'float_clean_coal_ash'
        assert inputs.collect()['float_ash']['value'] == 13.8
    # A later belt record overtakes the manual assay again: latest sample wins.
    with sessions.begin() as db:
        db.add(float_belt_row(HOUR-timedelta(seconds=4), 10.8))
    with sessions() as db:
        inputs, cells = collect_float_cells(db)
        assert cells['float_ash']['source'] == '历史浮精记录' and cells['float_ash']['value'] == 10.8
        assert inputs.collect()['float_ash']['value'] == 10.8


def test_manual_float_amount_participates_in_total_ash(sessions, seeded):
    # Baseline: the seeded store manual float value 40 feeds the three-part total ash.
    with sessions() as db:
        live = shared_snapshot(db)
        assert live['cells']['float_amount']['value'] == 40
        assert live['total_ash']['value'] == pytest.approx((8.5*435+9*40+9.2*25)/500)
    # A newer manual amount entry (the new 补录 metric) replaces the stored 40.
    with sessions.begin() as db:
        add_manual(db, 15, HOUR-timedelta(minutes=1), HOUR-timedelta(seconds=30), 'float_clean_coal_amount')
    with sessions() as db:
        live = shared_snapshot(db)
        cell = live['cells']['float_amount']
        assert cell['value'] == 15 and cell['status'] == 'manual' and cell['metric'] == 'float_clean_coal_amount'
        assert cell['unit'] == 't/h'
        assert live['total_ash']['value'] == pytest.approx((8.5*460+9*15+9.2*25)/500)


def test_manual_float_amount_competes_with_belt_records_by_time(sessions, seeded):
    with sessions.begin() as db:
        db.delete(db.get(AutoState, 'amountInputs'))
        db.add(float_belt_row(HOUR-timedelta(seconds=20), 11.2, amount=27))
    with sessions.begin() as db:
        add_manual(db, 15, HOUR-timedelta(minutes=5), HOUR-timedelta(minutes=4), 'float_clean_coal_amount')
    with sessions() as db:
        inputs, cells = collect_float_cells(db)
        assert cells['float_amount']['source'] == '历史浮精记录' and cells['float_amount']['value'] == 27
    # A manual entry newer than the belt record wins; the older record cannot mask it.
    with sessions.begin() as db:
        add_manual(db, 15, HOUR-timedelta(seconds=10), HOUR-timedelta(seconds=5), 'float_clean_coal_amount')
    with sessions() as db:
        inputs, cells = collect_float_cells(db)
        assert cells['float_amount']['value'] == 15 and cells['float_amount']['metric'] == 'float_clean_coal_amount'
    # A later belt record overtakes the manual entry again: latest sample wins.
    with sessions.begin() as db:
        db.add(float_belt_row(HOUR-timedelta(seconds=4), 10.8, amount=22))
    with sessions() as db:
        inputs, cells = collect_float_cells(db)
        assert cells['float_amount']['source'] == '历史浮精记录' and cells['float_amount']['value'] == 22
