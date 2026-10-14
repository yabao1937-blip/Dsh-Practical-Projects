"""Real setpoint history, stale chemistry and exact per-channel execution limits."""
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.gpt_density.execution import advise, feedback_start, setpoints
from app.hourly_brief.inputs import Inputs
from app.services.decision_sources import shared_snapshot
from app.telemetry.domain import iso_time
from app.telemetry.models import TelemetryPoint
from app.decision_hourly.models import DensityAction
from test_hourly_brief import HOUR, sessions, send
from test_decision_sources import seeded
from test_hourly_quality_advice import prepared

TARGET = dict(min=8.5, max=9)


def basis_at(db, when=HOUR):
    return setpoints(Inputs(db, when, authoritative=True),
        dict(source_id='kepserver-main', item_id='TIA_4_1.TIA_4_1.A304/MD'))


def test_live_uses_setpoint_and_does_not_follow_actual_density(sessions, seeded):
    cfg, repo, values = seeded
    values[4] = 1.53
    send(repo, cfg, HOUR-timedelta(seconds=1), values)
    with sessions() as db:
        before = list(db.connection().connection.driver_connection.iterdump())
        r = shared_snapshot(db)
        assert r['cells']['density']['value'] == 1.53
        assert r['execution_basis']['value'] == 1.48
        assert r['suggested_density']['value'] == 1.48  # In range: keep MD_SET.
        assert list(db.connection().connection.driver_connection.iterdump()) == before


def test_raw_change_protects_before_observer_and_keeps_original_reference(sessions, seeded):
    cfg, repo, values = seeded
    with sessions() as db:
        old = basis_at(db)
        g = advise(db,HOUR,old,9.6,TARGET,old['value'],HOUR-timedelta(hours=2),anchor_to_setpoint=True)
        assert g['rhoTargetFull'] == pytest.approx(1.44)
        assert g['rhoNew'] == 1.45
    values[-1] = 1.45
    send(repo,cfg,HOUR-timedelta(seconds=1),values)
    # No observe() call: a different client/restart must still see the same guard.
    for _ in range(2):
        with sessions() as db:
            basis = basis_at(db)
            g = advise(db,HOUR,basis,9.6,TARGET,basis['value'],HOUR-timedelta(hours=2),anchor_to_setpoint=True)
            assert g['execution_state'] == 'awaiting_feedback'
            assert g['rhoNew'] == 1.45 and g['deltaRho'] == 0
            assert g['rhoTargetFull'] == pytest.approx(1.44)
            assert g['actions'][0]['evidence'] == 'raw-setpoint-history'


def test_new_post_action_feedback_releases_and_future_actions_do_not_leak(sessions, seeded):
    cfg, repo, values = seeded
    values[-1] = 1.45
    send(repo,cfg,HOUR-timedelta(seconds=1),values)
    with sessions.begin() as db:
        basis = basis_at(db)
        c = basis['components']['A304']
        db.add(DensityAction(id='future',channel='A304',detected_at=iso_time(HOUR+timedelta(seconds=1)),
            evidence=dict(before=dict(pair=dict(setpoint=dict(value=1.48))),
                after=dict(pair=dict(setpoint=dict(c,definition_id=c['sample']['definition_id']))))))
    with sessions() as db:
        g=advise(db,HOUR,basis_at(db),9.6,TARGET,1.45,HOUR)
        assert g['execution_state']=='adjust' and g['rhoNew']==1.42
        past=basis_at(db,HOUR-timedelta(seconds=2))
        assert past['value']==1.48


def test_mixed_hour_assay_and_late_entry_do_not_unlock_move():
    # 10:20 sample represents 08–09. A move at 08:30 is inside that window.
    assert feedback_start(dict(status='manual',observed_at=iso_time(HOUR+timedelta(minutes=20)),
        entered_at=iso_time(HOUR+timedelta(hours=5)))) == HOUR-timedelta(hours=2)


