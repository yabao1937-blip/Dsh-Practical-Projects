"""Meaningful trial cases: preserved reference, timing, quality and step bounds."""
from copy import deepcopy
from datetime import timedelta

import pytest

from app.gpt_density.candidate import CandidateParameters, evaluate_candidate
from app.telemetry.domain import iso_time
from test_hourly_quality_advice import HOUR


def evidence(ash=9.3, reference=1.46, current=1.48, provisional=False):
    def cell(value, key):
        return dict(value=value, status='good', observed_at=iso_time(HOUR), unit='g/cm³',
            source_id='kep', point_key=key, definition_id=key+'-v1', batch_id='same-batch')
    return dict(evaluated_at=iso_time(HOUR), identity_known=True, action_history_status='good',
        mode='historical-replay', reference=dict(value=reference, status='calculated',
            policy='gpt-hourly-reference-v4', generated_at=iso_time(HOUR),
            identity='online-default', availability='before-observation',
            target=dict(min=8.5,max=9), basis=dict(value=1.48,channels=['A304']),
            ash=dict(value=ash,start=iso_time(HOUR-timedelta(hours=2)),
                end=iso_time(HOUR-timedelta(hours=1)),coverage=.95,provisional=provisional)),
        current_setpoint=cell(current,'A304/MD_SET'), actual_density=cell(current,'A304/MD'),
        operating=dict(status='good',stopped=False), latest_action=None)


@pytest.mark.parametrize('ash,reference,expected', [(9.3,1.46,1.47),(8.2,1.50,1.49)])
@pytest.mark.parametrize('provisional', [False,True])
def test_current_step_both_directions_and_smaller_provisional(ash,reference,expected,provisional):
    e = evidence(ash,reference,provisional=provisional)
    original = deepcopy(e)
    result = evaluate_candidate(e)
    if provisional:
        expected = 1.475 if ash>9 else 1.485
    assert result['candidate']['value'] == expected
    assert result['candidate']['status'] == 'adjust'
    assert result['quality']['level'] == ('provisional' if provisional else 'non-provisional')
    assert result['reference'] == e['reference'] and e == original


def test_minimal_end_to_end_reference_is_preserved_and_not_a_new_step():
    e = evidence(current=1.52,reference=1.46)
    r = evaluate_candidate(e)
    assert r['reference']['value'] == 1.46
    assert r['setting']['value'] == 1.52
    assert r['candidate']['value'] == 1.51
    assert r['parameters']['max_step'] == .010


@pytest.mark.parametrize('status', ['missing','pending','blocked','held',None])
def test_numeric_invalid_status_reference_is_not_accepted(status):
    e=evidence()
    e['reference']['status']=status
    r=evaluate_candidate(e)
    assert r['candidate']['value'] is None
    assert r['candidate']['reason_codes']==['reference_unavailable']


def test_other_route_reference_cannot_be_used_for_a304():
    e=evidence()
    e['reference']['basis']['channels']=['A303']
    assert evaluate_candidate(e)['candidate']['reason_codes']==['reference_route']


@pytest.mark.parametrize('ash', [8.5,8.8,9.0,9.05,8.45])
def test_target_band_and_deadband_keep_actual_current_setting(ash):
    e = evidence(ash=ash,current=1.52,reference=1.48)
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'hold'
    assert r['candidate']['value'] == 1.52
    assert r['basis']['band'] == 'in_band'


@pytest.mark.parametrize('ash,reference,current', [(9.3,1.46,1.45),(8.2,1.50,1.51)])
def test_no_reverse_chase_when_operator_has_passed_reference(ash,reference,current):
    r = evaluate_candidate(evidence(ash,reference,current))
    assert r['candidate']['value'] == current
    assert r['candidate']['reason_codes'] == ['reference_reached']


def test_wait_uses_production_start_not_fixed_two_hour_timer_and_recovers():
    e = evidence()
    # Even elapsed time alone is insufficient: this action is inside that hour.
    action_at = HOUR-timedelta(hours=1,minutes=59)
    e['latest_action'] = dict(id='real',before_at=iso_time(action_at-timedelta(seconds=60.2)),
        after_at=iso_time(action_at),continuous=False,gap=False,identity_known=True)
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'wait'
    assert r['observation']['ash_fully_after_action'] is False
    assert 'uncertain_recent_action' not in r['candidate']['reason_codes']
    e['reference']['ash'].update(start=iso_time(HOUR-timedelta(hours=1)),end=iso_time(HOUR))
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'adjust'
    assert r['observation']['state'] == 'post-action-covered'


def test_long_gap_unknown_action_blocks_related_ash_but_not_forever():
    e = evidence()
    a = HOUR-timedelta(hours=1,minutes=30)
    e['latest_action'] = dict(id='gap',before_at=iso_time(a-timedelta(minutes=10)),
        after_at=iso_time(a),continuous=False,gap=True,identity_known=False)
    assert evaluate_candidate(e)['candidate']['reason_codes'] == ['uncertain_recent_action']
    e['reference']['ash'].update(start=iso_time(HOUR-timedelta(hours=1)),end=iso_time(HOUR))
    assert evaluate_candidate(e)['candidate']['status'] == 'adjust'


def test_old_missing_before_time_does_not_permanently_lock_new_production():
    e=evidence()
    e['latest_action']=dict(id='legacy',before_at=None,
        after_at=iso_time(HOUR-timedelta(hours=3)),continuous=False,gap=True,identity_known=False)
    assert evaluate_candidate(e)['candidate']['status']=='adjust'
    e['latest_action']['after_at']=iso_time(HOUR-timedelta(hours=1))
    assert evaluate_candidate(e)['candidate']['reason_codes']==['action_time_unknown']