def test_recorded_change_then_return_is_still_guarded(sessions, seeded):
    with sessions.begin() as db:
        basis=basis_at(db)
        c=basis['components']['A304']
        db.add(DensityAction(id='returned',channel='A304',detected_at=iso_time(HOUR-timedelta(seconds=10)),
            evidence=dict(before=dict(pair=dict(setpoint=dict(value=1.48))),
                after=dict(pair=dict(setpoint=dict(c,value=1.45,definition_id=c['sample']['definition_id']))))))
    with sessions() as db:
        g=advise(db,HOUR,basis_at(db),9.6,TARGET,1.48,HOUR-timedelta(hours=2))
        assert g['execution_state']=='awaiting_feedback' and g['rhoNew']==1.48


@pytest.mark.parametrize('ash,current', [(20,1.4904),(1,1.4906),(20,1.3504),(1,1.5996)])
def test_rounding_never_crosses_each_channel_step(sessions,seeded,ash,current):
    with sessions() as db:
        basis=basis_at(db)
        basis['value']=current
        basis['components']['A304']['value']=current
        g=advise(db,HOUR,basis,ash,TARGET,current,HOUR)
        c=g['channels']['A304']
        assert abs(c['suggested']-current)<=.03+1e-9
        assert 1.35<=c['suggested']<=1.6


@pytest.mark.parametrize('ash,current,step', [(20,1.4904,.0001),(1,1.4904,.0001),
    (1,1.4904,.0004),(20,1.4906,.0004),(20,1.4904,.0004),(1,1.4906,.0004)])
def test_subprecision_step_never_exceeds_limit_or_reverses_direction(sessions,seeded,ash,current,step):
    with sessions() as db:
        basis=basis_at(db)
        basis['value']=current
        basis['components']['A304']['value']=current
        g=advise(db,HOUR,basis,ash,TARGET,current,HOUR,settings=dict(maxStep=step))
        delta=g['rhoNew']-current
        assert abs(delta)<=step+1e-9
        assert delta<=0 if ash>9 else delta>=0
        if delta==0:
            assert '三位小数精度' in g['reason']


@pytest.mark.parametrize('problem',['missing','unit','stale','bad','wrong-source'])
def test_invalid_setpoint_does_not_fall_back_to_actual(sessions,seeded,problem):
    cfg,repo,values=seeded
    if problem=='bad':
        statuses=[0]*len(values);statuses[-1]=0x80000000
        send(repo,cfg,HOUR-timedelta(seconds=1),values,statuses)
    with sessions.begin() as db:
        p=db.get(TelemetryPoint,(cfg.source_id,cfg.points[-1].key))
        if problem=='missing': p.enabled=False
        elif problem=='unit': p.config={**p.config,'unit':'%'}
    with sessions() as db:
        if problem=='wrong-source':
            b=setpoints(Inputs(db,HOUR,authoritative=True),dict(source_id='wrong',item_id=cfg.points[4].item_id))
        else:
            b=basis_at(db,HOUR+timedelta(minutes=2) if problem=='stale' else HOUR)
        assert b['value'] is None


def test_dual_channel_steps_are_separately_limited(sessions,seeded):
    with sessions() as db:
        b=setpoints(Inputs(db,HOUR,authoritative=True),dict(source_id='kepserver-main',item_id='a303_a304_density_average'))
        g=advise(db,HOUR,b,20,TARGET,1.45,HOUR)
        assert set(g['channels'])=={'A303','A304'}
        for c in g['channels'].values():
            assert abs(c['suggested']-c['current'])<=.03+1e-9


def test_hourly_mean_is_target_reference_but_current_setpoint_limits_execution(sessions,prepared):
    from app.decision_hourly.service import read_hour
    current,settlement,_,_=prepared
    current['execution_basis']['value']=1.50
    current['execution_basis']['components']['A304']['value']=1.50
    with sessions() as db:
        r=read_hour(db,HOUR,current)
        assert r['guidance']['reference_target']['value']==pytest.approx(1.46)
        assert r['guidance']['rhoCur']==1.50
        assert r['suggested_density']['value']==1.47
        settlement['item']['value']=8.7
        assert read_hour(db,HOUR,current)['suggested_density']['value']==1.50