def test_post_action_provisional_window_remains_unconfirmed():
    e = evidence(provisional=True)
    a = HOUR-timedelta(hours=3)
    e['latest_action'] = dict(id='old',before_at=iso_time(a-timedelta(seconds=60)),
        after_at=iso_time(a),continuous=True,gap=False,identity_known=True)
    r = evaluate_candidate(e)
    assert r['observation']['state'] == 'post-action-covered-provisional'
    assert r['candidate']['value'] == 1.475
    assert evaluate_candidate(e,{'provisional_mode':'hold'})['candidate']['status'] == 'hold'


@pytest.mark.parametrize('issue,code', [
    ('missing_setting','setpoint_unavailable'),('future_setting','setpoint_unavailable'),
    ('stale_actual','actual_unavailable'),('batch','batch_mismatch'),
    ('identity','identity_unknown'),('history','action_history_unknown'),
    ('operating','operating_unknown'),('replay','reference_provenance'),
    ('pending','reference_unavailable'),('future_reference','reference_unavailable'),
    ('coverage','ash_coverage'),('unknown_provisional','ash_quality_unknown'),
    ('future_ash','ash_unavailable')])
def test_insufficient_evidence_is_explicit_and_recovers(issue,code):
    e = evidence()
    if issue=='missing_setting': e['current_setpoint']['value']=None
    if issue=='future_setting': e['current_setpoint']['observed_at']=iso_time(HOUR+timedelta(seconds=1))
    if issue=='stale_actual': e['actual_density']['observed_at']=iso_time(HOUR-timedelta(seconds=31))
    if issue=='batch': e['actual_density']['batch_id']='different'
    if issue=='identity': e['identity_known']=False
    if issue=='history': e['action_history_status']='unavailable'
    if issue=='operating': e['operating']['status']='missing'
    if issue=='replay': e['reference']['origin']='advice-backfill-v1'
    if issue=='pending': e['reference'].update(value=None,status='pending')
    if issue=='future_reference': e['reference']['generated_at']=iso_time(HOUR+timedelta(seconds=1))
    if issue=='coverage': e['reference']['ash']['coverage']=.8
    if issue=='unknown_provisional': e['reference']['ash']['provisional']=None
    if issue=='future_ash': e['reference']['ash'].update(start=iso_time(HOUR),end=iso_time(HOUR+timedelta(hours=1)))
    r = evaluate_candidate(e)
    assert r['candidate']['status'] == 'unavailable' and r['candidate']['value'] is None
    assert r['candidate']['reason_codes'] == [code]
    assert evaluate_candidate(evidence())['candidate']['status'] == 'adjust'


def test_stopped_and_actual_response_and_precision_bounds():
    e = evidence()
    e['operating']['stopped']=True
    assert evaluate_candidate(e)['candidate']['reason_codes'] == ['stopped']
    e['operating']['stopped']=False
    e['actual_density']['value']=1.50
    assert evaluate_candidate(e)['candidate']['reason_codes'] == ['actual_not_following']
    e = evidence(reference=1.477)
    assert evaluate_candidate(e)['candidate']['value'] == 1.477
    e['current_setpoint']['value']=1.480000019
    e['actual_density']['value']=1.48
    assert evaluate_candidate(e)['candidate']['value'] == 1.477


def test_stale_operating_timestamp_is_not_current_operating_evidence():
    e=evidence()
    e['operating']['observed_at']=iso_time(HOUR-timedelta(seconds=31))
    assert evaluate_candidate(e)['candidate']['reason_codes']==['operating_unknown']


def test_actual_single_reading_contract_remains_explicit_when_stale():
    e=evidence()
    e['actual_density']['value']=1.482
    reading=evaluate_candidate(e)['observation']['actual_reading']
    assert reading['value']==1.482 and reading['difference_from_setting']==.002
    assert reading['tolerance']==.005 and reading['observed_at']==iso_time(HOUR)
    assert reading['usable'] and reading['status']=='good' and reading['age_seconds']==0
    e['actual_density']['observed_at']=iso_time(HOUR-timedelta(seconds=31))
    reading=evaluate_candidate(e)['observation']['actual_reading']
    assert reading['value'] is None and reading['difference_from_setting'] is None
    assert reading['raw_value']==1.482 and reading['status']=='stale'
    assert not reading['usable'] and reading['age_seconds']==31


@pytest.mark.parametrize('actual,following', [(1.515,True),(1.525,True),
    (1.514999,False),(1.525001,False)])
def test_follow_tolerance_float_boundaries_are_symmetric(actual,following):
    e=evidence(current=1.520,reference=1.500)
    e['actual_density']['value']=actual
    result=evaluate_candidate(e)
    assert result['observation']['actual_response']==('following' if following else 'not-following')
    assert result['candidate']['status']==('adjust' if following else 'wait')
    assert result['parameters']['actual_follow_tolerance']==.005


@pytest.mark.parametrize('parameters', [{'max_step':.04},{'provisional_step':.02},
    {'provisional_mode':'bad'},{'actual_follow_tolerance':float('nan')},{'max_age_seconds':0}])
def test_trial_parameters_are_validated(parameters):
    with pytest.raises(ValueError):
        evaluate_candidate(evidence(), parameters)
